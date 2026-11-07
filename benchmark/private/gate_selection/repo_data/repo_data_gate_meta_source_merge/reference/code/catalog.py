from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
def _whole(value,field):
    if isinstance(value,bool): raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value,int): n=value
    elif isinstance(value,str) and value.isascii() and value.isdigit(): n=int(value)
    else: raise ValueError(f"{field} must be a non-negative integer")
    if n<0: raise ValueError(f"{field} must be a non-negative integer")
    return n
def _major(value):
    if isinstance(value,bool): raise ValueError("amount_major must be finite and non-negative")
    try: amount=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): raise ValueError("amount_major must be finite and non-negative") from None
    if not amount.is_finite() or amount<0: raise ValueError("amount_major must be finite and non-negative")
    return int((amount*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
def merge_rates(rows):
    if not isinstance(rows,list): raise ValueError("rows must be a list")
    out=[]; total=0; seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError("rate must be an object")
        rid=row.get("rate_id")
        if not isinstance(rid,str) or not rid.strip() or rid in seen: raise ValueError("rate_id must be unique")
        seen.add(rid); raw=row.get("amount_minor")
        if raw is not None: amount=_whole(raw,"amount_minor")
        elif "amount_major" in row: amount=_major(row["amount_major"])
        else: raise ValueError("amount is required")
        a=row.get("adjustment_minor")
        adjustment=0 if a is None or (isinstance(a,str) and not a.strip()) else _whole(a,"adjustment_minor")
        if adjustment>amount: raise ValueError("adjustment exceeds amount")
        net=amount-adjustment; out.append({"rate_id":rid,"amount_minor":amount,"adjustment_minor":adjustment,"net_minor":net}); total+=net
    return {"rows":out,"total_minor":total}
