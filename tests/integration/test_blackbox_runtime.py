from pathlib import Path
import pytest

from evocode.config import AppConfig
from evocode.persistence import EventStore
from evocode.skills.registry import SkillRegistry
from scripts.blackbox_runtime import Instance, TaskExecutionTimedOut, clone_state, task_telemetry


def test_cloned_state_rebases_artifacts_and_skill_files(tmp_path):
    source = tmp_path / "source"
    store = EventStore(source / "evocode.db", source / "artifacts")
    try:
        artifact = store.save_artifact("historical evidence")
        skill = SkillRegistry(store, source).create_candidate("example", "frozen skill", level="general")
        clone = tmp_path / "clone"
        clone_state(source, clone)
        copied = EventStore(clone / "evocode.db", clone / "artifacts")
        try:
            artifact_path = Path(copied.get_artifact(artifact["artifact_id"])["storage_path"])
            assert artifact_path.is_relative_to(clone)
            assert artifact_path.read_text() == "historical evidence"
            version = SkillRegistry(copied, clone).load(skill["version_id"])
            assert Path(version["file_path"]).is_relative_to(clone)
            assert version["content"] == "frozen skill"
        finally:
            copied.close()
    finally:
        store.close()


def test_observes_only_evidence_in_contexts_with_a_completed_model_call():
    def event(kind, **payload):
        return {"event_type": kind, "payload": payload}

    events = [
        event("context.assembled", context_id="unused", role="coder",
              node_context={"context_id": "unused", "evidence_refs": ["old"]}),
        event("context.assembled", context_id="sent", role="coder",
              node_context={"context_id": "sent", "evidence_refs": ["current"]}),
        event("tool.completed", tool_call_id="memory", metadata={"memory_retrieval": {
            "evidence_event_ids": ["via-tool", "dropped-from-result"],
        }}),
        event("model.usage", context_id="sent", provider_total_tokens=12),
    ]
    telemetry = task_telemetry(events, {"sent": [
        {"role": "tool", "tool_call_id": "memory", "content": '["via-tool"]'},
    ]})
    assert telemetry["reported_tokens"] == 12
    assert [row["evidence_refs"] for row in telemetry["evidence_contexts"]] == [["current"]]
    assert telemetry["evidence_contexts"][0]["tool_evidence_refs"] == ["via-tool"]


@pytest.mark.asyncio
async def test_run_task_polls_through_planning_and_replanning_states(tmp_path):
    # 验证 run_task 不会把 planning/replanning 误判为终态并提前退出轮询
    instance = Instance(AppConfig(data_dir=str(tmp_path / "state")))
    statuses = iter(["created", "planning", "running", "replanning", "completed"])
    current_status = "created"

    async def fake_request(method, path, json=None):
        nonlocal current_status
        if method == "POST" and path == "/api/tasks":
            return {"task_id": "t-1", "status": "created"}
        if method == "GET" and path == "/api/tasks/t-1":
            current_status = next(statuses, "completed")
            return {"task_id": "t-1", "status": current_status}
        if method == "GET" and path == "/api/jobs":
            active = [] if current_status == "completed" else ["t-1"]
            return {"active": active, "worker_error": None}
        if method == "GET" and path == "/api/tasks/t-1/events":
            return []
        raise AssertionError(f"Unexpected request: {method} {path}")

    instance.request = fake_request
    result = await instance.run_task(tmp_path, "test prompt", "repo-1")
    assert result["status"] == "completed"


async def test_run_task_classifies_execution_timeout_and_stops_task(tmp_path):
    instance = Instance(AppConfig(data_dir=str(tmp_path / "state")), timeout=0.01)
    calls = []

    async def fake_request(method, path, json=None):
        calls.append((method, path))
        if path == "/api/jobs":
            return {"active": ["t-1"]}
        return {"task_id": "t-1", "status": "running"}

    instance.request = fake_request
    with pytest.raises(TaskExecutionTimedOut, match="t-1"):
        await instance.run_task(tmp_path, "test prompt", "repo-1")
    assert calls[-1] == ("POST", "/api/tasks/t-1/stop")
