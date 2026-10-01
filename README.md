# frostlake (Python driver)

A pure-stdlib DB-API 2.0 (PEP 249) driver for [Frostlake](https://frostlake.dev), speaking
the engine's HTTP protocol against a running `DatabaseHttpServer`. No JVM, no dependencies.

## Engine version

Requires a Frostlake engine **0.2.0 or newer**. Ask a running server which one it is with
`SELECT CURRENT_VERSION()` — every release answers it, so the check works against any engine.

The driver versions independently of the engine: it speaks the HTTP protocol, not
the jar, so this is a floor rather than a lockstep pin.

## Usage

```python
import frostlake

conn = frostlake.connect("frostlake://localhost:18082/MY_DB?schema=PUBLIC")
cur = conn.cursor()
cur.execute("SELECT id, name FROM people WHERE id = ?", (1,))
for row in cur:
    print(row)
```

`connect()` accepts a DSN (`frostlake://host:port[/DATABASE][?schema=SCHEMA]`) or
`host=`/`port=`/`database=`/`schema=` keywords. The DSN's database/schema apply as `USE`
statements on the connection's session before its first statement. A `USE` the engine
refuses (a database or schema that does not exist, or one the role cannot use) stays
first in line: every statement fails with that refusal, whose `statement` attribute names
the refused `USE`, until the engine accepts it. Nothing runs in the server's default
database in its place.

Those names follow SQL's own rule: written plainly, a name folds to upper case, so
`database="my_db"` selects `MY_DB`. To reach an object whose real name is lower- or
mixed-case, include the double quotes — `database='"my_db"'`, or
`frostlake://host:port/"my_db"` — and it is used exactly as written. A name that cannot
be written bare (a space, a leading digit) is quoted for you.

## Semantics

- `paramstyle = "qmark"`; parameters are inlined client-side with the same rules as
  Frostlake's JDBC driver (strings escape backslashes and quotes; `bytes` binds as a hex
  `BINARY` literal; `datetime`/`date`/`time` as typed literals; lists as array literals).
  A `?` inside a string literal, quoted identifier or comment is never a placeholder.
- **Types**: fixed-point `NUMBER`/`DECIMAL` with a scale → `decimal.Decimal` carrying the
  exact digits the server sent; scale-0 numerics → `int`; `FLOAT`/`DOUBLE`/`REAL` →
  `float`; `BOOLEAN` → `bool`; `DATE`/`TIME`/`TIMESTAMP*` → `datetime.date`/`time`/
  `datetime`; semi-structured cells (`VARIANT`/`OBJECT`/`ARRAY`) as the JSON text the
  engine returns.
- **Column sizes**: `description[i][3]` (`internal_size`) is the column's length —
  characters for a text column, bytes for a binary one — when the server sends one, and
  `None` for every other type and for engines predating the field. `display_size` stays
  `None` throughout, as it does in the account's own Python client: the server sends no
  display width, so there is none to report.
- **Type objects and constructors**: the PEP 249 singletons `STRING`, `BINARY`, `NUMBER`,
  `DATETIME`, `ROWID` compare equal to the engine type names in their family, so
  `cur.description[i][1] == frostlake.NUMBER` works (parameterized spellings like
  `NUMBER(38,10)` included). `Date`, `Time`, `Timestamp`, the `*FromTicks` variants and
  `Binary` are all present. `ROWID` matches nothing — the engine has no rowid.
- **Multi-statement**, once the call or the session asks for it: as on the account, a request
  carries one statement unless something says otherwise, and a pack sent without asking is
  refused. `cursor.execute(sql, num_statements=n)` declares how many statements that one call
  carries — `0` for any number — the way the account's own connector spells it: the count
  travels with that request, outranks the session's `MULTI_STATEMENT_COUNT` for it, and moves
  no session state, so there is nothing to put back and other cursors on the connection are
  unaffected. `ALTER SESSION SET MULTI_STATEMENT_COUNT = n` still sets it for the session;
  left out, nothing is sent and the session's value decides, which is 1 until it is told
  otherwise. `execute()` exposes the first result set; `cursor.nextset()` steps to the next
  and returns `None` once the last one is current.
- **Transactions**: connections start in autocommit rather than the strict DB-API
  default; `conn.begin()` or `conn.autocommit = False` for explicit transactions, then
  `commit()`/`rollback()`. With autocommit off the connection stays transactional —
  ending one transaction opens the next. A transaction opened with a `BEGIN` statement
  counts too: `commit()`/`rollback()` end it. `begin()` turns autocommit off only once its
  `BEGIN` took, so a `begin()` that raises leaves the connection as it was, and the next
  statement commits on its own. With autocommit off, a `BEGIN` that failed goes out again
  ahead of the next statement, so no statement runs outside a transaction. Turning
  autocommit back on leaves no `BEGIN` owed, even when the `COMMIT` it sends fails. A
  `COMMIT` the engine refuses is followed by a `ROLLBACK`, which ends the transaction before
  the refusal is raised. A `COMMIT` whose answer never came (a dropped connection, a
  timeout, an unreadable answer) leaves the transaction open: the next statement joins it,
  and the next `commit()` sends `COMMIT` again. A `ROLLBACK` ends the transaction whatever
  it meets. After any of these, with autocommit off, the next statement runs in a
  transaction: the open one, or a fresh one.
- `cursor.rowcount` is derived from the engine's one-cell DML result
  (`number of rows inserted` / `updated` / `deleted`). `cursor.lastrowid` is always `None`.
- `cursor.callproc(name, params)` issues `CALL name(...)` and returns the input sequence
  unchanged; the engine has no OUT parameters, so any result is read with the fetch methods.
- Using a closed cursor or connection raises `InterfaceError`; fetching before any
  `execute()` raises `ProgrammingError`.
- The exception classes are also reachable as connection attributes (`conn.Error`, …).
- One engine session per connection; see below.

## Session lifetime

A connection holds one engine session, which carries its current database and schema,
session variables, `ALTER SESSION` settings and an open transaction. The engine names the
session in its first answer, and every later request names it again.

- **What is sent.** Once the engine has shown that it tracks sessions (its answers carry
  `newSession`, as engines from 0.1.0 do), every request that names the session also sends
  `requireSession: true`: resume this session, or refuse. An engine whose answers lack the
  field is never sent it.
- **After a lost session.** The engine forgets a session that sat idle for 30 minutes, was
  released, or went with a restart, and it refuses a request that requires it (HTTP 404)
  without running anything. The driver drops the session and then:
  - when the session held nothing a fresh one would lack, it starts a fresh session on the
    connection's scope (the DSN's `USE DATABASE` / `USE SCHEMA`) and sends the statement
    once more. A second refusal raises;
  - when the session held an open transaction, or context set up with `USE`, `SET` /
    `UNSET`, `ALTER SESSION`, a temporary object, or a `CREATE` / `DROP` of a database or
    schema, it raises `frostlake.SessionLostError` (an `OperationalError`, also
    `conn.SessionLostError`) instead. The statement did not run: in a fresh session it
    would run somewhere its author did not intend. The connection stays usable. The next
    statement starts a fresh session on the connection's scope, and with autocommit off it
    opens a transaction there first.

  The driver reads what a statement did from its text: `BEGIN` / `START TRANSACTION` and
  `COMMIT` / `ROLLBACK` for the transaction, and the statements above for context.
- **Close.** `close()` releases the session with `DELETE /api/sessions/{id}`, which rolls
  back a transaction left open. An engine without that endpoint gets a `ROLLBACK` for an
  open transaction instead, and keeps the session until its own idle expiry. Either is one
  request, bounded by the shorter of the connection's `timeout` and 5 seconds. `close()`
  never raises, and a second `close()` sends nothing. Leaving a `with` block closes the
  connection even when its commit fails.

## Tests

`test_frostlake_unit.py` needs no server and no JVM:

```bash
python3 -m unittest test_frostlake_unit -v
```

`test_frostlake.py` boots a real engine and needs both:

```bash
JAVA_HOME=~/.jdks/liberica-17.0.18 \
FROSTLAKE_CLASSPATH="<engine classes>:<dependency classpath>" \
python3 -m unittest -v
```

Unset `FROSTLAKE_CLASSPATH` skips the integration suite; the unit suite still runs.

Set `FL_CORPUS` to the engine's testkit directory (an absolute path) and that run also
replays the engine's language-neutral SQL corpus through the driver (`testkit_runner.py`);
without it, `test_testkit.py` skips:

```bash
FL_CORPUS=/path/to/frostlake/engine/src/test/resources/testkit \
JAVA_HOME=~/.jdks/liberica-17.0.18 \
FROSTLAKE_CLASSPATH="<engine classes>:<dependency classpath>" \
python3 -m unittest -v
```
