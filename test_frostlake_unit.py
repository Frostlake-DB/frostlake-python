"""Unit tests for the driver's PEP 249 surface and value handling.

These need no server and no JVM, so they run on a bare checkout:

    python3 -m unittest test_frostlake_unit -v
"""

import collections
import datetime
import decimal
import http.server
import json
import threading
import time
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

    def test_plain_names_are_left_bare_in_any_case(self):
        # Bare, a name folds the way SQL folds it: my_db selects MY_DB.
        self.assertEqual("MY_DB", frostlake._quote_ident("MY_DB"))
        self.assertEqual("T1$X", frostlake._quote_ident("T1$X"))
        self.assertEqual("my_db", frostlake._quote_ident("my_db"))
        self.assertEqual("Mixed_Case", frostlake._quote_ident("Mixed_Case"))

    def test_names_that_cannot_be_written_bare_are_quoted(self):
        self.assertEqual('"has space"', frostlake._quote_ident("has space"))
        self.assertEqual('"1ABC"', frostlake._quote_ident("1ABC"))
        self.assertEqual('"my-db"', frostlake._quote_ident("my-db"))

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


class DescriptionSizeTest(unittest.TestCase):
    """display_size/internal_size come from the wire's `length`, when it sends one."""

    def cursor_for(self, columns):
        # No connection is opened: the cursor is handed a server answer directly.
        conn = frostlake.connect("frostlake://localhost:18082/db")
        cur = conn.cursor()
        cur._load({"resultSets": [{"columns": columns, "rows": []}]})
        return cur

    def test_text_and_binary_lengths_are_reported(self):
        cur = self.cursor_for([
            {"name": "A", "dataType": "VARCHAR", "length": 20, "nullable": True},
            {"name": "C", "dataType": "BINARY", "length": 10, "nullable": True},
        ])
        self.assertEqual([20, 10], [d[3] for d in cur.description])
        # display_size is None even where a length is known: the account's own client
        # reports no display width, because the server sends none.
        self.assertEqual([None, None], [d[2] for d in cur.description])

    def test_a_column_without_a_length_reports_none(self):
        # Non-text types omit the field, and so does a server predating it. PEP 249
        # reads a missing size as None, which is not the same as 0.
        cur = self.cursor_for([
            {"name": "E", "dataType": "NUMBER", "precision": 12, "scale": 2, "nullable": True},
            {"name": "F", "dataType": "DATE", "nullable": True},
        ])
        self.assertEqual([None, None], [d[3] for d in cur.description])
        self.assertEqual([None, None], [d[2] for d in cur.description])


class DsnTest(unittest.TestCase):

    def test_rejects_unusable_dsn(self):
        self.assertRaises(frostlake.InterfaceError, frostlake.connect, "mysql://host:1/db")
        self.assertRaises(frostlake.InterfaceError, frostlake.connect, "frostlake://")
        self.assertRaises(frostlake.InterfaceError, frostlake.connect)

    def test_dsn_parts_become_use_statements(self):
        # No connection is opened until the first statement, so this stays server-free.
        conn = frostlake.connect("frostlake://localhost:18082/MY_DB?schema=PUBLIC")
        self.assertEqual(["USE DATABASE MY_DB", "USE SCHEMA PUBLIC"], conn._pending_use)

    def test_lowercase_dsn_names_are_sent_bare_so_they_fold(self):
        conn = frostlake.connect("frostlake://localhost:18082/my_db")
        self.assertEqual(["USE DATABASE my_db"], conn._pending_use)

    def test_a_quoted_name_is_passed_through_so_it_matches_exactly(self):
        # Quotes are how a caller asks for an object whose real name is not upper
        # case; doubling them here would ask for a name made of quote characters.
        conn = frostlake.connect(host="h", database='"my_db"', schema='"s"')
        self.assertEqual(['USE DATABASE "my_db"', 'USE SCHEMA "s"'], conn._pending_use)
        self.assertEqual('"my db"', frostlake._quote_ident('"my db"'))
        self.assertEqual('"a""b"', frostlake._quote_ident('"a""b"'))

    def test_a_name_that_only_looks_quoted_is_still_escaped(self):
        # Anything that is not a well-formed quoted identifier is escaped, so a stray
        # quote can never end the identifier early and let the rest through as SQL.
        self.assertEqual('"""a"', frostlake._quote_ident('"a'))
        self.assertEqual('"""a""b"""', frostlake._quote_ident('"a"b"'))
        self.assertEqual('"a""; DROP DATABASE x; --"',
                         frostlake._quote_ident('a"; DROP DATABASE x; --'))


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


class MultiStatementCountWireTest(unittest.TestCase):
    """What `num_statements` puts on the wire, and what it leaves alone."""

    BODIES = []

    @classmethod
    def setUpClass(cls):
        bodies = cls.BODIES

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = b'{"success": true, "sessionId": "s1", "resultSets": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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

    def setUp(self):
        del self.BODIES[:]
        self.conn = frostlake.connect(host="127.0.0.1",
                                      port=self.server.server_address[1], timeout=5)
        self.cur = self.conn.cursor()

    def test_asking_for_nothing_sends_no_field(self):
        self.cur.execute("SELECT 1")
        self.assertNotIn("multiStatementCount", self.BODIES[-1])

    def test_a_count_rides_on_the_request(self):
        self.cur.execute("SELECT 1; SELECT 2;", num_statements=2)
        self.assertEqual(2, self.BODIES[-1]["multiStatementCount"])
        # The count went with the statement: nothing else was sent to move the session.
        self.assertEqual(["SELECT 1; SELECT 2;"], [b["sql"] for b in self.BODIES])

    def test_zero_is_an_answer_and_is_sent(self):
        self.cur.execute("SELECT 1; SELECT 2;", num_statements=0)
        self.assertEqual(0, self.BODIES[-1]["multiStatementCount"])

    def test_a_bad_count_is_refused_before_anything_is_sent(self):
        for bad in (-1, "2", 1.5, True):
            self.assertRaises(frostlake.ProgrammingError,
                              self.cur.execute, "SELECT 1", None, bad)
        self.assertEqual([], self.BODIES)


# -- session lifetime, against a scripted engine -------------------------------

class ScriptedEngine(object):
    """A stand-in engine on a local socket: it answers each request from a script and
    records what it was sent, so a scenario sees every round trip it makes. A request
    the script did not expect is recorded as such and answered with a body that is not
    an engine's, which the driver reports as an error."""

    def __init__(self):
        self.requests = []
        self.unscripted = []
        self._script = collections.deque()
        self._stop = threading.Event()
        engine = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                engine._handle(self)

            do_POST = do_GET
            do_DELETE = do_GET

            def log_message(self, fmt, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.port = self._server.server_address[1]
        self.dsn = "frostlake://127.0.0.1:%d" % self.port

    def reply(self, body, status=200):
        self._script.append(("reply", status, json.dumps(body)))

    def reply_text(self, text, status):
        """Answer with a body that is not JSON, the way a proxy in the way does."""
        self._script.append(("reply", status, text))

    def drop(self):
        """Close the socket without answering."""
        self._script.append(("drop", 0, ""))

    def hang(self):
        """Hold the request unanswered until the engine is stopped."""
        self._script.append(("hang", 0, ""))

    @property
    def pending(self):
        return len(self._script)

    def stop(self):
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        self.requests.append((handler.command, handler.path, body))
        if not self._script:
            self.unscripted.append((handler.command, handler.path, body))
            action = ("reply", 500, "<html>unscripted</html>")
        else:
            action = self._script.popleft()
        kind, status, text = action
        if kind == "hang":
            self._stop.wait(30)
        if kind != "reply":
            handler.close_connection = True
            return
        payload = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    # what the driver sent

    def executes(self):
        return [body for verb, path, body in self.requests if path == "/api/execute"]

    def statements(self):
        return [body["sql"] for body in self.executes()]


def status_set():
    return {"columns": [{"name": "status", "dataType": "VARCHAR", "precision": 0, "scale": 0,
                         "nullable": False}],
            "rows": [["Statement executed successfully."]], "rowCount": 1, "updateCount": -1}


def number_set(name, value):
    return {"columns": [{"name": name, "dataType": "NUMBER", "precision": 38, "scale": 0,
                         "nullable": False}],
            "rows": [[value]], "rowCount": 1, "updateCount": -1}


def answer(session_id, started, *sets):
    """An answer from an engine that reports newSession (0.1.0 and later)."""
    return {"success": True, "sessionId": session_id, "newSession": started,
            "errorMessage": None, "executionTimeMs": 1, "resultSets": list(sets)}


def legacy_answer(session_id, *sets):
    """An answer from an engine that predates newSession (0.0.7)."""
    return {"success": True, "sessionId": session_id, "errorMessage": None,
            "executionTimeMs": 1, "resultSets": list(sets)}


def gone(session_id):
    """The 404 a requireSession request gets when its session is gone: nothing ran."""
    return {"success": False, "sessionId": None, "newSession": False,
            "errorMessage": "Session '%s' does not exist or has expired." % session_id,
            "executionTimeMs": 0, "resultSets": []}


RELEASED = {"success": True, "sessionId": None, "newSession": False, "errorMessage": None,
            "executionTimeMs": 0, "resultSets": []}

SCOPE = ["USE DATABASE APP", "USE SCHEMA PUBLIC"]


class SessionLifetimeTest(unittest.TestCase):
    """How a connection keeps its idea of the engine session in step with the engine's."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def opened(self, legacy=False):
        """A connection on the DSN's scope that has run one statement: USE DATABASE and
        USE SCHEMA went first, the session is s1, and the engine's kind is known."""
        if legacy:
            for sets in ((status_set(),), (status_set(),), (number_set("N", 1),)):
                self.engine.reply(legacy_answer("s1", *sets))
        else:
            self.engine.reply(answer("s1", True, status_set()))
            self.engine.reply(answer("s1", False, status_set()))
            self.engine.reply(answer("s1", False, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn + "/APP?schema=PUBLIC", timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS N")
        return conn, cur

    def assert_all_answered(self):
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def assert_session_lost(self, call, *args):
        # Caught as the PEP 249 class first, so a driver that raises something else
        # shows what it raised.
        with self.assertRaises(frostlake.OperationalError) as caught:
            call(*args)
        self.assertIsInstance(caught.exception, frostlake.SessionLostError)
        return caught.exception

    def test_the_session_is_not_required_before_the_engine_has_said_it_can(self):
        conn, cur = self.opened()
        first, second, third = self.engine.executes()
        # The first request names no session, so it has nothing to require.
        self.assertNotIn("sessionId", first)
        self.assertNotIn("requireSession", first)
        # Its answer carried newSession, so from then on the session is required.
        self.assertEqual("s1", second["sessionId"])
        self.assertIs(True, second["requireSession"])
        self.assertIs(True, third["requireSession"])
        self.assertEqual(SCOPE + ["SELECT 1 AS N"], self.engine.statements())
        self.assertEqual([(1,)], cur.fetchall())

    def test_an_older_engine_is_never_sent_the_field(self):
        conn, cur = self.opened(legacy=True)
        self.engine.reply(legacy_answer("s1", number_set("N", 2)))
        cur.execute("SELECT 2 AS N")
        for body in self.engine.executes()[1:]:
            self.assertEqual("s1", body["sessionId"])
            self.assertNotIn("requireSession", body)
        conn.close()
        # No DELETE either: an engine that predates newSession has no such endpoint.
        self.assertEqual(["POST"] * 4, [verb for verb, _, _ in self.engine.requests])
        self.assert_all_answered()

    def test_an_answer_naming_no_session_settles_nothing(self):
        # A refusal of the request itself carries no session and says nothing about the
        # engine; the first answer that names a session does.
        self.engine.reply({"success": False, "sessionId": None,
                           "errorMessage": "SQL is required"}, status=400)
        self.engine.reply(answer("n1", True, number_set("N", 1)))
        self.engine.reply(answer("n1", False, number_set("N", 2)))
        conn = frostlake.connect(self.engine.dsn, timeout=5)
        cur = conn.cursor()
        self.assertRaises(frostlake.ProgrammingError, cur.execute, " ")
        cur.execute("SELECT 1 AS N")
        cur.execute("SELECT 2 AS N")
        bodies = self.engine.executes()
        self.assertNotIn("requireSession", bodies[1])
        self.assertIs(True, bodies[2]["requireSession"])
        self.assert_all_answered()

    def test_a_lost_session_is_replaced_on_the_scope_and_the_statement_sent_once_more(self):
        conn, cur = self.opened()
        self.engine.reply(gone("s1"), status=404)
        self.engine.reply(answer("s2", True, status_set()))
        self.engine.reply(answer("s2", False, status_set()))
        self.engine.reply(answer("s2", False, number_set("N", 2)))
        cur.execute("SELECT 2 AS N")
        self.assertEqual([(2,)], cur.fetchall())
        self.assertEqual(SCOPE + ["SELECT 1 AS N", "SELECT 2 AS N"] + SCOPE + ["SELECT 2 AS N"],
                         self.engine.statements())
        bodies = self.engine.executes()
        # The replacement starts without an id, and everything after it names the new one.
        self.assertNotIn("sessionId", bodies[4])
        self.assertEqual(["s2", "s2"], [bodies[5]["sessionId"], bodies[6]["sessionId"]])
        self.assertEqual("s2", conn._session_id)
        self.assert_all_answered()

    def test_a_second_404_is_reported_rather_than_retried(self):
        conn, cur = self.opened()
        self.engine.reply(gone("s1"), status=404)
        self.engine.reply(answer("s2", True, status_set()))
        self.engine.reply(answer("s2", False, status_set()))
        self.engine.reply(gone("s2"), status=404)
        self.assert_session_lost(cur.execute, "SELECT 2 AS N")
        self.assertEqual(2, self.engine.statements().count("SELECT 2 AS N"))
        self.assert_all_answered()

    def test_a_lost_session_with_an_open_transaction_is_reported_not_replaced(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        conn.begin()
        self.engine.reply(gone("s1"), status=404)
        error = self.assert_session_lost(cur.execute, "INSERT INTO T VALUES (1)")
        self.assertIn("transaction", str(error))
        self.assertEqual(SCOPE + ["SELECT 1 AS N", "BEGIN", "INSERT INTO T VALUES (1)"],
                         self.engine.statements())
        # The connection stays usable: the next statement starts a fresh session on the
        # scope, and with autocommit still off it opens a transaction there first.
        self.engine.reply(answer("s2", True, status_set()))
        self.engine.reply(answer("s2", False, status_set()))
        self.engine.reply(answer("s2", False, status_set()))
        self.engine.reply(answer("s2", False, number_set("N", 3)))
        cur.execute("SELECT 3 AS N")
        self.assertEqual(SCOPE + ["BEGIN", "SELECT 3 AS N"], self.engine.statements()[5:])
        self.assertNotIn("sessionId", self.engine.executes()[5])
        self.assert_all_answered()

    def test_a_commit_that_finds_the_session_gone_says_the_transaction_went_with_it(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
        conn.begin()
        cur.execute("INSERT INTO T VALUES (1)")
        self.engine.reply(gone("s1"), status=404)
        error = self.assert_session_lost(conn.commit)
        self.assertIn("transaction", str(error))
        # The COMMIT was not sent again on a fresh session, where it would have
        # "succeeded" with nothing to commit.
        self.assertEqual(1, self.engine.statements().count("COMMIT"))
        self.assert_all_answered()

    def test_a_transaction_opened_by_a_statement_is_tracked_too(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        cur.execute("BEGIN TRANSACTION")
        self.engine.reply(gone("s1"), status=404)
        error = self.assert_session_lost(cur.execute, "INSERT INTO T VALUES (1)")
        self.assertIn("transaction", str(error))
        self.assert_all_answered()

    def test_a_lost_session_whose_context_moved_is_reported_not_replaced(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        cur.execute("USE SCHEMA OTHER")
        self.engine.reply(gone("s1"), status=404)
        error = self.assert_session_lost(cur.execute, "SELECT * FROM T")
        self.assertIn("context", str(error))
        self.assertEqual(SCOPE + ["SELECT 1 AS N", "USE SCHEMA OTHER", "SELECT * FROM T"],
                         self.engine.statements())
        # Starting over on the scope puts the connection back where it began.
        self.engine.reply(answer("s2", True, status_set()))
        self.engine.reply(answer("s2", False, status_set()))
        self.engine.reply(answer("s2", False, number_set("N", 4)))
        cur.execute("SELECT 4 AS N")
        self.assertEqual(SCOPE + ["SELECT 4 AS N"], self.engine.statements()[5:])
        self.assert_all_answered()

    def test_session_state_is_noticed_anywhere_in_a_request(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, number_set("N", 1), status_set()))
        cur.execute("SELECT 1 AS N; SET v = 1", num_statements=2)
        self.engine.reply(gone("s1"), status=404)
        self.assert_session_lost(cur.execute, "SELECT $v")
        self.assert_all_answered()

    def test_a_server_that_replaced_the_session_gets_the_scope_back_first(self):
        # An engine that ran a statement in a fresh session in place of ours says so;
        # the scope goes back on before the next statement.
        conn, cur = self.opened()
        self.engine.reply(answer("s1", True, number_set("N", 2)))
        cur.execute("SELECT 2 AS N")
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("N", 3)))
        cur.execute("SELECT 3 AS N")
        self.assertEqual(SCOPE + ["SELECT 3 AS N"], self.engine.statements()[4:])
        self.assert_all_answered()

    def test_closing_releases_the_session_once(self):
        conn, cur = self.opened()
        self.engine.reply(RELEASED)
        conn.close()
        verb, path, body = self.engine.requests[-1]
        self.assertEqual(("DELETE", "/api/sessions/s1", None), (verb, path, body))
        count = len(self.engine.requests)
        conn.close()
        self.assertEqual(count, len(self.engine.requests))
        self.assertRaises(frostlake.InterfaceError, cur.execute, "SELECT 1")
        self.assert_all_answered()

    def test_closing_a_connection_that_never_ran_anything_sends_nothing(self):
        conn = frostlake.connect(self.engine.dsn + "/APP", timeout=5)
        conn.close()
        self.assertEqual([], self.engine.requests)

    def test_closing_with_a_transaction_open_leaves_the_rollback_to_the_release(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        conn.begin()
        self.engine.reply(RELEASED)
        conn.close()
        self.assertEqual("DELETE", self.engine.requests[-1][0])
        self.assertNotIn("ROLLBACK", self.engine.statements())
        self.assert_all_answered()

    def test_closing_on_an_older_engine_rolls_back_an_open_transaction_instead(self):
        conn, cur = self.opened(legacy=True)
        self.engine.reply(legacy_answer("s1", status_set()))
        conn.begin()
        self.engine.reply(legacy_answer("s1", status_set()))
        conn.close()
        self.assertEqual("ROLLBACK", self.engine.statements()[-1])
        self.assertEqual(["POST"] * 5, [verb for verb, _, _ in self.engine.requests])
        self.assert_all_answered()

    def test_closing_never_raises(self):
        refusals = (
            lambda: self.engine.reply(gone("s1"), status=404),
            lambda: self.engine.reply({"error": "Method not allowed"}, status=405),
            self.engine.drop,
        )
        for script in refusals:
            conn, cur = self.opened()
            script()
            conn.close()
            self.assertEqual("DELETE", self.engine.requests[-1][0])
            self.assert_all_answered()
            del self.engine.requests[:]

    def test_closing_is_bounded_when_the_engine_does_not_answer(self):
        self.engine.reply(answer("s1", True, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn, timeout=0.5)
        conn.cursor().execute("SELECT 1 AS N")
        self.engine.hang()
        started = time.monotonic()
        conn.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual("DELETE", self.engine.requests[-1][0])

    def test_closing_after_the_engine_went_away_does_not_raise(self):
        conn, cur = self.opened()
        self.engine.stop()
        conn.close()

    def test_a_scope_statement_the_engine_refuses_names_itself(self):
        self.engine.reply(answer("s1", True, status_set()))
        self.engine.reply({"success": False, "sessionId": "s1", "newSession": False,
                           "errorMessage": "Schema 'NOPE' does not exist or not authorized.",
                           "resultSets": []})
        conn = frostlake.connect(self.engine.dsn + "/APP?schema=NOPE", timeout=5)
        with self.assertRaises(frostlake.ProgrammingError) as caught:
            conn.cursor().execute("SELECT 1")
        self.assertEqual("USE SCHEMA NOPE", caught.exception.statement)
        self.assertEqual(["USE DATABASE APP", "USE SCHEMA NOPE"], self.engine.statements())
        self.assert_all_answered()


def refused(session_id, message, started=False):
    """A statement the engine ran and refused: it names the session it ran in."""
    return {"success": False, "sessionId": session_id, "newSession": started,
            "errorMessage": message, "executionTimeMs": 0, "resultSets": []}


NO_SUCH_OBJECT = "SQL compilation error:\nObject does not exist, or operation cannot be performed."


class RefusedScopeTest(unittest.TestCase):
    """A USE of the DSN's that the engine refuses stays in line, so nothing runs past it
    in the server's default database."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def assert_all_answered(self):
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def test_a_refused_use_keeps_failing_instead_of_falling_through(self):
        self.engine.reply(refused("s1", NO_SUCH_OBJECT, started=True))
        self.engine.reply(refused("s1", NO_SUCH_OBJECT))
        conn = frostlake.connect(self.engine.dsn + "/NOPE?schema=PUBLIC", timeout=5)
        cur = conn.cursor()
        with self.assertRaises(frostlake.ProgrammingError) as first:
            cur.execute("SELECT 1")
        with self.assertRaises(frostlake.ProgrammingError) as second:
            cur.execute("SELECT CURRENT_DATABASE()")
        # Neither statement reached the engine: each met the refused USE first.
        self.assertEqual(["USE DATABASE NOPE", "USE DATABASE NOPE"], self.engine.statements())
        for caught in (first, second):
            self.assertIn("does not exist", str(caught.exception))
            self.assertEqual("USE DATABASE NOPE", caught.exception.statement)
        # Once the engine takes it (the database was created meanwhile), the rest of the
        # scope follows, and then the statement.
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("N", 1)))
        cur.execute("SELECT 1 AS N")
        self.assertEqual([(1,)], cur.fetchall())
        self.assertEqual(["USE DATABASE NOPE"] * 3 + ["USE SCHEMA PUBLIC", "SELECT 1 AS N"],
                         self.engine.statements())
        self.assert_all_answered()

    def test_the_scope_picks_up_at_the_refused_statement(self):
        self.engine.reply(answer("s1", True, status_set()))
        self.engine.reply(refused("s1", NO_SUCH_OBJECT))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn + "/APP?schema=NOPE", timeout=5)
        cur = conn.cursor()
        self.assertRaises(frostlake.ProgrammingError, cur.execute, "SELECT 1 AS N")
        cur.execute("SELECT 1 AS N")
        # USE DATABASE took and is not sent again; the refused USE SCHEMA is.
        self.assertEqual(["USE DATABASE APP", "USE SCHEMA NOPE", "USE SCHEMA NOPE",
                          "SELECT 1 AS N"], self.engine.statements())
        self.assert_all_answered()

    def test_a_session_replaced_while_the_scope_goes_on_gets_all_of_it(self):
        # An older engine's answer, then one saying the engine ran USE SCHEMA in a fresh
        # session in place of ours: the fresh one gets the whole scope, database first.
        self.engine.reply(legacy_answer("s1", status_set()))
        self.engine.reply(answer("s1", True, status_set()))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn + "/APP?schema=PUBLIC", timeout=5)
        conn.cursor().execute("SELECT 1 AS N")
        self.assertEqual(SCOPE + SCOPE + ["SELECT 1 AS N"], self.engine.statements())
        self.assert_all_answered()

    def test_begin_opens_no_transaction_past_a_refused_use(self):
        self.engine.reply(refused("s1", NO_SUCH_OBJECT, started=True))
        self.engine.reply(refused("s1", NO_SUCH_OBJECT))
        conn = frostlake.connect(self.engine.dsn + "/NOPE", timeout=5)
        self.assertRaises(frostlake.ProgrammingError, conn.begin)
        conn.autocommit = False
        self.assertRaises(frostlake.ProgrammingError,
                          conn.cursor().execute, "INSERT INTO T VALUES (1)")
        # Neither BEGIN nor the INSERT went out ahead of the scope.
        self.assertEqual(["USE DATABASE NOPE", "USE DATABASE NOPE"], self.engine.statements())
        self.assert_all_answered()


class FailedBeginTest(unittest.TestCase):
    """A BEGIN that did not take changes nothing: autocommit stays as it was, and a BEGIN
    still owed goes out again before the next statement."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def assert_all_answered(self):
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def opened(self, dsn=""):
        self.engine.reply(answer("s1", True, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn + dsn, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS N")
        return conn, cur

    def bodies_of(self, sql):
        return [body for body in self.engine.executes() if body["sql"] == sql]

    def assert_begin_left_autocommit_on(self, conn, cur):
        self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
        cur.execute("INSERT INTO T VALUES (1)")
        # The row commits on its own, so commit() has nothing left to send.
        self.assertIs(True, self.bodies_of("INSERT INTO T VALUES (1)")[-1]["autoCommit"],
                      "the INSERT went out inside a transaction nobody opened")
        self.assertTrue(conn.autocommit)
        conn.commit()
        self.assertNotIn("COMMIT", self.engine.statements())
        for body in self.bodies_of("BEGIN"):
            self.assertIs(False, body["autoCommit"])
        self.assert_all_answered()

    def test_commit_after_a_refused_begin_leaves_no_work_uncommitted(self):
        conn, cur = self.opened()
        self.engine.reply(refused("s1", "SQL compilation error:\nno transactions today"))
        self.assertRaises(frostlake.ProgrammingError, conn.begin)
        self.assert_begin_left_autocommit_on(conn, cur)

    def test_an_unreadable_answer_to_begin_leaves_autocommit_on(self):
        conn, cur = self.opened()
        self.engine.reply_text("<html><body>502 Bad Gateway</body></html>", 502)
        self.assertRaises(frostlake.OperationalError, conn.begin)
        self.assert_begin_left_autocommit_on(conn, cur)

    def test_a_hang_up_on_begin_leaves_autocommit_on(self):
        conn, cur = self.opened()
        self.engine.drop()
        self.assertRaises(frostlake.OperationalError, conn.begin)
        self.assert_begin_left_autocommit_on(conn, cur)

    def test_a_refused_use_ahead_of_begin_leaves_autocommit_on(self):
        self.engine.reply(refused("s1", NO_SUCH_OBJECT, started=True))
        conn = frostlake.connect(self.engine.dsn + "/NOPE", timeout=5)
        cur = conn.cursor()
        self.assertRaises(frostlake.ProgrammingError, conn.begin)
        self.assertTrue(conn.autocommit)
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
        cur.execute("INSERT INTO T VALUES (1)")
        self.assertEqual(["USE DATABASE NOPE", "USE DATABASE NOPE", "INSERT INTO T VALUES (1)"],
                         self.engine.statements())
        self.assertEqual([True, True], [b["autoCommit"] for b in self.engine.executes()[1:]])
        self.assert_all_answered()

    def test_a_begin_whose_lost_session_held_context_leaves_autocommit_on(self):
        conn, cur = self.opened()
        self.engine.reply(answer("s1", False, status_set()))
        cur.execute("SET marker = 1")
        self.engine.reply(gone("s1"), status=404)
        self.assertRaises(frostlake.SessionLostError, conn.begin)
        self.assertTrue(conn.autocommit)
        # The next statement starts a fresh session with no transaction on it.
        self.engine.reply(answer("s2", True, number_set("number of rows inserted", 1)))
        cur.execute("INSERT INTO T VALUES (1)")
        self.assertEqual(["SELECT 1 AS N", "SET marker = 1", "BEGIN", "INSERT INTO T VALUES (1)"],
                         self.engine.statements())
        self.assertIs(True, self.engine.executes()[-1]["autoCommit"])
        self.assert_all_answered()

    def test_a_pending_begin_that_failed_goes_out_again_first(self):
        conn, cur = self.opened()
        conn.autocommit = False
        self.engine.reply(refused("s1", "SQL compilation error:\nno transactions today"))
        self.assertRaises(frostlake.ProgrammingError, cur.execute, "INSERT INTO T VALUES (1)")
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
        self.engine.reply(answer("s1", False, status_set()))
        cur.execute("INSERT INTO T VALUES (2)")
        conn.commit()
        # The second INSERT ran inside the BEGIN it was owed, and commit() committed it.
        self.assertEqual(["SELECT 1 AS N", "BEGIN", "BEGIN", "INSERT INTO T VALUES (2)", "COMMIT"],
                         self.engine.statements())
        self.assert_all_answered()


class AutocommitBackOnTest(unittest.TestCase):
    """Turning autocommit back on leaves no BEGIN owed, whatever its COMMIT meets."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def opened(self):
        self.engine.reply(answer("s1", True, number_set("N", 1)))
        conn = frostlake.connect(self.engine.dsn, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS N")
        return conn, cur

    def refuse_commit(self):
        # A refused COMMIT is followed by a ROLLBACK of the transaction nobody chose.
        self.engine.reply(refused("s1", "SQL compilation error:\nnot now"))
        self.engine.reply(answer("s1", False, status_set()))

    def failures(self):
        """How the COMMIT can fail, what the driver sends after it, the session the next
        statement then runs in, and the error the caller sees."""
        return (
            (self.refuse_commit, ["ROLLBACK"], "s1", frostlake.ProgrammingError),
            (self.engine.drop, [], "s1", frostlake.OperationalError),
            (lambda: self.engine.reply(gone("s1"), status=404), [], "s2",
             frostlake.SessionLostError),
        )

    def assert_next_statement_commits_on_its_own(self, cur, session_id, before):
        self.engine.reply(answer(session_id, session_id != "s1",
                                 number_set("number of rows inserted", 1)))
        try:
            cur.execute("INSERT INTO T VALUES (2)")
        except frostlake.Error:
            pass  # what went out, checked next, says why
        # No BEGIN went out ahead of it, and it went out with autocommit on.
        self.assertEqual(before + ["INSERT INTO T VALUES (2)"], self.engine.statements())
        self.assertIs(True, self.engine.executes()[-1]["autoCommit"])
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def test_a_failed_commit_turning_autocommit_on_leaves_the_next_statement_committing(self):
        for fail, after_commit, session_id, error in self.failures():
            conn, cur = self.opened()
            conn.autocommit = False
            self.engine.reply(answer("s1", False, status_set()))
            self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
            cur.execute("INSERT INTO T VALUES (1)")
            fail()
            self.assertRaises(error, setattr, conn, "autocommit", True)
            self.assertTrue(conn.autocommit)
            self.assert_next_statement_commits_on_its_own(
                cur, session_id,
                ["SELECT 1 AS N", "BEGIN", "INSERT INTO T VALUES (1)", "COMMIT"] + after_commit)
            del self.engine.requests[:]

    def test_a_begin_armed_inside_a_transaction_is_dropped_when_autocommit_comes_back_on(self):
        for fail, after_commit, session_id, error in self.failures():
            conn, cur = self.opened()
            self.engine.reply(answer("s1", False, status_set()))
            cur.execute("BEGIN")
            # Turning autocommit off inside that transaction arms a BEGIN for the next one.
            conn.autocommit = False
            fail()
            self.assertRaises(error, setattr, conn, "autocommit", True)
            self.assertTrue(conn.autocommit)
            self.assert_next_statement_commits_on_its_own(
                cur, session_id, ["SELECT 1 AS N", "BEGIN", "COMMIT"] + after_commit)
            del self.engine.requests[:]


class FailedCommitTest(unittest.TestCase):
    """With autocommit off, a COMMIT or ROLLBACK that failed still leaves the next
    statement inside a transaction that commit() reaches."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def in_transaction(self):
        """A connection with autocommit off that has run one INSERT inside its BEGIN."""
        self.engine.reply(answer("s1", True, number_set("N", 1)))
        self.engine.reply(answer("s1", False, status_set()))
        self.engine.reply(answer("s1", False, number_set("number of rows inserted", 1)))
        conn = frostlake.connect(self.engine.dsn, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS N")
        conn.autocommit = False
        cur.execute("INSERT INTO T VALUES (1)")
        return conn, cur

    def run_next(self, conn, cur, session_id, opens_one):
        """INSERT 2, then commit(): the replies they need, the INSERT with a fresh BEGIN
        ahead of it when `opens_one`."""
        if opens_one:
            self.engine.reply(answer(session_id, session_id != "s1", status_set()))
        self.engine.reply(answer(session_id, False, number_set("number of rows inserted", 1)))
        self.engine.reply(answer(session_id, False, status_set()))
        try:
            cur.execute("INSERT INTO T VALUES (2)")
            conn.commit()
        except frostlake.Error:
            pass  # what went out, checked next, says why

    def assert_sent(self, expected, label):
        self.assertEqual(["SELECT 1 AS N", "BEGIN", "INSERT INTO T VALUES (1)"] + expected,
                         self.engine.statements(), label)
        self.assertEqual([], self.engine.unscripted, label)
        self.assertEqual(0, self.engine.pending, label)
        del self.engine.requests[:]

    def test_a_refused_commit_is_rolled_back_and_the_next_statement_opens_a_transaction(self):
        rollbacks = (
            ("the ROLLBACK taken", lambda: self.engine.reply(answer("s1", False, status_set()))),
            ("the ROLLBACK lost", self.engine.drop),
        )
        for label, rollback in rollbacks:
            conn, cur = self.in_transaction()
            self.engine.reply(refused("s1", "SQL compilation error:\nnot now"))
            rollback()
            with self.assertRaises(frostlake.ProgrammingError) as caught:
                conn.commit()
            self.assertIn("not now", str(caught.exception), label)
            self.run_next(conn, cur, "s1", opens_one=True)
            self.assert_sent(["COMMIT", "ROLLBACK", "BEGIN", "INSERT INTO T VALUES (2)", "COMMIT"],
                             label)

    def test_a_commit_whose_answer_never_came_keeps_the_transaction_open(self):
        failures = (
            ("hang-up", self.engine.drop),
            ("unreadable", lambda: self.engine.reply_text("<html>502 Bad Gateway</html>", 502)),
        )
        for label, fail in failures:
            conn, cur = self.in_transaction()
            fail()
            self.assertRaises(frostlake.OperationalError, conn.commit)
            # The next INSERT joins the open transaction, and commit() sends COMMIT again.
            self.run_next(conn, cur, "s1", opens_one=False)
            self.assertIs(False, self.engine.executes()[-2]["autoCommit"], label)
            self.assert_sent(["COMMIT", "INSERT INTO T VALUES (2)", "COMMIT"], label)

    def test_a_commit_that_finds_the_session_gone_leaves_a_fresh_transaction_next(self):
        conn, cur = self.in_transaction()
        self.engine.reply(gone("s1"), status=404)
        self.assertRaises(frostlake.SessionLostError, conn.commit)
        self.run_next(conn, cur, "s2", opens_one=True)
        self.assert_sent(["COMMIT", "BEGIN", "INSERT INTO T VALUES (2)", "COMMIT"], "lost")

    def test_a_failed_rollback_leaves_a_fresh_transaction_next(self):
        failures = (
            ("refused", lambda: self.engine.reply(refused("s1", "SQL compilation error:\nno")),
             frostlake.ProgrammingError),
            ("hang-up", self.engine.drop, frostlake.OperationalError),
        )
        for label, fail, error in failures:
            conn, cur = self.in_transaction()
            fail()
            self.assertRaises(error, conn.rollback)
            self.run_next(conn, cur, "s1", opens_one=True)
            self.assert_sent(["ROLLBACK", "BEGIN", "INSERT INTO T VALUES (2)", "COMMIT"], label)


class SessionTrackingTest(unittest.TestCase):
    """Which statements the driver reads as moving the session or its transaction."""

    def test_requests_split_on_top_level_semicolons_only(self):
        self.assertEqual(["SELECT 1", " SELECT ';'", ' SELECT ";"'],
                         frostlake._split_statements("SELECT 1; SELECT ';'; SELECT \";\";"))
        self.assertEqual(1, len(frostlake._split_statements(
            "EXECUTE IMMEDIATE $$ SELECT 1; SELECT 2; $$")))
        self.assertEqual(2, len(frostlake._split_statements("SELECT 1 -- a; b\n; SELECT 2")))

    def test_leading_words_skip_comments_and_fold_case(self):
        self.assertEqual(["CREATE", "OR", "REPLACE"], frostlake._leading_words(
            "/* c */ -- x\n create or replace table t", 3))

    def test_which_statements_leave_session_state_behind(self):
        moving = ("USE SCHEMA s", "use database d", "SET x = 1", "UNSET x",
                  "ALTER SESSION SET TIMEZONE = 'UTC'", "CREATE OR REPLACE DATABASE d",
                  "CREATE SCHEMA IF NOT EXISTS s", "DROP DATABASE d",
                  "CREATE TEMPORARY TABLE t (a INT)", "CREATE OR REPLACE TEMP TABLE t (a INT)",
                  "CREATE LOCAL TEMPORARY TABLE t (a INT)")
        staying = ("CREATE TABLE t (a INT)", "CREATE OR REPLACE TRANSIENT TABLE t (a INT)",
                   "ALTER TABLE t ADD COLUMN b INT", "SELECT 1", "INSERT INTO t VALUES (1)")
        for sql in moving:
            self.assertTrue(frostlake._touches_session(sql), sql)
        for sql in staying:
            self.assertFalse(frostlake._touches_session(sql), sql)

    def test_transaction_control_is_recognised_and_a_scripting_block_is_not(self):
        cases = (("BEGIN", "begins"), ("begin transaction", "begins"), ("BEGIN WORK", "begins"),
                 ("BEGIN NAME t1", "begins"), ("START TRANSACTION", "begins"),
                 ("COMMIT", "ends"), ("ROLLBACK WORK", "ends"),
                 ("BEGIN LET x := 1; RETURN x; END", None), ("SELECT 1", None))
        for sql, expected in cases:
            first = frostlake._split_statements(sql)[0]
            self.assertEqual(expected, frostlake._transaction_effect(first), sql)


if __name__ == "__main__":
    unittest.main()
