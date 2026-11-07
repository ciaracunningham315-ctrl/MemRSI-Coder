import copy
import unittest
from api.service import ServiceAPI

class AcceptanceTests(unittest.TestCase):
    route = "/exports"
    def body(self, value):
        return {"days": {"interval": value}} if False else {"days": value}

    def invalid(self, body, field):
        api = ServiceAPI()
        api.dispatch("POST", self.route, self.body(2))
        state = copy.deepcopy((api.records, api.next_id))
        self.assertEqual(api.dispatch("POST", self.route, body),
                         (400, {"error": {"code": "invalid_request", "field": field}}))
        self.assertEqual((api.records, api.next_id), state)

    def test_type_and_range_matrix(self):
        for value in [None, True, False, 0, -3, 91, 2.0, 2.75, "2", "02", " 2 ", "２", [], {}]:
            with self.subTest(value=value):
                self.invalid(self.body(value), "days")

    def test_missing_and_malformed(self):
        for body in [None, [], "x", 3, True]:
            with self.subTest(body=body): self.invalid(body, "body")
        self.invalid({}, "days")
        if False:
            for value in [None, [], {}, 3]: self.invalid({"days": value}, "days")

    def test_optional_parameter_validation(self):
        for value in [None, 0, 1, 'true', []]:
            body = self.body(3)
            body["compressed"] = value
            with self.subTest(value=value): self.invalid(body, "compressed")

    def test_boundaries_sequence_and_payload(self):
        api = ServiceAPI()
        for index, value in enumerate([1, 90, 4], 1):
            status, record = api.dispatch("POST", self.route, self.body(value))
            self.assertEqual(status, 201)
            self.assertEqual(record, {"id": index, "days": value, "compressed": False})
        self.assertEqual(api.next_id, 4)
        self.assertEqual(len(api.records), 3)

    def test_optional_valid_value(self):
        api = ServiceAPI()
        body = self.body(4)
        body["compressed"] = True
        status, record = api.dispatch("POST", self.route, body)
        self.assertEqual(status, 201)
        self.assertEqual(record["compressed"], body["compressed"])
