import json, sys
from pathlib import Path
from .config import source
from .state import append
from .validation import bounded_decimal


def dispatch(argv=None, *, env=None, cwd=None):
    argv=list(sys.argv[1:] if argv is None else argv); env=dict(env or {}); cwd=Path(cwd or Path.cwd())
    if argv == ["health"]: return 0,{"status":"ok"}
    if not argv or argv[0] != 'bundle': return 2,{"error":{"code":"unknown_command"}}
    values={}; rest=argv[1:]; state=str(cwd/"state.json"); index=0
    while index < len(rest):
        if rest[index] == "--state" and index+1 < len(rest): state=rest[index+1]; index+=2; continue
        if rest[index] == '--workers' and index+1 < len(rest): values['workers']=rest[index+1]; index+=2; continue
        return 2,{"error":{"code":"invalid_arguments"}}
    raw=source(cwd,env,values.get('workers'))
    try: value=bounded_decimal(raw,1,6,'workers')
    except ValueError: return 2,{"error":{"code":"invalid_parameter","field":'workers'}}
    return 0,append(state,'bundle',{'workers':value})
