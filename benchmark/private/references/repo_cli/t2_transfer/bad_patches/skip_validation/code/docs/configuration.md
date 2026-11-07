# Configuration contract

`publish` keeps the seed contract. `mirror --workers N` reads command line, `REPOCLI_MIRROR_WORKERS`, `[mirror] workers` from `repo-cli.ini`, then default `2`, in that order. The selected value must be an ASCII decimal integer from 1 through 8 inclusive. Invalid input must leave records and the next ID unchanged. `health` returns `{"status":"ok"}`.
