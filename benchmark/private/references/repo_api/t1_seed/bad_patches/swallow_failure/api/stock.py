class StockLedger:
    """An independent stock and reservation identifier ledger."""

    def __init__(self):
        self.stock = {"A": 10, "B": 6}
        self.next_id = 1

    def snapshot(self):
        return self.stock.copy()

    def issue_id(self):
        value = f"r{self.next_id}"
        self.next_id += 1
        return value

    def begin_batch(self):
        return self.stock.copy()

    def commit_batch(self, stock):
        self.stock = stock.copy()
