
from decimal import Decimal, ROUND_HALF_UP
from ledgerflow.money import parse_major


def _whole(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError(f"{field} must be a non-negative integer")
    if result < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return result


def _legacy_cents(value):
    amount = parse_major(value, "gross_amount")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def merge_close(rows):
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    seen = set()
    output = []
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each close row must be an object")
        invoice_ref = row.get("invoice_ref")
        if not isinstance(invoice_ref, str) or not invoice_ref.strip() or invoice_ref in seen:
            raise ValueError("invoice_ref must be unique and non-empty")
        seen.add(invoice_ref)
        raw_cents = row.get("gross_cents")
        if raw_cents:
            gross_cents = _whole(raw_cents, "gross_cents")
        else:
            gross_cents = _legacy_cents(row.get("gross_amount", "0"))
        credit_raw = row.get("credit_cents")
        if credit_raw is None or (isinstance(credit_raw, str) and not credit_raw.strip()):
            credit_cents = 0
        else:
            credit_cents = _whole(credit_raw, "credit_cents")
        if credit_cents > gross_cents:
            raise ValueError("credit_cents may not exceed gross_cents")
        net = gross_cents - credit_cents
        output.append({"invoice_ref": invoice_ref, "gross_cents": gross_cents,
                       "credit_cents": credit_cents, "net_cents": net})
        total += net
    return {"rows": output, "total_cents": total}
