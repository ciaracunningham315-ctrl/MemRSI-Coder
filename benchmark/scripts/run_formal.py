"""Preflight and run the frozen 54-position three-repository benchmark."""
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

from runner.chain import digest, inventory
from runner.dataset_validation import load_manifest
from runner.formal import (FormalRunner, FrozenNSRRAuditReader, formal_execution_schedule,
    load_protocol, validate_formal_structure, validate_gate_regression_matrix)
from runner.stage_packages import validate_stage_package
from runner.nsrr_gate_api import LiveNSRRGateEvaluator, api_supported
from scripts.validate_protocol import validate


def _load_env(agent_root: Path) -> None:
    env_file = agent_root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_nsrr_gate_evaluator(root: Path, agent_root: Path):
    """Return the direct NSRR evaluator only when the local Agent source supports it."""
    if not api_supported(agent_root):
        return None
    return LiveNSRRGateEvaluator(root)


async def preflight(root: Path, agent_root: Path, config_file: Path | None,
                    max_total_tokens: int | None, *, check_services: bool = False) -> tuple[dict, object | None, object | None]:
    root, agent_root = root.resolve(), agent_root.resolve()
    errors: list[str] = []
    checks: dict = {"actual_model_inference_requests": 0, "embedding_requests": 0,
        "rerank_requests": 0, "external_inference_requests": 0}
    protocol = None
    schedule = []
    config = None
    nsrr_evaluator = load_nsrr_gate_evaluator(root, agent_root)
    try:
        protocol = load_protocol(root)
        checks["protocol_id"] = protocol.get("protocol_id")
        errors.extend(validate_formal_structure(protocol))
        errors.extend(validate(protocol, "formal_benchmark"))
        schedule = formal_execution_schedule(root, protocol)
        checks["planned_main_positions"] = len(schedule)
        checks["planned_a_positions"] = sum(row["experiment"] == "A" for row in schedule)
        checks["planned_b_positions"] = sum(row["experiment"] == "B" for row in schedule)
        checks["execution_schedule"] = schedule
        dataset = {}
        for repository_id in protocol["profiles"]["formal_benchmark"]["repositories"]:
            dataset[repository_id] = {}
            for stage_name, stage in protocol["dataset"]["stage_directories"].items():
                manifest = load_manifest(root, stage, repository_id)
                package = validate_stage_package(root, manifest)
                dataset[repository_id][stage_name] = {"task_id": manifest["task_id"],
                    "repository_id": manifest["repository_id"], "stage": manifest["stage"],
                    "stage_package_sha256": manifest["git_stage_package_sha256"],
                    "stage_package_valid": True, "stage_package_evidence": package}
        checks["datasets"] = dataset
        matrix = validate_gate_regression_matrix(root, protocol)
        checks["gate_regression_matrix"] = matrix["matrix"]
        if not matrix["valid"]:
            errors.extend("Gate/regression: " + item for item in matrix["errors"])
        audit_ids = protocol["profiles"]["formal_benchmark"].get("nsrr", {}).get("audit_task_ids")
        rows = None
        try:
            rows = FrozenNSRRAuditReader().read(root, audit_ids)
            checks["nsrr_audit_ids"] = [row["audit_task_id"] for row in rows]
            checks["nsrr_external_labels"] = {row["candidate_id"]: row["observed_label"] for row in rows}
            checks["nsrr_freeze_verified"] = True
        except Exception as exc:
            checks["nsrr_freeze_verified"] = False
            errors.append(f"NSRR audit inputs: {type(exc).__name__}: {exc}")
        if rows is not None and nsrr_evaluator is not None:
            try:
                checks["nsrr_gate_input_fingerprint"] = nsrr_evaluator.input_manifest("formal_preflight", rows)
                checks["nsrr_fixture_qualified"] = True
            except Exception as exc:
                checks["nsrr_fixture_qualified"] = False
                errors.append(f"NSRR sandbox fixtures: {type(exc).__name__}: {exc}")
        checks["nsrr_candidate_evaluation_api_supported"] = nsrr_evaluator is not None
        checks["nsrr_gate_adapter_status"] = "available" if nsrr_evaluator is not None else "unavailable"
        checks["nsrr_gate_decision"] = "not_run" if nsrr_evaluator is not None else "unverified"
        if nsrr_evaluator is None:
            errors.append("formal-ready blocked: direct NSRR candidate-evaluation API or frozen fixture adapter is unavailable")
    except Exception as exc:
        errors.append(f"benchmark preflight: {type(exc).__name__}: {exc}")

    if config_file is not None:
        try:
            if not config_file.is_file():
                raise ValueError("Agent config file does not exist")
            _load_env(agent_root)
            from runner.chain import load_agent_runtime
            load_agent_runtime(agent_root)
            from evocode.config import RuntimeConfig, load_config
            config = load_config(str(config_file))
            if "max_tool_calls" not in RuntimeConfig.model_fields:
                errors.append("Agent does not support the hard 80-tool admission cap")
            for role, model in (("planner_coder", config.models.local), ("evolution", config.models.evolution)):
                if not model.model_id or model.model_id.lower() in {"default", "model", "placeholder", "your-model"}:
                    errors.append(f"{role} actual model_id must be explicitly configured")
                if not os.getenv(model.api_key_env) and not model.base_url.startswith(("http://127.0.0.1", "http://localhost")):
                    errors.append(f"{role} credential environment variable is unset: {model.api_key_env}")
            memory = config.raw_rag
            if not memory.embedding_model or not memory.embedding_dimensions:
                errors.append("embedding_model and embedding_dimensions must be explicit")
            if not memory.rerank_enabled or not memory.rerank_model:
                errors.append("frozen protocol requires rerank_enabled=true and an explicit reranker")
            if not config.evolution.allow_remote_code_context:
                errors.append("evolution.allow_remote_code_context must explicitly authorize learning")
            if max_total_tokens is None or max_total_tokens <= 0:
                errors.append("--execute requires an explicit positive --max-total-tokens")
            if max(config.models.local.max_context_tokens, config.models.evolution.max_context_tokens) > 131072:
                errors.append("configured model context exceeds protocol limit 131072")
            checks["actual_model_ids"] = {"planner_coder": config.models.local.model_id,
                "evolution": config.models.evolution.model_id}
            checks["generation_parameters"] = {"planner_coder": config.models.local.model_dump(exclude={"api_key_env"}),
                "evolution": config.models.evolution.model_dump(exclude={"api_key_env"})}
            checks["max_agent_turns"] = config.runtime.max_turns
            checks["max_tool_calls"] = 80
            checks["timeout_seconds"] = 600
            checks["max_total_tokens"] = max_total_tokens
            checks["retrieval_provider_ids"] = {"embedding": memory.embedding_model,
                "reranker": memory.rerank_model, "dimensions": memory.embedding_dimensions}
            checks["agent_code_fingerprint"] = digest(inventory(agent_root / "src"))
            prompt_root = agent_root / "src/evocode/prompts"
            checks["prompt_fingerprints"] = inventory(prompt_root) if prompt_root.is_dir() else inventory(agent_root / "src/evocode/agent")
            checks["dependency_lock_fingerprint"] = hashlib.sha256((agent_root / "uv.lock").read_bytes()).hexdigest()
        except Exception as exc:
            errors.append(f"Agent environment: {type(exc).__name__}: {exc}")
    elif max_total_tokens is not None and max_total_tokens <= 0:
        errors.append("max_total_tokens must be positive")

    # This is intentionally gated on all local readiness checks and an actual
    # NSRR evaluator. GET /models, Qdrant collection listing and docker inspect
    # are read-only; no inference endpoint is called.
    if check_services and config is not None:
        try:
            import httpx

            async def _get_with_retry(client_obj: httpx.AsyncClient, url: str, headers: dict | None = None, max_attempts: int = 3) -> httpx.Response:
                last_exc = None
                for attempt in range(max_attempts):
                    try:
                        resp = await client_obj.get(url, headers=headers)
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
                    response = await _get_with_retry(client, model.base_url.rstrip("/") + "/models", headers=headers)
                    model_ids = {row["id"] for row in response.json()["data"]}
                    checks[role + "_model_inventory_verified"] = model.model_id in model_ids
                    if model.model_id not in model_ids:
                        errors.append(f"{role} configured model ID is absent from provider /models")
                response = await _get_with_retry(client, config.raw_rag.qdrant_url.rstrip("/") + "/collections")
                checks["qdrant_reachable"] = True
        except Exception as exc:
            errors.append(f"read-only service readiness: {type(exc).__name__}")
        try:
            image = subprocess.run(["docker", "image", "inspect", config.runtime.docker_image,
                "--format", "{{.Id}}"], capture_output=True, text=True, timeout=20)
            if image.returncode:
                errors.append("Docker image unavailable locally; preflight does not pull images")
            else:
                checks["container_image_digest"] = image.stdout.strip()
                config.runtime.docker_image = image.stdout.strip()
                config.evolution.sandbox_image = image.stdout.strip()
        except Exception as exc:
            errors.append(f"Docker image readiness: {type(exc).__name__}")
    else:
        checks["service_readiness_checks"] = "not_run"

    static_ready = not errors
    execution_ready = static_ready and config is not None and check_services
    return ({"kind": "formal_benchmark_preflight", "ready": execution_ready,
        "static_preflight_ready": static_ready, "errors": errors,
        "checks": checks,
        "boundary": "No model, embedding, rerank, Agent task, Docker task or external inference request was made."},
        config, nsrr_evaluator)


def compact_preflight_report(report: dict) -> dict:
    """Keep the CLI summary readable while retaining full runner artifacts."""
    rendered = dict(report)
    checks = dict(report.get("checks", {}))
    schedule = checks.pop("execution_schedule", None)
    if schedule is not None:
        encoded = json.dumps(schedule, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        checks["execution_schedule_sha256"] = hashlib.sha256(encoded).hexdigest()
    datasets = checks.get("datasets")
    if isinstance(datasets, dict):
        checks["datasets"] = {
            repository_id: {
                stage: {key: value for key, value in package.items() if key != "stage_package_evidence"}
                for stage, package in stages.items()
            }
            for repository_id, stages in datasets.items()
        }
    rendered["checks"] = checks
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--agent-root", type=Path, default=ROOT.parent)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--batch-id")
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preflight-only", action="store_true", help="perform read-only model/Qdrant/Docker checks without running tasks")
    parser.add_argument("--skip-service-checks", action="store_true", help="local-only preflight; cannot execute")
    args = parser.parse_args(argv)
    if args.execute and args.preflight_only:
        parser.error("--execute and --preflight-only are mutually exclusive")
    if (args.execute or args.preflight_only) and (args.config is None or args.max_total_tokens is None or args.max_total_tokens <= 0):
        parser.error("--execute/--preflight-only require --config and a positive --max-total-tokens")
    if args.execute and args.skip_service_checks:
        parser.error("--execute cannot skip service readiness checks")
    if args.preflight_only and args.skip_service_checks:
        parser.error("--preflight-only cannot skip service readiness checks")
    root, agent_root = args.root.resolve(), args.agent_root.resolve()
    config_path = args.config.resolve() if args.config else None
    report, config, evaluator = asyncio.run(preflight(root, agent_root, config_path,
        args.max_total_tokens, check_services=(args.execute or args.preflight_only) and not args.skip_service_checks))
    if args.preflight_only:
        print(json.dumps(compact_preflight_report(report), ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    if not args.execute:
        report["checks"]["service_readiness_checks"] = "not_run"
        print(json.dumps(compact_preflight_report(report), ensure_ascii=False, indent=2))
        return 0 if report["static_preflight_ready"] else 1
    if not report["ready"]:
        print(json.dumps(compact_preflight_report(report), ensure_ascii=False, indent=2))
        return 1
    batch_id = args.batch_id or ("formal_" + __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id):
        parser.error("batch-id must contain 1-80 ASCII letters, digits, underscores or hyphens")
    if config is None or evaluator is None:
        raise RuntimeError("preflight-ready state requires actual Agent config and NSRR evaluator")
    from runner.chain import LiveBackend
    backend = LiveBackend(root, agent_root, config, root / "results" / batch_id, args.max_total_tokens)
    evaluator.bind_backend(backend)
    runner = FormalRunner(root, batch_id, backend, batch_metadata=report["checks"],
        nsrr_gate_evaluator=evaluator, require_ready=True)
    summary = asyncio.run(runner.run())
    print(json.dumps({key: value for key, value in summary.items() if key not in {"results", "planned_matrix"}},
        ensure_ascii=False, indent=2))
    return 0 if summary.get("complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
