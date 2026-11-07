import unittest
from telemetry import normalize
class PublicTests(unittest.TestCase):
 def test_known(self):
  self.assertEqual(normalize([{"sample_id":"S","reading":"3.5"}]),[{"sample_id":"S","reading":"3.50","status":"recorded"}])
