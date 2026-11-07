from __future__ import annotations

import sys
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from scripts.run_nsrr_audit import (  # noqa: E402
    ALLOWED_LABELS,
    EXPECTED_CANDIDATES,
    run_audit,
    verify_freeze_manifest,
)


class NsrrAuditTests(unittest.TestCase):
    def test_frozen_nsrr_inventory_and_independent_observations(self):
        report, labels_doc = run_audit()
        rows = labels_doc["observed_labels"]

        self.assertEqual(len(rows), 8)
        self.assertEqual({row["candidate_id"] for row in rows}, set(EXPECTED_CANDIDATES))
        self.assertLessEqual({row["observed_label"] for row in rows}, ALLOWED_LABELS)
        self.assertEqual(report["execution"], "offline deterministic policy shim; no model or API invoked")
        self.assertEqual(report["observed_label_counts"], {
            "harmful": 4,
            "neutral": 2,
            "beneficial": 1,
            "unknown": 1,
        })


    def test_candidate_categories_do_not_determine_labels(self):
        _, labels_doc = run_audit()
        by_id = {row["candidate_id"]: row for row in labels_doc["observed_labels"]}

        self.assertEqual(by_id["nsrr_dup_headers"]["observed_label"], "neutral")
        self.assertEqual(by_id["nsrr_dup_config_null"]["observed_label"], "neutral")
        self.assertEqual(by_id["nsrr_benefit_decimal"]["observed_label"], "beneficial")
        self.assertEqual(by_id["nsrr_benefit_cache"]["observed_label"], "unknown")
        self.assertEqual(by_id["nsrr_benefit_cache"]["rule_id"], "performance_claim_lacks_minimum_measurements")


    def test_frozen_hashes_cover_candidate_skill_task_tests_rubric_and_runner(self):
        frozen, freeze_sha = verify_freeze_manifest()
        self.assertGreaterEqual(len(frozen["inputs"]), 110)
        self.assertEqual(frozen["runner_sha256"], frozen["inputs"]["scripts/run_nsrr_audit.py"])
        self.assertEqual(len(freeze_sha), 64)
        self.assertTrue(all(len(value) == 64 for value in frozen["inputs"].values()))
        self.assertIn("runner/nsrr_audit_tasks.py", frozen["inputs"])
        self.assertIn("scripts/qualify_nsrr_audit_fixtures.py", frozen["inputs"])
        self.assertIn("tests/test_nsrr_audit_fixture_qualification.py", frozen["inputs"])
        for candidate_id in EXPECTED_CANDIDATES:
            task_id = candidate_id.replace("nsrr_", "nsrr_audit_", 1)
            task_root = f"private/gate_audit/tasks/{task_id}/fixture"
            self.assertTrue(any(path.startswith(task_root + "/code/") for path in frozen["inputs"]))
            self.assertTrue(any(path.startswith(task_root + "/reference/") for path in frozen["inputs"]))
            self.assertTrue(any(path.startswith(task_root + "/private_tests/") for path in frozen["inputs"]))
            self.assertIn(f"{task_root}/task.md", frozen["inputs"])
            self.assertIn(f"private/gate_audit/tasks/{task_id}/fixture_manifest.json", frozen["inputs"])
        self.assertFalse(any("gate_selection" in path for path in frozen["inputs"]))
        self.assertFalse(any("t4_heldout" in path for path in frozen["inputs"]))


    def test_report_declares_offline_evidence_boundary(self):
        report, _ = run_audit()
        self.assertIn("does not run or approximate an LLM Gate", report["limitations"])
        self.assertIn("benchmark/private/gate_selection/", report["excluded_sources"])
        self.assertIn("main-task learning scores or Gate decisions", report["excluded_sources"])


if __name__ == "__main__":
    unittest.main()
