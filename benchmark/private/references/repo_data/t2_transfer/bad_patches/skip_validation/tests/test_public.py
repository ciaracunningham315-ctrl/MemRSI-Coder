
import unittest
from ledgerflow.sales import summarize_sales
from ledgerflow.shipments import prepare_manifest


class PublicTransferTests(unittest.TestCase):
    def test_sales_behavior_is_stable(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S-1", "quantity": 2, "unit_price": "4.25"}
        ])["grand_total"], "8.50")

    def test_known_shipment(self):
        self.assertEqual(prepare_manifest([
            {"shipment_id": "P-1", "parcel_count": "2", "weight_kg": "1.25"}
        ]), [{"shipment_id": "P-1", "parcel_count": 2, "weight_kg": "1.25",
              "status": "ready", "fee": "0.88"}])
