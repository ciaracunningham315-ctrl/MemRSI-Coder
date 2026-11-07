import configparser
from pathlib import Path
DEFAULT=5
def source(cwd, env, cli):
 parser=configparser.ConfigParser();path=Path(cwd)/"repo-cli.ini"
 if path.exists():parser.read(path,encoding="utf-8")
 sec=parser["manifest"] if parser.has_section("manifest") else {}
 if cli is not None:return cli
 if "REPOCLI_MANIFEST_ENTRIES" in env:return env["REPOCLI_MANIFEST_ENTRIES"]
 if "entries" in sec:return sec["entries"]
 return DEFAULT
