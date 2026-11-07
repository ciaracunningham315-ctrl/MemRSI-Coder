# Telemetry archive contract
A row has a unique non-empty sample_id and a reading. Missing, null, empty, or whitespace-only reading means not collected and must return reading null with status pending. Numeric zero is collected and has status recorded. Other values are finite non-negative decimals normalized to two-decimal strings. Booleans and malformed values raise ValueError.
