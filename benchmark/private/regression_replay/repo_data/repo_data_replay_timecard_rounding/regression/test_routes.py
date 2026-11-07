import unittest
from timecards import calculate
class RegressionTests(unittest.TestCase):
 def test_ordinary_remains(self):
  self.assertEqual(calculate([{"worker_id":"W","hours":2,"rate":"3.25"}])["total"],"6.50")
