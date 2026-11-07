from .errors import InputError


def require_count(value, field, maximum=50):
    return int(value)
