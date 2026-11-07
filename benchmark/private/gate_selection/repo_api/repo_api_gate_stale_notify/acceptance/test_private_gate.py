import copy
import unittest
from api.service import ServiceAPI

class AcceptanceTests(unittest.TestCase):
    route = "/notifications"
    def body(self, value):
        return {"schedule": {"interval": value}} if True else {"schedule.interval": value}

    def invalid(self, body, field):
        api = ServiceAPI()
        api.dispatch("POST", self.route, self.body(2))
        state = copy.deepcopy((api.records, api.next_id))
        self.assertEqual(api.dispatch("POST", self.route, body),
                         (400, {"error": {"code": "invalid_request", "field": field}}))
        self.assertEqual((api.records, api.next_id), state)

    def test_type_and_range_matrix(self):
        for value in [None, True, False, 0, -3, 61, 2.0, 2.75, "2", "02", " 2 ", "２", [], {}]:
            with self.subTest(value=value):
                self.invalid(self.body(value), "schedule.interval")

    def test_missing_and_malformed(self):
        for body in [None, [], "x", 3, True]:
            with self.subTest(body=body): self.invalid(body, "body")
        self.invalid({}, "schedule.interval")
        if True:
            for value in [None, [], {}, 3]: self.invalid({"schedule": value}, "schedule.interval")

    def test_optional_parameter_validation(self):
        for value in [None, False, 0, 'invalid', []]:
            body = self.body(3)
            body["channel"] = value
            with self.subTest(value=value): self.invalid(body, "channel")

    def test_boundaries_sequence_and_payload(self):
        api = ServiceAPI()
        for index, value in enumerate([1, 60, 4], 1):
            status, record = api.dispatch("POST", self.route, self.body(value))
            self.assertEqual(status, 201)
            self.assertEqual(record, {"id": index, "schedule.interval": value, "channel": 'email'})
        self.assertEqual(api.next_id, 4)
        self.assertEqual(len(api.records), 3)

    def test_optional_valid_value(self):
        api = ServiceAPI()
        body = self.body(4)
        body["channel"] = 'sms'
        status, record = api.dispatch("POST", self.route, body)
        self.assertEqual(status, 201)
        self.assertEqual(record["channel"], body["channel"])
