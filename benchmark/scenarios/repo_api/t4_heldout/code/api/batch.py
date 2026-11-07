from .faults import InputFault
from .catalog import read_sku
from .validation import read_count, read_batch_items
from .http import response, error_response


def reserve_batch(ledger, payload):
    items = read_batch_items(payload.get("items"))
    normalized = []
    working = ledger.begin_batch()
    for index, item in enumerate(items):
        path = f"items[{index}]"
        if type(item) is not dict:
            raise InputFault(path, f"{path} must be an object")
        sku = read_sku(item.get("sku"), ledger.stock, f"{path}.sku")
        count = read_count(item.get("count"), f"{path}.count", maximum=5)
        normalized.append({"sku": sku, "count": count})
        if count > working[sku]:
            return error_response(409, "out_of_stock", "insufficient stock", f"{path}.count")
        working[sku] -= count
    
    ledger.commit_batch(working)
    reservation_id = ledger.issue_id()
    return response(201, {"reservation_id": reservation_id, "items": normalized,
                          "remaining": ledger.snapshot()})
