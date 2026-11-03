"""delegate-d6f: the schema subset checker follows JSON Schema on bool vs number, integral floats and unknown types."""
import unittest

import hermetic  # noqa: F401  (a home of its own for every test)

from delegate.app.answer import schema_problem


class SchemaProblemTest(unittest.TestCase):
    def test_true_is_not_the_enum_value_1(self):
        self.assertIsNotNone(schema_problem(True, {"enum": [1]}))
        self.assertIsNotNone(schema_problem(0, {"enum": [False]}))
        self.assertIsNotNone(schema_problem([True], {"enum": [[1]]}))
        self.assertIsNone(schema_problem(True, {"enum": [True]}))
        self.assertIsNone(schema_problem(1, {"enum": [1]}))

    def test_an_integral_float_is_an_integer(self):
        self.assertIsNone(schema_problem(1.0, {"type": "integer"}))
        self.assertIsNotNone(schema_problem(1.5, {"type": "integer"}))
        self.assertIsNotNone(schema_problem(True, {"type": "integer"}))
        self.assertIsNone(schema_problem(1.5, {"type": "number"}))

    def test_an_unknown_type_is_reported(self):
        self.assertIn("unsupported", schema_problem("x", {"type": "strng"}) or "")
        self.assertIn("unsupported", schema_problem("x", {"type": ["string", "date"]}) or "")
        self.assertIsNone(schema_problem("x", {"type": ["string", "null"]}))


if __name__ == "__main__":
    unittest.main()
