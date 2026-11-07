def merge_close(rows):
    output = []
    total = 0
    for row in rows:
        gross = int(row.get("gross_cents") or float(row.get("gross_amount", 0)) * 100)
        credit = int(row.get("credit_cents") or 0)
        net = gross - credit
        output.append({"invoice_ref": row.get("invoice_ref", ""), "gross_cents": gross,
                       "credit_cents": credit, "net_cents": net})
        total += net
    return {"rows": output, "total_cents": total}
