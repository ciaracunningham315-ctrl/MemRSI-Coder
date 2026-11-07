
import unittest
from ledgerflow.sales import summarize_sales
from ledgerflow.shipments import prepare_manifest
from ledgerflow.reconcile import reconcile_partner
from ledgerflow.closeout import merge_close


class PublicHeldoutTests(unittest.TestCase):
    def test_prior_flows_remain_available(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S", "quantity": 2, "unit_price": "4.25"}
        ])["grand_total"], "8.50")
        self.assertEqual(prepare_manifest([
            {"shipment_id": "P", "parcel_count": 2, "weight_kg": "1.25"}
        ])[0]["fee"], "0.88")
        self.assertEqual(reconcile_partner({
            "source": "p", "entries": [{"transaction_id": "T", "amount_minor": 1200, "basis_points": 500}]
        })["total_minor"], 1260)

    def test_simple_monthly_close(self):
        self.assertEqual(merge_close([
            {"invoice_ref": "I-1", "gross_cents": 2500, "credit_cents": "300"}
        ]), {"rows": [{"invoice_ref": "I-1", "gross_cents": 2500, "credit_cents": 300, "net_cents": 2200}],
             "total_cents": 2200})
