import unittest
from telemetry import normalize_one,convert_batch
class PublicTests(unittest.TestCase):
 def test_old_and_integer_batch(self):
  self.assertEqual(normalize_one("1.25"),"1.25")
  self.assertEqual(convert_batch({"readings":[{"sensor_id":"S","reading_milli":1200,"offset_milli":200}]}),{"readings":[{"sensor_id":"S","reading_milli":1200,"offset_milli":200,"net_milli":1000}],"total_milli":1000})
