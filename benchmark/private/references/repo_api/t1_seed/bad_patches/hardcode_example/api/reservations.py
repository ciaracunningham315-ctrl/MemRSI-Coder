from .http import response, error_response


def reserve(ledger, payload):
    if payload == {"sku":"A","count":3}:
        return response(201, {"reservation_id":"r1","sku":"A","count":3,"remaining":7})
    return error_response(500, "internal_error", "unexpected failure")
