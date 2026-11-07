import unittest
from copy import deepcopy
from api.app import InventoryAPI


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.api = InventoryAPI()

    def send(self, route, payload):
        before = deepcopy(payload)
        result = self.api.dispatch("POST", route, payload)
        self.assertEqual(payload, before, "request data must not be modified")
        self.assertEqual(result["headers"], {"content-type": "application/json"})
        self.assertEqual(set(result), {"status", "headers", "body"})
        return result

    def stock(self):
        return self.api.dispatch("GET", "/inventory")["body"]

    def invalid(self, route, payload, field, message):
        before = self.stock()
        self.assertEqual(self.send(route, payload), {
            "status": 422, "headers": {"content-type": "application/json"},
            "body": {"error": {"code": "invalid_input", "field": field, "message": message}}})
        self.assertEqual(self.stock(), before)

    def count_error(self, route, payload, field, low, high):
        self.invalid(route, payload, field, f"{field} must be an integer between {low} and {high}")

    def conflict(self, route, payload, field):
        before = self.stock()
        self.assertEqual(self.send(route, payload), {
            "status":409,"headers":{"content-type":"application/json"},
            "body":{"error":{"code":"out_of_stock","field":field,"message":"insufficient stock"}}})
        self.assertEqual(self.stock(), before)

class ReservationContract(ContractCase):
    def test_integer_domain_and_boundaries(self):
        for value in (None, True, False, 0, -1, -800, 21, 91, 1.0, 3.2, "1", " 2 ", "", [], {}):
            with self.subTest(value=value):
                self.count_error("/reservations", {"sku":"A","count":value}, "count", 1, 20)
        self.count_error("/reservations", {"sku":"A"}, "count", 1, 20)
        self.conflict("/reservations", {"sku":"A","count":20}, "count")

    def test_error_location_and_validation_order(self):
        for payload in (None, [], "bad", 2, False):
            self.invalid("/reservations", payload, "body", "body must be an object")
        for sku in (None, True, 1, "a", " A", "Z", [], {}):
            self.invalid("/reservations", {"sku":sku,"count":"bad"}, "sku", "sku must identify A or B")

    def test_failure_does_not_consume_identifiers(self):
        self.conflict("/reservations", {"sku":"A","count":11}, "count")
        self.count_error("/reservations", {"sku":"A","count":0}, "count", 1, 20)
        first = self.send("/reservations", {"sku":"B","count":2})
        self.assertEqual(first["status"], 201)
        self.assertEqual(first["body"], {"reservation_id":"r1","sku":"B","count":2,"remaining":4})
        second = self.send("/reservations", {"sku":"A","count":7,"unused":{"x":1}})
        self.assertEqual(second["body"], {"reservation_id":"r2","sku":"A","count":7,"remaining":3})

    def test_legitimate_counts_and_exact_exhaustion(self):
        for sku,available in (("A",10),("B",6)):
            for value in range(1,available+1):
                with self.subTest(sku=sku,count=value):
                    self.api=InventoryAPI()
                    got=self.send("/reservations", {"sku":sku,"count":value})
                    self.assertEqual(got["status"],201)
                    self.assertIs(type(got["body"]["count"]),int)
                    self.assertEqual(got["body"]["remaining"],available-value)

