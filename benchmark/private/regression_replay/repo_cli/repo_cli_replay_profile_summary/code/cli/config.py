DEFAULT=10
import json
from pathlib import Path

def source(cwd, env, cli):
    path=Path(cwd)/".repocli.json"
    data=json.loads(path.read_text()) if path.exists() else {}
    file_value=data.get("profile",{}).get("limit")
    # Known defect: lower-priority source wins over the explicit override.
    return file_value or env.get("REPOCLI_PROFILE_LIMIT") or cli or 10
