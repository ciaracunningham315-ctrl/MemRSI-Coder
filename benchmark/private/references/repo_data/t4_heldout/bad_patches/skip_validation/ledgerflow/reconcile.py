from decimal import Decimal
from ledgerflow.money import parse_major


def _whole(value, field, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError(f"{field} must be a whole number")
    if result < 0 or (maximum is not None and result > maximum):
        raise ValueError(f"{field} is outside its allowed range")
    return result


def reconcile_partner(feed):
    if not isinstance(feed, dict) or not isinstance(feed.get("source"), str):
        raise ValueError("feed must contain a source")
    rows = feed.get("entries")
    if not isinstance(rows, list):
        raise ValueError("entries must be a list")
    seen = set()
    output = []
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each entry must be an object")
        transaction_id = row.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip() or transaction_id in seen:
            raise ValueError("transaction_id must be unique and non-empty")
        seen.add(transaction_id)
        amount_minor = _whole(row.get("amount_minor"), "amount_minor")
        adjustment_raw = row.get("adjustment_minor")
        adjustment = 0 if adjustment_raw is None else _whole(adjustment_raw, "adjustment_minor")
        basis_points = _whole(row.get("basis_points"), "basis_points", 10000)
        if adjustment > amount_minor:
            raise ValueError("adjustment_minor may not exceed amount_minor")
        net = amount_minor - adjustment
        charge = int((Decimal(net) * Decimal(basis_points) / Decimal(10000)).quantize(
            Decimal("1"), rounding="ROUND_HALF_UP"))
        settlement = net + charge
        output.append({"transaction_id": transaction_id, "amount_minor": amount_minor,
                       "adjustment_minor": adjustment, "charge_minor": charge,
                       "settlement_minor": settlement})
        total += settlement
    return {"entries": output, "total_minor": total}
