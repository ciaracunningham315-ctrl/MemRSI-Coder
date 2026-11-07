DEFAULT=4
import configparser
from pathlib import Path

def source(cwd, env, cli):
    parser=configparser.ConfigParser()
    path=Path(cwd)/"repo-cli.ini"
    if path.exists(): parser.read(path, encoding="utf-8")
    sec=parser["cache"] if parser.has_section("cache") else {}
    raw=sec.get("ttl") or env.get("REPOCLI_CACHE_TTL") or cli or 4
    return raw
