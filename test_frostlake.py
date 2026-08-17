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


def setUpModule():
    global SERVER, DSN
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
