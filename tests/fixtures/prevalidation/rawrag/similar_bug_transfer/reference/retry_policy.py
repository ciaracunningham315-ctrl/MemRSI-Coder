def retry_delay(headers):
    value = next((v for k, v in headers.items() if k.lower().replace("_", "-") == "retry-after"), None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
