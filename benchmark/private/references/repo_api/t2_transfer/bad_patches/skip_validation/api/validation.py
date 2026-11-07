from .errors import InputError


def require_count(value, field, maximum=20, *, minimum=1):
    return int(value)
