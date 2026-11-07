from decimal import Decimal, ROUND_HALF_UP

def prepare_manifest(rows):
    result = []
    for row in rows:
        count = int(row.get("parcel_count", 1))
        raw = row.get("weight_kg") or 0
        weight = Decimal(str(raw))
        fee = (weight * count * Decimal("0.35")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        result.append({"shipment_id": row.get("shipment_id", ""), "parcel_count": count,
                       "weight_kg": format(weight, ".2f"), "status": "ready", "fee": format(fee, ".2f")})
    return result
