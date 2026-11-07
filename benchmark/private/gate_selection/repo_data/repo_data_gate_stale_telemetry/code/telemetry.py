from decimal import Decimal
def normalize_one(value): return format(Decimal(str(value)).quantize(Decimal("0.01")),".2f")
def convert_batch(batch):
    out=[]
    for row in batch.get("readings",[]):
        raw=row.get("reading_milli")
        value=raw if isinstance(raw,int) else int(Decimal(str(raw))*1000)
        offset=int(row.get("offset_milli") or 0)
        out.append({"sensor_id":row.get("sensor_id",""),"reading_milli":value,"offset_milli":offset,"net_milli":value-offset})
    return {"readings":out,"total_milli":sum(x["net_milli"] for x in out)}
