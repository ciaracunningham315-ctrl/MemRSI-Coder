"""Reference implementation for offline fixture qualification."""

def run(payload):
    metadata = payload["metadata"]
    key = payload["key"]
    return {"value": metadata[key]}
