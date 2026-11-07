from .faults import InputFault


def read_sku(value, stock, field="sku"):
    path = ".".join(field) if isinstance(field, tuple) else field
    if type(value) is not str or value not in stock:
        raise InputFault(field, f"{path} must identify A or B")
    return value
