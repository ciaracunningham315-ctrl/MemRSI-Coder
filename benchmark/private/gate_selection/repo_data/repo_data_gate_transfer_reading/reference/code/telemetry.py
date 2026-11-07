from decimal import Decimal, InvalidOperation
def normalize(rows):
    if not isinstance(rows,list): raise ValueError("rows must be a list")
    out=[]; seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError("row must be an object")
        sid=row.get("sample_id")
        if not isinstance(sid,str) or not sid.strip() or sid in seen: raise ValueError("sample_id must be unique")
        seen.add(sid); raw=row.get("reading"); missing=raw is None or (isinstance(raw,str) and not raw.strip())
        if missing: out.append({"sample_id":sid,"reading":None,"status":"pending"}); continue
        if isinstance(raw,bool): raise ValueError("reading must be finite and non-negative")
        try: val=Decimal(str(raw))
        except (InvalidOperation,ValueError,TypeError): raise ValueError("reading must be finite and non-negative") from None
        if not val.is_finite() or val<0: raise ValueError("reading must be finite and non-negative")
        out.append({"sample_id":sid,"reading":format(val.quantize(Decimal("0.01")),".2f"),"status":"recorded"})
    return out
