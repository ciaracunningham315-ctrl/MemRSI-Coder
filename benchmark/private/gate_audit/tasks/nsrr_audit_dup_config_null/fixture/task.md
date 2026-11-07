# Configuration view utility

Implement task_module.run(payload: dict) -> dict for returning a configuration view. The payload contains a configuration mapping. Return the corresponding mapping under the public config field, preserving the caller-visible shape and supplied values. Do not mutate the payload.
