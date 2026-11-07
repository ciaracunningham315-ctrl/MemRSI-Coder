This package only calculates retry delay. The caller owns retry decisions.

## Legacy Adapter Header Format

The legacy adapter normalizes HTTP header keys by replacing hyphens (`-`) with underscores (`_`) and may emit keys in either lowercase or uppercase (for example, `x_request_id` or `X_REQUEST_ID`). Header lookup helpers must match header names case-insensitively across both hyphenated and underscored forms.
