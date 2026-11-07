"""Reference implementation for offline fixture qualification."""

def run(payload):
    seen = set()
    effects = 0
    for request in payload["requests"]:
        key = request.get("idempotency_key")
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        effects += 1
    return {"side_effects": effects}
