# frostlake (Python driver)

A pure-stdlib DB-API 2.0 (PEP 249) driver for [Frostlake](https://frostlake.dev), speaking
the engine's HTTP protocol against a running `DatabaseHttpServer`. No JVM, no dependencies.

## Engine version

Requires a Frostlake engine **0.0.7 or newer**. Ask a running server which one it is with
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
statements on the connection's session before its first statement.

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
- **Type objects and constructors**: the PEP 249 singletons `STRING`, `BINARY`, `NUMBER`,
  `DATETIME`, `ROWID` compare equal to the engine type names in their family, so
  `cur.description[i][1] == frostlake.NUMBER` works (parameterized spellings like
  `NUMBER(38,10)` included). `Date`, `Time`, `Timestamp`, the `*FromTicks` variants and
  `Binary` are all present. `ROWID` matches nothing — the engine has no rowid.
- **Multi-statement**: `execute()` exposes the first result set; `cursor.nextset()` steps
  to the next and returns `None` once the last one is current.
- **Transactions**: connections start in autocommit (matching the Snowflake Python
  connector, not strict DB-API); `conn.begin()` or `conn.autocommit = False` for explicit
  transactions, then `commit()`/`rollback()`.
- `cursor.rowcount` is derived from the engine's one-cell DML result
  (`number of rows inserted` / `updated` / `deleted`). `cursor.lastrowid` is always `None`.
- `cursor.callproc(name, params)` issues `CALL name(...)` and returns the input sequence
  unchanged; the engine has no OUT parameters, so any result is read with the fetch methods.
- Using a closed cursor or connection raises `InterfaceError`; fetching before any
  `execute()` raises `ProgrammingError`.
- The exception classes are also reachable as connection attributes (`conn.Error`, …).
- One HTTP session per connection.

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
