"""Paired Raw-RAG black-box evaluation runner.

Formal execution uses independent EvoCode HTTP instances implemented by
``scripts/blackbox_runtime.py``. The small helpers below remain import-compatible
for older offline fixture checks; none of them creates a task or queries memory.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evidence_provenance(evidence: list[dict], messages: list[dict], role: str) -> list[dict]:
    """Legacy offline helper: check whether an evidence string occurs in messages."""
    request = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    request_hash = canonical_hash(messages)
    return [{
        "event_id": item.get("event_id"),
        "text_sha256": hashlib.sha256(str(item.get("text", "")).encode("utf-8")).hexdigest(),
        "role": role,
        "request_sha256": request_hash,
        "entered_request": bool(item.get("text") and item["text"] in request),
    } for item in evidence]


def assert_off_isolated(requests: list[dict]) -> None:
    """Legacy offline assertion for model-call fixtures."""
    for call in requests:
        if call.get("evidence"):
            raise ValueError("OFF branch received memory evidence")
        encoded = json.dumps(call.get("messages", []), ensure_ascii=False)
        if any(tool.get("function", {}).get("name") == "memory_search"
               for tool in (call.get("tools") or [])):
            raise ValueError("OFF branch exposed memory_search")
        if '"name": "memory_search"' in encoded or '"name":"memory_search"' in encoded:
            raise ValueError("OFF branch request contains a memory tool call")
        if "Untrusted retrieved evidence:" in encoded:
            raise ValueError("OFF branch request contains injected Raw-RAG context")


def retrieval_mode(config: dict, qdrant_reachable: bool | None) -> tuple[str, str | None]:
    rag = config.get("raw_rag", {})
    if not rag.get("embedding_model"):
        return "unavailable", "Embedding model is required"
    if not rag.get("rerank_enabled") or not rag.get("rerank_model") or not rag.get("rerank_top_k"):
        return "unavailable", "Reranker is required"
    if qdrant_reachable is False:
        return "unavailable", "Qdrant is unreachable"
    if qdrant_reachable is None:
        return "hybrid", "Providers are verified when execution starts"
    return "hybrid", None


def pair_status(records: list[dict], expected_task_ids: list[str]) -> str:
    by_id = {row.get("task_id"): row for row in records}
    if len(expected_task_ids) != 2 or any(task_id not in by_id for task_id in expected_task_ids):
        return "invalid/incomplete"
    on, off = (by_id.get(task_id) for task_id in expected_task_ids)
    return "complete" if on and off and on.get("condition") == "ON" and off.get("condition") == "OFF" else "invalid/incomplete"


def validate_pair_alignment(on: dict, off: dict) -> None:
    if on.get("condition") != "ON" or off.get("condition") != "OFF":
        raise ValueError("pair must contain ON followed by OFF records")
    for field in ("task_spec_sha256", "config_sha256", "workspace_template_sha256"):
        if not on.get(field) or on.get(field) != off.get(field):
            raise ValueError(f"ON/OFF pair is not aligned: {field}")


def task_outcome(agent_terminal_status: str, acceptance_protected: bool, acceptance_passed: bool) -> dict:
    return {
        "agent_terminal_status": agent_terminal_status,
        "agent_execution_completed": agent_terminal_status == "completed",
        "task_passed": bool(acceptance_protected and acceptance_passed),
        "tests_protected": bool(acceptance_protected),
        "protected_tests_passed": bool(acceptance_passed),
        "acceptance_outcome": "unverified" if not acceptance_protected else "passed" if acceptance_passed else "failed",
    }


def structural_pair_complete(records: list[dict]) -> bool:
    by_condition = {row.get("condition"): row for row in records}
    return set(by_condition) >= {"ON", "OFF"} and all(
        row.get("branch_returned") and row.get("rawrag_condition_clear")
        and row.get("tokens_complete") is not None and row.get("acceptance_aligned")
        for row in (by_condition["ON"], by_condition["OFF"])
    )


class CappedClient:
    """Legacy helper retained for offline budget-policy tests only."""
    def __init__(self, delegate, max_completions: int, max_tokens: int):
        self.delegate = delegate
        self.max_completions = max_completions
        self.max_tokens = max_tokens
        self.calls: list[dict] = []
        self.estimated_tokens = 0
        self.reported_tokens = 0
        self.usage_complete = True
        self.cap_details: dict | None = None

    @property
    def cap_hit(self) -> bool:
        return self.cap_details is not None

    async def complete(self, messages, *, role, tools=None, **kwargs):
        if len(self.calls) >= self.max_completions:
            self.cap_details = {"reason": "completion_limit", "role": role,
                                "completed_or_attempted": len(self.calls), "limit": self.max_completions}
            raise RuntimeError("completion cap reached before model request")
        request = {"messages": messages, "tools": tools, "role": role,
                   "response_format": kwargs.get("response_format")}
        estimate = max(1, len(json.dumps(request, ensure_ascii=False).encode("utf-8")) // 3)
        estimate += min(int(kwargs.get("max_tokens") or 0), 1024)
        base_tokens = self.reported_tokens if self.usage_complete else self.estimated_tokens
        if base_tokens + estimate > self.max_tokens:
            self.cap_details = {"reason": "token_reservation", "role": role,
                                "used_or_reserved": base_tokens, "next_request_estimate": estimate,
                                "limit": self.max_tokens}
            raise RuntimeError("token cap reservation rejected before model request")
        self.estimated_tokens = base_tokens + estimate
        record = {"role": role, "messages": messages, "tools": tools,
                  "request_sha256": canonical_hash(messages), "estimated_tokens": estimate,
                  "started_at_monotonic": time.monotonic()}
        self.calls.append(record)
        started = time.monotonic()
        try:
            response = await self.delegate.complete(messages, role=role, tools=tools, **kwargs)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["latency_seconds"] = time.monotonic() - started
            self.usage_complete = False
            raise
        record["latency_seconds"] = time.monotonic() - started
        usage = getattr(response, "usage", None) or {}
        if "total_tokens" not in usage:
            self.usage_complete = False
        else:
            record["reported_tokens"] = int(usage["total_tokens"])
            self.reported_tokens += record["reported_tokens"]
            if self.usage_complete:
                self.estimated_tokens = self.reported_tokens
            if self.reported_tokens > self.max_tokens:
                self.cap_details = {"reason": "reported_token_limit", "role": role,
                                    "reported_tokens": self.reported_tokens, "limit": self.max_tokens}
                record["post_response_cap_exceeded"] = True
                raise RuntimeError("reported token usage exceeded cap")
        return response


def main(argv=None) -> int:
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from rawrag_blackbox import main as blackbox_main
    return blackbox_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
