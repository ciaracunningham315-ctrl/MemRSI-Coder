
# Data processing contracts

Sales summaries, shipment manifests, and partner reconciliation retain the previous contracts in their modules.

## Monthly close import

Each monthly-close row has a unique invoice_ref and may provide gross_cents, gross_amount, and optional credit_cents. gross_cents represents integer minor units and accepts an integer or an ASCII base-10 integer string. If gross_cents is present and non-null, it is authoritative even when its value is zero. If it is absent or null, gross_amount is required and is a major-unit decimal value converted to cents using decimal half-up rounding. Do not use binary floating-point for this conversion.

A missing, null, empty, or whitespace-only credit_cents means no credit. A provided credit value must be a non-negative integer or ASCII base-10 integer string. Credits may not exceed gross cents. Booleans, fractions, negative values, non-finite amounts, duplicate invoice_ref values, and malformed records raise ValueError.

The result preserves input order and returns normalized gross_cents, credit_cents, and net_cents per row plus total_cents. The input is not modified. Existing flows remain compatible.
