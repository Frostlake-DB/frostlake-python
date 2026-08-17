"""Integration tests: boot a real DatabaseHttpServer from FROSTLAKE_CLASSPATH.

Run:  JAVA_HOME=... FROSTLAKE_CLASSPATH=... python3 -m unittest -v
Unset FROSTLAKE_CLASSPATH skips the suite.
"""

import datetime
import decimal
import os
import socket
import subprocess
import time
import unittest
import urllib.request

import frostlake

SERVER = None
DSN = None
PORT = None


def setUpModule():
    global SERVER, DSN, PORT
    cp = os.environ.get("FROSTLAKE_CLASSPATH")
    if not cp:
        raise unittest.SkipTest("FROSTLAKE_CLASSPATH not set")
    java = os.path.join(os.environ.get("JAVA_HOME", ""), "bin", "java") if os.environ.get("JAVA_HOME") else "java"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    SERVER = subprocess.Popen([java, "-cp", cp, "dev.frostlake.http.DatabaseHttpServer", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.2)
    DSN = "frostlake://127.0.0.1:%d" % port
    PORT = port


def tearDownModule():
    if SERVER:
        SERVER.kill()


class FrostlakeDriverTest(unittest.TestCase):
    def setUp(self):
        self.conn = frostlake.connect(DSN)

    def tearDown(self):
        self.conn.close()

    def test_ddl_dml_and_typed_query(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_test_db")
        cur.execute("USE DATABASE py_test_db")
        cur.execute("CREATE TABLE people (id INTEGER, name VARCHAR, score FLOAT, ok BOOLEAN, born DATE, created_at TIMESTAMP_NTZ)")
        cur.execute("INSERT INTO people VALUES (?, ?, ?, ?, ?, ?)",
                    (1, "Ada O'Hara \\ Byron", 9.5, True,
                     datetime.date(1815, 12, 10), datetime.datetime(2026, 8, 13, 12, 30, 0)))
        self.assertEqual(1, cur.rowcount)
        cur.execute("SELECT id, name, score, ok, born, created_at FROM people WHERE id = ?", (1,))
        row = cur.fetchone()
        self.assertEqual((1, "Ada O'Hara \\ Byron", 9.5, True), row[:4])
        self.assertEqual(datetime.date(1815, 12, 10), row[4])
        self.assertEqual(datetime.datetime(2026, 8, 13, 12, 30, 0), row[5])
        self.assertIsNone(cur.fetchone())

    def test_description_and_iteration(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS n, 'x' AS s")
        self.assertEqual(["N", "S"], [d[0] for d in cur.description])
        self.assertEqual([(1, "x")], list(cur))

    def test_session_state_persists(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_sess_db")
        cur.execute("USE DATABASE py_sess_db")
        cur.execute("CREATE TABLE t1 (a INTEGER)")
        cur.execute("INSERT INTO t1 VALUES (7)")
        cur.execute("SELECT a FROM t1")
        self.assertEqual([(7,)], cur.fetchall())

    def test_transaction_rollback(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_tx_db")
        cur.execute("USE DATABASE py_tx_db")
        cur.execute("CREATE TABLE acc (n INTEGER)")
        cur.execute("INSERT INTO acc VALUES (1)")
        self.conn.begin()
        cur.execute("INSERT INTO acc VALUES (2)")
        self.conn.rollback()
        self.conn.autocommit = True
        cur.execute("SELECT COUNT(*) FROM acc")
        self.assertEqual([(1,)], cur.fetchall())

    def test_error_surface(self):
        cur = self.conn.cursor()
        with self.assertRaises(frostlake.ProgrammingError):
            cur.execute("SELECT FROM nowhere")

    def test_placeholder_inside_literal(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 'a?b' || ?", ("!",))
        self.assertEqual([("a?b!",)], cur.fetchall())

    def test_executemany(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_many_db")
        cur.execute("USE DATABASE py_many_db")
        cur.execute("CREATE TABLE m (v INTEGER)")
        cur.executemany("INSERT INTO m VALUES (?)", [(i,) for i in range(5)])
        self.assertEqual(5, cur.rowcount)
        cur.execute("SELECT COUNT(*) FROM m")
        self.assertEqual([(5,)], cur.fetchall())

    def test_connect_with_host_and_port_keywords(self):
        conn = frostlake.connect(host="127.0.0.1", port=PORT)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS n")
        self.assertEqual([(1,)], cur.fetchall())
        conn.close()

    def test_dsn_database_and_schema_are_applied(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_dsn_db")
        cur.execute("CREATE SCHEMA IF NOT EXISTS py_dsn_db.s2")

        # The DSN's database/schema are queued and drained before the first statement.
        scoped = frostlake.connect(DSN + "/py_dsn_db?schema=s2")
        scoped_cur = scoped.cursor()
        scoped_cur.execute("SELECT CURRENT_DATABASE() AS d, CURRENT_SCHEMA() AS s")
        self.assertEqual([("PY_DSN_DB", "S2")], scoped_cur.fetchall())
        scoped.close()

    def test_connection_context_manager_commits(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_ctx_db")
        cur.execute("USE DATABASE py_ctx_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        with frostlake.connect(DSN + "/py_ctx_db") as conn:
            inner = conn.cursor()
            conn.begin()
            inner.execute("INSERT INTO t VALUES (1)")
        # Leaving the block commits, so the row outlives the connection.
        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(1,)], cur.fetchall())

    def test_connection_context_manager_rolls_back_on_error(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_ctxerr_db")
        cur.execute("USE DATABASE py_ctxerr_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        class Boom(Exception):
            pass

        try:
            with frostlake.connect(DSN + "/py_ctxerr_db") as conn:
                inner = conn.cursor()
                conn.begin()
                inner.execute("INSERT INTO t VALUES (1)")
                raise Boom()
        except Boom:
            pass
        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(0,)], cur.fetchall())

    def test_begin_sends_the_dsn_use_statements_first(self):
        # A USE inside a transaction implicitly commits it, so begin() has to drain the
        # DSN's pending USE statements before opening one.
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_beginorder_db")
        cur.execute("USE DATABASE py_beginorder_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        scoped = frostlake.connect(DSN + "/py_beginorder_db")
        scoped_cur = scoped.cursor()
        scoped.begin()                       # the very first call on the connection
        scoped_cur.execute("INSERT INTO t VALUES (1)")
        scoped.rollback()
        scoped.close()

        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(0,)], cur.fetchall())

    def test_work_after_commit_is_still_transactional(self):
        # With autocommit off the connection stays transactional: commit() ends one
        # transaction and the next statement opens another, which rollback() can undo.
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_aftercommit_db")
        cur.execute("USE DATABASE py_aftercommit_db")
        cur.execute("CREATE TABLE t (a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")

        self.conn.begin()
        cur.execute("INSERT INTO t VALUES (2)")
        self.conn.commit()
        cur.execute("INSERT INTO t VALUES (3)")
        self.conn.rollback()
        self.conn.autocommit = True

        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(2,)], cur.fetchall())

    def test_work_after_rollback_is_still_transactional(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_afterrollback_db")
        cur.execute("USE DATABASE py_afterrollback_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        self.conn.begin()
        cur.execute("INSERT INTO t VALUES (1)")
        self.conn.rollback()
        cur.execute("INSERT INTO t VALUES (2)")
        self.conn.rollback()
        self.conn.autocommit = True

        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(0,)], cur.fetchall())

    def test_autocommit_false_opens_a_transaction(self):
        # Setting the attribute has to behave like begin(), not merely flip a flag.
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_acattr_db")
        cur.execute("USE DATABASE py_acattr_db")
        cur.execute("CREATE TABLE t (a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")

        self.conn.autocommit = False
        cur.execute("DELETE FROM t")
        self.conn.rollback()
        self.conn.autocommit = True

        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(1,)], cur.fetchall())

    def test_autocommit_back_on_commits_open_work(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_acon_db")
        cur.execute("USE DATABASE py_acon_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        self.conn.autocommit = False
        cur.execute("INSERT INTO t VALUES (1)")
        self.conn.autocommit = True        # commits what was open
        self.conn.rollback()               # nothing left to undo

        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(1,)], cur.fetchall())

    def test_fetchmany_zero_returns_nothing(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS n UNION ALL SELECT 2")
        self.assertEqual([], cur.fetchmany(0))
        self.assertEqual([(1,), (2,)], cur.fetchall())   # nothing was consumed

    def test_binary_round_trips_as_bytes(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_bin_db")
        cur.execute("USE DATABASE py_bin_db")
        cur.execute("CREATE TABLE b (v BINARY)")
        payload = b"\x00\x01\xfe\xff"
        cur.execute("INSERT INTO b VALUES (?)", (payload,))
        cur.execute("SELECT v FROM b")
        self.assertEqual(payload, cur.fetchone()[0])

    def test_procedure_body_with_parameters_is_not_mangled(self):
        # The body is dollar-quoted, so its semicolons and any '?' are data.
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_procbody_db")
        cur.execute("USE DATABASE py_procbody_db")
        cur.execute("CREATE TABLE t (label VARCHAR)")
        cur.execute("""
            CREATE OR REPLACE PROCEDURE tag(x INTEGER)
            RETURNS INTEGER LANGUAGE SQL
            AS $$ BEGIN LET y INTEGER := x; RETURN y; END; $$
        """)
        cur.execute("INSERT INTO t VALUES (?)", ("has ? and ; inside",))
        cur.execute("SELECT label FROM t")
        self.assertEqual([("has ? and ; inside",)], cur.fetchall())
        cur.callproc("tag", (3,))
        self.assertEqual([(3,)], cur.fetchall())

    def test_database_name_with_a_quote_is_escaped(self):
        # A hostile name must not be able to break out of the identifier.
        conn = frostlake.connect(DSN, database='we"ird')
        self.assertEqual(['USE DATABASE "we""ird"'], conn._pending_use)
        conn.close()

    def test_explicit_commit_persists(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_commit_db")
        cur.execute("USE DATABASE py_commit_db")
        cur.execute("CREATE TABLE t (a INTEGER)")
        self.conn.begin()
        cur.execute("INSERT INTO t VALUES (1), (2)")
        self.conn.commit()
        self.conn.autocommit = True
        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(2,)], cur.fetchall())

    def test_commit_and_rollback_are_noops_while_autocommitting(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1")
        self.assertTrue(self.conn.autocommit)
        self.conn.commit()      # no transaction open; must not raise
        self.conn.rollback()
        cur.execute("SELECT 2 AS n")
        self.assertEqual([(2,)], cur.fetchall())

    def test_fetchmany(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4")
        self.assertEqual(1, cur.arraysize)
        self.assertEqual([(1,)], cur.fetchmany())          # arraysize defaults to 1
        self.assertEqual([(2,), (3,)], cur.fetchmany(2))
        self.assertEqual([(4,)], cur.fetchmany(10))        # asking past the end is fine
        self.assertEqual([], cur.fetchmany(10))            # exhausted
        self.assertIsNone(cur.fetchone())

    def test_arraysize_drives_fetchmany(self):
        cur = self.conn.cursor()
        cur.arraysize = 3
        cur.execute("SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4")
        self.assertEqual(3, len(cur.fetchmany()))

    def test_rowcount_for_every_dml_statement(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_dml_db")
        cur.execute("USE DATABASE py_dml_db")
        cur.execute("CREATE TABLE t (a INTEGER)")

        cur.execute("INSERT INTO t VALUES (1), (2), (3), (4)")
        self.assertEqual(4, cur.rowcount)

        # UPDATE reports a second "multi-joined" counter, so it is not a one-column
        # status row like INSERT is.
        cur.execute("UPDATE t SET a = a + 10 WHERE a <= ?", (2,))
        self.assertEqual(2, cur.rowcount)

        cur.execute("DELETE FROM t WHERE a > ?", (10,))
        self.assertEqual(2, cur.rowcount)

    def test_dml_leaves_no_result_set_behind(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_dml2_db")
        cur.execute("USE DATABASE py_dml2_db")
        cur.execute("CREATE TABLE t (a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1), (2)")
        cur.execute("UPDATE t SET a = a + 1")
        self.assertIsNone(cur.description)
        self.assertEqual([], cur.fetchall())

    def test_merge_rowcount(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_merge_db")
        cur.execute("USE DATABASE py_merge_db")
        cur.execute("CREATE TABLE target (id INTEGER, v INTEGER)")
        cur.execute("INSERT INTO target VALUES (1, 10)")
        cur.execute("""
            MERGE INTO target t
            USING (SELECT 2 AS id, 20 AS v) s ON t.id = s.id
            WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)
        """)
        self.assertEqual(1, cur.rowcount)

    def test_fixed_point_keeps_full_precision(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 12345678901234567890.1234567890::NUMBER(38,10) AS big")
        self.assertEqual(decimal.Decimal("12345678901234567890.1234567890"),
                         cur.fetchone()[0])

    def test_money_arithmetic_is_exact(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1.10::NUMBER(10,2) AS money")
        self.assertEqual(decimal.Decimal("3.30"), cur.fetchone()[0] * 3)

    def test_numeric_type_mapping(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 42::INT AS i, 0.1::FLOAT AS f, 1.50::NUMBER(10,2) AS d")
        row = cur.fetchone()
        self.assertIsInstance(row[0], int)
        self.assertIsInstance(row[1], float)
        self.assertIsInstance(row[2], decimal.Decimal)

    def test_decimal_round_trips_as_a_parameter(self):
        cur = self.conn.cursor()
        cur.execute("SELECT ?::NUMBER(20,4) AS d", (decimal.Decimal("123.4500"),))
        self.assertEqual(decimal.Decimal("123.4500"), cur.fetchone()[0])

    def test_multiple_result_sets(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS a; SELECT 2 AS b; SELECT 3 AS c;")
        collected = [cur.fetchall()]
        while cur.nextset():
            collected.append(cur.fetchall())
        self.assertEqual([[(1,)], [(2,)], [(3,)]], collected)
        self.assertIsNone(cur.nextset())

    def test_nextset_returns_none_for_a_single_statement(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS a")
        self.assertIsNone(cur.nextset())

    def test_description_reports_nullability(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_null_db")
        cur.execute("USE DATABASE py_null_db")
        cur.execute("CREATE TABLE n (req INTEGER NOT NULL, opt INTEGER)")
        cur.execute("SELECT req, opt FROM n")
        self.assertEqual([False, True], [d[6] for d in cur.description])

    def test_description_type_codes_match_the_type_objects(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS n, 'x' AS s, '2024-01-05'::DATE AS d")
        codes = [d[1] for d in cur.description]
        self.assertEqual(frostlake.NUMBER, codes[0])
        self.assertEqual(frostlake.STRING, codes[1])
        self.assertEqual(frostlake.DATETIME, codes[2])

    def test_callproc(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_proc_db")
        cur.execute("USE DATABASE py_proc_db")
        cur.execute("""CREATE OR REPLACE PROCEDURE add_two(a INTEGER, b INTEGER)
                       RETURNS INTEGER LANGUAGE SQL AS $$ BEGIN RETURN a + b; END; $$""")
        self.assertEqual((3, 4), cur.callproc("add_two", (3, 4)))
        self.assertEqual([(7,)], cur.fetchall())

    def test_user_defined_function_is_callable(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_udf_db")
        cur.execute("USE DATABASE py_udf_db")
        cur.execute("CREATE OR REPLACE FUNCTION double_it(a INTEGER) "
                    "RETURNS INTEGER AS $$ a * 2 $$")
        cur.execute("SELECT double_it(?) AS doubled", (21,))
        self.assertEqual([(42,)], cur.fetchall())

    def test_function_used_across_a_table(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_udf2_db")
        cur.execute("USE DATABASE py_udf2_db")
        cur.execute("CREATE TABLE nums (n INTEGER)")
        cur.executemany("INSERT INTO nums VALUES (?)", [(1,), (2,), (3,)])
        cur.execute("CREATE OR REPLACE FUNCTION squared(a INTEGER) "
                    "RETURNS INTEGER AS $$ a * a $$")
        cur.execute("SELECT n, squared(n) AS sq FROM nums ORDER BY n")
        self.assertEqual([(1, 1), (2, 4), (3, 9)], cur.fetchall())

    def test_function_returning_a_string(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_udf3_db")
        cur.execute("USE DATABASE py_udf3_db")
        cur.execute("CREATE OR REPLACE FUNCTION shout(v VARCHAR) "
                    "RETURNS VARCHAR AS $$ UPPER(v) || '!' $$")
        cur.execute("SELECT shout(?) AS loud", ("hello",))
        self.assertEqual([("HELLO!",)], cur.fetchall())

    def test_function_can_be_dropped(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_udf4_db")
        cur.execute("USE DATABASE py_udf4_db")
        cur.execute("CREATE OR REPLACE FUNCTION temp_fn(a INTEGER) "
                    "RETURNS INTEGER AS $$ a $$")
        cur.execute("DROP FUNCTION temp_fn(INTEGER)")
        self.assertRaises(frostlake.ProgrammingError,
                          cur.execute, "SELECT temp_fn(1)")

    def test_procedure_returning_a_computed_value(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_proc2_db")
        cur.execute("USE DATABASE py_proc2_db")
        cur.execute("CREATE TABLE amounts (v NUMBER(10,2))")
        cur.executemany("INSERT INTO amounts VALUES (?)",
                        [(decimal.Decimal("1.50"),), (decimal.Decimal("2.25"),)])
        cur.execute("""
            CREATE OR REPLACE PROCEDURE total() RETURNS NUMBER(10,2) LANGUAGE SQL
            AS $$ BEGIN LET t NUMBER(10,2) := (SELECT SUM(v) FROM amounts); RETURN t; END; $$
        """)
        cur.callproc("total")
        self.assertEqual([(decimal.Decimal("3.75"),)], cur.fetchall())

    def test_callproc_without_arguments(self):
        cur = self.conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE py_proc0_db")
        cur.execute("USE DATABASE py_proc0_db")
        cur.execute("""CREATE OR REPLACE PROCEDURE answer() RETURNS INTEGER
                       LANGUAGE SQL AS $$ BEGIN RETURN 9; END; $$""")
        cur.callproc("answer")
        self.assertEqual([(9,)], cur.fetchall())

    def test_closed_cursor_refuses_work(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        self.assertRaises(frostlake.InterfaceError, cur.execute, "SELECT 1")
        self.assertRaises(frostlake.InterfaceError, cur.fetchall)

    def test_closed_connection_blocks_its_cursors(self):
        conn = frostlake.connect(DSN)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        self.assertRaises(frostlake.InterfaceError, cur.execute, "SELECT 1")


if __name__ == "__main__":
    unittest.main()
