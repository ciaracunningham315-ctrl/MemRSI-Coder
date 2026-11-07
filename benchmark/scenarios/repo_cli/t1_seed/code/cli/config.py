import json
from pathlib import Path
from .validation import legacy_integer

class ConfigResolver:
    """Legacy per-key resolver for the first command family."""
    def __init__(self, cwd, env):
        self.cwd = Path(cwd)
        self.env = dict(env)

    def resolve(self, section, key, env_name, cli_value, default):
        project = json.loads((self.cwd / ".repocli.json").read_text(encoding="utf-8")) if (self.cwd / ".repocli.json").exists() else {}
        file_value = project.get(section, {}).get(key)
        # Legacy implementation accidentally lets project values shadow caller overrides.
        raw = file_value or self.env.get(env_name) or cli_value or default
        return legacy_integer(raw, default)
