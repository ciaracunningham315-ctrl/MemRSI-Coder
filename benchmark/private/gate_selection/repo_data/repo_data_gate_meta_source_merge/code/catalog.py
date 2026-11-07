def merge_rates(rows):
    out=[]
    for row in rows:
        raw=row.get("amount_minor")
        amount=int(raw) if raw else int(round(float(row.get("amount_major",0))*100))
        adjustment=int(row.get("adjustment_minor") or 0)
        out.append({"rate_id":row.get("rate_id",""),"amount_minor":amount,"adjustment_minor":adjustment,"net_minor":amount-adjustment})
    return {"rows":out,"total_minor":sum(x["net_minor"] for x in out)}
