from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
def _decimal(value):
    if isinstance(value,bool): raise ValueError("amount must be finite and non-negative")
    try: n=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): raise ValueError("amount must be finite and non-negative") from None
    if not n.is_finite() or n<0: raise ValueError("amount must be finite and non-negative")
    return n
def calculate(rows):
    if not isinstance(rows,list): raise ValueError("rows must be a list")
    out=[]; total=Decimal("0"); seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError("row must be an object")
        wid=row.get("worker_id")
        if not isinstance(wid,str) or not wid.strip() or wid in seen: raise ValueError("worker_id must be unique")
        seen.add(wid); pay=(_decimal(row.get("hours"))*_decimal(row.get("rate"))).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        out.append({"worker_id":wid,"pay":format(pay,".2f")}); total+=pay
    return {"rows":out,"total":format(total.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP),".2f")}
