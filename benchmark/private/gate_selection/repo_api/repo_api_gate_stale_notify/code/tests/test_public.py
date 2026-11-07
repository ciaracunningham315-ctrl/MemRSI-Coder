import unittest
from api.service import ServiceAPI

class PublicTests(unittest.TestCase):
    def test_health(self):
        self.assertEqual(ServiceAPI().dispatch("GET", "/health"), (200, {"status": "ok"}))

    def test_ordinary_request(self):
        status, result = ServiceAPI().dispatch("POST", "/notifications", {'schedule': {'interval': 2}})
        self.assertEqual(status, 201)
        self.assertEqual(result["schedule.interval"], 2)
