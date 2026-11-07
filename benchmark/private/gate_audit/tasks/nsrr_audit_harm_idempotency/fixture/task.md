# Delivery effect counter

Implement task_module.run(payload: dict) -> dict for estimating the number of effects produced by a sequence of delivery requests. The payload contains request records, and the result reports the total under side_effects. Re-delivery of the same operation must not create extra work.
