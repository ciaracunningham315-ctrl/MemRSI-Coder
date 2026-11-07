import unittest
from telemetry import normalize
class GateTests(unittest.TestCase):
 def test_missing_is_not_zero(self):
  rows=[{"sample_id":"A"},{"sample_id":"B","reading":None},{"sample_id":"C","reading":" "},{"sample_id":"D","reading":0}]
  self.assertEqual([(x["reading"],x["status"]) for x in normalize(rows)],[(None,"pending"),(None,"pending"),(None,"pending"),("0.00","recorded")])
 def test_bool_rejected(self):
  with self.assertRaises(ValueError): normalize([{"sample_id":"B","reading":True}])
