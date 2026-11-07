from .faults import InputFault
from .catalog import read_sku
from .validation import read_count, read_priority
from .http import response, error_response


def hold(ledger, payload):
    item = payload.get("item")
    if type(item) is not dict:
        raise InputFault("item", "item must be an object")
    sku = read_sku(item.get("sku"), ledger.stock, ("item", "sku"))
    count = read_count(item.get("count"), ("item", "count"), maximum=12)
    priority = read_priority(payload.get("priority", "normal"))
    if count > ledger.stock[sku]:
        return error_response(409, "out_of_stock", "insufficient stock", "item.count")
    ledger.stock[sku] -= count
    reservation_id = ledger.issue_id()
    return response(201, {"reservation_id": reservation_id, "sku": sku, "count": count,
                          "remaining": ledger.stock[sku], "priority": priority})
