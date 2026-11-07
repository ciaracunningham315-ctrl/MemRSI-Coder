import unittest
from receipts import summarize
class PublicTests(unittest.TestCase):
 def test_ordinary(self):
  self.assertEqual(summarize([{"receipt_id":"R-1","quantity":"2","unit_amount":"3.25"}]),{"lines":[{"receipt_id":"R-1","amount":"6.50"}],"total":"6.50"})
