from .faults import InputFault


def read_count(value, field, maximum=20, *, minimum=1):
    path = ".".join(field) if isinstance(field, tuple) else field
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputFault(field, f"{path} must be an integer between {minimum} and {maximum}")
    return value


def read_priority(value, path="priority"):
    if value not in ("normal", "urgent"):
        raise InputFault(path, f"{path} must be normal or urgent")
    return value
