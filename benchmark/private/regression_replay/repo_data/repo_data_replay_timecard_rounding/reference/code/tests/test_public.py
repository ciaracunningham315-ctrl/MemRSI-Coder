import unittest
from timecards import calculate
class PublicTests(unittest.TestCase):
 def test_ordinary(self):
  self.assertEqual(calculate([{"worker_id":"W","hours":"2","rate":"3.25"}]),{"rows":[{"worker_id":"W","pay":"6.50"}],"total":"6.50"})
