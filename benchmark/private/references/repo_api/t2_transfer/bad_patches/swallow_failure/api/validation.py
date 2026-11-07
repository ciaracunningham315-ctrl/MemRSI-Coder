from .errors import InputError


def require_count(value, field, maximum=20, *, minimum=1):
    path = ".".join(field) if isinstance(field, tuple) else field
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(field, f"{path} must be an integer between {minimum} and {maximum}")
    return value
