
# Sales summary contract

The input is a list of objects with a unique, non-empty string sale_id, a quantity from 1 through 999, a non-negative unit_price, and an optional non-negative discount. Quantity accepts an integer or a base-10 integer string. Boolean values are not integers for this API. Money accepts a finite non-negative integer, decimal string, or JSON numeric value. A missing or null discount means zero. A discount may not exceed the extended price.

The result contains one line per input in the same order. Each line has sale_id and total as a two-decimal string. The total for a line is quantity times unit_price minus discount, rounded to cents using decimal half-up rounding. grand_total is the sum of the displayed line totals, returned as a two-decimal string. Invalid input raises ValueError and does not mutate the input.

The public tests cover ordinary values. Boundary cases and invalid data are part of the task.
