# Request validation adapter

Implement task_module.run(payload: dict) -> dict for adapting a request validation result to the public status/body response shape. Accepted payloads should return their supplied body. Rejected payloads must be represented as a client error with a structured error body. Do not turn rejected input into a successful response.
