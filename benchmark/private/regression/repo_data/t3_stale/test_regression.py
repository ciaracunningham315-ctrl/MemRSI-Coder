
import unittest
from ledgerflow.sales import summarize_sales
from ledgerflow.shipments import prepare_manifest


class PriorFlowsRegression(unittest.TestCase):
    def test_sales_exact_rounding(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S", "quantity": 1, "unit_price": "1.005"}
        ])["grand_total"], "1.01")

    def test_shipment_missing_weight_is_preserved(self):
        self.assertEqual(prepare_manifest([
            {"shipment_id": "P", "parcel_count": 1, "weight_kg": None}
        ])[0]["status"], "needs_quote")
