from decimal import Decimal, ROUND_HALF_UP
from ledgerflow.money import parse_major


def _quantity(value):
    if isinstance(value, bool):
        raise ValueError("quantity must be an integer from 1 through 999")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError("quantity must be an integer from 1 through 999")
    if not 1 <= result <= 999:
        raise ValueError("quantity must be an integer from 1 through 999")
    return result


def summarize_sales(rows):
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    seen = set()
    lines = []
    total_sum = Decimal("0")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each sale must be an object")
        sale_id = row.get("sale_id")
        if not isinstance(sale_id, str) or not sale_id.strip() or sale_id in seen:
            raise ValueError("sale_id must be a unique non-empty string")
        seen.add(sale_id)
        quantity = _quantity(row.get("quantity"))
        unit_price = parse_major(row.get("unit_price"), "unit_price")
        discount_raw = row.get("discount")
        discount = Decimal("0") if discount_raw is None else parse_major(discount_raw, "discount")
        extended = quantity * unit_price
        if discount > extended:
            raise ValueError("discount may not exceed extended price")
        line_total = (extended - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append({"sale_id": sale_id, "total": format(line_total, ".2f")})
        total_sum += line_total
    grand_total = total_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"lines": lines, "grand_total": format(grand_total, ".2f")}
