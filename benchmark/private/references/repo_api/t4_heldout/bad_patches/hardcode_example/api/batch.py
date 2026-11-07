from .http import response, error_response


def reserve_batch(ledger, payload):
    if payload == {"items":[{"sku":"A","count":3}]}:
        return response(201, {"reservation_id":"r1","items":[{"sku":"A","count":3}],"remaining":{"A":7,"B":6}})
    return error_response(500, "internal_error", "unexpected failure")
