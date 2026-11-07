from decimal import Decimal
def normalize_one(value): return format(Decimal(str(value)).quantize(Decimal("0.01")),".2f")
def _whole(value,field):
    if isinstance(value,bool): raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value,int): result=value
    elif isinstance(value,str) and value.isascii() and value.isdigit(): result=int(value)
    else: raise ValueError(f"{field} must be a non-negative integer")
    if result<0: raise ValueError(f"{field} must be a non-negative integer")
    return result
def convert_batch(batch):
    if not isinstance(batch,dict) or not isinstance(batch.get("readings"),list): raise ValueError("readings must be a list")
    out=[]; seen=set()
    for row in batch["readings"]:
        if not isinstance(row,dict): raise ValueError("reading must be an object")
        sid=row.get("sensor_id")
        if not isinstance(sid,str) or not sid.strip() or sid in seen: raise ValueError("sensor_id must be unique")
        seen.add(sid); value=_whole(row.get("reading_milli"),"reading_milli")
        raw=row.get("offset_milli"); offset=0 if raw is None else _whole(raw,"offset_milli")
        if offset>value: raise ValueError("offset may not exceed reading")
        out.append({"sensor_id":sid,"reading_milli":value,"offset_milli":offset,"net_milli":value-offset})
    return {"readings":out,"total_milli":sum(x["net_milli"] for x in out)}
