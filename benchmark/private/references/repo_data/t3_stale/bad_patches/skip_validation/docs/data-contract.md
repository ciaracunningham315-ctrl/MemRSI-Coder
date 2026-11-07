
# Data processing contracts

## Stable flows

Sales summaries and shipment manifests retain the contracts documented in their module history. Their outputs and ordinary valid cases remain supported.

## Partner reconciliation

The current partner feed is an object containing source and entries. Each entry has transaction_id, amount_minor, optional adjustment_minor, and basis_points. amount_minor and adjustment_minor are integer minor units; an ASCII base-10 integer string is also accepted because some transport exports encode whole numbers as text. Booleans, fractions, negative values, and non-finite values are invalid. A missing or null adjustment_minor means zero.

basis_points is an integer from 0 through 10000, with an ASCII base-10 integer string accepted. It represents the partner charge rate in hundredths of one percent. The charge is rounded to a whole minor unit using half-up rounding after adjustment. An adjustment larger than amount_minor is invalid.

The result contains entries in input order with transaction_id, amount_minor, adjustment_minor, charge_minor, and settlement_minor. settlement_minor is amount_minor minus adjustment_minor plus charge_minor. total_minor is the exact sum of settlement_minor. Invalid input raises ValueError and the input is not mutated.
