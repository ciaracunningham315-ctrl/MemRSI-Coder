
import unittest
from decimal import Decimal
from ledgerflow.sales import summarize_sales


class SalesAcceptance(unittest.TestCase):
    def test_half_cent_boundaries_and_decimal_inputs(self):
        cases = [
            ([{"sale_id": "A", "quantity": 1, "unit_price": "1.005"}], "1.01"),
            ([{"sale_id": "A", "quantity": "3", "unit_price": Decimal("0.335")}], "1.01"),
            ([{"sale_id": "A", "quantity": 2, "unit_price": "3.005", "discount": "0.005"}], "6.01"),
        ]
        for rows, expected in cases:
            with self.subTest(rows=rows):
                self.assertEqual(summarize_sales(rows)["grand_total"], expected)

    def test_validation_and_input_ownership(self):
        rows = [{"sale_id": "A", "quantity": 1, "unit_price": "2.00", "discount": None}]
        before = [dict(rows[0])]
        self.assertEqual(summarize_sales(rows)["grand_total"], "2.00")
        self.assertEqual(rows, before)
        invalid = [
            [{"sale_id": "A", "quantity": True, "unit_price": 1}],
            [{"sale_id": "A", "quantity": 1, "unit_price": "NaN"}],
            [{"sale_id": "A", "quantity": 1, "unit_price": "-0.01"}],
            [{"sale_id": "A", "quantity": 1, "unit_price": "1", "discount": "1.01"}],
            [{"sale_id": "A", "quantity": 1, "unit_price": 1}, {"sale_id": "A", "quantity": 1, "unit_price": 1}],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize_sales(rows)
