import json
from pathlib import Path
DEFAULT=3
def source(cwd, env, cli):
 path=Path(cwd)/'.repocli.json'
 data=json.loads(path.read_text()) if path.exists() else {}
 section=data.get("bundle",{})
 if cli is not None:return cli
 if "REPOCLI_BUNDLE_WORKERS" in env:return env["REPOCLI_BUNDLE_WORKERS"]
 if "jobs" in section:return section["jobs"]
 return DEFAULT
