from .errors import InputError


def require_count(value, field, maximum=20):
    return int(value)
