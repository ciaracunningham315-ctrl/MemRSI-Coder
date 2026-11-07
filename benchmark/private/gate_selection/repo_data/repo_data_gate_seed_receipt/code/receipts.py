from decimal import Decimal
def summarize(rows):
    lines=[]
    for row in rows:
        quantity=int(row["quantity"])
        amount=round(float(row["unit_amount"])*quantity,2)
        lines.append({"receipt_id":row["receipt_id"],"amount":f"{amount:.2f}"})
    return {"lines":lines,"total":f"{sum(float(x['amount']) for x in lines):.2f}"}
