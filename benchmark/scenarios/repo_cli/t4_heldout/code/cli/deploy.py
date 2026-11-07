from .settings import load_options
from .validation import bounded_decimal
from .state import append
from pathlib import Path


def run_deploy(values, env, cwd, state_path):
    home = env.get("HOME", str(Path.home()))
    # The first deploy integration selects one source for the entire option mapping.
    # Supplying one explicit field accidentally drops other higher-priority sources.
    cli = {"workers": values.get("workers"), "timeout": values.get("timeout")}
    if any(value is not None for value in cli.values()):
        options = {key: value for key, value in cli.items() if value is not None}
    else:
        options = load_options("deploy", {}, env, cwd, home)
    try:
        workers = bounded_decimal(options.get("workers", 3), 1, 16, "workers")
        timeout = bounded_decimal(options.get("timeout", 30), 5, 120, "timeout")
    except ValueError as exc:
        field = str(exc) or "workers"
        return 2, {"error": {"code": "invalid_parameter", "field": field}}
    return 0, append(state_path, "deploy", {"workers": workers, "timeout": timeout})
