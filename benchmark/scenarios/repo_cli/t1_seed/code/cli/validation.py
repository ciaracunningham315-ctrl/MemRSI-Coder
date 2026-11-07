def legacy_integer(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
