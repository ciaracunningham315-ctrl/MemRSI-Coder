import unittest
from catalog import merge_rates
class PublicTests(unittest.TestCase):
 def test_new_rate(self):
  self.assertEqual(merge_rates([{"rate_id":"R","amount_minor":2500,"adjustment_minor":300}]),{"rows":[{"rate_id":"R","amount_minor":2500,"adjustment_minor":300,"net_minor":2200}],"total_minor":2200})
