# Safe retry policy

Retry idempotent reads and writes carrying an idempotency key. Do not retry a write after an ambiguous committed timeout when no idempotency key is available.
