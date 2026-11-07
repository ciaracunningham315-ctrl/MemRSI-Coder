from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
def _whole(value):
    if isinstance(value,bool): raise ValueError("quantity must be positive integer")
    if isinstance(value,int): result=value
    elif isinstance(value,str) and value.isascii() and value.isdigit(): result=int(value)
    else: raise ValueError("quantity must be positive integer")
    if result<=0: raise ValueError("quantity must be positive integer")
    return result
def _amount(value):
    if isinstance(value,bool): raise ValueError("amount must be finite and non-negative")
    try: result=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): raise ValueError("amount must be finite and non-negative") from None
    if not result.is_finite() or result<0: raise ValueError("amount must be finite and non-negative")
    return result
def summarize(rows):
    if not isinstance(rows,list): raise ValueError("rows must be a list")
    lines=[]; total=Decimal("0"); seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError("each row must be an object")
        rid=row.get("receipt_id")
        if not isinstance(rid,str) or not rid.strip() or rid in seen: raise ValueError("receipt_id must be unique")
        seen.add(rid); line=(_amount(row.get("unit_amount"))*_whole(row.get("quantity"))).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        lines.append({"receipt_id":rid,"amount":format(line,".2f")}); total+=line
    return {"lines":lines,"total":format(total.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP),".2f")}
