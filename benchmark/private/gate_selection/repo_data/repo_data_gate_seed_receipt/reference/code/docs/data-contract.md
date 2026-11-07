# Receipt contract
Each row has a unique string receipt_id, a positive whole quantity, and a finite non-negative unit_amount. Line amount is quantity times unit_amount rounded to two decimals using decimal half-up rounding. The result contains ordered lines and an exact total string. Booleans, invalid quantities, negative values, and malformed records raise ValueError.
