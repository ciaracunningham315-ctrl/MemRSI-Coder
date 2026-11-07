
import unittest
from ledgerflow.sales import summarize_sales
from ledgerflow.shipments import prepare_manifest
from ledgerflow.reconcile import reconcile_partner


class PublicStaleTests(unittest.TestCase):
    def test_prior_sales_and_shipment_flows(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S-1", "quantity": 2, "unit_price": "4.25"}
        ])["grand_total"], "8.50")
        self.assertEqual(prepare_manifest([
            {"shipment_id": "P-1", "parcel_count": 2, "weight_kg": "1.25"}
        ])[0]["fee"], "0.88")

    def test_partner_reconciliation_with_integer_transport_values(self):
        self.assertEqual(reconcile_partner({
            "source": "partner-7",
            "entries": [{"transaction_id": "T-1", "amount_minor": 1200,
                         "adjustment_minor": 0, "basis_points": 500}]
        }), {"entries": [{"transaction_id": "T-1", "amount_minor": 1200,
                          "adjustment_minor": 0, "charge_minor": 60,
                          "settlement_minor": 1260}], "total_minor": 1260})
