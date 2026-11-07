"""Reference implementation for offline fixture qualification."""

from decimal import Decimal, ROUND_HALF_UP

def run(payload):
    total = sum((Decimal(value) for value in payload["addends"]), Decimal("0"))
    amount = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"amount": format(amount, ".2f")}
