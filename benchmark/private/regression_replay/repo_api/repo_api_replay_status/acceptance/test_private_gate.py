import copy
import unittest
from api.status import StatusAPI
class ReplayTests(unittest.TestCase):
    def test_full_response_isolation(self):
        api = StatusAPI(); before = copy.deepcopy(api.metadata)
        first = api.dispatch("GET", "/status")[1]
        second = api.dispatch("GET", "/status")[1]
        first["service"] = "changed"; first["labels"].append("dirty"); first["limits"]["workers"] = 900
        self.assertEqual(api.metadata, before)
        self.assertEqual(second, before)
        self.assertEqual(api.dispatch("GET", "/status")[1], before)
        self.assertEqual(StatusAPI().metadata, before)

    def test_preserve_runtime_metadata(self):
        api = StatusAPI(); api.metadata["limits"]["workers"] = 7; api.metadata["labels"].append("custom")
        result = api.dispatch("GET", "/status")[1]
        self.assertEqual(result, api.metadata)
        result["labels"].clear()
        self.assertEqual(api.metadata["labels"], ["stable", "custom"])
