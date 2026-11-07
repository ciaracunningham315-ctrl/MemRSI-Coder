"""Offline dataset admission checks. No model execution or ability metrics.

Every command runs on a newly copied code tree. Hidden tests enter grader
copies only; reports and scratch work remain beneath the benchmark root.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

STAGES = ("t1_seed", "t2_transfer", "t3_stale", "t4_heldout")
IGNORED = {".git", "__pycache__", ".pytest_cache", ".venv", ".DS_Store"}


def inventory(directory: Path) -> dict[str, str]:
    """Fingerprint source/test content while excluding transient interpreter state."""
    result = {}
    for path in sorted(directory.rglob("*")):
        if any(part in IGNORED for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not supported: {path}")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def source_path(root: Path, value: str) -> Path:
    path = (root / value).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes benchmark root: {value}")
    return path


def copy_clean(source: Path, target: Path) -> None:
    inventory(source)  # Reject symlinks rather than following them into private data.
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED))


def execute(argv: list[str], cwd: Path, log: Path, timeout: int = 60) -> dict:
    """Capture the actual command, exit code and full combined log; kill its group on timeout."""
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.defpath, "HOME": str(cwd), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    start = time.monotonic()
    timed_out = False
    error = None
    with log.open("w", encoding="utf-8") as stream:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                exit_code = proc.wait()
        except OSError as exc:
            error = str(exc)
            stream.write(error + "\n")
            exit_code = None
    output = log.read_text(encoding="utf-8")
    counts = re.findall(r"Ran (\d+) tests? in", output)
    failures = re.findall(r"failures=(\d+)", output)
    errors = re.findall(r"errors=(\d+)", output)
    return {"argv": argv, "cwd": str(cwd), "exit_code": exit_code,
            "timed_out": timed_out, "error": error, "duration_seconds": round(time.monotonic() - start, 4),
            "log": str(log), "tests_run": int(counts[-1]) if counts else None,
            "test_failures": int(failures[-1]) if failures else 0,
            "test_errors": int(errors[-1]) if errors else 0}


def completed(result: dict) -> bool:
    return result["exit_code"] is not None and not result["timed_out"] and not result["error"] and bool(result["tests_run"])


def passed(result: dict) -> bool:
    return completed(result) and result["exit_code"] == 0


def load_manifest(root: Path, stage: str, repository_id: str = "repo_api") -> dict:
    path = root / "scenarios" / repository_id / stage / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("task_id", "repository_id", "stage", "code_dir", "task_file",
                "private_tests_dir", "regression_tests_dir", "reference_dir", "acceptance_command"):
        if key not in data:
            raise ValueError(f"{path}: missing {key}")
    for key in ("code_dir", "task_file", "private_tests_dir", "regression_tests_dir", "reference_dir"):
        source_path(root, data[key])
    command = data["acceptance_command"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("acceptance_command must be an argv list")
    if command[:4] != ["python", "-m", "unittest", "discover"]:
        raise ValueError("offline admission currently supports python -m unittest discover")
    return data


def check_variant(root: Path, work: Path, manifest: dict, name: str, overlay: Path | None,
                  python: str) -> dict:
    """Use four fresh workspaces so preceding tests cannot modify grading state."""
    results = {}
    for suite in ("public", "acceptance", "regression", "combined"):
        destination = work / name / suite
        copy_clean(source_path(root, manifest["code_dir"]), destination)
        if overlay is not None:
            # References are complete code trees. Replace, so deletions are represented.
            shutil.rmtree(destination)
            copy_clean(overlay, destination)
        if suite in ("acceptance", "regression"):
            key = "private_tests_dir" if suite == "acceptance" else "regression_tests_dir"
            private = destination / ("_grader_" + suite)
            copy_clean(source_path(root, manifest[key]), private)
            argv = [python, "-m", "unittest", "discover", "-s", private.name, "-v"]
        else:
            if suite == "combined":
                tests = destination / "tests"
                tests.mkdir(exist_ok=True)
                for key in ("private_tests_dir", "regression_tests_dir"):
                    private = source_path(root, manifest[key])
                    for relative in inventory(private):
                        target = tests / relative
                        if target.exists():
                            raise ValueError(f"trusted test collision: {relative}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(private / relative, target)
            argv = [python, *manifest["acceptance_command"][1:]]
        results[suite] = execute(argv, destination, work / "logs" / f"{name}_{suite}.log")
    return results


def validate_stage(root: Path, work: Path, stage: str, python: str, repository_id: str = "repo_api") -> dict:
    manifest = load_manifest(root, stage, repository_id)
    paths = {key: source_path(root, manifest[key]) for key in
             ("code_dir", "private_tests_dir", "regression_tests_dir", "reference_dir")}
    fingerprints = {key: inventory(path) for key, path in paths.items()}
    fingerprints["task_file"] = {manifest["task_file"]: hashlib.sha256(source_path(root, manifest["task_file"]).read_bytes()).hexdigest()}
    fingerprints["manifest"] = {"sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    baseline = check_variant(root, work, manifest, "baseline", None, python)
    reference = check_variant(root, work, manifest, "reference", paths["reference_dir"], python)
    checks = {"public_baseline_passes": passed(baseline["public"]),
              "baseline_target_fails": (completed(baseline["acceptance"]) and baseline["acceptance"]["exit_code"] != 0
                                        and baseline["acceptance"]["test_failures"] > 0 and baseline["acceptance"]["test_errors"] == 0),
              "baseline_regression_passes": passed(baseline["regression"]),
              "reference_all_pass": all(passed(item) for item in reference.values())}
    bad_results = []
    categories = set()
    for item in manifest.get("representative_bad_patches", []):
        name = item["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("bad patch name must be a safe path component")
        overlay = source_path(root, item["overlay_dir"])
        results = check_variant(root, work, manifest, "bad_" + name, overlay, python)
        rejected = (completed(results["combined"]) and results["combined"]["exit_code"] != 0)
        categories.add(item["category"])
        bad_results.append({**item, "fingerprints": inventory(overlay), "commands": results, "rejected": rejected})
    checks["bad_patches_rejected"] = bool(bad_results) and all(item["rejected"] for item in bad_results)
    checks["bad_patch_categories_covered"] = {"swallow_exception", "hardcode", "remove_validation"}.issubset(categories)
    return {"task_id": manifest["task_id"], "repository_id": manifest["repository_id"], "stage": manifest["stage"], "checks": checks,
            "passed": all(checks.values()), "fingerprints": fingerprints,
            "baseline": baseline, "reference": reference, "bad_patches": bad_results}


def validate_dataset(root: Path, python: str = sys.executable, probe_python: str | None = None,
                     repository_id: str = "repo_api") -> dict:
    root = root.resolve()
    work = root / ".runtime" / ("dataset_validation_" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    stages = []
    for stage in STAGES:
        try:
            stages.append(validate_stage(root, work / stage, stage, python, repository_id))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            stages.append({"stage": stage, "passed": False, "infrastructure_error": str(exc)})
    report = {"kind": "offline_dataset_admission", "formal_results": False,
              "model_calls": 0, "ability_metrics": None, "work_dir": str(work), "stages": stages,
              "boundary": "Offline engineering checks only; no Agent trajectory, solve rate, model effect or formal experiment is measured."}
    if probe_python and repository_id == "repo_api":
        probe_file = work / "applicability.json"
        script = root / "scripts" / "validate_dataset.py"
        command = [probe_python, str(script), "--internal-probe", str(probe_file), "--root", str(root), "--probe-work", str(work / "probe")]
        result = execute(command, root, work / "logs" / "applicability.log")
        report["applicability_command"] = result
        try:
            report["applicability"] = json.loads(probe_file.read_text())
        except (OSError, ValueError) as exc:
            report["applicability"] = {"passed": False, "error": str(exc)}
    else:
        report["applicability"] = {"passed": False, "status": "not_run", "reason": "requires --probe-python with Agent dependencies"}
    probe_ok = report.get("applicability_command", {}).get("exit_code") == 0
    if repository_id == "repo_api":
        report["passed"] = all(item["passed"] for item in stages) and report["applicability"]["passed"] and probe_ok
    else:
        report["applicability"] = {"passed": True, "status": "not_applicable",
                                    "reason": "the repository has no repo_api-specific applicability probe"}
        report["passed"] = all(item["passed"] for item in stages)
    report["repository_id"] = repository_id
    report["path_base"] = "benchmark_root"

    def relative_artifacts(value):
        if isinstance(value, dict):
            return {key: (str(Path(item).relative_to(root))
                          if key in ("log", "cwd", "work_dir") and isinstance(item, str) and Path(item).is_relative_to(root)
                          else relative_artifacts(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [relative_artifacts(item) for item in value]
        return value

    report = relative_artifacts(report)
    return report


def render_markdown(report: dict) -> str:
    repository_id = report.get("repository_id", "repo_api")
    lines = [f"# {repository_id} 离线题集验收", "", "本报告仅验证题目和判分工程链路；未调用模型，不是 Agent 能力、正式实验或模型效果成绩。", "",
             f"整体离线验收：{'通过' if report['passed'] else '未通过'}。模型调用：0。", "",
             "| 阶段 | 通过 | 基线目标失败 | 基线回归 | 参考修复 | 错误补丁 |", "|---|---|---|---|---|---|"]
    for item in report["stages"]:
        c = item.get("checks", {})
        lines.append(f"| {item['stage']} | {item['passed']} | {c.get('baseline_target_fails')} | {c.get('baseline_regression_passes')} | {c.get('reference_all_pass')} | {c.get('bad_patches_rejected')} |")
    if repository_id == "repo_api":
        lines += ["", f"T3 实际适用性工程探针：{report['applicability'].get('passed')}。",
                  "真实模型历史产生后的证据提取复核尚未运行。符号检查只判断声明名称，不判断参数签名。",
                  "", "| T3 探针 | 预期状态 | 实际状态 | 通过 |", "|---|---|---|---|"]
    for case in report["applicability"].get("cases", []):
        expected = ", ".join(f"{key}={value}" for key, value in case["expected"].items())
        actual = ", ".join(f"{key}={case['actual']['checks'][key]['status']}" for key in case["expected"])
        lines.append(f"| {case['name']} | {expected} | {actual} | {case['passed']} |")
    lines += [
              "", "JSON 包含所有实际命令、退出码、完整日志路径、代码/私测/参考/错误补丁逐文件 SHA256。文件变化后本报告需重新生成。",
              "所有产物路径以 Benchmark 根目录为基准解析。",
              "", f"保留的独立工作副本及日志：`{report['work_dir']}`。", ""]
    return "\n".join(lines)


def applicability_probe(root: Path, work: Path) -> dict:
    """Exercise actual Agent extraction and applicability with explicit engineering stimuli.

    Real ToolExecutor read and edit results provide hashes, commits and actual
    minimal diffs. Reference-edit replay is an engineering probe, not a model
    solution. Real model-history revalidation remains outstanding.
    """
    import asyncio
    import ast

    from runner.stage_packages import materialize_stage_package

    from evocode.agent.tools import ToolExecutor
    from evocode.config import AppConfig
    from evocode.contracts import ToolCall
    from evocode.memory.service import MemoryService

    work.mkdir(parents=True)
    manifests = {stage: load_manifest(root, stage) for stage in ("t2_transfer", "t3_stale")}
    repos = {stage: work / stage for stage in manifests}
    package_checks = {stage: materialize_stage_package(root, manifests[stage], repos[stage]) for stage in manifests}

    class ProbeArtifacts:
        def __init__(self):
            self.counter = 0

        def save_artifact(self, content, kind):
            self.counter += 1
            path = work / "artifacts" / f"{self.counter}_{kind}.txt"
            path.parent.mkdir(exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"artifact_id": path.name}

        def get_artifact(self, artifact_id):
            return {"storage_path": str(work / "artifacts" / artifact_id)}

    service = MemoryService(None, AppConfig())
    executor = ToolExecutor(ProbeArtifacts(), AppConfig())
    evidence = {}
    for label, relative in (("renamed_symbol", "api/validation.py"),
                            ("moved_path", "api/errors.py"), ("unaffected", "api/http.py")):
        task = {"task_id": "offline_engineering_probe", "repository_id": "repo_api", "repo_path": str(repos["t2_transfer"])}
        result = asyncio.run(executor.execute(task, ToolCall(id="offline_read_" + label, name="read_file",
                              arguments={"path": relative, "max_lines": 10000})))
        if result.status != "completed":
            raise ValueError(f"actual ToolExecutor read failed: {result.output}")
        if label != "unaffected":
            # Replay an actual reference edit. ToolExecutor itself generates the
            # minimal unified diff; no artificial declaration-only diff is used.
            reference = root / manifests["t2_transfer"]["reference_dir"] / relative
            old = (repos["t2_transfer"] / relative).read_text()
            edited = asyncio.run(executor.execute(task, ToolCall(id="offline_edit_" + label, name="edit_file",
                arguments={"path": relative, "old_text": old, "new_text": reference.read_text()})))
            if edited.status != "completed":
                raise ValueError(edited.output)
            patch = Path(executor.store.get_artifact(edited.metadata["patch_artifact_id"])["storage_path"]).read_text()
            metadata = service._evidence_metadata([{"payload": {**edited.metadata, "metadata": edited.metadata}}], patch)
            evidence[label] = {"tool_result": edited.model_dump(), "actual_tool_patch": patch, "metadata": metadata}
        else:
            metadata = service._evidence_metadata([{"payload": result.metadata}], "")
            # Explicit current-source assertion for a stable-module control,
            # separate from historical patch extraction under test above.
            metadata["symbols"] = [node.name for node in ast.walk(ast.parse(Path(executor.store.get_artifact(result.metadata["artifact_id"])["storage_path"]).read_text()))
                                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
            evidence[label] = {"tool_result": result.model_dump(), "control": "AST of actual read snapshot", "metadata": metadata}
        metadata["repository_id"] = "repo_api"

    cases = []

    def check(label, metadata, repo, expected):
        actual = service._applicability(metadata, {"repository_id": "repo_api", "repo_path": str(repo)})
        statuses = {key: value["status"] for key, value in actual["checks"].items()}
        cases.append({"name": label, "expected": expected, "actual": actual,
                      "passed": all(statuses[key] == value for key, value in expected.items())})

    checks = {"renamed_symbol": {"paths": "valid", "symbols": "stale", "content": "stale", "git": "valid"},
              "moved_path": {"paths": "stale", "symbols": "unknown", "content": "stale", "git": "valid"},
              "unaffected": {"paths": "valid", "symbols": "valid", "content": "valid", "git": "valid"}}
    for label, expected in checks.items():
        check("T2_to_T3_" + label, evidence[label]["metadata"], repos["t3_stale"], expected)

    # A real descendant commit changes only an unrelated file.
    unrelated = work / "unrelated_commit"
    shutil.copytree(repos["t2_transfer"], unrelated)
    (unrelated / "OFFLINE_PROBE.txt").write_text("Unrelated engineering control.\n")
    commands = []
    for args in (["add", "OFFLINE_PROBE.txt"],
                 ["-c", "user.name=Offline Probe", "-c", "user.email=probe@example.invalid",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", "offline unrelated control"]):
        result = execute(["git", *args], unrelated, work / "logs" / f"git_{len(commands)}.log")
        commands.append(result)
        if result["exit_code"] != 0:
            raise ValueError("could not create unrelated-commit control")
    check("unrelated_commit_only", evidence["renamed_symbol"]["metadata"], unrelated,
          {"paths": "valid", "symbols": "valid", "content": "valid", "git": "valid"})

    signature = work / "signature_only"
    shutil.copytree(repos["t2_transfer"], signature)
    path = signature / "api/validation.py"
    source = path.read_text()
    changed, count = re.subn(r"def require_count\(([^)]*)\)", r"def require_count(\1, _offline_probe=None)", source, count=1)
    if count != 1:
        raise ValueError("require_count declaration missing for signature-only control")
    path.write_text(changed)
    check("signature_only_name_unchanged", evidence["renamed_symbol"]["metadata"], signature,
          {"paths": "valid", "symbols": "valid", "content": "stale", "git": "valid"})
    extracted = all(item["metadata"]["file_paths"] and item["metadata"]["content_evidence"] for item in evidence.values())
    required_symbol = "require_count" in evidence["renamed_symbol"]["metadata"]["symbols"]
    return {"passed": extracted and required_symbol and all(item["passed"] for item in cases),
            "kind": "actual_Agent_methods_offline_engineering_probe", "package_checks": package_checks,
            "agent_implementation_sha256": {str(path.relative_to(root.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root.parent / "src/evocode/agent/tools.py", root.parent / "src/evocode/memory/service.py")},
            "evidence": evidence, "cases": cases, "control_commands": commands,
            "evidence_extraction_passed": extracted and required_symbol,
            "model_history_revalidation": "not_run", "model_calls": 0,
            "boundary": "ToolExecutor.read_file/edit_file generate actual snapshots and minimal reference-repair diffs. MemoryService extraction and applicability are real methods. Reference replay is offline engineering evidence, not model-generated history."}
