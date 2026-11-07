# Raw-RAG prevalidation fixtures

This is a four-task held-out set for paired ON/OFF evaluation. `manifest.json` lists each task spec, editable workspace template, training workspace, and protected acceptance script. The repository contains no authored training trace, model answer, run result, or success count.

The cases cover convention reuse, similar-bug transfer, rejection of a stale version, and rejection of evidence from another repository scope. Each held-out prompt and workspace is identical across the ON and OFF branches. Acceptance files stay outside the editable workspace and are copied into the isolated oracle only after the agent task finishes.

## Offline fixture check

From the repository root:

```sh
python3 -m pytest -q tests/integration/test_prevalidation_rawrag.py
```

This checks the protected acceptance scripts against the authored baseline and reference workspaces. It does not establish a live Raw-RAG effect.

## Live black-box protocol

The runner starts real EvoCode HTTP instances through `scripts/blackbox_runtime.py`. In separate ON and OFF instances, it submits each manifest `training_prompt` through the public task API using a scratch copy of `training_workspace`. Same-project cases explicitly map the event scope to the held-out workspace identity; `wrong_scope` keeps the training repository identity distinct. Both condition snapshots are then cloned separately for each held-out task. Holdout indexing is disabled, and no search query is injected by the runner.

Each branch runs the same held-out prompt in an independent workspace copy. The runner records public task status, observed model-call/token telemetry, every retrieval query and its FTS/dense/rerank result, and evidence contexts linked to completed model-usage events. The isolated oracle grades completed tasks. Agent noncompletion counts as failure, and an unavailable evaluation result is recorded separately. Retrieval and index health are reported independently from task scores. Qdrant uses one temporary collection that is removed on exit.

Check the offline preflight first:

```sh
python3 scripts/live_memory_ablation.py --case convention_reuse
```

When the model, embedding, reranker, Docker, and Qdrant services are configured, a bounded real pilot is:

```sh
python3 scripts/live_memory_ablation.py --execute --case convention_reuse --max-turns 6 \
  --output /tmp/rawrag-convention-reuse.json
```

Omit `--case` to run all four fixture pairs. `--output` writes the result report; it does not change where tasks or model calls run. The runner does not accept caller-authored traces as input.
