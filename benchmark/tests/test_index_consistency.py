import importlib.util
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("index_consistency", ROOT / "runner" / "index_consistency.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE memory_chunks (chunk_id TEXT PRIMARY KEY, task_id TEXT, repository_id TEXT)")
        db.executemany("INSERT INTO memory_chunks VALUES (?, ?, ?)", [("a", "t1", "r1"), ("b", "t2", "r2")])
    return path


@pytest.fixture
def manifest():
    return {"collection": "frozen", "embedding_model": "test-model", "points_complete": True}


def point(identifier, task, repository):
    return {"id": identifier, "payload": {"task_id": task, "repository_id": repository}}


def test_matching_inventory_fingerprint_is_order_independent(snapshot, manifest):
    points = [point("a", "t1", "r1"), point("b", "t2", "r2")]
    first = module.compare_index_inventory(snapshot, points, manifest)
    second = module.compare_index_inventory(snapshot, reversed(points), manifest)
    assert first["matches"] is True
    assert first["inventory_sha256"] == second["inventory_sha256"]
    assert first["scope"] == "identity_mapping_only"


def test_missing_extra_and_wrong_payload_reported(snapshot, manifest):
    result = module.compare_index_inventory(snapshot, [point("a", "wrong", "r1"), point("c", "t3", "r3")], manifest)
    assert result["matches"] is False
    assert result["missing_point_ids"] == ["b"]
    assert result["extra_point_ids"] == ["c"]
    assert result["field_mismatches"][0]["chunk_id"] == "a"
    assert result["field_mismatches"][0]["sqlite"]["task_id"] == "t1"


def test_duplicate_and_incomplete_inventory_are_rejected(snapshot, manifest):
    with pytest.raises(ValueError, match="duplicate"):
        module.compare_index_inventory(snapshot, [point("a", "t1", "r1"), point("a", "t1", "r1")], manifest)
    with pytest.raises(ValueError, match="points_complete"):
        module.compare_index_inventory(snapshot, [], {**manifest, "points_complete": False})
    with pytest.raises(ValueError, match="embedding_model"):
        module.compare_index_inventory(snapshot, [], {**manifest, "embedding_model": ""})


def test_fingerprint_binds_model_and_collection(snapshot, manifest):
    points = [point("a", "t1", "r1"), point("b", "t2", "r2")]
    original = module.compare_index_inventory(snapshot, points, manifest)["inventory_sha256"]
    changed = module.compare_index_inventory(snapshot, points, {**manifest, "collection": "other"})["inventory_sha256"]
    assert original != changed
