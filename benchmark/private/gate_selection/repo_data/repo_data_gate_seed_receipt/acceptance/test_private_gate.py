import unittest
from receipts import summarize
class GateTests(unittest.TestCase):
 def test_half_up_and_bool(self):
  self.assertEqual(summarize([{"receipt_id":"R","quantity":3,"unit_amount":"0.335"}])["total"],"1.01")
  with self.assertRaises(ValueError): summarize([{"receipt_id":"B","quantity":True,"unit_amount":"1"}])
