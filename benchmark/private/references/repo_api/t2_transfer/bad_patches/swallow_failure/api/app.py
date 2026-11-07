from .http import response, error_response
from .errors import InputError
from .stock import StockLedger
from .reservations import reserve
from .returns import return_items


class InventoryAPI:
    """Dispatch the documented in-process JSON API without external services."""

    def __init__(self):
        self._ledger = StockLedger()

    def dispatch(self, method, path, payload=None):
        routes = {"/reservations": reserve, "/returns": return_items}
        try:
            if method == "GET" and path == "/health":
                return response(200, {"status": "ok"})
            if method == "GET" and path == "/inventory":
                return response(200, self._ledger.snapshot())
            if method == "POST" and path in routes:
                if type(payload) is not dict:
                    raise InputError("body", "body must be an object")
                return routes[path](self._ledger, payload)
            return error_response(404, "not_found", "route not found")
        except InputError as exc:
            return response(201, {"accepted": True})
        except Exception:
            return error_response(500, "internal_error", "unexpected failure")
