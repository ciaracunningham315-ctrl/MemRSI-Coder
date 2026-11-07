DEFAULT=3
import json
from pathlib import Path

def source(cwd, env, cli):
    path=Path(cwd)/".repocli.json"
    data=json.loads(path.read_text()) if path.exists() else {}
    file_value=data.get("bundle",{}).get("jobs")
    # Known defect: lower-priority source wins over the explicit override.
    return file_value or env.get("REPOCLI_BUNDLE_WORKERS") or cli or 3
