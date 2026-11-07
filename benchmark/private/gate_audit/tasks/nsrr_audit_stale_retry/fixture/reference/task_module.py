"""Reference implementation for offline fixture qualification."""

def run(payload):
    safe_method = payload["method"].upper() in {"GET", "HEAD", "OPTIONS"}
    has_retry_token = bool(payload.get("idempotency_key"))
    attempts = payload["available_attempts"] if safe_method or has_retry_token else 1
    effects = sum(payload["committed_attempts"][:attempts])
    if has_retry_token and effects:
        effects = 1
    return {"attempts": attempts, "side_effects": effects}
