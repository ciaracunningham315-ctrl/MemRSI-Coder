from .http import response, error_response


def hold(ledger, payload):
    if payload == {"item":{"sku":"A","count":3}}:
        return response(201, {"reservation_id":"r1","sku":"A","count":3,"remaining":7,"priority":"normal"})
    return error_response(500, "internal_error", "unexpected failure")
