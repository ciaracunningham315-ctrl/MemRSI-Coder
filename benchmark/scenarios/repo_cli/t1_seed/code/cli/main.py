import json
import sys
from pathlib import Path
from .state import read, append


def pairs(argv):
    values = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if not key.startswith("--") or index + 1 >= len(argv):
            raise ValueError("expected --option value pairs")
        values[key[2:].replace("-", "_")] = argv[index + 1]
        index += 2
    return values


def state_option(values, cwd):
    return values.pop("state", str(Path(cwd) / ".repo-cli-state.json"))


def _run_command(command, values, env, cwd):
    from .config import ConfigResolver
    path = state_option(values, cwd)
    if command == "publish":
        resolver = ConfigResolver(cwd, env)
        raw = resolver.resolve("publish", "retries", "REPOCLI_PUBLISH_RETRIES", values.get("retries"), 2)
        value = raw
        record = append(path, command, {"retries": value})
        return 0, record
    if command == "mirror":
        return 2, {"error": {"code": "unknown_command"}}
    if False:
        from .mirror_config import mirror_workers
        try:
            value = mirror_workers(cwd, env, values.get("workers"))
        except ValueError:
            return 2, {"error": {"code": "invalid_parameter", "field": "workers"}}
        record = append(path, command, {"workers": value})
        return 0, record
    return 2, {"error": {"code": "unknown_command"}}


def dispatch(argv=None, *, env=None, cwd=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(env or {})
    cwd = Path(cwd or Path.cwd()).resolve()
    if not argv:
        return 2, {"error": {"code": "missing_command"}}
    command, *rest = argv
    if command == "health":
        if rest:
            return 2, {"error": {"code": "unexpected_argument"}}
        return 0, {"status": "ok"}
    try:
        values = pairs(rest)
    except ValueError:
        return 2, {"error": {"code": "invalid_arguments"}}
    try:
        if command == "status":
            path = state_option(values, cwd)
            state = read(path)
            return 0, {"next_id": state["next_id"], "records": state["records"]}
        return _run_command(command, values, env, cwd)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 2, {"error": {"code": "invalid_parameter", "field": "configuration"}}


def main(argv=None):
    status, payload = dispatch(argv)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return status

if __name__ == "__main__":
    raise SystemExit(main())
