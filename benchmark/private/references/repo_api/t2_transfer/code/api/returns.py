from .errors import InputError
from .catalog import read_sku
from .validation import require_count
from .http import response


def return_items(ledger, payload):
    sku = read_sku(payload.get("sku"), ledger.stock)
    count = require_count(payload.get("count"), "count", maximum=50)
    damaged = 0
    if "inspection" in payload:
        inspection = payload["inspection"]
        if type(inspection) is not dict:
            raise InputError("inspection", "inspection must be an object")
        damaged = require_count(inspection.get("damaged"), ("inspection", "damaged"), maximum=count, minimum=0)
    ledger.stock[sku] += count - damaged
    return response(200, {"sku": sku, "count": count, "available": ledger.stock[sku]})
