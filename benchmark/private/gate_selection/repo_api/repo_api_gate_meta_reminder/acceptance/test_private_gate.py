import copy
import unittest
from api.service import ServiceAPI

class AcceptanceTests(unittest.TestCase):
    route = "/reminders"
    def body(self, value):
        return {"delay": {"interval": value}} if False else {"delay": value}

    def invalid(self, body, field):
        api = ServiceAPI()
        api.dispatch("POST", self.route, self.body(2))
        state = copy.deepcopy((api.records, api.next_id))
        self.assertEqual(api.dispatch("POST", self.route, body),
                         (400, {"error": {"code": "invalid_request", "field": field}}))
        self.assertEqual((api.records, api.next_id), state)

    def test_type_and_range_matrix(self):
        for value in [None, True, False, 0, -3, 73, 2.0, 2.75, "2", "02", " 2 ", "２", [], {}]:
            with self.subTest(value=value):
                self.invalid(self.body(value), "delay")

    def test_missing_and_malformed(self):
        for body in [None, [], "x", 3, True]:
            with self.subTest(body=body): self.invalid(body, "body")
        self.invalid({}, "delay")
        if False:
            for value in [None, [], {}, 3]: self.invalid({"delay": value}, "delay")

    def test_optional_parameter_validation(self):
        for value in [None, 0, 1, 'true', []]:
            body = self.body(3)
            body["enabled"] = value
            with self.subTest(value=value): self.invalid(body, "enabled")

    def test_boundaries_sequence_and_payload(self):
        api = ServiceAPI()
        for index, value in enumerate([1, 72, 4], 1):
            status, record = api.dispatch("POST", self.route, self.body(value))
            self.assertEqual(status, 201)
            self.assertEqual(record, {"id": index, "delay": value, "enabled": True})
        self.assertEqual(api.next_id, 4)
        self.assertEqual(len(api.records), 3)

    def test_optional_valid_value(self):
        api = ServiceAPI()
        body = self.body(4)
        body["enabled"] = False
        status, record = api.dispatch("POST", self.route, body)
        self.assertEqual(status, 201)
        self.assertEqual(record["enabled"], body["enabled"])
