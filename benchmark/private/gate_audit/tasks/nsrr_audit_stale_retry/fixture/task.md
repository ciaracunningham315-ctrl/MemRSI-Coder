# Request retry outcome utility

Implement task_module.run(payload: dict) -> dict for reporting a request's retry outcome. The payload includes the method, available attempts, per-attempt commit indicators, and an optional retry token. Return selected attempts and resulting side effects as integer fields named attempts and side_effects. Avoid duplicate effects when a retry token identifies the same operation.
