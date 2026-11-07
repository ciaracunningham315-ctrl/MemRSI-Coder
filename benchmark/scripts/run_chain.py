"""Preflight by default; --execute is an explicit live inference boundary."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.chain import ChainRunner, LiveBackend, digest, execution_schedule, inventory, load_agent_runtime
from runner.dataset_validation import load_manifest, STAGES
from runner.stage_packages import validate_stage_package
from scripts.validate_protocol import validate


async def preflight(root: Path, agent_root: Path, config_file: Path, max_total_tokens: int | None,
                    *, check_services: bool = True) -> tuple[dict, object | None]:
    """Read only with respect to model providers; never POST inference/embeddings."""
    import yaml
    errors, checks = [], {}
    protocol = yaml.safe_load((root / "configs/protocol.yaml").read_text())
    errors += validate(protocol, "chain_integration")
    checks["protocol_id"] = protocol["protocol_id"]
    checks["planned_main_tasks"] = len(execution_schedule())
    checks["actual_model_inference_requests"] = 0
    checks["seed_supported"] = False
    checks["formal_results"] = False
    checks["execution_schedule"] = execution_schedule()
    for stage in STAGES:
        try:
            checks[stage] = validate_stage_package(root, load_manifest(root, stage))
        except Exception as exc:
            errors.append(f"{stage}: {type(exc).__name__}: {exc}")
    try:
        from runner.gate_tasks import _selected_ids, load_gate_manifest, require_gate_qualification
        catalog = json.loads((root / "configs/gate_task_catalog.json").read_text())
        profile = protocol["profiles"]["chain_integration"]
        matrix = {}
        for stage in ("seed", "transfer", "stale"):
            declared = profile["gate_task_ids"][stage]
            selected = _selected_ids(root, stage, "task", "repo_api")
            if selected != declared["project"]["repo_api"] or declared["general"]:
                raise ValueError("profile and independent project Gate matrix differ or General scope lacks a gate")
            expected_replay = profile["regression_task_ids"]["repo_api"]
            if _selected_ids(root, stage, "regression", "repo_api") != expected_replay:
                raise ValueError("profile and fixed regression matrix differ")
            ids = selected + expected_replay
            if stage == "transfer":
                meta = _selected_ids(root, stage, "meta", "repo_api")
                if meta != declared["meta"]:
                    raise ValueError("profile and Meta Gate matrix differ")
                ids += meta
            for task_id in ids:
                manifest = load_gate_manifest(root, task_id)
                require_gate_qualification(root, task_id, manifest)
            matrix[stage] = ids
        checks["gate_and_regression_matrix"] = matrix
        checks["gate_catalog_sha256"] = digest(catalog)
    except Exception as exc:
        errors.append(f"gate readiness: {type(exc).__name__}: {exc}")
    config = None
    try:
        if not config_file.is_file():
            raise ValueError("explicit Agent config file is missing")
        runtime = load_agent_runtime(agent_root)
        # Load only into the process environment. Neither values nor the .env
        # file are copied to manifests, results or container environments.
        env_file = agent_root / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
        from evocode.config import load_config, RuntimeConfig
        config = load_config(str(config_file))
        if "max_tool_calls" not in RuntimeConfig.model_fields:
            errors.append("Agent does not support the hard 80-tool admission cap")
        checks["actual_model_ids"] = {"planner_coder": config.models.local.model_id,
                                      "evolution": config.models.evolution.model_id}
        for role, model in (("local", config.models.local), ("evolution", config.models.evolution)):
            if not model.model_id or model.model_id.lower() in {"default", "model", "placeholder", "your-model"}:
                errors.append(f"{role} actual model_id must be explicitly configured")
            if not os.getenv(model.api_key_env) and not model.base_url.startswith(("http://127.0.0.1", "http://localhost")):
                errors.append(f"{role} credential environment variable is unset: {model.api_key_env}")
        memory = config.raw_rag
        if not memory.embedding_model or not memory.embedding_dimensions:
            errors.append("embedding_model and embedding_dimensions must be explicit")
        if not memory.rerank_enabled or not memory.rerank_model:
            errors.append("frozen protocol requires rerank_enabled=true and explicit rerank_model")
        if not config.evolution.allow_remote_code_context:
            errors.append("evolution.allow_remote_code_context must explicitly authorize learning")
        if max_total_tokens is None or max_total_tokens <= 0:
            errors.append("--execute requires explicit positive --max-total-tokens")
        checks["generation_parameters"] = {"planner_coder": config.models.local.model_dump(exclude={"api_key_env"}),
                                           "evolution": config.models.evolution.model_dump(exclude={"api_key_env"})}
        checks["max_agent_turns"] = config.runtime.max_turns
        checks["max_tool_calls"] = 80
        checks["timeout_seconds"] = 600
        checks["max_total_tokens"] = max_total_tokens
        checks["retrieval_provider_ids"] = {"embedding": memory.embedding_model, "reranker": memory.rerank_model,
                                             "dimensions": memory.embedding_dimensions}
        if max(config.models.local.max_context_tokens, config.models.evolution.max_context_tokens) > 131072:
            errors.append("model context exceeds protocol hard limit 131072")
        if check_services:
            import httpx

            async def _get_with_retry(url: str, headers: dict | None = None, max_attempts: int = 3) -> httpx.Response:
                last_exc = None
                for attempt in range(max_attempts):
                    try:
                        resp = await client.get(url, headers=headers)
                        resp.raise_for_status()
                        return resp
                    except Exception as exc:
                        last_exc = exc
                        if attempt + 1 < max_attempts:
                            await asyncio.sleep(1.0 * (2 ** attempt))
                raise last_exc

            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                for role, model in (("planner_coder", config.models.local), ("evolution", config.models.evolution)):
                    headers = {"Authorization": "Bearer " + os.environ[model.api_key_env]} if os.getenv(model.api_key_env) else {}
                    try:
                        response = await _get_with_retry(model.base_url.rstrip("/") + "/models", headers=headers)
                        ids = {row["id"] for row in response.json()["data"]}
                        checks[role + "_model_inventory_verified"] = model.model_id in ids
                        if model.model_id not in ids:
                            errors.append(f"{role} configured ID is absent from provider /models")
                    except Exception as exc:
                        errors.append(f"{role} /models verification failed: {type(exc).__name__}")
                try:
                    response = await _get_with_retry(memory.qdrant_url.rstrip("/") + "/collections")
                    checks["qdrant_reachable"] = True
                except Exception as exc:
                    errors.append(f"Qdrant health failed: {type(exc).__name__}")
            image = subprocess.run(["docker", "image", "inspect", config.runtime.docker_image,
                                    "--format", "{{.Id}}"], capture_output=True, text=True, timeout=20)
            if image.returncode:
                errors.append("Docker image unavailable locally; preflight does not pull images")
            else:
                checks["container_image_digest"] = image.stdout.strip()
                # Pin the content-addressed image for Agent and grader, avoiding
                # later mutable-tag drift inside this batch.
                config.runtime.docker_image = image.stdout.strip()
                config.evolution.sandbox_image = image.stdout.strip()
        else:
            checks["service_checks"] = "not_run"
            errors.append("service checks disabled; live execution readiness not verified")
        checks["agent_code_fingerprint"] = digest(inventory(agent_root / "src"))
        checks["prompt_fingerprints"] = inventory(agent_root / "src/evocode/prompts") if (agent_root / "src/evocode/prompts").is_dir() else inventory(agent_root / "src/evocode/agent")
        checks["dependency_lock_fingerprint"] = hashlib.sha256((agent_root / "uv.lock").read_bytes()).hexdigest()
    except Exception as exc:
        errors.append(f"Agent environment: {type(exc).__name__}")
    # Bind authoritative task inputs, not qualification logs (which contain
    # execution times and disposable cwd paths and may be regenerated offline).
    dataset_inputs = {}
    for stage in STAGES:
        task = load_manifest(root, stage)
        dataset_inputs[stage] = {"manifest": task,
            "directories": {key: inventory(root / task[key]) for key in
                            ("code_dir", "private_tests_dir", "regression_tests_dir", "reference_dir")},
            "task_sha256": hashlib.sha256((root / task["task_file"]).read_bytes()).hexdigest()}
    if (root / "configs/gate_task_catalog.json").is_file():
        gate_catalog = json.loads((root / "configs/gate_task_catalog.json").read_text())
        dataset_inputs["gate_catalog"] = gate_catalog
        dataset_inputs["gate_manifests"] = {task_id: json.loads((root / relative).read_text())
                                            for task_id, relative in gate_catalog["tasks"].items()}
    checks["dataset_fingerprint"] = digest(dataset_inputs)
    return {"kind": "online_runner_preflight", "ready": not errors, "errors": errors, "checks": checks,
            "boundary": "No model inference, embeddings, rerank, or Agent tasks are executed. Provider /models and Qdrant health are read-only. Actual retrieval readiness is additionally verified at every live freeze."}, config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--agent-root", type=Path, default=ROOT.parent)
    parser.add_argument("--config", type=Path, help="explicit actual-ID Agent YAML; required with --execute")
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--skip-service-checks", action="store_true", help="offline inventory only; cannot execute")
    args = parser.parse_args()
    if args.execute and (args.config is None or args.max_total_tokens is None or args.max_total_tokens <= 0):
        parser.error("--execute requires --config and a positive --max-total-tokens")
    if args.execute and args.skip_service_checks:
        parser.error("--execute cannot skip service readiness checks")
    root, agent = args.root.resolve(), args.agent_root.resolve()
    config_path = args.config.resolve() if args.config else agent / "config/local.yaml"
    report, config = asyncio.run(preflight(root, agent, config_path, args.max_total_tokens,
                                           check_services=not args.skip_service_checks))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0 if report["ready"] else 1
    if not report["ready"]:
        return 1
    batch_id = args.batch_id or ("chain_" + __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id):
        parser.error("batch-id must contain 1-80 ASCII letters, digits, underscores or hyphens")
    backend = LiveBackend(root, agent, config, root / "results" / batch_id, args.max_total_tokens)
    runner = ChainRunner(root, batch_id, backend, batch_metadata=report["checks"])
    summary = asyncio.run(runner.run())
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
