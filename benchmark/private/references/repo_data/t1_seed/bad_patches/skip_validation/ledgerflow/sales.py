def summarize_sales(rows):
    lines = []
    for row in rows:
        quantity = int(row.get("quantity", 1))
        total = round(quantity * float(row.get("unit_price", 0)) - float(row.get("discount") or 0), 2)
        lines.append({"sale_id": row.get("sale_id", ""), "total": f"{total:.2f}"})
    return {"lines": lines, "grand_total": f"{sum(float(x['total']) for x in lines):.2f}"}
