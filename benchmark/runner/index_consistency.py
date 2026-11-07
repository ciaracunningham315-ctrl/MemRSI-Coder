"""Compare a frozen SQLite chunk snapshot with an exported Qdrant point inventory.

Caller must export all pages from an immutable collection and provide its model
and collection identity. This check makes no network requests and does not
verify vector contents, FTS contents, model identity, or snapshot immutability.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable


def compare_index_inventory(db_path: str | Path, points: Iterable[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    """Return discrepancies and a stable fingerprint of identities and mappings.

    Each point uses Qdrant's ``{id, payload: {task_id, repository_id}}`` shape.
    The caller attests a complete export by setting ``points_complete`` true.
    The supplied DB must already be a consistent, frozen SQLite snapshot.
    """
    for name in ("collection", "embedding_model"):
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            raise ValueError(f"manifest.{name} must be a nonempty string")
    if manifest.get("points_complete") is not True:
        raise ValueError("manifest.points_complete must attest a complete inventory")
    path = Path(db_path).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        rows = db.execute("SELECT chunk_id, task_id, repository_id FROM memory_chunks").fetchall()

    def valid(value):
        return isinstance(value, str) and bool(value.strip())

    expected = {}
    for chunk_id, task_id, repository_id in rows:
        if not all(valid(value) for value in (chunk_id, task_id, repository_id)):
            raise ValueError("SQLite memory_chunks contains an invalid identity")
        if chunk_id in expected:
            raise ValueError(f"duplicate SQLite chunk_id: {chunk_id}")
        expected[chunk_id] = {"task_id": task_id, "repository_id": repository_id}
    actual = {}
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
            raise ValueError("point and point payload must be mappings")
        chunk_id = point.get("id")
        payload = point["payload"]
        if not all(valid(value) for value in (chunk_id, payload.get("task_id"), payload.get("repository_id"))):
            raise ValueError("point contains an invalid identity")
        if chunk_id in actual:
            raise ValueError(f"duplicate Qdrant point id: {chunk_id}")
        actual[chunk_id] = {"task_id": payload["task_id"], "repository_id": payload["repository_id"]}

    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    mismatches = [
        {"chunk_id": key, "sqlite": expected[key], "qdrant": actual[key]}
        for key in sorted(expected.keys() & actual.keys()) if expected[key] != actual[key]
    ]
    canonical = {
        "collection": manifest["collection"], "embedding_model": manifest["embedding_model"],
        "sqlite": [{"chunk_id": key, **expected[key]} for key in sorted(expected)],
        "qdrant": [{"chunk_id": key, **actual[key]} for key in sorted(actual)],
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"matches": not (missing or extra or mismatches), "sqlite_count": len(expected),
            "qdrant_count": len(actual), "missing_point_ids": missing, "extra_point_ids": extra,
            "field_mismatches": mismatches, "inventory_sha256": digest,
            "collection": manifest["collection"], "embedding_model": manifest["embedding_model"],
            "scope": "identity_mapping_only"}
