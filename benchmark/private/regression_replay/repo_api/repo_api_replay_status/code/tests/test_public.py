import unittest
from api.status import StatusAPI
class PublicTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(StatusAPI().dispatch("GET", "/status")[0], 200)
