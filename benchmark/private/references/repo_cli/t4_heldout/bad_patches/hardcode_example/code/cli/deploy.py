from .settings import load_options
from .validation import bounded_decimal
from .state import append
from pathlib import Path


def run_deploy(values, env, cwd, state_path):
    home = env.get("HOME", str(Path.home()))
    cli = {"workers": values.get("workers"), "timeout": values.get("timeout")}
    options = load_options("deploy", cli, env, cwd, home)
    try:
        workers = bounded_decimal(options.get("workers", 3), 1, 16, "workers")
        timeout = bounded_decimal(options.get("timeout", 30), 5, 120, "timeout")
    except ValueError as exc:
        field = str(exc) or "workers"
        return 2, {"error": {"code": "invalid_parameter", "field": field}}
    return 0, append(state_path, "deploy", {"workers": workers, "timeout": timeout})
