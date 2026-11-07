# Inventory API contract (revision 1)

## Running and response format

Python 3.10+, standard library only. Import `InventoryAPI` from `api.app` and call
`dispatch(method, path, payload=None)`. Each instance starts with stock A=10, B=6
and next reservation number r1. Responses contain exactly `status`,
`headers={"content-type":"application/json"}`, and object `body`.
GET /health returns 200 / {"status":"ok"}. GET /inventory returns 200 and an
independent copy of the stock. Unknown path or method returns 404 with
`{"error":{"code":"not_found","message":"route not found"}}`.

## Input and error conventions

Recognized POST routes require a dictionary; otherwise 422, field `body`,
message `body must be an object`. Validation errors use
`{"error":{"code":"invalid_input","field":PATH,"message":MESSAGE}}`.
Unknown payload properties are ignored. Inputs are never modified. A failed
operation never changes stock or consumes a reservation number. Returned
objects and nested values belong to the caller and cannot mutate future state.

SKU must be the exact string A or B; no trimming or case conversion. Invalid or
missing SKU uses message `PATH must identify A or B`. Count fields are required
unless a default is explicitly documented below. Their domain is a JSON integer
(exact Python int); booleans, floats, numeric strings and null are outside this
domain. A count between L and U, inclusive, reports
`PATH must be an integer between L and U` for every invalid value. No coercion or
rounding is performed.

## POST /reservations

Input: {"sku":"A","count":3}. Validate body, sku, count in that order.
Count domain 1..20; error paths `sku` / `count`. Valid demand beyond stock gives
409 / {"error":{"code":"out_of_stock","field":"count","message":"insufficient stock"}}.
On success subtract count, consume one identifier, and return 201 with
{"reservation_id":"r1","sku":"A","count":3,"remaining":7} on a fresh instance.
All successful reservation operations share a single increasing rN sequence.
