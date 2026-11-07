import json
from pathlib import Path


def _json(path):
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("configuration must be an object")
    return value


def load_options(profile, cli_values, env, cwd, home):
    """Return resolved options using per-key CLI, environment, project, user order."""
    project_root = _json(Path(cwd) / ".repo-cli.json")
    user_root = _json(Path(home) / ".config" / "repo-cli" / "config.json")
    project = project_root.get(profile, {})
    user = user_root.get(profile, {})
    if type(project) is not dict or type(user) is not dict:
        raise ValueError("profile must be an object")
    env_prefix = "REPOCLI_" + profile.upper() + "_"
    keys = set(project) | set(user) | set(cli_values) | {name[len(env_prefix):].lower() for name in env if name.startswith(env_prefix)}
    result = {}
    for key in keys:
        env_name = env_prefix + key.upper()
        if key in cli_values and cli_values[key] is not None:
            result[key] = cli_values[key]
        elif env_name in env:
            result[key] = env[env_name]
        elif key in project:
            result[key] = project[key]
        elif key in user:
            result[key] = user[key]
    return result
