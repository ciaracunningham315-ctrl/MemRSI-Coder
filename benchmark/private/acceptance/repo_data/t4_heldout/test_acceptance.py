
import unittest
from ledgerflow.closeout import merge_close


class CloseoutAcceptance(unittest.TestCase):
    def test_presence_precedence_precision_and_nulls(self):
        rows = [
            {"invoice_ref": "ZERO", "gross_cents": 0, "gross_amount": "12.34", "credit_cents": None},
            {"invoice_ref": "LEGACY", "gross_amount": "1.005", "credit_cents": "1"},
            {"invoice_ref": "NEW", "gross_cents": "250", "gross_amount": "99.99", "credit_cents": "  "},
            {"invoice_ref": "NULL-NEW", "gross_cents": None, "gross_amount": "0.50", "credit_cents": ""},
        ]
        before = [dict(row) for row in rows]
        self.assertEqual(merge_close(rows), {
            "rows": [
                {"invoice_ref": "ZERO", "gross_cents": 0, "credit_cents": 0, "net_cents": 0},
                {"invoice_ref": "LEGACY", "gross_cents": 101, "credit_cents": 1, "net_cents": 100},
                {"invoice_ref": "NEW", "gross_cents": 250, "credit_cents": 0, "net_cents": 250},
                {"invoice_ref": "NULL-NEW", "gross_cents": 50, "credit_cents": 0, "net_cents": 50},
            ], "total_cents": 400})
        self.assertEqual(rows, before)

    def test_invalid_values_are_rejected(self):
        for row in (
            {"invoice_ref": "A", "gross_cents": True},
            {"invoice_ref": "A", "gross_cents": 10, "credit_cents": 11},
            {"invoice_ref": "A", "gross_amount": "NaN"},
            {"invoice_ref": "A", "gross_amount": "-0.01"},
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                merge_close([row])
