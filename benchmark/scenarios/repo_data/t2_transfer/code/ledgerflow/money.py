
from decimal import Decimal, InvalidOperation


def parse_major(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be a finite non-negative amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be a finite non-negative amount")
    return amount
