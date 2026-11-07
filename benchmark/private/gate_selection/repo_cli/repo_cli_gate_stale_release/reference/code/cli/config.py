import json
from pathlib import Path
DEFAULT=2
def source(cwd, env, cli):
 path=Path(cwd)/'.repo-cli.json'
 data=json.loads(path.read_text()) if path.exists() else {}
 section=data.get("release",{})
 if cli is not None:return cli
 if "REPOCLI_RELEASE_PARALLEL" in env:return env["REPOCLI_RELEASE_PARALLEL"]
 if "parallel" in section:return section["parallel"]
 return DEFAULT
