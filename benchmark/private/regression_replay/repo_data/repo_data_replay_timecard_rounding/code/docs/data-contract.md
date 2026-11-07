# Timecard contract
Rows have unique worker_id, hours and rate as finite non-negative decimals. Pay is hours times rate rounded to cents using decimal half-up rounding. Output preserves order and reports an exact total. Booleans and malformed values raise ValueError.
