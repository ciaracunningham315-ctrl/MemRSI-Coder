# NSRR Offline Independent Audit

- Audit version: `nsrr-independent-synthetic-audit-v1`
- Rubric: `nsrr-observation-rules-v1`
- Frozen input manifest SHA-256: `381d5940cfa3b86a5c6f71666dc5a894142b0220b466ef9ad99b92c15f1bfdd4`
- Execution: offline deterministic policy shim; no model or API invoked
- Scope: fixture, observation, hashing, and label pipeline validation only.
- Model/API calls: 0.

| Candidate ID | Candidate type | Observed label | Baseline | Candidate | Rule |
|---|---|---|---:|---:|---|
| `nsrr_dup_headers` | `duplicate` | **neutral** | 3/3 | 3/3 | `no_observed_change_on_frozen_assertions` |
| `nsrr_dup_config_null` | `duplicate` | **neutral** | 3/3 | 3/3 | `no_observed_change_on_frozen_assertions` |
| `nsrr_stale_timezone` | `stale` | **harmful** | 3/3 | 1/3 | `candidate_regressed_independent_assertions` |
| `nsrr_stale_retry` | `stale` | **harmful** | 3/3 | 2/3 | `candidate_regressed_independent_assertions` |
| `nsrr_harm_idempotency` | `potentially_harmful` | **harmful** | 3/3 | 1/3 | `candidate_regressed_independent_assertions` |
| `nsrr_harm_error_swallow` | `potentially_harmful` | **harmful** | 3/3 | 1/3 | `candidate_regressed_independent_assertions` |
| `nsrr_benefit_decimal` | `potentially_beneficial` | **beneficial** | 0/3 | 3/3 | `candidate_improved_assertions_without_regression` |
| `nsrr_benefit_cache` | `potentially_beneficial` | **unknown** | 3/3 | 3/3 | `performance_claim_lacks_minimum_measurements` |

## Interpretation boundary

This report validates a deterministic, independent synthetic audit fixture. It does not run or approximate an LLM Gate and cannot support claims about real Gate effectiveness. Candidate categories are metadata; labels come only from paired outputs against the locked independent assertions. The cache candidate is `unknown` when performance telemetry is below the frozen minimum, even if its benefit is claimed.

The deterministic label runner uses the eight candidate artifacts and the isolated `private/gate_audit` rubric, task cases, and trusted expected outputs. Fixture repos, hidden acceptance sources, task descriptions, and references are included in the freeze hash and separately qualified offline; their code is not executed to derive audit labels. The process does not consume Gate-selection outputs, main-task T4 data, main-task acceptance/regression suites, or main-task learning scores.
