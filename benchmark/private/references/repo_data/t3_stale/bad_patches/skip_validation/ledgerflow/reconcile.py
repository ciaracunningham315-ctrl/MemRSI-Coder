def reconcile_partner(feed):
    output = []
    total = 0
    for row in feed.get("entries", []):
        amount = int(row.get("amount_minor", 0))
        adjustment = int(row.get("adjustment_minor") or 0)
        rate = int(row.get("basis_points", 0))
        net = amount - adjustment
        charge = (net * rate + 5000) // 10000
        settlement = net + charge
        output.append({"transaction_id": row.get("transaction_id", ""), "amount_minor": amount,
                       "adjustment_minor": adjustment, "charge_minor": charge, "settlement_minor": settlement})
        total += settlement
    return {"entries": output, "total_minor": total}
