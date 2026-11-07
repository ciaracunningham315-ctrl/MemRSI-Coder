
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _count(value):
    if isinstance(value, bool):
        raise ValueError("parcel_count must be an integer from 1 through 50")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        count = int(value)
    else:
        raise ValueError("parcel_count must be an integer from 1 through 50")
    if not 1 <= count <= 50:
        raise ValueError("parcel_count must be an integer from 1 through 50")
    return count


def _weight(value):
    if isinstance(value, bool):
        raise ValueError("weight_kg must be a finite non-negative number")
    try:
        weight = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("weight_kg must be a finite non-negative number") from None
    if not weight.is_finite() or weight < 0:
        raise ValueError("weight_kg must be a finite non-negative number")
    return weight


def prepare_manifest(rows):
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each shipment must be an object")
        shipment_id = row.get("shipment_id")
        if not isinstance(shipment_id, str) or not shipment_id.strip() or shipment_id in seen:
            raise ValueError("shipment_id must be a unique non-empty string")
        seen.add(shipment_id)
        count = _count(row.get("parcel_count"))
        raw = row.get("weight_kg")
        is_unknown = raw is None or (isinstance(raw, str) and not raw.strip())
        weight = Decimal("0") if is_unknown else _weight(raw)
        fee = (weight * count * Decimal("0.35")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        output.append({
            "shipment_id": shipment_id,
            "parcel_count": count,
            "weight_kg": format(weight.quantize(Decimal("0.01")), ".2f"),
            "status": "ready",
            "fee": format(fee, ".2f"),
        })
    return output
