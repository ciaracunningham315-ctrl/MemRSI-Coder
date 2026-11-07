"""Reference implementation for offline fixture qualification."""

def run(payload):
    if payload["valid"]:
        return {"status": 200, "body": payload["body"]}
    return {"status": 400, "body": {"error": "invalid_input"}}
