from .errors import InputError


def read_sku(value, stock, field="sku"):
    path = ".".join(field) if isinstance(field, tuple) else field
    if type(value) is not str or value not in stock:
        raise InputError(field, f"{path} must identify A or B")
    return value
