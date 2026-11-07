import copy
import unittest
from api.status import StatusAPI
class RegressionTests(unittest.TestCase):
    def test_error_routes(self):
        api = StatusAPI(); before = copy.deepcopy(api.metadata)
        self.assertEqual(api.dispatch("POST", "/status"), (405, {"error":{"code":"method_not_allowed"}}))
        self.assertEqual(api.dispatch("GET", "/missing"), (404, {"error":{"code":"not_found"}}))
        self.assertEqual(api.metadata, before)
