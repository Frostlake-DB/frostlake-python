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

The connection starts in autocommit mode rather than the strict DB-API default; set
conn.autocommit = False (or call conn.begin()) for explicit transactions. With it off
the connection stays transactional: ending one transaction opens the next.

Each connection holds one engine session, and close() releases it. When the engine has
lost the session (idle expiry, release, restart), the driver starts a fresh one on the
connection's scope and sends the statement once more, unless the lost session held a
transaction or context a fresh one would lack; then SessionLostError says so instead.
"""

import datetime as _dt
import decimal as _decimal
import json as _json
import re as _re
import time as _time
import urllib.error as _urlerror
import urllib.parse as _urlparse
import urllib.request as _urlrequest

__version__ = "0.2.1"

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


class SessionLostError(OperationalError):
    """The engine no longer holds the connection's session and the statement did not run.

    The session expired, was released, or went with a server restart, and it held something
    a fresh session would not have: an open transaction, or context set up with USE, SET,
    ALTER SESSION or a temporary object. Running the statement in a fresh session would put
    it somewhere its author did not intend, so it is refused instead. Also raised when the
    engine refuses the fresh session that was to replace a lost one. The connection stays
    usable: its next statement starts a fresh session on the connection's scope.
    """


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
    SessionLostError = SessionLostError

    def __init__(self, base_url, database, schema, timeout):
        self._base_url = base_url
        self._timeout = timeout
        self._session_id = None
        self._closed = False
        # The engine only treats work as transactional after an explicit BEGIN, so the
        # three pieces of state are: what the caller asked for, whether a transaction is
        # actually open, and whether one still has to be opened before the next
        # statement. Ending a transaction while autocommit is off arms the next.
        self._autocommit = True
        self._in_transaction = False
        self._begin_pending = False
        self._pending_use = []
        if database:
            self._pending_use.append("USE DATABASE " + _quote_ident(database))
        if schema:
            self._pending_use.append("USE SCHEMA " + _quote_ident(schema))
        # The connection's scope: the statements that put a fresh session where the
        # connection asked to be. _pending_use holds the part not yet on the session;
        # all of it goes back on a session that replaces a lost one.
        self._scope = list(self._pending_use)
        # Whether the engine reports `newSession`, which arrived together with
        # requireSession and DELETE /api/sessions. None until the first answer that
        # names a session.
        self._tracks_sessions = None
        # Set once a statement left state behind that a fresh session would not have.
        self._dirty = False

    # -- PEP 249 surface ----------------------------------------------------

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        wanted = bool(value)
        if wanted == self._autocommit:
            return
        self._autocommit = wanted
        if wanted:
            # No BEGIN is owed with autocommit on, whatever the COMMIT meets: one left
            # armed would open a transaction that nobody commits.
            self._begin_pending = False
            self._end_transaction("COMMIT")
        else:
            self._begin_pending = True

    def cursor(self):
        self._check_open()
        return Cursor(self)

    def commit(self):
        self._check_open()
        try:
            self._end_transaction("COMMIT")
        finally:
            self._arm_next_transaction()

    def rollback(self):
        self._check_open()
        try:
            self._end_transaction("ROLLBACK")
        finally:
            self._arm_next_transaction()

    def _arm_next_transaction(self):
        """With autocommit off the connection stays transactional: once no transaction is
        open, however the last one ended, the next statement opens a fresh one. One still
        open (a COMMIT whose fate is unknown) is joined instead."""
        self._begin_pending = not self._autocommit and not self._in_transaction

    def begin(self):
        """Start an explicit transaction (disables autocommit for this connection).

        BEGIN itself goes out with autocommit off, and the connection turns autocommit off
        only once BEGIN took: a begin() that raises leaves the connection as it was, so
        the next statement does not run in a transaction nobody opened.
        """
        self._check_open()
        # Send the DSN's USE statements first. Run inside the transaction they would
        # implicitly commit it, quietly losing everything that followed.
        self._drain_pending_use()
        self._execute_raw("BEGIN", autocommit=False)
        self._autocommit = False
        self._begin_pending = False
        self._in_transaction = True

    def _end_transaction(self, statement):
        """Send COMMIT or ROLLBACK for the open transaction, when one is open.

        The transaction stays marked open while the statement travels: a COMMIT that finds
        the session gone must say the transaction went with it, not run on a fresh one.
        Afterwards it is ended, except after a COMMIT whose answer never came (a transport
        failure, a timeout, an unreadable answer): its fate is unknown, so the transaction
        stays open, the next statement joins it, and the next commit() sends COMMIT again.
        A COMMIT the engine refused leaves a transaction nobody chose, so it is rolled back,
        best effort, before the refusal is raised.
        """
        if not self._in_transaction:
            return
        if statement != "COMMIT":
            try:
                self._execute_raw(statement)
            finally:
                self._in_transaction = False
            return
        try:
            self._execute_raw(statement)
        except SessionLostError:
            raise  # the transaction went with its session, and _recover said so
        except ProgrammingError:
            try:
                self._execute_raw("ROLLBACK")
            except Exception:
                pass
            self._in_transaction = False
            raise
        self._in_transaction = False

    def close(self):
        """Close the connection and release its engine session.

        The release is DELETE /api/sessions/{id}, which also rolls back a transaction the
        session left open. An engine that predates the endpoint gets a ROLLBACK for an
        open transaction instead, and keeps the session until its own idle expiry. Either
        is one best-effort request, bounded by the shorter of the connection's timeout and
        five seconds: closing never raises, and closing again sends nothing.
        """
        if self._closed:
            return
        self._closed = True
        session_id = self._session_id
        in_transaction = self._in_transaction
        self._session_id = None
        self._in_transaction = False
        if session_id is None:
            return
        budget = _CLOSE_BUDGET
        if self._timeout and self._timeout < budget:
            budget = self._timeout
        if self._tracks_sessions:
            self._courtesy("DELETE", "/api/sessions/" + _urlparse.quote(session_id, safe=""),
                           None, budget)
        elif in_transaction:
            self._courtesy("POST", "/api/execute",
                           {"sql": "ROLLBACK", "autoCommit": self._autocommit,
                            "sessionId": session_id},
                           budget)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                try:
                    self.rollback()
                except Error:
                    pass
        finally:
            self.close()

    # -- protocol -----------------------------------------------------------

    def _check_open(self):
        if self._closed:
            raise InterfaceError("connection is closed")

    def _use_scope(self, statements):
        """Make `statements` the connection's scope, in place of the DSN's USE DATABASE /
        USE SCHEMA: for a client built on this driver that sets up more (a role, a
        warehouse, session parameters). Called before the first statement; the
        statements are applied lazily like the DSN's, and go back on a session that
        replaces a lost one."""
        self._scope = list(statements)
        self._pending_use = list(statements)

    def _drain_pending_use(self):
        """Put the connection's scope on its session, starting one when there is none.

        The statements run one at a time, and each is spent only once the engine accepts
        it. A refused one stays first in line, so every later statement is refused with it
        until the scope applies, rather than running in the server's default database. A
        session found gone before the scope is on it held nothing yet, so the scope starts
        over on a fresh session — once: an engine that refuses a session it has just
        started is reported instead.
        """
        restarted = False
        while self._pending_use:
            pending = self._pending_use
            statement = pending[0]
            out = self._post(statement)
            if out is None:
                if restarted:
                    raise SessionLostError("the engine refused a session it had just started")
                restarted = True
                self._session_id = None
                self._pending_use = list(self._scope)
                continue
            if not out.get("success"):
                error = ProgrammingError(out.get("errorMessage") or "statement failed")
                # The caller's own statement never ran; this names the one that failed.
                error.statement = statement
                raise error
            # An answer that says the engine replaced the session put the whole scope back
            # in line (_absorb), and that line starts over from its first statement.
            if self._pending_use is pending:
                pending.pop(0)

    def _execute(self, sql, num_statements=None):
        self._check_open()
        self._drain_pending_use()
        if self._begin_pending:
            # Spent only once BEGIN took: one that failed goes out again ahead of the next
            # statement, which never runs outside the transaction autocommit off promises.
            self._execute_raw("BEGIN", autocommit=False)
            self._begin_pending = False
            self._in_transaction = True
        return self._execute_raw(sql, num_statements)

    def _execute_raw(self, sql, num_statements=None, autocommit=None):
        """Run one statement, recovering a lost session. `autocommit` is this request's
        own autoCommit, in place of the connection's."""
        out = self._post(sql, num_statements, autocommit)
        if out is None:
            out = self._recover(sql, num_statements, autocommit)
        if not out.get("success"):
            raise ProgrammingError(out.get("errorMessage") or "statement failed")
        self._track(sql)
        return out

    def _post(self, sql, num_statements=None, autocommit=None):
        """One POST /api/execute, without any recovery: the engine's answer, or None when
        it refused the session id as unknown (requireSession) — then nothing ran."""
        payload = {"sql": sql,
                   "autoCommit": self._autocommit if autocommit is None else autocommit}
        sent_id = bool(self._session_id)
        required = False
        if sent_id:
            payload["sessionId"] = self._session_id
            if self._tracks_sessions:
                # Resume this session or refuse: without the flag the engine starts a
                # fresh session under the same id when the old one is gone, and the
                # statement runs without the context the connection set up. Sent only to
                # an engine that has shown it knows the field.
                payload["requireSession"] = True
                required = True
        if num_statements is not None:
            # Checked before anything is sent, so a bad value never reaches the server.
            payload["multiStatementCount"] = _statement_count(num_statements)
        req = _urlrequest.Request(
            self._base_url + "/api/execute",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status = 200
        try:
            with _urlrequest.urlopen(req, timeout=self._timeout) as resp:
                out = _load_json(resp.read().decode("utf-8"))
        except _urlerror.HTTPError as e:
            # The server answers failed statements with a non-2xx status AND the
            # error payload in the body — read it instead of surfacing the status.
            status = e.code
            try:
                out = _load_json(e.read().decode("utf-8"))
            except Exception:
                raise OperationalError(str(e)) from e
        except OSError as e:
            raise OperationalError(str(e)) from e
        if required and status == 404 and not out.get("success") and not out.get("sessionId"):
            return None
        self._absorb(out, sent_id)
        return out

    def _absorb(self, out, sent_id):
        """Take the session an answer names, and what it says about the engine."""
        session_id = out.get("sessionId")
        if not session_id:
            return
        self._session_id = session_id
        started = out.get("newSession")
        if isinstance(started, bool):
            self._tracks_sessions = True
            if started and sent_id:
                # The engine ran the statement in a fresh session in place of ours:
                # whatever the old one held is gone, and the scope goes back on before
                # the next statement.
                self._forget_session()
        elif self._tracks_sessions is None:
            self._tracks_sessions = False

    def _forget_session(self):
        """What the session held is gone; the next statement starts on the scope."""
        self._in_transaction = False
        self._dirty = False
        self._pending_use = list(self._scope)

    def _recover(self, sql, num_statements, autocommit=None):
        """The engine no longer knows the session (it expired, was released, or the server
        restarted), and nothing ran. With a transaction or a moved context gone with it,
        re-running would put the statement somewhere its author did not intend, so that
        is refused; otherwise a fresh session on the connection's scope takes over and the
        statement is sent once more."""
        had_transaction = self._in_transaction
        had_context = self._dirty
        self._session_id = None
        self._forget_session()
        if had_transaction or had_context:
            # With autocommit off the connection stays transactional, so the fresh
            # session the next statement starts opens a transaction first.
            self._begin_pending = not self._autocommit
        if had_transaction:
            raise SessionLostError(
                "the engine no longer holds this connection's session (it expired, was "
                "released, or the server restarted), so its open transaction is gone; the "
                "statement did not run")
        if had_context:
            raise SessionLostError(
                "the engine no longer holds this connection's session (it expired, was "
                "released, or the server restarted), and the context set up on it (USE, "
                "SET, ALTER SESSION or a temporary object) went with it, so the statement "
                "was not re-run; the next statement starts a fresh session on the "
                "connection's scope")
        self._drain_pending_use()
        out = self._post(sql, num_statements, autocommit)
        if out is None:
            raise SessionLostError("the engine refused a session it had just started")
        return out

    def _track(self, sql):
        """Follow what a statement that ran did to the session: whether it left state a
        fresh session would not have, and whether it opened or ended a transaction."""
        for statement in _split_statements(sql):
            if _touches_session(statement):
                self._dirty = True
            effect = _transaction_effect(statement)
            if effect == _TRANSACTION_BEGINS:
                self._in_transaction = True
            elif effect == _TRANSACTION_ENDS:
                self._in_transaction = False

    def _courtesy(self, method, path, payload, timeout):
        """One best-effort request on the way out; its answer and any failure are
        ignored."""
        data = None
        headers = {}
        if payload is not None:
            data = _json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = _urlrequest.Request(self._base_url + path, data=data, headers=headers,
                                  method=method)
        try:
            with _urlrequest.urlopen(req, timeout=timeout) as resp:
                resp.read()
        except Exception:
            pass


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

    def execute(self, operation, parameters=None, num_statements=None):
        """Run one statement, or a pack of them.

        `num_statements` says how many statements this one call carries, `0` for any
        number, and is sent with the request: it outranks the session's
        MULTI_STATEMENT_COUNT for this call only and changes no session state. Left out,
        nothing is sent and the session's value decides, which is 1 until it is told
        otherwise.
        """
        self._check_open()
        sql = _substitute(operation, parameters) if parameters else operation
        out = self.connection._execute(sql, num_statements)
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
        # size=0 means zero rows; only an omitted size falls back to arraysize.
        n = self.arraysize if size is None else size
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
        # internal_size carries the wire's `length`: characters for a text column, bytes
        # for a binary one. Every other type omits the field, as do servers predating it,
        # and PEP 249 reads a missing size as None — so an absent length stays None
        # rather than becoming 0 or an invented maximum.
        #
        # display_size stays None even when the length is known, which is what the
        # account's own Python client does (its ResultMetadata passes None for that slot).
        # The server sends no display width, and deriving one from the length would be a
        # guess dressed up as metadata.
        self.description = [
            (c.get("name"), c.get("dataType"), None, c.get("length"),
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
        # DML answers with a status row of counters rather than data; surface it as
        # rowcount and hand back an empty result set.
        if len(self._rows) == 1 and _is_dml_status(columns):
            self.rowcount = _dml_rowcount(columns, self._rows[0])
            self.description = None
            self._rows = []
        else:
            self.rowcount = len(self._rows)


# -- DML status rows --------------------------------------------------------

def _is_dml_status(columns):
    """Whether a result set is a DML status row rather than data.

    The protocol carries no statement type, so this goes by shape: DML answers with a
    single row whose every column is a "number of ..." counter. INSERT and DELETE report
    one; UPDATE adds "number of multi-joined rows updated", and MERGE reports both an
    inserted and an updated count.
    """
    if not columns:
        return False
    for column in columns:
        if not str(column.get("name") or "").lower().startswith("number of "):
            return False
    return True


def _dml_rowcount(columns, row):
    """Total rows affected. "number of multi-joined rows updated" is a diagnostic
    sub-count of the rows already counted as updated, so it is deliberately left out."""
    total = 0
    for index, column in enumerate(columns):
        name = str(column.get("name") or "").lower()
        if name.startswith("number of rows ") and index < len(row):
            value = row[index]
            if value is not None:
                total += int(value)
    return total


# -- value conversion -------------------------------------------------------

# Types the engine stores as binary floating point; every other numeric is fixed-point.
_APPROXIMATE_TYPES = frozenset(("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE",
                                "DOUBLE PRECISION", "REAL"))


def _statement_count(value):
    """The per-call statement count a caller asked for, checked before it is sent.

    How many statements the call carries, `0` meaning any number. It rides on the request
    and leaves the session's MULTI_STATEMENT_COUNT alone, so there is nothing to save and
    put back, and a connection shared between cursors is unaffected.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProgrammingError(
            "num_statements must be an int, not " + type(value).__name__)
    if value < 0:
        raise ProgrammingError("num_statements must be 0 or more, not %d" % value)
    return value


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
        if data_type in ("BINARY", "VARBINARY"):
            # The engine renders binary as hex; hand back the bytes that went in.
            try:
                return bytes.fromhex(value)
            except ValueError:
                return value
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

_PLAIN_IDENT = _re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_QUOTED_IDENT = _re.compile(r'"(?:[^"]|"")*"\Z')


def _quote_ident(name):
    """Render a DSN's database or schema name for a USE statement.

    A plain name is sent bare, so it folds to upper case exactly as it does in SQL:
    ``my_db`` selects MY_DB. Quoted as given it would ask for an object named
    ``my_db`` in lower case, which USE refuses — it resolves names exactly, as live
    does.

    A name already written in double quotes is passed through untouched, which is how
    a caller reaches an object whose real name is not upper case:
    ``database='"my_db"'`` selects the lower-case my_db. Anything else (a space, a
    leading digit, a stray quote) is quoted as given.

    An embedded double quote has to be doubled: without that, a name carrying one
    would end the quoted identifier early and the rest would be parsed as SQL. Only a
    well-formed quoted name — every interior quote already doubled — is passed
    through, so a half-quoted one cannot smuggle SQL past this.
    """
    text = str(name)
    if _PLAIN_IDENT.match(text) or _QUOTED_IDENT.match(text):
        return text
    return '"' + text.replace('"', '""') + '"'


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
        elif ch == "$" and sql.startswith("$$", i):
            j = _skip_dollar_quoted(sql, i)
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


def _skip_dollar_quoted(s, i):
    """Step over a $$…$$ block — a dollar-quoted string, which is how the engine's
    procedure and UDF bodies are written. Everything inside is data, including the
    semicolons and placeholders that would otherwise be interpreted."""
    end = s.find("$$", i + 2)
    return len(s) if end < 0 else end + 2


# -- session tracking ---------------------------------------------------------

# How long close() may spend releasing the session, at most.
_CLOSE_BUDGET = 5.0

_TRANSACTION_BEGINS = "begins"
_TRANSACTION_ENDS = "ends"

# The words that may sit between CREATE/DROP/ALTER and the kind of object being named.
_OBJECT_MODIFIERS = frozenset((
    "OR", "REPLACE", "TRANSIENT", "TEMPORARY", "TEMP", "VOLATILE", "LOCAL", "GLOBAL",
    "SECURE", "IF", "NOT", "EXISTS", "PUBLIC", "PRIVATE", "ICEBERG", "DYNAMIC", "HYBRID",
    "EVENT", "RECURSIVE", "MATERIALIZED", "EXTERNAL",
))
_TEMPORARY_MODIFIERS = frozenset(("TEMPORARY", "TEMP", "VOLATILE"))


def _skip_non_code(sql, i):
    """Index just past the literal, quoted identifier, $$ body or comment starting at
    `i`, or -1 when `i` is code: the constructs _substitute steps over."""
    ch = sql[i]
    if ch == "'":
        return _skip_string(sql, i)
    if ch == '"':
        return _skip_quoted(sql, i, '"')
    if sql.startswith("--", i) or sql.startswith("//", i):
        return _skip_line(sql, i)
    if sql.startswith("/*", i):
        end = sql.find("*/", i + 2)
        return len(sql) if end < 0 else end + 2
    if sql.startswith("$$", i):
        return _skip_dollar_quoted(sql, i)
    return -1


def _split_statements(sql):
    """The request split on its top-level semicolons; one inside a literal, a quoted
    identifier, a $$ body or a comment does not split. Blank pieces are dropped.

    A scripting block is split along with everything else, which only makes the session
    checks more willing to flag a request — the safe direction to be wrong in.
    """
    pieces = []
    start = 0
    i = 0
    n = len(sql)
    while i < n:
        past = _skip_non_code(sql, i)
        if past >= 0:
            i = past
        elif sql[i] == ";":
            pieces.append(sql[start:i])
            i += 1
            start = i
        else:
            i += 1
    pieces.append(sql[start:])
    return [piece for piece in pieces if piece.strip()]


def _is_word_char(ch):
    return ch == "_" or ch == "$" or ch.isalnum()


def _leading_words(statement, limit):
    """Up to `limit` leading words of a statement, upper-cased, skipping whitespace and
    comments and stopping at the first thing that is not a word."""
    words = []
    i = 0
    n = len(statement)
    while len(words) < limit and i < n:
        ch = statement[i]
        if ch.isspace():
            i += 1
        elif statement.startswith("--", i) or statement.startswith("//", i):
            i = _skip_line(statement, i)
        elif statement.startswith("/*", i):
            end = statement.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif _is_word_char(ch):
            start = i
            while i < n and _is_word_char(statement[i]):
                i += 1
            words.append(statement[start:i].upper())
        else:
            break
    return words


def _touches_session(statement):
    """Whether a statement leaves behind state a fresh session would not have: a moved
    scope (USE, CREATE or DROP of a DATABASE or SCHEMA), a session variable or setting
    (SET, UNSET, ALTER SESSION), or a temporary object. CREATE TABLE and its kind leave
    the session as it was."""
    words = _leading_words(statement, 16)
    if not words:
        return False
    verb, rest = words[0], words[1:]
    if verb in ("USE", "SET", "UNSET"):
        return True
    if verb not in ("ALTER", "CREATE", "DROP"):
        return False
    kind = 0
    while kind < len(rest) and rest[kind] in _OBJECT_MODIFIERS:
        kind += 1
    named = rest[kind] if kind < len(rest) else None
    if verb == "ALTER":
        return named == "SESSION"
    if named in ("DATABASE", "SCHEMA"):
        return True
    if verb == "CREATE":
        for word in rest[:kind]:
            if word in _TEMPORARY_MODIFIERS:
                return True
    return False


def _transaction_effect(statement):
    """Whether a statement opens or ends a transaction. BEGIN on its own (or with
    TRANSACTION, WORK or NAME) opens one; BEGIN followed by a statement opens a scripting
    block instead."""
    words = _leading_words(statement, 2)
    if words == ["BEGIN"] or words == ["START", "TRANSACTION"]:
        return _TRANSACTION_BEGINS
    if len(words) == 2 and words[0] == "BEGIN" and words[1] in ("TRANSACTION", "WORK", "NAME"):
        return _TRANSACTION_BEGINS
    if words and words[0] in ("COMMIT", "ROLLBACK"):
        return _TRANSACTION_ENDS
    return None
