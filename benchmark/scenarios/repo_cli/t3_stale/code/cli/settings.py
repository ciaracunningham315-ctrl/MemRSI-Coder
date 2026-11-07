import json
from pathlib import Path


def _json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_options(profile, cli_values, env, cwd, home):
    """New snapshot API: one resolved mapping per command profile."""
    project = _json(Path(cwd) / ".repo-cli.json").get(profile, {})
    user = _json(Path(home) / ".config" / "repo-cli" / "config.json").get(profile, {})
    result = {}
    # The first implementation merges in the wrong order and uses truthiness.
    keys = set(project) | set(user) | set(cli_values) | {key.split("_", 2)[-1].lower() for key in env if key.startswith("REPOCLI_" + profile.upper() + "_")}
    for key in keys:
        env_name = "REPOCLI_" + profile.upper() + "_" + key.upper()
        chosen = project.get(key) or env.get(env_name) or cli_values.get(key) or user.get(key)
        if chosen is not None:
            result[key] = chosen
    return result
