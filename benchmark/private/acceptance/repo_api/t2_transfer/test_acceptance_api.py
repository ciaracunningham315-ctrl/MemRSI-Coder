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

class ReturnContract(ContractCase):
    def test_return_count_domain_and_upper_limit(self):
        for value in (None,True,False,0,-1,51,1000,1.0,2.5,"3",[],{}):
            self.count_error("/returns", {"sku":"A","count":value}, "count", 1, 50)
        self.count_error("/returns", {"sku":"A"}, "count", 1, 50)
        self.assertEqual(self.send("/returns", {"sku":"B","count":50})["body"],
                         {"sku":"B","count":50,"available":56})

    def test_zero_damage_all_damage_and_partial_damage(self):
        for count in (1,4,17,50):
            for damaged in sorted({0, count//2, count}):
                with self.subTest(count=count,damaged=damaged):
                    self.api=InventoryAPI()
                    got=self.send("/returns", {"sku":"A","count":count,"inspection":{"damaged":damaged}})
                    self.assertEqual(got["status"],200)
                    self.assertEqual(got["body"],{"sku":"A","count":count,"available":10+count-damaged})
                    self.assertEqual(self.stock()["B"],6)

    def test_damage_errors_keep_full_path_and_dynamic_bounds(self):
        for count in (1,7,50):
            for damaged in (None,True,False,-1,count+1,0.0,"0",[],{}):
                self.count_error("/returns", {"sku":"A","count":count,"inspection":{"damaged":damaged}},
                                 "inspection.damaged",0,count)
            self.count_error("/returns", {"sku":"A","count":count,"inspection":{}}, "inspection.damaged",0,count)

    def test_validation_precedence_and_no_identifier_consumption(self):
        for body in (None, [], False):
            self.invalid("/returns",body,"body","body must be an object")
        self.invalid("/returns",{"sku":"bad","count":0,"inspection":None},"sku","sku must identify A or B")
        self.count_error("/returns",{"sku":"A","count":0,"inspection":None},"count",1,50)
        for inspection in (None,False,2,[],"bad"):
            self.invalid("/returns",{"sku":"A","count":3,"inspection":inspection},"inspection","inspection must be an object")
        self.send("/returns",{"sku":"A","count":5,"inspection":{"damaged":2,"extra":1}})
        self.assertEqual(self.stock(),{"A":13,"B":6})
        result=self.send("/reservations",{"sku":"A","count":1})
        self.assertEqual(result["body"]["reservation_id"],"r1")

