"""Reference implementation for offline fixture qualification."""

def run(payload):
    return {"headers": {name.lower(): value for name, value in payload["headers"].items()}}
