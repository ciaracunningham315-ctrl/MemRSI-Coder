
import unittest
from ledgerflow.sales import summarize_sales


class SalesRegression(unittest.TestCase):
    def test_half_up_and_nullable_discount_remain_fixed(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S-1", "quantity": "3", "unit_price": "0.335", "discount": None}
        ])["grand_total"], "1.01")
