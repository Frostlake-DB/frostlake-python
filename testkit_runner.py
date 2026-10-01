"""Runs the engine-owned, language-neutral JSON test suites through THIS package.

The suites belong to the engine repo, and this file is only the Python runner — a port of
the Java reference (`engine/src/test/java/dev/frostlake/testkit`, spec in `SCHEMA.md`
beside the suites). Suites added on the engine side are picked up with no change here.

    export FL_CORPUS=/path/to/frostlake/engine/src/test/resources/testkit
    FROSTLAKE_CLASSPATH="<engine jar + deps>" python3 testkit_runner.py   # boots an engine
    FROSTLAKE_URL=http://localhost:18082      python3 testkit_runner.py   # or attaches to one

Suites are read from $FL_CORPUS/suites. With FL_CORPUS unset, or with no engine named,
everything skips rather than passing falsely; an FL_CORPUS without suites is an error.
The package's own test run replays the corpus through main() whenever FL_CORPUS is set.

Semantics, mirroring SCHEMA.md and the other drivers' runners:
  - per-test isolation: ALTER SESSION SET MULTI_STATEMENT_COUNT = 0 -> CREATE OR REPLACE
    DATABASE test_db -> USE -> CREATE OR REPLACE SCHEMA test_schema -> USE, then the test's
    steps on ONE session, which is what carries USE, variables and transactions across them;
  - capabilities: SESSION, COLUMN_NAMES, UPDATE_COUNT (derived). No ERROR_CODE — this
    transport carries a message only, so an expected error's code/sqlState counts as a
    missing API rather than a failure, and lands in target/tmp/missing-apis-<backend>.md;
  - values compare after the reference's normalisation: NULL and booleans folded, anything
    numeric rounded to 10 significant digits, everything else trimmed text;
  - a VARIANT/OBJECT/ARRAY cell reaches a client as its JSON text; the suites record the
    value, so such a cell is decoded one level, exactly as the reference does.
"""

import argparse
import datetime
import decimal
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BACKEND = "python"

RESET = [
    "ALTER SESSION SET MULTI_STATEMENT_COUNT = 0",
    "CREATE OR REPLACE DATABASE test_db",
    "USE DATABASE test_db",
    "CREATE OR REPLACE SCHEMA test_schema",
    "USE SCHEMA test_schema",
]

# A suite's skip clause names backends. Every package here rides the driver over HTTP, so a
# test skipped for `http` or for the `python` driver is skipped for the layers above it too.
ALIASES = {
    "python": ("python", "http"),
    "connector": ("connector", "python", "http"),
    "dbt": ("dbt", "connector", "python", "http"),
    "sqlalchemy": ("sqlalchemy", "connector", "python", "http"),
}

SEMI_STRUCTURED = ("VARIANT", "OBJECT", "ARRAY")


class Result(object):
    """One statement's outcome, in the reference's shape."""

    def __init__(self, columns=None, rows=None, update_count=-1, error=None):
        self.columns = columns or []
        self.rows = rows
        self.update_count = update_count
        self.error = error

    def failed(self):
        return self.error is not None


# -- value handling (Compare.java) -------------------------------------------

def norm(raw):
    if raw is None:
        return "NULL"
    value = str(raw).strip()
    if value == "" or value.lower() == "null":
        return "NULL"
    if value.lower() == "true":
        return "TRUE"
    if value.lower() == "false":
        return "FALSE"
    try:
        number = decimal.Decimal(value)
    except (decimal.InvalidOperation, ValueError):
        return value
    if number == 0:
        return "0"
    with decimal.localcontext() as ctx:
        ctx.prec = 10
        rounded = +number
    text = format(rounded.normalize(), "f")
    return text


def cell_text(value):
    """A driver cell as text, the way the reference stringifies the wire value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        return "".join("%02X" % b for b in value)
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return str(value)
    return str(value)


def semi_structured_value(text):
    """A JSON string cell becomes its content; anything else is left as it came."""
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    return parsed if isinstance(parsed, str) else text


def grid_of(rows, column_types):
    out = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            text = cell_text(value)
            declared = column_types[index] if index < len(column_types) else None
            if declared and str(declared).split("(")[0].strip().upper() in SEMI_STRUCTURED:
                text = semi_structured_value(text)
            cells.append(text)
        out.append(cells)
    return out


def derive_update_count(columns, rows, reported):
    """The reference's rule: a single count row whose columns are all `number of ...`.

    `reported` is what the package itself says (a DB-API rowcount). It is used only when the
    package hid the count grid and derived the number itself, which is the DML case; a DDL
    status row or a SELECT reports no count, exactly as the wire's -1 does.
    """
    if rows is None and reported is not None and reported >= 0:
        return reported
    if not columns or rows is None or len(rows) != 1:
        return -1
    for name in columns:
        if name is None or not str(name).lower().startswith("number of"):
            return -1
    try:
        return int(str(rows[0][0]).strip())
    except (ValueError, IndexError, TypeError):
        return -1


# -- expectation checking (Compare.java) -------------------------------------

def check(expect, result):
    """Returns (ok, detail, missing_api) for one step."""
    error = (expect or {}).get("error")
    if error is not None:
        return check_refusal(error, result)
    if result.failed():
        return False, "unexpected error: " + result.error, None
    if not expect:
        return True, "", None
    missing = None
    if "value" in expect:
        want = expect["value"]
        rows = result.rows or []
        actual = rows[0][0] if rows and rows[0] else None
        if norm(None if want is None else want) != norm(actual):
            return False, "value [%s] != expected [%s]" % (actual, want), None
    if "rows" in expect:
        diff = grid_diff(expect["rows"], result.rows or [], bool(expect.get("ordered")))
        if diff:
            return False, diff, None
    if "rowCount" in expect:
        got = len(result.rows or [])
        if got != expect["rowCount"]:
            return False, "rowCount %d != expected %d" % (got, expect["rowCount"]), None
    if "columns" in expect:
        want = [str(c) for c in expect["columns"]]
        got = [str(c) for c in (result.columns or [])]
        if len(want) != len(got):
            return False, "column count %d != expected %d %s" % (len(got), len(want), got), None
        for i, name in enumerate(want):
            if name.lower() != got[i].lower():
                return False, "column[%d] [%s] != expected [%s]" % (i, got[i], name), None
    if "updateCount" in expect:
        if result.update_count != expect["updateCount"]:
            return False, "updateCount %s != expected %s" % (result.update_count,
                                                             expect["updateCount"]), None
    return True, "", missing


def check_refusal(error, result):
    if not result.failed():
        return False, "expected an error, statement succeeded", None
    want = error.get("messageContains")
    if want is not None and want.lower() not in result.error.lower():
        return False, "error message [%s] does not contain [%s]" % (result.error, want), None
    if error.get("code") is None and error.get("sqlState") is None:
        return True, "", None
    # No ERROR_CODE capability on this transport: recorded, not failed.
    return True, "", "ERROR_CODE: cannot check error code/sqlState (backend reports message only)"


def grid_diff(want_rows, got_rows, ordered):
    want = ["\x1f".join(norm(None if c is None else c) for c in row) for row in want_rows]
    got = ["\x1f".join(norm(c) for c in row) for row in got_rows]
    if not ordered:
        want, got = sorted(want), sorted(got)
    if want == got:
        return None
    return "rows differ: expected %s got %s" % (want, got)


# -- backends: one per package, each speaking through ITS own public API ------

class DriverBackend(object):
    """The PEP 249 driver itself."""

    name = "python"

    def __init__(self, host, port):
        import frostlake
        self._mod = frostlake
        self._conn = frostlake.connect(host=host, port=port)
        self._cur = self._conn.cursor()

    def execute(self, sql):
        try:
            self._cur.execute(sql)
        except self._mod.Error as refusal:
            return Result(error=str(refusal))
        description = self._cur.description
        if description is None:
            # The driver hides a DML count grid and reports the number as rowcount.
            return Result(update_count=derive_update_count(None, None, self._cur.rowcount))
        columns = [d[0] for d in description]
        rows = grid_of(self._cur.fetchall(), [d[1] for d in description])
        return Result(columns, rows, derive_update_count(columns, rows, None))

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class ConnectorBackend(object):
    """The high-level client."""

    name = "connector"

    def __init__(self, host, port):
        import frostlake_connector
        from frostlake_connector import constants, errors
        self._errors = errors
        self._constants = constants
        self._conn = frostlake_connector.connect(host=host, port=port)
        self._cur = self._conn.cursor()

    def execute(self, sql):
        try:
            self._cur.execute(sql)
        except self._errors.Error as refusal:
            return Result(error=str(refusal))
        description = self._cur.description
        if description is None:
            return Result(update_count=derive_update_count(None, None, self._cur.rowcount))
        columns = [d[0] for d in description]
        types = [self._constants.FIELD_ID_TO_NAME.get(d[1]) for d in description]
        rows = grid_of(self._cur.fetchall(), types)
        return Result(columns, rows, derive_update_count(columns, rows, None))

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class SqlAlchemyBackend(object):
    """Through the SQLAlchemy dialect, on one connection in AUTOCOMMIT.

    AUTOCOMMIT keeps SQLAlchemy from owning the transaction, so the suites' own BEGIN /
    COMMIT statements mean what they say and session state carries across steps.
    """

    name = "sqlalchemy"

    def __init__(self, host, port):
        import sqlalchemy
        # Importing the dialect registers frostlake:// with SQLAlchemy directly. Without it the
        # URL resolves only through an installed package's entry point, so a runner started
        # from a source checkout finds no dialect at all.
        import frostlake_sqlalchemy  # noqa: F401
        self._sa = sqlalchemy
        engine = sqlalchemy.create_engine("frostlake://%s:%d/" % (host, port))
        self._engine = engine
        self._conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    def execute(self, sql):
        try:
            result = self._conn.exec_driver_sql(sql)
        except self._sa.exc.DatabaseError as refusal:
            return Result(error=str(getattr(refusal, "orig", refusal)))
        if not result.returns_rows:
            return Result(update_count=derive_update_count(None, None, result.rowcount))
        description = result.cursor.description if result.cursor is not None else None
        columns = [str(k) for k in result.keys()]
        types = [d[1] for d in description] if description else []
        rows = grid_of([tuple(r) for r in result.fetchall()], types)
        return Result(columns, rows, derive_update_count(columns, rows, None))

    def close(self):
        try:
            self._conn.close()
            self._engine.dispose()
        except Exception:
            pass


class DbtBackend(object):
    """Through the dbt adapter's own connection manager, as dbt itself opens one."""

    name = "dbt"

    def __init__(self, host, port):
        import frostlake_connector
        from frostlake_connector import constants, errors
        from dbt.adapters.contracts.connection import Connection
        from dbt.adapters.frostlake.connections import FrostlakeConnectionManager
        from dbt.adapters.frostlake.connections import FrostlakeCredentials
        self._errors = errors
        self._constants = constants
        # A dbt profile names a database and schema, and open() issues USE for them, so they
        # have to exist before the adapter connects. The per-test reset recreates them.
        boot = frostlake_connector.connect(host=host, port=port)
        cursor = boot.cursor()
        cursor.execute("CREATE DATABASE IF NOT EXISTS test_db")
        cursor.execute("USE DATABASE test_db")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS test_schema")
        boot.close()
        connection = Connection(
            type="frostlake", name="testkit", state="init", transaction_open=False,
            handle=None,
            credentials=FrostlakeCredentials(host=host, port=port,
                                             database="test_db", schema="test_schema"),
        )
        self._opened = FrostlakeConnectionManager.open(connection)
        self._cur = self._opened.handle.cursor()

    def execute(self, sql):
        try:
            self._cur.execute(sql)
        except self._errors.Error as refusal:
            return Result(error=str(refusal))
        description = self._cur.description
        if description is None:
            return Result(update_count=derive_update_count(None, None, self._cur.rowcount))
        columns = [d[0] for d in description]
        types = [self._constants.FIELD_ID_TO_NAME.get(d[1]) for d in description]
        rows = grid_of(self._cur.fetchall(), types)
        return Result(columns, rows, derive_update_count(columns, rows, None))

    def close(self):
        try:
            self._opened.handle.close()
        except Exception:
            pass


BACKENDS = {"python": DriverBackend, "connector": ConnectorBackend,
            "sqlalchemy": SqlAlchemyBackend, "dbt": DbtBackend}


# -- engine and suites -------------------------------------------------------

def start_engine():
    """(host, port, process) from FROSTLAKE_URL or FROSTLAKE_CLASSPATH, else None."""
    url = os.environ.get("FROSTLAKE_URL")
    if url:
        rest = url.split("://", 1)[-1].split("/", 1)[0]
        host, _, port = rest.partition(":")
        return host or "localhost", int(port or 18082), None
    classpath = os.environ.get("FROSTLAKE_CLASSPATH")
    if not classpath:
        return None
    java = "java"
    if os.environ.get("JAVA_HOME"):
        java = os.path.join(os.environ["JAVA_HOME"], "bin", "java")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # An engine keeps stage files and other per-user state under user.home, so give this
    # run its own: without it, two runners started at the same time share a stage directory
    # and race over the files the FILE-function suites put there. The other drivers'
    # harnesses isolate the same way.
    home = tempfile.mkdtemp(prefix="frostlake-testkit-")
    process = subprocess.Popen(
        [java, "-Duser.home=" + home, "-cp", classpath,
         "dev.frostlake.http.DatabaseHttpServer", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process._testkit_home = home
    for _ in range(200):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2)
            return "127.0.0.1", port, process
        except Exception:
            if process.poll() is not None:
                raise SystemExit("the engine exited during startup (exit %s)" % process.returncode)
            time.sleep(0.2)
    process.kill()
    raise SystemExit("the engine never became healthy on port %d" % port)


def suites_directory():
    """$FL_CORPUS/suites, or None with FL_CORPUS unset; an FL_CORPUS without suites is fatal."""
    corpus = os.environ.get("FL_CORPUS")
    if not corpus:
        return None
    suites = pathlib.Path(corpus) / "suites"
    if not suites.is_dir() or not any(suites.glob("*.json")):
        raise SystemExit("FL_CORPUS=%s holds no suites/*.json; point it at frostlake's "
                         "engine/src/test/resources/testkit" % corpus)
    return suites


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default=DEFAULT_BACKEND, choices=sorted(BACKENDS))
    parser.add_argument("--suite", help="only suites whose name contains this")
    parser.add_argument("--max-report", type=int, default=25)
    args = parser.parse_args(argv)

    suites = suites_directory()
    if suites is None:
        print("set FL_CORPUS to frostlake's engine/src/test/resources/testkit to replay the "
              "testkit corpus - skipping")
        return 0
    engine = start_engine()
    if engine is None:
        print("no engine (set FROSTLAKE_CLASSPATH or FROSTLAKE_URL) - skipping")
        return 0
    host, port, process = engine

    backend = None
    aliases = set(ALIASES[args.backend])
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    report, missing, notable = [], [], []
    started = time.time()
    try:
        # Built inside the try: a backend that cannot connect must not leak the engine this
        # run started, nor its private user.home.
        backend = BACKENDS[args.backend](host, port)
        for path in sorted(suites.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            suite_name = document.get("suite") or path.stem
            if args.suite and args.suite not in suite_name:
                continue
            for test in document.get("tests", []):
                test_name = test.get("name", "?")
                clause = test.get("skip") or {}
                hit = [b for b in (clause.get("backends") or []) if b in aliases]
                if hit:
                    counts["SKIP"] += 1
                    report.append((suite_name, test_name, "SKIP", "",
                                   "skip[%s]: %s" % (hit[0], clause.get("reason", "")), 0))
                    continue
                begin = time.time()
                status, failed_step, detail = "PASS", "", ""
                try:
                    for sql in RESET:
                        outcome = backend.execute(sql)
                        if outcome.failed():
                            raise RuntimeError("reset failed [%s]: %s" % (sql, outcome.error))
                    for number, step in enumerate(test.get("steps", []), 1):
                        result = backend.execute(step["sql"])
                        ok, why, absent = check(step.get("expect"), result)
                        if absent:
                            missing.append((suite_name, test_name, absent))
                        if not ok:
                            status = "FAIL"
                            failed_step = number
                            detail = "%s | sql: %s" % (why, step["sql"].replace("\n", " ")[:200])
                            break
                except Exception as broke:  # transport or infrastructure, not an expectation
                    status, detail = "ERROR", "%s: %s" % (type(broke).__name__,
                                                          str(broke).replace("\n", " ")[:200])
                counts[status] += 1
                report.append((suite_name, test_name, status, failed_step, detail,
                               int((time.time() - begin) * 1000)))
                if status in ("FAIL", "ERROR") and len(notable) < args.max_report:
                    notable.append("  %-38s %-42s %s %s" % (suite_name, test_name, status, detail))
    finally:
        if backend is not None:
            backend.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(getattr(process, "_testkit_home", ""), ignore_errors=True)

    out = pathlib.Path(__file__).resolve().parent / "target" / "tmp"
    out.mkdir(parents=True, exist_ok=True)
    tsv = out / ("testkit-%s.tsv" % args.backend)
    with open(tsv, "w", encoding="utf-8") as handle:
        handle.write("suite\ttest\tstatus\tfailedStep\tdetail\tms\n")
        for row in report:
            handle.write("\t".join(str(c) for c in row) + "\n")
    if missing:
        seen = sorted({note for _, _, note in missing})
        with open(out / ("missing-apis-%s.md" % args.backend), "w", encoding="utf-8") as handle:
            handle.write("# Missing APIs for backend `%s`\n\n" % args.backend)
            handle.write("Checks the suites ask for that this transport cannot express. "
                         "Not failures: the day the API exists they light up.\n\n")
            for note in seen:
                handle.write("- %s (%d checks)\n"
                             % (note, sum(1 for _, _, n in missing if n == note)))

    total = sum(counts.values())
    print("\n== testkit corpus through `%s` ==" % args.backend)
    print("  suites dir : %s" % suites)
    print("  tests      : %d" % total)
    print("  PASS %d  FAIL %d  ERROR %d  SKIP %d   in %.1fs"
          % (counts["PASS"], counts["FAIL"], counts["ERROR"], counts["SKIP"],
             time.time() - started))
    print("  report     : %s" % tsv)
    if missing:
        print("  missing-API checks recorded: %d" % len(missing))
    if notable:
        print("  first failures:")
        for line in notable:
            print(line)
    return 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
