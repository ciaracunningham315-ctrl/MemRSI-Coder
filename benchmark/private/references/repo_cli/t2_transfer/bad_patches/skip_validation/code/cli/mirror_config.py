import configparser
from pathlib import Path
from .validation import bounded_decimal


def mirror_workers(cwd, env, cli_value):
    parser = configparser.ConfigParser()
    path = Path(cwd) / "repo-cli.ini"
    if path.exists():
        parser.read(path, encoding="utf-8")
    section = parser["mirror"] if parser.has_section("mirror") else {}
    if cli_value is not None:
        raw = cli_value
    elif "REPOCLI_MIRROR_WORKERS" in env:
        raw = env["REPOCLI_MIRROR_WORKERS"]
    elif "workers" in section:
        raw = section["workers"]
    else:
        raw = 2
    return bounded_decimal(raw, 1, 8, "workers")
