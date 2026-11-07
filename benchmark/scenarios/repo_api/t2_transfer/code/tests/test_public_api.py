import unittest
from api.app import InventoryAPI


class PublicAPI(unittest.TestCase):
    def test_health(self):
        self.assertEqual(InventoryAPI().dispatch("GET", "/health")["body"], {"status": "ok"})

    def test_target_example(self):
        result = InventoryAPI().dispatch("POST", '/returns', {"sku":"A","count":3})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["headers"], {"content-type": "application/json"})

    def test_unknown_route(self):
        self.assertEqual(InventoryAPI().dispatch("POST", "/unknown", {})["status"], 404)
