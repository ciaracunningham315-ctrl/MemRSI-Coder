import json
from pathlib import Path
import unittest

import task_module

CASES = json.loads(Path(__file__).with_name("gate_cases.json").read_text(encoding="utf-8"))["cases"]

class IndependentNSRRAcceptance(unittest.TestCase):
    def test_frozen_independent_cases(self):
        self.assertGreaterEqual(len(CASES), 3)
        for case in CASES:
            with self.subTest(case_id=case["id"]):
                self.assertEqual(task_module.run(case["input"]), case["expected"])

if __name__ == "__main__":
    unittest.main()
