def bounded_decimal(raw, minimum, maximum, field):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return minimum
