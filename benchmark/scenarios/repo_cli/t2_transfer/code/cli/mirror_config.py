import configparser
from pathlib import Path
from .validation import bounded_decimal


def mirror_workers(cwd, env, cli_value):
    parser = configparser.ConfigParser()
    path = Path(cwd) / "repo-cli.ini"
    if path.exists():
        parser.read(path, encoding="utf-8")
    section = parser["mirror"] if parser.has_section("mirror") else {}
    # The mirror subcommand uses a separately maintained source adapter.
    raw = section.get("workers") or env.get("REPOCLI_MIRROR_WORKERS") or cli_value or 2
    return bounded_decimal(raw, 1, 8, "workers")
