"""contract.json's `status_schema` is generated from contracts/status.py, validates a real delegate 0.51.0 status answer, and the records
write that answer back byte for byte (key order included)."""
import json
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)

from delegate.app import answer
from delegate.contracts import codec, schema, status

ROOT = Path(__file__).resolve().parent.parent.parent
REAL = ROOT / "tests" / "fixtures" / "delegate_status_0.51.0_real.json"
CONTRACT = json.loads((ROOT / "skills" / "delegate" / "contract.json").read_text(encoding="utf-8"))


class StatusSchema(unittest.TestCase):
    def test_contract_json_carries_the_schema_the_records_generate(self):
        self.assertEqual(CONTRACT["status_schema"], schema.of(status.Status))

    def test_the_schema_validates_the_real_0_51_0_answer(self):
        self.assertIsNone(answer.schema_problem(json.loads(REAL.read_text(encoding="utf-8")), CONTRACT["status_schema"]))

    def test_the_schema_refuses_an_answer_missing_a_key(self):
        bad = json.loads(REAL.read_text(encoding="utf-8"))
        del bad["cockpit"]["lanes"][0]["state"]
        self.assertIsNotNone(answer.schema_problem(bad, CONTRACT["status_schema"]))

    def test_the_records_write_the_real_answer_back_byte_for_byte(self):
        raw = REAL.read_text(encoding="utf-8")
        self.assertEqual(json.dumps(codec.to_json(codec.from_json(status.Status, json.loads(raw)))), json.dumps(json.loads(raw)))


if __name__ == "__main__":
    unittest.main()
