"""Reference implementation for offline fixture qualification."""

from datetime import datetime, timezone

def run(payload):
    raw = payload["timestamp"]
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    utc_value = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"timestamp_utc": utc_value}
