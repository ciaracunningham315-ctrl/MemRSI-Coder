import unittest
from catalog import merge_rates
class GateTests(unittest.TestCase):
 def test_zero_and_legacy_precision(self):
  self.assertEqual(merge_rates([{"rate_id":"Z","amount_minor":0,"amount_major":"10.00"},{"rate_id":"L","amount_major":"1.005","adjustment_minor":"1"}]),{"rows":[{"rate_id":"Z","amount_minor":0,"adjustment_minor":0,"net_minor":0},{"rate_id":"L","amount_minor":101,"adjustment_minor":1,"net_minor":100}],"total_minor":100})
 def test_bool_rejected(self):
  with self.assertRaises(ValueError): merge_rates([{"rate_id":"B","amount_minor":True}])
