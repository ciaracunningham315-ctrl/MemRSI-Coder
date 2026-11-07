import unittest
from receipts import summarize
class RegressionTests(unittest.TestCase):
 def test_ordinary(self):
  self.assertEqual(summarize([{"receipt_id":"R","quantity":2,"unit_amount":"3.25"}])["total"],"6.50")
