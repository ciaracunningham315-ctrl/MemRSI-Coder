import unittest
from telemetry import normalize
class RegressionTests(unittest.TestCase):
 def test_nonzero(self):
  self.assertEqual(normalize([{"sample_id":"S","reading":"2.75"}])[0]["reading"],"2.75")
