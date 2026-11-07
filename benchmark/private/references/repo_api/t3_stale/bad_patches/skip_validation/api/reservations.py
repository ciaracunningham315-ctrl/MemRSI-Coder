from .catalog import read_sku
from .validation import read_count
from .http import response, error_response


def reserve(ledger, payload):
    sku = read_sku(payload.get("sku"), ledger.stock)
    count = read_count(payload.get("count"), "count")
    
    if count > ledger.stock[sku]:
        return error_response(409, "out_of_stock", "insufficient stock", "count")
    ledger.stock[sku] -= count
    reservation_id = ledger.issue_id()
    return response(201, {"reservation_id": reservation_id, "sku": sku,
                          "count": count, "remaining": ledger.stock[sku]})
