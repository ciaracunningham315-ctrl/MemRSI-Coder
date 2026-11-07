import unittest
from api.service import ServiceAPI

class RegressionTests(unittest.TestCase):
    def test_routes_are_stable_and_read_only(self):
        api = ServiceAPI()
        self.assertEqual(api.dispatch("GET", "/health"), (200, {"status":"ok"}))
        self.assertEqual(api.dispatch("DELETE", "/exports"), (405, {"error":{"code":"method_not_allowed"}}))
        self.assertEqual(api.dispatch("POST", "/not-a-route", {}), (404, {"error":{"code":"not_found"}}))
        self.assertEqual((api.records, api.next_id), ([], 1))
