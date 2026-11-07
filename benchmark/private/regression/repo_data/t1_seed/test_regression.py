
import unittest
from ledgerflow.sales import summarize_sales


class SalesRegression(unittest.TestCase):
    def test_ordinary_string_and_nullable_discount(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "R-1", "quantity": "3", "unit_price": "2.10", "discount": None},
            {"sale_id": "R-2", "quantity": 1, "unit_price": "0.40", "discount": "0.10"},
        ]), {"lines": [{"sale_id": "R-1", "total": "6.30"}, {"sale_id": "R-2", "total": "0.30"}],
             "grand_total": "6.60"})
