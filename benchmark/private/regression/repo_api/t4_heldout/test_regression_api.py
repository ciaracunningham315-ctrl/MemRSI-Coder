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

