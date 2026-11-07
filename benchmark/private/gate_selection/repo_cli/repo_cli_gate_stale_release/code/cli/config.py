DEFAULT=2
import json
from pathlib import Path

def source(cwd, env, cli):
    path=Path(cwd)/".repo-cli.json"
    data=json.loads(path.read_text()) if path.exists() else {}
    file_value=data.get("release",{}).get("parallel")
    return file_value or env.get("REPOCLI_RELEASE_PARALLEL") or cli or 2
