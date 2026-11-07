
# Data processing contracts

## Sales summary

Sales input, validation, output ordering, decimal half-up rounding, and nullable discount behavior follow the same contract in this document's history section. The public tests exercise ordinary values.

## Shipment manifest

Each shipment has a unique non-empty shipment_id, parcel_count from 1 through 50, and optional weight_kg. parcel_count accepts an integer or a base-10 integer string; booleans and fractional values are invalid. A missing, null, empty, or whitespace-only weight means the weight has not been supplied. In that case the result preserves weight_kg as null, uses status needs_quote, and has a null fee. An actual zero weight is known and uses status ready with fee 0.00.

A known weight is a finite non-negative decimal value. Its fee is parcel_count times weight_kg times 0.35, rounded to cents using decimal half-up rounding. The output preserves input order and includes shipment_id, parcel_count, normalized weight_kg, status, and fee. Known weights and fees are two-decimal strings. Invalid rows raise ValueError without modifying input.
