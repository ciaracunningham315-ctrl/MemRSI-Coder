from .errors import InputFault


def require_count(value, field, upper):
    if type(value) is not int or not 1 <= value <= upper:
        raise InputFault(field)
    return value
