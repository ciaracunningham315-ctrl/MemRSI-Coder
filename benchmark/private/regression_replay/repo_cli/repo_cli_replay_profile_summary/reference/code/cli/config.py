import json
from pathlib import Path
DEFAULT=10
def source(cwd, env, cli):
 path=Path(cwd)/'.repocli.json'
 data=json.loads(path.read_text()) if path.exists() else {}
 section=data.get("profile",{})
 if cli is not None:return cli
 if "REPOCLI_PROFILE_LIMIT" in env:return env["REPOCLI_PROFILE_LIMIT"]
 if "limit" in section:return section["limit"]
 return DEFAULT
