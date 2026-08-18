"""Unit tests for the driver's PEP 249 surface and value handling.

These need no server and no JVM, so they run on a bare checkout:

    python3 -m unittest test_frostlake_unit -v
"""

import datetime
import decimal
import http.server
import threading
import unittest

import frostlake


class ModuleSurfaceTest(unittest.TestCase):
    """PEP 249 requires these names at module level."""

    def test_globals(self):
        self.assertEqual("2.0", frostlake.apilevel)
        self.assertEqual(1, frostlake.threadsafety)
        self.assertEqual("qmark", frostlake.paramstyle)

    def test_version(self):
        # pyproject reads the package version from this attribute, so it has to stay.
        self.assertRegex(frostlake.__version__, r"^\d+\.\d+")

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(frostlake.Warning, Exception))
        self.assertTrue(issubclass(frostlake.Error, Exception))
        self.assertTrue(issubclass(frostlake.InterfaceError, frostlake.Error))
        self.assertTrue(issubclass(frostlake.DatabaseError, frostlake.Error))
        for name in ("DataError", "OperationalError", "IntegrityError",
                     "InternalError", "ProgrammingError", "NotSupportedError"):
            self.assertTrue(issubclass(getattr(frostlake, name), frostlake.DatabaseError),
                            name + " must derive from DatabaseError")

    def test_connection_carries_the_exception_classes(self):
        for name in ("Warning", "Error", "InterfaceError", "DatabaseError", "DataError",
                     "OperationalError", "IntegrityError", "InternalError",
                     "ProgrammingError", "NotSupportedError"):
            self.assertIs(getattr(frostlake.Connection, name), getattr(frostlake, name))


class TypeConstructorTest(unittest.TestCase):

    def test_date_time_timestamp(self):
        self.assertEqual(datetime.date(2024, 1, 5), frostlake.Date(2024, 1, 5))
        self.assertEqual(datetime.time(10, 20, 30), frostlake.Time(10, 20, 30))
        self.assertEqual(datetime.datetime(2024, 1, 5, 10, 20, 30),
                         frostlake.Timestamp(2024, 1, 5, 10, 20, 30))

    def test_from_ticks_agree_with_localtime(self):
        import time
        ticks = 1_700_000_000
        parts = time.localtime(ticks)
        self.assertEqual(datetime.date(*parts[:3]), frostlake.DateFromTicks(ticks))
        self.assertEqual(datetime.time(*parts[3:6]), frostlake.TimeFromTicks(ticks))
        self.assertEqual(datetime.datetime(*parts[:6]), frostlake.TimestampFromTicks(ticks))

    def test_binary(self):
        self.assertEqual(b"hi", frostlake.Binary("hi"))
        self.assertEqual(b"\x00\x01", frostlake.Binary(b"\x00\x01"))
        self.assertEqual(b"\x01\x02", frostlake.Binary(bytearray([1, 2])))


class TypeObjectTest(unittest.TestCase):

    def test_matches_engine_type_names(self):
        self.assertEqual(frostlake.NUMBER, "NUMBER")
        self.assertEqual(frostlake.NUMBER, "FLOAT")
        self.assertEqual(frostlake.STRING, "VARCHAR")
        self.assertEqual(frostlake.BINARY, "BINARY")
        self.assertEqual(frostlake.DATETIME, "DATE")
        self.assertEqual(frostlake.DATETIME, "TIMESTAMP_TZ")

    def test_ignores_column_parameters(self):
        self.assertEqual(frostlake.NUMBER, "NUMBER(38,10)")
        self.assertEqual(frostlake.STRING, "VARCHAR(16777216)")
        self.assertEqual(frostlake.DATETIME, "TIME(9)")

    def test_comparison_is_symmetric(self):
        # A description's type_code is a plain str, so it sits on the left in real code.
        self.assertTrue("NUMBER" == frostlake.NUMBER)
        self.assertTrue(frostlake.NUMBER == "NUMBER")

    def test_families_do_not_overlap(self):
        self.assertNotEqual(frostlake.NUMBER, "VARCHAR")
        self.assertNotEqual(frostlake.STRING, "NUMBER")
        self.assertNotEqual(frostlake.DATETIME, "VARCHAR")
        self.assertNotEqual(frostlake.NUMBER, 17)

    def test_rowid_matches_nothing(self):
        # The engine has no rowid; the name exists only because the spec requires it.
        self.assertNotEqual(frostlake.ROWID, "NUMBER")
        self.assertNotEqual(frostlake.ROWID, "VARCHAR")

    def test_usable_in_sets(self):
        self.assertEqual(5, len({frostlake.STRING, frostlake.BINARY, frostlake.NUMBER,
                                 frostlake.DATETIME, frostlake.ROWID}))


class BaseTypeNameTest(unittest.TestCase):

    def test_strips_parameters_and_normalizes(self):
        self.assertEqual("NUMBER", frostlake._base_type_name("NUMBER(38,10)"))
        self.assertEqual("VARCHAR", frostlake._base_type_name("varchar(100)"))
        self.assertEqual("TIMESTAMP_NTZ", frostlake._base_type_name(" timestamp_ntz "))
        self.assertEqual("", frostlake._base_type_name(None))


class ConvertTest(unittest.TestCase):

    def test_fixed_point_keeps_exact_digits(self):
        value = decimal.Decimal("12345678901234567890.1234567890")
        self.assertEqual(value, frostlake._convert(value, "NUMBER", 10))
        self.assertIsInstance(frostlake._convert(value, "NUMBER", 10), decimal.Decimal)

    def test_scaled_number_is_not_rounded_through_float(self):
        cents = frostlake._convert(decimal.Decimal("1.10"), "NUMBER", 2)
        self.assertEqual(decimal.Decimal("3.30"), cents * 3)

    def test_zero_scale_becomes_int(self):
        self.assertEqual(42, frostlake._convert(decimal.Decimal("42"), "NUMBER", 0))
        self.assertIsInstance(frostlake._convert(decimal.Decimal("42"), "NUMBER", 0), int)
        self.assertEqual(7, frostlake._convert(7, "NUMBER", 0))

    def test_zero_scale_never_truncates_a_fraction(self):
        # Defensive: if scale metadata is missing, keep the value rather than floor it.
        self.assertEqual(decimal.Decimal("1.5"),
                         frostlake._convert(decimal.Decimal("1.5"), "NUMBER", 0))

    def test_approximate_types_stay_float(self):
        for name in ("FLOAT", "DOUBLE", "REAL", "FLOAT8"):
            converted = frostlake._convert(decimal.Decimal("0.1"), name, 0)
            self.assertIsInstance(converted, float, name + " must stay a float")

    def test_temporal_types(self):
        self.assertEqual(datetime.date(2024, 1, 5),
                         frostlake._convert("2024-01-05", "DATE"))
        self.assertEqual(datetime.time(10, 20, 30),
                         frostlake._convert("10:20:30", "TIME"))
        self.assertEqual(datetime.datetime(2024, 1, 5, 10, 20, 30),
                         frostlake._convert("2024-01-05 10:20:30", "TIMESTAMP_NTZ"))

    def test_datetime_alias_is_a_timestamp_not_a_date(self):
        # DATETIME aliases TIMESTAMP_NTZ, so it must not be routed down the DATE branch.
        self.assertEqual(datetime.datetime(2024, 1, 5, 10, 20, 30),
                         frostlake._convert("2024-01-05 10:20:30", "DATETIME"))

    def test_offset_and_nanosecond_handling(self):
        converted = frostlake._convert("2024-01-05 10:20:30.123456789 +0100",
                                       "TIMESTAMP_TZ")
        self.assertEqual(2024, converted.year)
        self.assertEqual(123456, converted.microsecond)
        self.assertIsNotNone(converted.tzinfo)

    def test_passthrough_values(self):
        self.assertIsNone(frostlake._convert(None, "NUMBER", 0))
        self.assertIs(True, frostlake._convert(True, "BOOLEAN"))
        self.assertEqual("x", frostlake._convert("x", "VARCHAR"))
        # Semi-structured cells arrive as JSON text and are handed back untouched.
        self.assertEqual('{"a":1.5}', frostlake._convert('{"a":1.5}', "VARIANT"))


class DmlStatusTest(unittest.TestCase):
    """DML reports counters instead of data, and the shape varies by statement."""

    INSERT = [{"name": "number of rows inserted"}]
    DELETE = [{"name": "number of rows deleted"}]
    UPDATE = [{"name": "number of rows updated"},
              {"name": "number of multi-joined rows updated"}]
    MERGE = [{"name": "number of rows inserted"},
             {"name": "number of rows updated"}]

    def test_recognises_every_dml_shape(self):
        for columns in (self.INSERT, self.DELETE, self.UPDATE, self.MERGE):
            self.assertTrue(frostlake._is_dml_status(columns), columns)

    def test_data_result_sets_are_not_mistaken_for_status(self):
        self.assertFalse(frostlake._is_dml_status([{"name": "N"}]))
        self.assertFalse(frostlake._is_dml_status([]))
        self.assertFalse(frostlake._is_dml_status(
            [{"name": "number of rows inserted"}, {"name": "TOTAL"}]))

    def test_single_counter_statements(self):
        self.assertEqual(4, frostlake._dml_rowcount(self.INSERT, [4]))
        self.assertEqual(2, frostlake._dml_rowcount(self.DELETE, [2]))

    def test_update_ignores_the_multi_joined_diagnostic(self):
        # The second counter is a subset of the rows already counted, not extra ones.
        self.assertEqual(2, frostlake._dml_rowcount(self.UPDATE, [2, 0]))
        self.assertEqual(3, frostlake._dml_rowcount(self.UPDATE, [3, 3]))

    def test_merge_sums_its_counters(self):
        self.assertEqual(1, frostlake._dml_rowcount(self.MERGE, [1, 0]))
        self.assertEqual(5, frostlake._dml_rowcount(self.MERGE, [2, 3]))

    def test_missing_counter_values_are_tolerated(self):
        self.assertEqual(0, frostlake._dml_rowcount(self.MERGE, [None, None]))
        self.assertEqual(1, frostlake._dml_rowcount(self.MERGE, [1]))


class JsonPrecisionTest(unittest.TestCase):

    def test_wire_digits_survive_parsing(self):
        parsed = frostlake._load_json('{"n": 12345678901234567890.1234567890}')
        self.assertEqual(decimal.Decimal("12345678901234567890.1234567890"), parsed["n"])

    def test_undecimal_restores_plain_floats(self):
        nested = {"a": decimal.Decimal("1.5"), "b": [decimal.Decimal("2.25"), 3]}
        restored = frostlake._undecimal(nested)
        self.assertEqual({"a": 1.5, "b": [2.25, 3]}, restored)
        self.assertIsInstance(restored["a"], float)
        self.assertIsInstance(restored["b"][0], float)


class ParameterBindingTest(unittest.TestCase):

    def test_literal_formatting(self):
        self.assertEqual("NULL", frostlake._format_literal(None))
        self.assertEqual("true", frostlake._format_literal(True))
        self.assertEqual("1", frostlake._format_literal(1))
        self.assertEqual("'a''b'", frostlake._format_literal("a'b"))
        self.assertEqual("X'0001'", frostlake._format_literal(b"\x00\x01"))
        self.assertEqual("123.4500", frostlake._format_literal(decimal.Decimal("123.4500")))
        self.assertEqual("'2024-01-05'::DATE",
                         frostlake._format_literal(datetime.date(2024, 1, 5)))

    def test_unsupported_parameter_type_is_reported(self):
        self.assertRaises(frostlake.ProgrammingError, frostlake._format_literal, object())

    def test_placeholders_outside_literals_only(self):
        self.assertEqual("SELECT 1", frostlake._substitute("SELECT ?", (1,)))
        self.assertEqual("SELECT 'a?b', 1", frostlake._substitute("SELECT 'a?b', ?", (1,)))
        self.assertEqual('SELECT "c?l", 1', frostlake._substitute('SELECT "c?l", ?', (1,)))
        self.assertEqual("SELECT 1 -- ?\n", frostlake._substitute("SELECT ? -- ?\n", (1,)))
        self.assertEqual("SELECT 1 /* ? */", frostlake._substitute("SELECT ? /* ? */", (1,)))

    def test_too_few_parameters_is_reported(self):
        self.assertRaises(frostlake.ProgrammingError,
                          frostlake._substitute, "SELECT ?, ?", (1,))


class TypeObjectIdentityTest(unittest.TestCase):

    def test_type_objects_compare_to_each_other(self):
        self.assertEqual(frostlake.NUMBER, frostlake.NUMBER)
        self.assertNotEqual(frostlake.NUMBER, frostlake.STRING)

    def test_repr_is_the_family_name(self):
        self.assertEqual("NUMBER", repr(frostlake.NUMBER))
        self.assertEqual("ROWID", repr(frostlake.ROWID))


class LiteralFormattingTest(unittest.TestCase):
    """Branches of _format_literal that the round-trip tests do not reach."""

    def test_false_is_lowercase(self):
        self.assertEqual("false", frostlake._format_literal(False))
        self.assertEqual("true", frostlake._format_literal(True))

    def test_time_literal(self):
        self.assertEqual("'10:20:30'::TIME",
                         frostlake._format_literal(datetime.time(10, 20, 30)))

    def test_sequences_become_array_literals(self):
        self.assertEqual("[1, 2, 3]", frostlake._format_literal([1, 2, 3]))
        self.assertEqual("['a', 'b']", frostlake._format_literal(("a", "b")))
        self.assertEqual("[]", frostlake._format_literal([]))

    def test_nested_sequences(self):
        self.assertEqual("[1, ['x', NULL]]", frostlake._format_literal([1, ["x", None]]))

    def test_float_and_decimal_keep_their_own_spelling(self):
        self.assertEqual("0.5", frostlake._format_literal(0.5))
        self.assertEqual("1.50", frostlake._format_literal(decimal.Decimal("1.50")))


class SqlScannerTest(unittest.TestCase):
    """_substitute has to know where a '?' is data rather than a placeholder."""

    def test_double_slash_comments_are_skipped(self):
        self.assertEqual("SELECT 1 // ?\n", frostlake._substitute("SELECT ? // ?\n", (1,)))

    def test_unterminated_block_comment_swallows_the_rest(self):
        self.assertEqual("SELECT 1 /* ?", frostlake._substitute("SELECT ? /* ?", (1,)))

    def test_backslash_escapes_inside_string_literals(self):
        # The backslash escapes the following quote, so the literal runs on.
        self.assertEqual(r"SELECT 'a\'?b', 1", frostlake._substitute(r"SELECT 'a\'?b', ?", (1,)))

    def test_doubled_quote_inside_string_literals(self):
        self.assertEqual("SELECT 'it''s ? here', 1",
                         frostlake._substitute("SELECT 'it''s ? here', ?", (1,)))

    def test_doubled_quote_inside_identifiers(self):
        self.assertEqual('SELECT "a""?b", 1', frostlake._substitute('SELECT "a""?b", ?', (1,)))

    def test_unterminated_string_is_left_alone(self):
        self.assertEqual("SELECT 'unclosed ?", frostlake._substitute("SELECT 'unclosed ?", ()))

    def test_unterminated_identifier_is_left_alone(self):
        self.assertEqual('SELECT "unclosed ?', frostlake._substitute('SELECT "unclosed ?', ()))

    def test_no_placeholders_leaves_sql_untouched(self):
        self.assertEqual("SELECT 1", frostlake._substitute("SELECT 1", (99,)))


class DollarQuotedTest(unittest.TestCase):
    """$$…$$ bodies hold procedure source: semicolons and '?' in there are data."""

    def test_placeholder_inside_a_body_is_not_bound(self):
        self.assertEqual("SELECT $$a ? b$$, 'x'",
                         frostlake._substitute("SELECT $$a ? b$$, ?", ("x",)))

    def test_a_whole_procedure_body_survives(self):
        sql = ("CREATE PROCEDURE p(a INTEGER) RETURNS INTEGER LANGUAGE SQL "
               "AS $$ BEGIN LET x := 1; RETURN a; END; $$ -- ?")
        self.assertEqual(sql, frostlake._substitute(sql, ()))

    def test_unterminated_body_swallows_the_rest(self):
        self.assertEqual("SELECT $$ ? unterminated",
                         frostlake._substitute("SELECT $$ ? unterminated", ()))

    def test_skip_helper_spans_the_block(self):
        sql = "a$$body$$b"
        self.assertEqual(len("a$$body$$"), frostlake._skip_dollar_quoted(sql, 1))
        self.assertEqual(len("a$$open"), frostlake._skip_dollar_quoted("a$$open", 1))

    def test_a_lone_dollar_is_ordinary(self):
        self.assertEqual("SELECT $col, 1", frostlake._substitute("SELECT $col, ?", (1,)))


class QuoteIdentifierTest(unittest.TestCase):

    def test_plain_uppercase_names_are_left_bare(self):
        self.assertEqual("MY_DB", frostlake._quote_ident("MY_DB"))
        self.assertEqual("T1$X", frostlake._quote_ident("T1$X"))

    def test_lowercase_and_spaces_are_quoted(self):
        self.assertEqual('"my_db"', frostlake._quote_ident("my_db"))
        self.assertEqual('"has space"', frostlake._quote_ident("has space"))

    def test_embedded_quotes_are_doubled(self):
        # Without doubling, the identifier would end early and the remainder would be
        # parsed as SQL — an injection point for a database name taken from a DSN.
        self.assertEqual('"a""b"', frostlake._quote_ident('a"b'))
        self.assertEqual('"x""; DROP TABLE t; --"',
                         frostlake._quote_ident('x"; DROP TABLE t; --'))

    def test_empty_names_are_quoted_not_dropped(self):
        self.assertEqual('""', frostlake._quote_ident(""))


class BinaryConversionTest(unittest.TestCase):

    def test_hex_becomes_bytes(self):
        self.assertEqual(b"\x01\x02\xff", frostlake._convert("0102FF", "BINARY", 0))
        self.assertEqual(b"\x01", frostlake._convert("01", "VARBINARY", 0))

    def test_non_hex_is_left_as_text(self):
        self.assertEqual("not hex", frostlake._convert("not hex", "BINARY", 0))


class ConvertFallbackTest(unittest.TestCase):

    def test_float_passes_through(self):
        self.assertEqual(0.25, frostlake._convert(0.25, "FLOAT", 0))
        self.assertIsInstance(frostlake._convert(0.25, "FLOAT", 0), float)

    def test_parsed_structures_lose_their_decimals(self):
        cell = {"a": [decimal.Decimal("1.5")], "b": {"c": decimal.Decimal("2")}}
        converted = frostlake._convert(cell, "OBJECT", 0)
        self.assertEqual({"a": [1.5], "b": {"c": 2.0}}, converted)
        self.assertIsInstance(converted["a"][0], float)

    def test_unknown_type_names_pass_strings_through(self):
        self.assertEqual("raw", frostlake._convert("raw", "GEOGRAPHY", 0))


class AutocommitAttributeTest(unittest.TestCase):

    def test_setting_the_current_value_changes_nothing(self):
        # No statement should be sent, so this works without a server at all.
        conn = frostlake.connect("frostlake://localhost:18082/db")
        self.assertTrue(conn.autocommit)
        conn.autocommit = True
        self.assertTrue(conn.autocommit)
        self.assertFalse(conn._begin_pending)

    def test_turning_it_off_arms_a_transaction(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        conn.autocommit = False
        self.assertFalse(conn.autocommit)
        self.assertTrue(conn._begin_pending)   # BEGIN goes out before the next statement
        conn.autocommit = False                # again: still just armed, nothing sent
        self.assertTrue(conn._begin_pending)


class CursorNoOpTest(unittest.TestCase):

    def test_size_hints_are_accepted_and_ignored(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        cur = conn.cursor()
        self.assertIsNone(cur.setinputsizes([10, 20]))
        self.assertIsNone(cur.setoutputsize(100))
        self.assertIsNone(cur.setoutputsize(100, 1))


class ConnectKeywordTest(unittest.TestCase):
    """connect() also takes the parts separately, without a DSN."""

    def test_host_and_port(self):
        conn = frostlake.connect(host="example.test", port=9999)
        self.assertEqual("http://example.test:9999", conn._base_url)

    def test_port_defaults(self):
        conn = frostlake.connect(host="example.test")
        self.assertEqual("http://example.test:18082", conn._base_url)

    def test_database_and_schema_keywords(self):
        conn = frostlake.connect(host="example.test", database="D", schema="S")
        self.assertEqual(["USE DATABASE D", "USE SCHEMA S"], conn._pending_use)

    def test_explicit_arguments_win_over_the_dsn(self):
        conn = frostlake.connect("frostlake://example.test:1/FROM_DSN?schema=FROM_DSN",
                                 database="OVERRIDE", schema="ALSO")
        self.assertEqual(["USE DATABASE OVERRIDE", "USE SCHEMA ALSO"], conn._pending_use)

    def test_http_scheme_is_accepted(self):
        conn = frostlake.connect("http://example.test:8080/DB")
        self.assertEqual("http://example.test:8080", conn._base_url)


class UnreachableServerTest(unittest.TestCase):

    def test_connection_refused_becomes_operational_error(self):
        # Port 1 is not going to be listening; the socket error must surface as a
        # DB-API OperationalError rather than a raw OSError.
        conn = frostlake.connect(host="127.0.0.1", port=1, timeout=2)
        cur = conn.cursor()
        self.assertRaises(frostlake.OperationalError, cur.execute, "SELECT 1")


class NonJsonErrorResponseTest(unittest.TestCase):
    """Something that is not a Frostlake server, answering on the same shape of URL."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"<html><body>502 Bad Gateway</body></html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_unparseable_error_body_becomes_operational_error(self):
        # A proxy or the wrong port answers with HTML, not the engine's JSON envelope:
        # the status has to surface as OperationalError rather than a JSON decode error.
        port = self.server.server_address[1]
        conn = frostlake.connect(host="127.0.0.1", port=port, timeout=5)
        cur = conn.cursor()
        self.assertRaises(frostlake.OperationalError, cur.execute, "SELECT 1")


class ContextManagerFailureTest(unittest.TestCase):

    def test_rollback_failure_does_not_mask_the_original_error(self):
        # Leaving the block with an exception rolls back; if the rollback itself fails
        # (here, the connection is already closed) the original exception must win.
        class Boom(Exception):
            pass

        conn = frostlake.connect("frostlake://localhost:18082/db")
        with self.assertRaises(Boom):
            with conn:
                conn.close()
                raise Boom()
        self.assertTrue(conn._closed)


class DsnTest(unittest.TestCase):

    def test_rejects_unusable_dsn(self):
        self.assertRaises(frostlake.InterfaceError, frostlake.connect, "mysql://host:1/db")
        self.assertRaises(frostlake.InterfaceError, frostlake.connect, "frostlake://")
        self.assertRaises(frostlake.InterfaceError, frostlake.connect)

    def test_dsn_parts_become_use_statements(self):
        # No connection is opened until the first statement, so this stays server-free.
        conn = frostlake.connect("frostlake://localhost:18082/MY_DB?schema=PUBLIC")
        self.assertEqual(["USE DATABASE MY_DB", "USE SCHEMA PUBLIC"], conn._pending_use)

    def test_quoted_identifiers_for_lowercase_names(self):
        conn = frostlake.connect("frostlake://localhost:18082/my_db")
        self.assertEqual(['USE DATABASE "my_db"'], conn._pending_use)


class ClosedStateTest(unittest.TestCase):

    def test_closed_connection_refuses_work(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        conn.close()
        self.assertRaises(frostlake.InterfaceError, conn.cursor)
        self.assertRaises(frostlake.InterfaceError, conn.commit)
        self.assertRaises(frostlake.InterfaceError, conn.rollback)

    def test_closed_cursor_refuses_work(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        cur = conn.cursor()
        cur.close()
        self.assertRaises(frostlake.InterfaceError, cur.execute, "SELECT 1")
        self.assertRaises(frostlake.InterfaceError, cur.fetchone)
        self.assertRaises(frostlake.InterfaceError, cur.fetchmany)
        self.assertRaises(frostlake.InterfaceError, cur.fetchall)
        self.assertRaises(frostlake.InterfaceError, cur.nextset)
        cur.close()  # idempotent

    def test_fetch_before_execute_is_reported(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        cur = conn.cursor()
        self.assertRaises(frostlake.ProgrammingError, cur.fetchone)
        self.assertRaises(frostlake.ProgrammingError, cur.fetchall)
        self.assertRaises(frostlake.ProgrammingError, cur.nextset)

    def test_lastrowid_is_none(self):
        conn = frostlake.connect("frostlake://localhost:18082/db")
        self.assertIsNone(conn.cursor().lastrowid)


if __name__ == "__main__":
    unittest.main()
