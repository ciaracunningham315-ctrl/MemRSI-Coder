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

class StableAPI(ContractCase):
    def test_health_inventory_and_unknown_method(self):
        self.assertEqual(self.api.dispatch("GET","/health"),{"status":200,"headers":{"content-type":"application/json"},"body":{"status":"ok"}})
        inventory=self.stock()
        self.assertEqual(inventory,{"A":10,"B":6})
        inventory["A"]=900
        self.assertEqual(self.stock(),{"A":10,"B":6})
        for method,path in (("POST","/unknown"),("DELETE","/reservations"),("GET","/reservations")):
            self.assertEqual(self.api.dispatch(method,path,{}),{"status":404,"headers":{"content-type":"application/json"},"body":{"error":{"code":"not_found","message":"route not found"}}})

    def test_instances_are_independent(self):
        self.send("/reservations",{"sku":"A","count":1})
        self.assertEqual(InventoryAPI().dispatch("GET","/inventory")["body"],{"A":10,"B":6})

