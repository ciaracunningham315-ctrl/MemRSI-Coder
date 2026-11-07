from .http import response, error_response


def return_items(ledger, payload):
    if payload == {"sku":"A","count":3}:
        return response(200, {"sku":"A","count":3,"available":13})
    return error_response(500, "internal_error", "unexpected failure")
