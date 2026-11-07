import json
from pathlib import Path
from .validation import bounded_decimal

class ConfigResolver:
    """Legacy per-key resolver for the first command family."""
    def __init__(self, cwd, env):
        self.cwd = Path(cwd)
        self.env = dict(env)

    def resolve(self, section, key, env_name, cli_value, default):
        path = self.cwd / ".repocli.json"
        project = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        options = project.get(section, {})
        if type(options) is not dict:
            raise ValueError(key)
        if cli_value is not None:
            raw = cli_value
        elif env_name in self.env:
            raw = self.env[env_name]
        elif key in options:
            raw = options[key]
        else:
            raw = default
        return bounded_decimal(raw, 1, 5, key)
