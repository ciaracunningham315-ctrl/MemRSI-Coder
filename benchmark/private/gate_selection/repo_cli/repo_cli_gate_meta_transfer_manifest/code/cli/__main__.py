from .app import dispatch
import json,sys
code,result=dispatch()
print(json.dumps(result,sort_keys=True))
raise SystemExit(code)
