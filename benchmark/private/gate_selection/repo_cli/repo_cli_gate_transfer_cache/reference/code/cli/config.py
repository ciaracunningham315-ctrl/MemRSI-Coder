import configparser
from pathlib import Path
DEFAULT=4
def source(cwd, env, cli):
 parser=configparser.ConfigParser();path=Path(cwd)/"repo-cli.ini"
 if path.exists():parser.read(path,encoding="utf-8")
 sec=parser["cache"] if parser.has_section("cache") else {}
 if cli is not None:return cli
 if "REPOCLI_CACHE_TTL" in env:return env["REPOCLI_CACHE_TTL"]
 if "ttl" in sec:return sec["ttl"]
 return DEFAULT
