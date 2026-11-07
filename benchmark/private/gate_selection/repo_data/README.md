# repo_data independent publication fixtures

The fixture manifests bind each task to its own code, task, hidden acceptance tests, regression tests, and reference tree. Every manifest declares independent_of_main_tasks=true and future_main_inputs_used=[]. Gate and replay sources use synthetic receipt, telemetry, sensor, rate-catalog, and timecard records; no main-task private tests or T4 data are copied.

Selected IDs by stage and the T2 Meta Gate are recorded in selection.json. Shared runner/catalog integration is left to the benchmark owner. Qualification evidence is produced by scripts/validate_repo_data_gates.py; fixture admission is not model skill benefit.
