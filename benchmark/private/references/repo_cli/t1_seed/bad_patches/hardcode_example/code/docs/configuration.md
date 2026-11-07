# Configuration contract

`publish --retries N` reads a value from command line, `REPOCLI_PUBLISH_RETRIES`, `.repocli.json` under `publish.retries`, then default `2`, in that order. The chosen value must be an ASCII decimal integer from 1 through 5 inclusive. A malformed or out-of-range selected value is an error; the command must not create a record or consume an ID. `health` returns `{"status":"ok"}`.
