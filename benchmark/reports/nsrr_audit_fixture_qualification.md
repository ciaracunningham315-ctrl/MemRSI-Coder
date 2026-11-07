# NSRR Sandbox Fixture Qualification

This is offline engineering evidence for independent synthetic fixtures only.
It does not evaluate an Agent, candidate Skill, real Gate, main T4 task, or main-task score.

- Tasks: 8
- Sandbox-compatible TaskSpecs materialized: 8/8
- Model/API/embedding/reranker calls: 0
- Formal results: no
- Main-task score eligible: no

| Audit task | TaskSpec | Baseline public | Baseline private target | Reference public | Reference private target | Reference combined | Fingerprint |
|---|---:|---:|---:|---:|---:|---:|---|
| `nsrr_audit_benefit_cache` | PASS | PASS | PASS | PASS | PASS | PASS | `f716078d1dfe…` |
| `nsrr_audit_benefit_decimal` | PASS | PASS | PASS | PASS | PASS | PASS | `b16f48d6347c…` |
| `nsrr_audit_dup_config_null` | PASS | PASS | PASS | PASS | PASS | PASS | `532fd184802c…` |
| `nsrr_audit_dup_headers` | PASS | PASS | PASS | PASS | PASS | PASS | `e54631f6656e…` |
| `nsrr_audit_harm_error_swallow` | PASS | PASS | PASS | PASS | PASS | PASS | `5d91ca3f3d12…` |
| `nsrr_audit_harm_idempotency` | PASS | PASS | PASS | PASS | PASS | PASS | `f90f0ecc2933…` |
| `nsrr_audit_stale_retry` | PASS | PASS | PASS | PASS | PASS | PASS | `cdccc2826c3f…` |
| `nsrr_audit_stale_timezone` | PASS | PASS | PASS | PASS | PASS | PASS | `c71180fec64d…` |

Overall qualification: **PASS**.

Each prompt contains the business goal and public function surface. Private cases and expected outputs are mounted only as host-side protected acceptance inputs. Baseline stubs are expected to pass the import-only public check and fail the private target assertions; reference implementations must pass the target and combined suites.

This qualification runs local Python unit tests and validates each materialized host-side spec against the current `evocode.contracts.TaskSpec` model. It does not invoke SandboxEvaluator, an Agent, a model, an API, an embedding service, or a reranker. The public TaskSpec fields are `repo_path`, `prompt`, `acceptance_command`, and `repository_id`; SandboxEvaluator consumes `commit` and host-only `protected_acceptance_paths` from the surrounding spec dictionary.
