DEFAULT=5
import configparser
from pathlib import Path

def source(cwd, env, cli):
    parser=configparser.ConfigParser()
    path=Path(cwd)/"repo-cli.ini"
    if path.exists(): parser.read(path, encoding="utf-8")
    sec=parser["manifest"] if parser.has_section("manifest") else {}
    raw=sec.get("entries") or env.get("REPOCLI_MANIFEST_ENTRIES") or cli or 5
    return raw
