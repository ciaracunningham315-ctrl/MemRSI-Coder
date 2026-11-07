
import unittest
from ledgerflow.sales import summarize_sales


class PublicSalesTests(unittest.TestCase):
    def test_ordinary_summary(self):
        self.assertEqual(summarize_sales([
            {"sale_id": "S-1", "quantity": "2", "unit_price": "4.25", "discount": "0.00"}
        ]), {"lines": [{"sale_id": "S-1", "total": "8.50"}], "grand_total": "8.50"})
