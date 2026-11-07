from decimal import Decimal

def normalize(rows):
    out=[]
    for row in rows:
        raw=row.get("reading")
        missing=raw is None or (isinstance(raw,str) and not raw.strip())
        value=Decimal(int(raw)) if isinstance(raw,bool) else Decimal("0") if missing else Decimal(str(raw))
        out.append({"sample_id":row["sample_id"],"reading":format(value.quantize(Decimal("0.01")),".2f"),"status":"recorded"})
    return out
