
import unittest
from ledgerflow.shipments import prepare_manifest


class ShipmentAcceptance(unittest.TestCase):
    def test_absent_null_and_blank_are_unknown_but_zero_is_known(self):
        rows = [
            {"shipment_id": "A", "parcel_count": 1},
            {"shipment_id": "B", "parcel_count": "2", "weight_kg": None},
            {"shipment_id": "C", "parcel_count": 1, "weight_kg": ""},
            {"shipment_id": "D", "parcel_count": 1, "weight_kg": "  "},
            {"shipment_id": "E", "parcel_count": 3, "weight_kg": "0"},
        ]
        before = [dict(row) for row in rows]
        result = prepare_manifest(rows)
        for item in result[:4]:
            self.assertEqual((item["weight_kg"], item["status"], item["fee"]), (None, "needs_quote", None))
        self.assertEqual((result[4]["weight_kg"], result[4]["status"], result[4]["fee"]),
                         ("0.00", "ready", "0.00"))
        self.assertEqual(rows, before)

    def test_types_and_boundaries(self):
        for bad in (True, 1.5, "2.0", 0, 51):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prepare_manifest([{"shipment_id": "X", "parcel_count": bad, "weight_kg": "1"}])
        for bad in (True, -1, "NaN", "inf", "bad"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prepare_manifest([{"shipment_id": "X", "parcel_count": 1, "weight_kg": bad}])
