import unittest
from catalog import merge_rates
class RegressionTests(unittest.TestCase):
 def test_new_minor_value(self):
  self.assertEqual(merge_rates([{"rate_id":"R","amount_minor":2500}])["total_minor"],2500)
