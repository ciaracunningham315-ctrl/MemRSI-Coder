"""Record offline evidence for the repo_cli T3 configuration API transition."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T2 = ROOT / "private/references/repo_cli/t2_transfer/code"
T3 = ROOT / "scenarios/repo_cli/t3_stale/code"
MANIFEST = ROOT / "scenarios/repo_cli/t3_stale/manifest.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def symbols(path: Path) -> set[str]:
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def build_report() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = manifest["stale_transition"]
    moved_paths = ("cli/config.py", "cli/mirror_config.py")
    path_checks = {
        "old_config_helper_exists_in_t2_reference": (T2 / "cli/config.py").is_file(),
        "old_command_helper_exists_in_t2_reference": (T2 / "cli/mirror_config.py").is_file(),
        "old_config_helper_absent_in_t3": not (T3 / "cli/config.py").exists(),
        "old_command_helper_absent_in_t3": not (T3 / "cli/mirror_config.py").exists(),
        "new_settings_helper_exists_in_t3": (T3 / "cli/settings.py").is_file(),
        "legacy_ini_format_documented_in_t2": "repo-cli.ini" in (T2 / "docs/configuration.md").read_text(encoding="utf-8"),
        "json_snapshot_format_documented_in_t3": ".repo-cli.json" in (T3 / "docs/configuration.md").read_text(encoding="utf-8"),
    }
    t2_old_symbols = symbols(T2 / "cli/config.py") | symbols(T2 / "cli/mirror_config.py")
    t3_settings_symbols = symbols(T3 / "cli/settings.py")
    path_checks["old_resolver_symbols_present_in_t2"] = {name: name in t2_old_symbols for name in ("ConfigResolver", "resolve", "mirror_workers")}
    path_checks["old_resolver_symbols_absent_in_t3"] = {name: name not in (symbols(T3 / "cli/main.py") | t3_settings_symbols | symbols(T3 / "cli/validation.py")) for name in ("ConfigResolver", "resolve", "mirror_workers")}
    path_checks["replacement_symbol_load_options_present"] = "load_options" in t3_settings_symbols
    t2_validation = T2 / "cli/validation.py"
    t3_validation = T3 / "cli/validation.py"
    retained_symbols = symbols(t2_validation) & symbols(t3_validation)
    path_checks["validation_helper_path_retained"] = t2_validation.is_file() and t3_validation.is_file()
    path_checks["bounded_decimal_retained"] = "bounded_decimal" in retained_symbols
    path_checks["retained_helper_content_unchanged"] = sha(t2_validation) == sha(t3_validation)
    declared_matches = (
        set(declared.get("removed_paths", [])) >= {"cli/config.py", "cli/mirror_config.py"}
        and "cli/settings.py" in declared.get("added_paths", [])
        and "cli/validation.py" in declared.get("retained_paths", [])
        and "bounded_decimal" in declared.get("retained_symbols", [])
    )
    passed = all(value for key, value in path_checks.items() if isinstance(value, bool)) and all(all(x.values()) for key, x in path_checks.items() if isinstance(x, dict)) and declared_matches
    return {
        "kind": "repo_cli_t3_offline_transition_evidence",
        "passed": passed,
        "model_calls": 0,
        "real_agent_history_collected": False,
        "formal_results": False,
        "path_checks": path_checks,
        "t2_reference_validation_sha256": sha(t2_validation),
        "t3_baseline_validation_sha256": sha(t3_validation),
        "declared_transition_matches_observed_files": declared_matches,
        "transition": {
            "removed_paths": list(moved_paths),
            "replacement_path": "cli/settings.py",
            "replaced_config_representation": "repo-cli.ini per-command adapter",
            "current_representation": "per-command JSON snapshot via load_options",
            "still_valid_helper": "cli.validation.bounded_decimal",
        },
        "boundary": "This is a static file/symbol/hash check plus offline dataset admission, not a real Agent history or applicability run.",
    }


def main() -> int:
    report = build_report()
    target = ROOT / "reports/repo_cli_transition_validation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# repo_cli T3 结构变化核对",
        "",
        "本报告记录离线文件、符号和哈希证据；没有真实 Agent 历史轨迹、模型调用或正式结果。",
        "",
        f"检查结果：{'通过' if report['passed'] else '未通过'}。",
        "",
        "| 检查 | 结果 |",
        "|---|---:|",
    ]
    for key, value in report["path_checks"].items():
        lines.append(f"| `{key}` | `{value}` |")
    lines += [
        "",
        f"T2 历史代码中的 `{report['transition']['still_valid_helper']}` 与 T3 内容哈希相同，保留参数边界检查经验；旧配置入口与 INI 表示已移除，T3 使用新的 JSON snapshot loader。",
        "实际 Agent 轨迹尚未采集，故这里的 stale/valid 区分仅是可复核的代码结构证据。",
        "",
    ]
    target.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print("repo_cli T3 transition check:", "PASS" if report["passed"] else "FAIL")
    print(target)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
