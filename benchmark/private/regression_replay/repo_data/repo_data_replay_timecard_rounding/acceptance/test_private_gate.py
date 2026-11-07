import unittest
from timecards import calculate
class GateTests(unittest.TestCase):
 def test_half_up(self):
  self.assertEqual(calculate([{"worker_id":"W","hours":"3","rate":"0.335"}])["total"],"1.01")
 def test_bool_rejected(self):
  with self.assertRaises(ValueError): calculate([{"worker_id":"W","hours":True,"rate":1}])
