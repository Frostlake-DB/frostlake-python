"""A pure-stdlib DB-API 2.0 (PEP 249) driver for Frostlake.

Speaks the engine's HTTP protocol against a running DatabaseHttpServer:

    import frostlake
    conn = frostlake.connect("frostlake://localhost:18082/MY_DB?schema=PUBLIC")
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM people WHERE id = ?", (1,))
    print(cur.fetchall())

Parameters are inlined client-side (the protocol has no server-side binding), with the
same rules as Frostlake's JDBC driver. Temporal columns come back as datetime.date /
datetime.time / datetime.datetime; fixed-point NUMBERs keep the exact digits sent on the
wire as decimal.Decimal (integral ones as int), and FLOAT/DOUBLE/REAL as float.

A multi-statement execute() exposes the first result set; step to the rest with
cursor.nextset().

The connection starts in autocommit mode, matching the Snowflake Python connector's
behavior rather than strict DB-API default; set conn.autocommit = False (or call
conn.begin()) for explicit transactions.
"""

import datetime as _dt
import decimal as _decimal
import json as _json
import re as _re
import time as _time
import urllib.error as _urlerror
import urllib.parse as _urlparse
import urllib.request as _urlrequest

__version__ = "0.1.0"

apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"


class Warning(Exception):  # noqa: A001  (PEP 249 names)
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class ProgrammingError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class DataError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


# -- type objects (PEP 249) --------------------------------------------------

class _TypeObject(object):
    """Compares equal to every engine type name in one family, so the spec's

        cursor.description[i][1] == frostlake.NUMBER

    works against the raw type names the server reports. Comparison is symmetric:
    a plain string on the left defers to this class's __eq__ on the right.
    """

    def __init__(self, label, names):
        self._label = label
        self._names = frozenset(names)

    def __eq__(self, other):
        if isinstance(other, _TypeObject):
            return self._names == other._names
        if isinstance(other, str):
            return _base_type_name(other) in self._names
        return NotImplemented

    def __hash__(self):
        return hash(self._label)

    def __repr__(self):
        return self._label


STRING = _TypeObject("STRING", ("VARCHAR", "CHAR", "CHARACTER", "STRING", "TEXT",
                                "NCHAR", "NVARCHAR", "NVARCHAR2",
                                "CHAR VARYING", "NCHAR VARYING"))
BINARY = _TypeObject("BINARY", ("BINARY", "VARBINARY"))
NUMBER = _TypeObject("NUMBER", ("NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER",
                                "BIGINT", "SMALLINT", "TINYINT", "BYTEINT",
                                "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE",
                                "DOUBLE PRECISION", "REAL"))
DATETIME = _TypeObject("DATETIME", ("DATE", "TIME", "DATETIME", "TIMESTAMP",
                                    "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"))
# The engine has no rowid concept; the name exists because PEP 249 requires it, and
# matches nothing.
ROWID = _TypeObject("ROWID", ())


# -- type constructors (PEP 249) ---------------------------------------------

def Date(year, month, day):
    return _dt.date(year, month, day)


def Time(hour, minute, second):
    return _dt.time(hour, minute, second)


def Timestamp(year, month, day, hour, minute, second):
    return _dt.datetime(year, month, day, hour, minute, second)


def DateFromTicks(ticks):
    return Date(*_time.localtime(ticks)[:3])


def TimeFromTicks(ticks):
    return Time(*_time.localtime(ticks)[3:6])


def TimestampFromTicks(ticks):
    return Timestamp(*_time.localtime(ticks)[:6])


def Binary(string):
    """Hold a value as binary data: str is encoded UTF-8, anything else goes via bytes()."""
    if isinstance(string, str):
        return string.encode("utf-8")
    return bytes(string)


def connect(dsn=None, host=None, port=18082, database=None, schema=None, timeout=300):
    """Open a connection. Accepts a DSN (frostlake://host:port[/DB][?schema=S]) or parts."""
    if dsn is not None:
        u = _urlparse.urlparse(dsn)
        if u.scheme not in ("frostlake", "http"):
            raise InterfaceError("DSN must start with frostlake:// or http://")
        if not u.netloc:
            raise InterfaceError("DSN is missing host[:port]")
        host_port = u.netloc
        database = (u.path.strip("/") or None) if database is None else database
        q = _urlparse.parse_qs(u.query)
        if schema is None and "schema" in q:
            schema = q["schema"][0]
        base_url = "http://" + host_port
    else:
        if host is None:
            raise InterfaceError("either dsn or host is required")
        base_url = "http://%s:%d" % (host, port)
    return Connection(base_url, database, schema, timeout)


class Connection(object):
    # PEP 249 optional extension: the exception classes hang off the connection too, so
    # `except conn.Error` works without importing this module.
    Warning = Warning
    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError

    def __init__(self, base_url, database, schema, timeout):
        self._base_url = base_url
        self._timeout = timeout
        self._session_id = None
        self._closed = False
        self.autocommit = True
        self._pending_use = []
        if database:
            self._pending_use.append("USE DATABASE " + _quote_ident(database))
        if schema:
            self._pending_use.append("USE SCHEMA " + _quote_ident(schema))

    # -- PEP 249 surface ----------------------------------------------------

    def cursor(self):
        self._check_open()
        return Cursor(self)

    def commit(self):
        self._check_open()
        if not self.autocommit:
            self._execute_raw("COMMIT")

    def rollback(self):
        self._check_open()
        if not self.autocommit:
            self._execute_raw("ROLLBACK")

    def begin(self):
        """Start an explicit transaction (disables autocommit for this connection)."""
        self._check_open()
        self.autocommit = False
        self._execute_raw("BEGIN")

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            try:
                self.rollback()
            except Error:
                pass
        self.close()

    # -- protocol -----------------------------------------------------------

    def _check_open(self):
        if self._closed:
            raise InterfaceError("connection is closed")

    def _execute(self, sql):
        self._check_open()
        while self._pending_use:
            self._execute_raw(self._pending_use.pop(0))
        return self._execute_raw(sql)

    def _execute_raw(self, sql):
        payload = {"sql": sql, "autoCommit": self.autocommit}
        if self._session_id:
            payload["sessionId"] = self._session_id
        req = _urlrequest.Request(
            self._base_url + "/api/execute",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _urlrequest.urlopen(req, timeout=self._timeout) as resp:
                out = _load_json(resp.read().decode("utf-8"))
        except _urlerror.HTTPError as e:
            # The server answers failed statements with a non-2xx status AND the
            # error payload in the body — read it instead of surfacing the status.
            try:
                out = _load_json(e.read().decode("utf-8"))
            except Exception:
                raise OperationalError(str(e)) from e
        except OSError as e:
            raise OperationalError(str(e)) from e
        if out.get("sessionId"):
            self._session_id = out["sessionId"]
        if not out.get("success"):
            raise ProgrammingError(out.get("errorMessage") or "statement failed")
        return out


class Cursor(object):
    arraysize = 1

    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        # The engine has no rowid concept, so this stays None (PEP 249 allows it).
        self.lastrowid = None
        self._rows = []
        self._pos = 0
        self._result_sets = []
        self._set_index = 0
        self._executed = False
        self._closed = False

    # -- PEP 249 surface ----------------------------------------------------

    def execute(self, operation, parameters=None):
        self._check_open()
        sql = _substitute(operation, parameters) if parameters else operation
        out = self.connection._execute(sql)
        self._load(out)
        return self

    def executemany(self, operation, seq_of_parameters):
        self._check_open()
        total = 0
        for parameters in seq_of_parameters:
            self.execute(operation, parameters)
            if self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        return self

    def callproc(self, procname, parameters=None):
        """CALL a stored procedure. The engine has no OUT parameters, so the input
        sequence comes back unmodified; any result the procedure returns is fetchable."""
        self._check_open()
        params = list(parameters) if parameters else []
        placeholders = ", ".join(["?"] * len(params))
        self.execute("CALL " + procname + "(" + placeholders + ")", params)
        return parameters

    def nextset(self):
        """Advance to the next result set of a multi-statement execute(); None when the
        current set is the last one."""
        self._check_open()
        self._check_executed()
        if self._set_index + 1 >= len(self._result_sets):
            return None
        self._set_index += 1
        self._apply_current_set()
        return True

    def fetchone(self):
        self._check_open()
        self._check_executed()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=None):
        self._check_open()
        self._check_executed()
        n = size or self.arraysize
        out = self._rows[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def fetchall(self):
        self._check_open()
        self._check_executed()
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def close(self):
        self._closed = True
        self._rows = []
        self._result_sets = []
        self.description = None

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size, column=None):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    # -- state --------------------------------------------------------------

    def _check_open(self):
        if self._closed:
            raise InterfaceError("cursor is closed")
        self.connection._check_open()

    def _check_executed(self):
        if not self._executed:
            raise ProgrammingError("no statement has been executed on this cursor")

    # -- result loading -----------------------------------------------------

    def _load(self, out):
        self._result_sets = out.get("resultSets") or []
        self._set_index = 0
        self._executed = True
        self._apply_current_set()

    def _apply_current_set(self):
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0
        if self._set_index >= len(self._result_sets):
            return
        rs = self._result_sets[self._set_index] or {}
        columns = rs.get("columns") or []
        self.description = [
            (c.get("name"), c.get("dataType"), None, None,
             c.get("precision"), c.get("scale"), c.get("nullable"))
            for c in columns
        ]
        types = [_base_type_name(c.get("dataType")) for c in columns]
        scales = [c.get("scale") or 0 for c in columns]
        rows = []
        for row in rs.get("rows") or []:
            cells = []
            for i, v in enumerate(row):
                cells.append(_convert(v,
                                      types[i] if i < len(types) else "",
                                      scales[i] if i < len(scales) else 0))
            rows.append(tuple(cells))
        self._rows = rows
        # DML answers with a one-cell "number of rows ..." result; surface it as rowcount.
        if (len(columns) == 1 and len(self._rows) == 1
                and str(columns[0].get("name", "")).lower().startswith("number of rows")):
            self.rowcount = int(self._rows[0][0])
            self.description = None
            self._rows = []
        else:
            self.rowcount = len(self._rows)


# -- value conversion -------------------------------------------------------

# Types the engine stores as binary floating point; every other numeric is fixed-point.
_APPROXIMATE_TYPES = frozenset(("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE",
                                "DOUBLE PRECISION", "REAL"))


def _load_json(text):
    """Parse a server response, keeping every JSON number exact.

    The engine serializes fixed-point numerics from BigDecimal, so the full digits are on
    the wire; json's default float conversion is what used to throw them away. Reading
    them as Decimal preserves them, and _convert decides per column which ones stay.
    """
    return _json.loads(text, parse_float=_decimal.Decimal)


def _base_type_name(data_type):
    """'NUMBER(38,10)' -> 'NUMBER' — the engine parameterizes some spellings per column."""
    name = str(data_type or "").upper().strip()
    index = name.find("(")
    if index >= 0:
        name = name[:index].strip()
    return name


def _convert(value, data_type, scale=0):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        # DATETIME is an alias for TIMESTAMP_NTZ, so it has to be tested before DATE.
        if data_type.startswith("TIMESTAMP") or data_type == "DATETIME":
            return _dt.datetime.fromisoformat(_trim_fraction(_normalize_offset(value)))
        if data_type == "TIME":
            return _dt.time.fromisoformat(_trim_fraction(value))
        if data_type == "DATE":
            return _dt.date.fromisoformat(value)
        return value
    if isinstance(value, (int, _decimal.Decimal)):
        return _convert_number(value, data_type, scale)
    if isinstance(value, float):
        return value
    # Semi-structured cells (OBJECT/ARRAY/VARIANT) arrive as JSON text and are handled by
    # the str branch above; this guards anything that does arrive as a parsed structure,
    # so the numeric path's Decimal parsing can't leak into it.
    return _undecimal(value)


def _convert_number(value, data_type, scale):
    """Fixed-point columns keep the wire's exact digits; FLOAT/DOUBLE/REAL are genuine
    binary floats and stay that way."""
    if data_type in _APPROXIMATE_TYPES:
        return float(value)
    if isinstance(value, int):
        return value
    if scale:
        return value
    # scale 0: hand back an int when the value really is integral, never truncate.
    if value == value.to_integral_value():
        return int(value)
    return value


def _undecimal(value):
    """Walk a semi-structured cell, turning parsed Decimals back into floats."""
    if isinstance(value, _decimal.Decimal):
        return float(value)
    if isinstance(value, list):
        return [_undecimal(v) for v in value]
    if isinstance(value, dict):
        return dict((k, _undecimal(v)) for k, v in value.items())
    return value


def _normalize_offset(text):
    """The server's TZHTZM zone rendering (' +0100') into ISO 8601 ('+01:00'), which
    fromisoformat accepts on every supported Python."""
    match = _re.search(r"\s*([+-]\d{2}):?(\d{2})$", text)
    if match and match.start() > 0:
        return text[: match.start()] + match.group(1) + ":" + match.group(2)
    return text


def _trim_fraction(text):
    """Trim a fractional-seconds part to microseconds so fromisoformat accepts nanos."""
    if "." in text:
        head, frac = text.rsplit(".", 1)
        tz = ""
        for sep in ("+", "-", "Z"):
            if sep in frac:
                idx = frac.index(sep)
                frac, tz = frac[:idx], frac[idx:]
                break
        return head + "." + frac[:6] + tz
    return text


# -- client-side parameter binding ------------------------------------------

def _quote_ident(name):
    if all(("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "_$" for ch in name):
        return name
    return '"' + name + '"'


def _encode_string(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _format_literal(value):
    if value is None:
        return "NULL"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, _decimal.Decimal):
        return str(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, _dt.datetime):
        return "'" + value.isoformat() + "'::TIMESTAMP_NTZ"
    if isinstance(value, _dt.date):
        return "'" + value.isoformat() + "'::DATE"
    if isinstance(value, _dt.time):
        return "'" + value.isoformat() + "'::TIME"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_literal(v) for v in value) + "]"
    raise ProgrammingError("unsupported parameter type %r" % type(value))


def _substitute(sql, parameters):
    """Inline ? placeholders, skipping string literals (with Frostlake's backslash
    escaping), quoted identifiers and comments."""
    params = list(parameters)
    out = []
    next_param = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = _skip_string(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == '"':
            j = _skip_quoted(sql, i, '"')
            out.append(sql[i:j])
            i = j
        elif ch == "-" and sql.startswith("--", i):
            j = _skip_line(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            j = n if end < 0 else end + 2
            out.append(sql[i:j])
            i = j
        elif ch == "/" and sql.startswith("//", i):
            j = _skip_line(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == "?":
            if next_param >= len(params):
                raise ProgrammingError("not enough parameters for placeholders")
            out.append(_format_literal(params[next_param]))
            next_param += 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_string(s, i):
    j = i + 1
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == "\\":
            j += 2
        elif ch == "'":
            if j + 1 < n and s[j + 1] == "'":
                j += 2
            else:
                return j + 1
        else:
            j += 1
    return j


def _skip_quoted(s, i, q):
    j = i + 1
    n = len(s)
    while j < n:
        if s[j] == q:
            if j + 1 < n and s[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return j


def _skip_line(s, i):
    j = s.find("\n", i)
    return len(s) if j < 0 else j + 1
