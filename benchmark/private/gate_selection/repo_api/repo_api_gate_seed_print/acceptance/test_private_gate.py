import copy
import unittest
from api.service import ServiceAPI

class AcceptanceTests(unittest.TestCase):
    route = "/print-jobs"
    def body(self, value):
        return {"copies": {"interval": value}} if False else {"copies": value}

    def invalid(self, body, field):
        api = ServiceAPI()
        api.dispatch("POST", self.route, self.body(2))
        state = copy.deepcopy((api.records, api.next_id))
        self.assertEqual(api.dispatch("POST", self.route, body),
                         (400, {"error": {"code": "invalid_request", "field": field}}))
        self.assertEqual((api.records, api.next_id), state)

    def test_type_and_range_matrix(self):
        for value in [None, True, False, 0, -3, 10, 2.0, 2.75, "2", "02", " 2 ", "２", [], {}]:
            with self.subTest(value=value):
                self.invalid(self.body(value), "copies")

    def test_missing_and_malformed(self):
        for body in [None, [], "x", 3, True]:
            with self.subTest(body=body): self.invalid(body, "body")
        self.invalid({}, "copies")
        if False:
            for value in [None, [], {}, 3]: self.invalid({"copies": value}, "copies")

    def test_optional_parameter_validation(self):
        for value in [None, False, 0, 'invalid', []]:
            body = self.body(3)
            body["format"] = value
            with self.subTest(value=value): self.invalid(body, "format")

    def test_boundaries_sequence_and_payload(self):
        api = ServiceAPI()
        for index, value in enumerate([1, 9, 4], 1):
            status, record = api.dispatch("POST", self.route, self.body(value))
            self.assertEqual(status, 201)
            self.assertEqual(record, {"id": index, "copies": value, "format": 'mono'})
        self.assertEqual(api.next_id, 4)
        self.assertEqual(len(api.records), 3)

    def test_optional_valid_value(self):
        api = ServiceAPI()
        body = self.body(4)
        body["format"] = 'color'
        status, record = api.dispatch("POST", self.route, body)
        self.assertEqual(status, 201)
        self.assertEqual(record["format"], body["format"])
