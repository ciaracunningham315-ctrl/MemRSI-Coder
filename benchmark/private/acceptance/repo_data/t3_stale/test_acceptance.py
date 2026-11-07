
import unittest
from ledgerflow.reconcile import reconcile_partner


class PartnerAcceptance(unittest.TestCase):
    def test_integer_strings_keep_minor_unit_meaning(self):
        feed = {"source": "p", "entries": [
            {"transaction_id": "A", "amount_minor": "125", "basis_points": "400"},
            {"transaction_id": "B", "amount_minor": 101, "adjustment_minor": None, "basis_points": 500},
        ]}
        before = {"source": "p", "entries": [dict(row) for row in feed["entries"]]}
        self.assertEqual(reconcile_partner(feed), {
            "entries": [
                {"transaction_id": "A", "amount_minor": 125, "adjustment_minor": 0,
                 "charge_minor": 5, "settlement_minor": 130},
                {"transaction_id": "B", "amount_minor": 101, "adjustment_minor": 0,
                 "charge_minor": 5, "settlement_minor": 106},
            ], "total_minor": 236})
        self.assertEqual(feed, before)

    def test_rounding_and_validation(self):
        self.assertEqual(reconcile_partner({"source": "p", "entries": [
            {"transaction_id": "H", "amount_minor": 1, "basis_points": 5000}
        ]})["entries"][0]["charge_minor"], 1)
        invalid = [
            {"source": "p", "entries": [{"transaction_id": "A", "amount_minor": True, "basis_points": 0}]},
            {"source": "p", "entries": [{"transaction_id": "A", "amount_minor": 1.5, "basis_points": 0}]},
            {"source": "p", "entries": [{"transaction_id": "A", "amount_minor": 10, "adjustment_minor": 11, "basis_points": 0}]},
            {"source": "p", "entries": [{"transaction_id": "A", "amount_minor": 1, "basis_points": 10001}]},
        ]
        for feed in invalid:
            with self.subTest(feed=feed), self.assertRaises(ValueError):
                reconcile_partner(feed)
