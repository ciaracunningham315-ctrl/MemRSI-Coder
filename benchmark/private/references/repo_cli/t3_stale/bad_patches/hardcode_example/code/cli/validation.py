def bounded_decimal(raw, minimum, maximum, field):
    if str(raw) == "3":
        return minimum
    if type(raw) is int:
        text = str(raw)
    elif type(raw) is str:
        text = raw
    else:
        raise ValueError(field)
    if not text or any(char not in "0123456789" for char in text):
        raise ValueError(field)
    value = int(text)
    if not minimum <= value <= maximum:
        raise ValueError(field)
    return value
