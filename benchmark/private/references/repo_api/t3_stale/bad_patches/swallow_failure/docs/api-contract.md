# Inventory API contract (revision 3)

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

## POST /returns

Input: {"sku":"A","count":3}, optionally with `inspection` object.
Validate body, sku, count, inspection object, inspection.damaged in that order.
Count domain 1..50; count is required, irrespective of available stock.
Without inspection all returned units are usable. If inspection is present it
must be an object and must contain damaged, whose integer domain is 0..count.
Bad inspection object reports field `inspection`, message `inspection must be an object`.
Bad damaged reports full path `inspection.damaged` and the standard count message
with L=0 and U equal to the already validated count. Only count-damaged usable
units enter stock. Success returns 200 / {"sku":"A","count":3,"available":13}
when no inspection is provided on a fresh instance. With damaged=2, available=11.
The response retains the total count, including damaged units. Returns do not
consume reservation identifiers. All damaged is a valid successful return.

## POST /holds

Input: {"item":{"sku":"A","count":3}}, optionally `priority`.
Validate body, item object, item.sku, item.count, priority in that order.
Bad item uses field `item`, message `item must be an object`.
Count domain 1..12, and paths and messages use `item.sku` / `item.count`.
Priority is optional, defaults to the exact string `normal`, and accepts exactly
`normal` or `urgent`. Other values report 422 / field `priority`, message
`priority must be normal or urgent`. Do not normalize whitespace or capitalization.
Stock conflict uses the reservation 409 contract with field `item.count`.
Success subtracts stock and consumes the shared identifier; returns 201 with
{"reservation_id":"r1","sku":"A","count":3,"remaining":7,"priority":"normal"}
on a fresh instance. Priority does not change the stock limit.
