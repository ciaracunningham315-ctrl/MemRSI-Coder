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

class HoldContract(ContractCase):
    def test_nested_error_paths_and_count_limits(self):
        for value in (None,True,False,0,-2,13,1.0,"2",[],{}):
            self.count_error("/holds",{"item":{"sku":"A","count":value}},"item.count",1,12)
        self.count_error("/holds",{"item":{"sku":"A"}},"item.count",1,12)
        for sku in (None,True,3,"a","Z",{},[]):
            self.invalid("/holds",{"item":{"sku":sku,"count":0}},"item.sku","item.sku must identify A or B")

    def test_priority_exact_domain_without_normalization(self):
        for value in (None,True,False,0,1,"NORMAL","Urgent"," normal","urgent ","",[],{}):
            self.invalid("/holds",{"item":{"sku":"A","count":1},"priority":value},
                         "priority","priority must be normal or urgent")
        for value in ("normal","urgent"):
            self.api=InventoryAPI()
            result=self.send("/holds",{"item":{"sku":"B","count":2},"priority":value})
            self.assertEqual(result["body"],{"reservation_id":"r1","sku":"B","count":2,"remaining":4,"priority":value})

    def test_shape_order_and_failure_atomicity(self):
        for payload in (None,[],False):
            self.invalid("/holds",payload,"body","body must be an object")
        for item in (None,[],False,1,"A"):
            self.invalid("/holds",{"item":item,"priority":"BAD"},"item","item must be an object")
        self.invalid("/holds",{},"item","item must be an object")
        self.count_error("/holds",{"item":{"sku":"A","count":0},"priority":"BAD"},"item.count",1,12)
        self.invalid("/holds",{"item":{"sku":"A","count":12},"priority":"BAD"},"priority","priority must be normal or urgent")
        self.conflict("/holds",{"item":{"sku":"A","count":12}},"item.count")
        got=self.send("/holds",{"item":{"sku":"A","count":1,"other":[2]}})
        self.assertEqual(got["body"],{"reservation_id":"r1","sku":"A","count":1,"remaining":9,"priority":"normal"})
        self.assertEqual(self.send("/reservations",{"sku":"B","count":1})["body"]["reservation_id"],"r2")

    def test_migrated_legacy_routes_remain_available(self):
        self.assertEqual(self.send("/returns",{"sku":"A","count":4,"inspection":{"damaged":0}})["status"],200)
        self.count_error("/returns",{"sku":"B","count":3,"inspection":{"damaged":4}},"inspection.damaged",0,3)
        self.count_error("/reservations",{"sku":"A","count":False},"count",1,20)

