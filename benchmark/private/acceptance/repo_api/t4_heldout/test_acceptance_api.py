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

class BatchContract(ContractCase):
    def test_items_shape_and_empty_batch(self):
        for body in (None,False,[]):
            self.invalid("/batch-reservations",body,"body","body must be an object")
        for items in (None,False,{},"x",[],[{"sku":"A","count":1}]*9):
            self.invalid("/batch-reservations",{"items":items},"items","items must be a list containing 1 to 8 objects")
        self.invalid("/batch-reservations",{},"items","items must be a list containing 1 to 8 objects")

    def test_every_line_strict_validation_before_stock_decision(self):
        for index in range(4):
            prefix=[{"sku":"A","count":1} for _ in range(index)]
            for value in (None,True,False,0,-1,6,1.0,"2",[],{}):
                self.count_error("/batch-reservations",{"items":prefix+[{"sku":"B","count":value}]},f"items[{index}].count",1,5)
            self.invalid("/batch-reservations",{"items":prefix+[None]},f"items[{index}]",f"items[{index}] must be an object")
            self.invalid("/batch-reservations",{"items":prefix+[{"sku":"Z","count":False}]},f"items[{index}].sku",f"items[{index}].sku must identify A or B")
        self.send("/reservations",{"sku":"A","count":10})
        self.count_error("/batch-reservations",{"items":[{"sku":"A","count":1},{"sku":"B","count":False}]},"items[1].count",1,5)
        self.invalid("/batch-reservations",{"items":[{"sku":"A","count":1},None]},"items[1]","items[1] must be an object")

    def test_cumulative_conflict_rolls_back_and_preserves_identifier(self):
        for items,field in (([{"sku":"A","count":5},{"sku":"A","count":5},{"sku":"A","count":1}],"items[2].count"),
                            ([{"sku":"A","count":2},{"sku":"B","count":4},{"sku":"B","count":3}],"items[2].count")):
            self.api=InventoryAPI()
            self.conflict("/batch-reservations",{"items":items},field)
            result=self.send("/batch-reservations",{"items":[{"sku":"A","count":2}]})
            self.assertEqual(result["body"]["reservation_id"],"r1")
            self.assertEqual(result["body"]["remaining"],{"A":8,"B":6})

    def test_success_order_shared_ids_and_response_isolation(self):
        items=[{"sku":"A","count":2,"extra":{"x":1}},{"sku":"B","count":3},{"sku":"A","count":5}]
        result=self.send("/batch-reservations",{"items":items})
        self.assertEqual(result["status"],201)
        self.assertEqual(result["body"],{"reservation_id":"r1","items":[{"sku":"A","count":2},{"sku":"B","count":3},{"sku":"A","count":5}],"remaining":{"A":3,"B":3}})
        result["body"]["remaining"]["A"]=900
        result["body"]["items"][0]["count"]=900
        self.assertEqual(self.stock(),{"A":3,"B":3})
        self.assertEqual(items[0]["count"],2)
        hold=self.send("/holds",{"item":{"sku":"B","count":1}})
        self.assertEqual(hold["body"]["reservation_id"],"r2")

    def test_eight_lines_and_exact_exhaustion(self):
        items=[{"sku":"A","count":1}]*6+[{"sku":"B","count":3}]*2
        self.assertEqual(self.send("/batch-reservations",{"items":items})["body"]["remaining"],{"A":4,"B":0})
        self.api=InventoryAPI()
        for sku,available in (("A",10),("B",6)):
            self.api=InventoryAPI()
            items=[{"sku":sku,"count":5},{"sku":sku,"count":available-5}]
            got=self.send("/batch-reservations",{"items":items})
            self.assertEqual(got["status"],201)
            self.assertEqual(got["body"]["remaining"][sku],0)

