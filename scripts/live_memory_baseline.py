"""Verify real hybrid retrieval using synthetic events and a disposable Qdrant collection."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from evocode.agent.tools import ToolExecutor
from evocode.config import load_config
from evocode.context.assembler import ContextAssembler
from evocode.contracts import RetrievalResult, TaskSpec, ToolCall
from evocode.memory import MemoryService
from evocode.persistence import EventStore


class MeteredMemory(MemoryService):
    def __init__(self, store, config):
        super().__init__(store, config)
        self.calls = {"embedding": 0, "rerank": 0}
        self.dense_ids = []

    async def _embed(self, texts, config):
        if not config.embedding_model:
            return await super()._embed(texts, config)
        if self.calls["embedding"] >= 4:
            raise RuntimeError("Embedding request limit reached")
        self.calls["embedding"] += 1
        return await super()._embed(texts, config)

    async def _dense(self, query, task, config):
        ids = await super()._dense(query, task, config)
        self.dense_ids = ids
        return ids

    async def _rerank(self, query, rows, config):
        if self.calls["rerank"] >= 1:
            raise RuntimeError("Rerank request limit reached")
        self.calls["rerank"] += 1
        return await super()._rerank(query, rows, config)


async def run(qdrant_url, report):
    if urlsplit(qdrant_url).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use a local disposable Qdrant instance")
    config = load_config()
    rag = config.raw_rag
    if not rag.embedding_model or not rag.rerank_model:
        raise ValueError("Configure the embedding and reranking models first")
    rag.enabled = rag.rerank_enabled = True
    rag.qdrant_url = qdrant_url.rstrip("/")
    rag.collection = f"evocode_live_memory_{uuid4().hex}"
    rag.final_evidence_count = 3
    rag.dense_top_k = 4
    rag.rerank_top_k = 2
    config.skills.enabled = False
    collection_url = f"{rag.qdrant_url}/collections/{rag.collection}"
    report.update(collection=rag.collection, embedding_model=rag.embedding_model, rerank_model=rag.rerank_model)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        if (await client.get(collection_url)).status_code != 404:
            raise RuntimeError("Expected an absent collection in a reachable Qdrant instance")
        with TemporaryDirectory(prefix="evocode-memory-") as directory:
            config.data_dir = directory
            store = EventStore(config.sqlite_path)
            memory = MeteredMemory(store, config)
            report["provider_requests"] = memory.calls
            try:
                workspace = Path(directory) / "workspace"
                other = workspace.with_name("other")
                workspace.mkdir()
                other.mkdir()
                seed = store.create_task(TaskSpec(repo_path=str(workspace), prompt="Background sync investigation"), config.snapshot())
                source = store.append_event(seed["task_id"], "test.result", {
                    "output": "EVORAG429 sync requests passed after honoring Retry-After",
                    "file_paths": ["obsolete_worker.py"],
                })
                store.create_task(TaskSpec(repo_path=str(other), prompt="EVORAG429 unrelated repository"), config.snapshot())
                report["index"] = await memory.index_pending(limit=3)
                if report["index"] != {"status": "ready", "indexed": 3, "failed": 0}:
                    raise RuntimeError("Expected all three synthetic events to be indexed")

                followup = store.create_task(TaskSpec(repo_path=str(workspace), prompt="Recall the earlier throttle check"), config.snapshot())
                call = ToolCall(id="memory-live", name="memory_search", arguments={"query": "EVORAG429"})
                tool = await ToolExecutor(store, config, memory).execute(followup, call)
                if tool.status != "completed":
                    raise RuntimeError("Memory tool failed")
                result = RetrievalResult.model_validate_json(tool.output)
                with store.transaction() as db:
                    own_ids = {r[0] for r in db.execute(
                        "SELECT chunk_id FROM memory_chunks WHERE repository_id=?", (seed["repository_id"],)
                    )}
                match = next((ref for ref in result.evidence if ref.event_id == source["event_id"]), None)
                assembler = ContextAssembler(store, None, config)
                await assembler.build(followup, "coder", [], evidence=result.evidence)
                context_id = assembler.last_context_id
                context = store.read_events(task_id=followup["task_id"])[-1]["payload"]

                offline = config.model_copy(deep=True)
                offline.raw_rag.embedding_model = ""
                offline.raw_rag.rerank_enabled = False
                fallback = await memory.search(call.arguments["query"], {**followup, "config": offline.snapshot()})
                disabled = config.model_copy(deep=True)
                disabled.raw_rag.enabled = False
                disabled_task = {**followup, "config": disabled.snapshot()}
                calls_before = memory.calls.copy()
                blocked = await ToolExecutor(store, disabled, memory).execute(disabled_task, call)
                no_memory = await memory.search(call.arguments["query"], disabled_task)
                closed = await ContextAssembler(store, None, disabled).build(disabled_task, "coder", [], evidence=result.evidence)

                checks = {
                    "hybrid_and_reranked": result.mode == "hybrid" and result.status == "ok" and memory.calls == {"embedding": 4, "rerank": 1},
                    "dense_repository_scope": bool(memory.dense_ids) and set(memory.dense_ids) <= own_ids
                    and all(ref.task_id == seed["task_id"] for ref in result.evidence),
                    "original_event_and_session": match is not None and match.task_id == seed["task_id"]
                    and match.session_id == seed["session_id"] != followup["session_id"],
                    "stale_path_and_neighbor_reference": match is not None and match.state_validity == "stale"
                    and len(match.metadata["related_event_ids"]) > 1,
                    "context_preserves_evidence_ref": source["event_id"] in context["node_context"]["evidence_refs"],
                    "fts_fallback": fallback.mode == "fts" and fallback.status == "degraded"
                    and any(ref.event_id == source["event_id"] for ref in fallback.evidence),
                    "disabled_tool_search_and_context": blocked.status == "disabled" and no_memory.status == "memory_disabled"
                    and "EVORAG429" not in json.dumps(closed) and memory.calls == calls_before,
                }
                report.update(
                    checks=checks, result={"status": result.status, "mode": result.mode, "reason": result.reason},
                    source_event_id=source["event_id"], source_session_id=seed["session_id"],
                    target_session_id=followup["session_id"], context_id=context_id,
                    dense_chunk_ids=memory.dense_ids,
                    evidence=[ref.model_dump(exclude={"text"}) for ref in result.evidence],
                )
                if not all(checks.values()):
                    raise RuntimeError("Failed checks: " + ", ".join(key for key, value in checks.items() if not value))
            finally:
                store.close()
                (await client.delete(collection_url)).raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", required=True)
    args = parser.parse_args()
    path = Path("data/live-memory-baselines") / f"{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed"}
    try:
        asyncio.run(run(args.qdrant_url, report))
        report["status"] = "passed"
    finally:
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        print(path)


if __name__ == "__main__":
    main()
