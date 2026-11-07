import unittest
from telemetry import convert_batch
class GateTests(unittest.TestCase):
 def test_text_integer_is_current_unit(self):
  self.assertEqual(convert_batch({"readings":[{"sensor_id":"S","reading_milli":"1250","offset_milli":"250"}]}),{"readings":[{"sensor_id":"S","reading_milli":1250,"offset_milli":250,"net_milli":1000}],"total_milli":1000})
 def test_bool_rejected(self):
  with self.assertRaises(ValueError): convert_batch({"readings":[{"sensor_id":"S","reading_milli":True}]})
