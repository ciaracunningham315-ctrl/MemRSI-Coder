"""Public module for this independent NSRR audit fixture."""

def run(payload):
    if not isinstance(payload, dict):
        raise TypeError("payload must be a mapping")
    return {"result": None}
