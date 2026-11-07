
import unittest
from ledgerflow.sales import summarize_sales
from ledgerflow.shipments import prepare_manifest
from ledgerflow.reconcile import reconcile_partner


class PriorStagesRegression(unittest.TestCase):
    def test_exact_sales(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S", "quantity": 1, "unit_price": "1.005"}
        ])["grand_total"], "1.01")

    def test_missing_shipment_weight(self):
        self.assertIsNone(prepare_manifest([
            {"shipment_id": "P", "parcel_count": 1, "weight_kg": ""}
        ])[0]["fee"])

    def test_string_minor_units(self):
        self.assertEqual(reconcile_partner({"source": "p", "entries": [
            {"transaction_id": "T", "amount_minor": "125", "basis_points": 400}
        ]})["total_minor"], 130)
