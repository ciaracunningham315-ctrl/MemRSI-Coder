def retry_delay(headers):
    value = headers.get("Retry-After")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
