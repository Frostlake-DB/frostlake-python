"""Unit tests for the driver's PEP 249 surface and value handling.

These need no server and no JVM, so they run on a bare checkout:

    python3 -m unittest test_frostlake_unit -v
"""

import datetime
import decimal
import unittest

import frostlake


class ModuleSurfaceTest(unittest.TestCase):
    """PEP 249 requires these names at module level."""

    def test_globals(self):
        self.assertEqual("2.0", frostlake.apilevel)
        self.assertEqual(1, frostlake.threadsafety)
        self.assertEqual("qmark", frostlake.paramstyle)

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
