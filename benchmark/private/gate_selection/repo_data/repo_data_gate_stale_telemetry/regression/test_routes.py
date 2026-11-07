import unittest
from telemetry import normalize_one
class RegressionTests(unittest.TestCase):
 def test_legacy_stays_valid(self):
  self.assertEqual(normalize_one("2.5"),"2.50")
