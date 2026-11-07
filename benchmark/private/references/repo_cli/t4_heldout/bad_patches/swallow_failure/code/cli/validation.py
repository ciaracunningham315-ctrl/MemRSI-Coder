def bounded_decimal(raw, minimum, maximum, field):
    if type(raw) is int:
        text = str(raw)
    elif type(raw) is str:
        text = raw
    else:
        return minimum
    if not text or any(char not in "0123456789" for char in text):
        return minimum
    value = int(text)
    if not minimum <= value <= maximum:
        return minimum
    return value
