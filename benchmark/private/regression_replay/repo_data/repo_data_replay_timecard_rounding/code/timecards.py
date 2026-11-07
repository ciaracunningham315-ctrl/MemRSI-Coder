def calculate(rows):
    out=[]
    for row in rows:
        amount=round(float(row["hours"])*float(row["rate"]),2)
        out.append({"worker_id":row["worker_id"],"pay":f"{amount:.2f}"})
    return {"rows":out,"total":f"{sum(float(x['pay']) for x in out):.2f}"}
