import asyncio
import json
import re
from contextlib import contextmanager

import pytest

from evocode.config import AppConfig
from evocode.context import ContextAssembler


class FakeStore:
    def __init__(self):
        self.messages = []
        self.session = {
            "revision": 0,
            "summary": "",
            "summary_through_seq": 0,
            "working_state": {},
        }
        self.reads = []
        self.artifacts = {}
        self.events = []
        self.task_states = {}

    def read_conversation_messages(self, session_id, *, after_seq=0, before_seq=None, limit=None):
        self.reads.append((session_id, after_seq, before_seq, limit))
        return list(self.messages)

    def get_session(self, session_id):
        return {"session": dict(self.session)}

    def update_session_context(self, session_id, summary, summary_through_seq, working_state=None):
        assert session_id == "session-1"
        if summary_through_seq >= self.session["summary_through_seq"]:
            self.session["summary"] = summary
            self.session["summary_through_seq"] = summary_through_seq
        if working_state is not None:
            self.session["working_state"] = working_state

    @contextmanager
    def transaction(self):
        yield self

    def update_task(self, task_id, state=None, status=None):
        if state is not None:
            self.task_states[task_id] = dict(state)
        return {"task_id": task_id, "status": status or "running", "state": state}

    def save_artifact(self, content, kind="text"):
        artifact_id = f"artifact-{len(self.artifacts) + 1}"
        self.artifacts[artifact_id] = {"content": content, "kind": kind}
        return {"artifact_id": artifact_id}

    def append_event(self, task_id, event_type, payload):
        event = {"task_id": task_id, "event_type": event_type, "payload": payload}
        self.events.append(event)
        return event


class SessionScopedStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.sessions = {
            "session-a": {"revision": 1, "summary": "A summary", "summary_through_seq": 1, "working_state": {}},
            "session-b": {"revision": 7, "summary": "B summary", "summary_through_seq": 4, "working_state": {}},
        }
        self.conversations = {
            "session-a": [{"message_id": "a1", "role": "user", "content": "A private request", "seq": 1}],
            "session-b": [{"message_id": "b1", "role": "user", "content": "B separate request", "seq": 4}],
        }

    def read_conversation_messages(self, session_id, *, after_seq=0, before_seq=None, limit=None):
        self.reads.append((session_id, after_seq, before_seq, limit))
        return list(self.conversations[session_id])

    def get_session(self, session_id):
        return {"session": dict(self.sessions[session_id])}


class UnusedModel:
    async def complete(self, *args, **kwargs):
        raise AssertionError("Context assembly must not make model calls")


class SummaryModel:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, role="planner", max_tokens=None, **kwargs):
        payload = json.loads(messages[1]["content"])
        self.calls.append({"role": role, "payload": payload, "max_tokens": max_tokens})
        previous = payload.get("previous_summary", "")
        facts = []
        for item in payload["new_history"]:
            if isinstance(item, list):
                facts.extend(str(entry.get("content", ""))[:80] for entry in item)
            else:
                facts.append(str(item.get("content", ""))[:80])
        content = " | ".join(filter(None, [previous, *facts]))
        return type("Response", (), {"content": content, "usage": {"total_tokens": 17}})()


def config():
    result = AppConfig()
    result.raw_rag.enabled = False
    result.skills.enabled = False
    return result


def task(state=None):
    return {
        "task_id": "task-1",
        "session_id": "session-1",
        "prompt": "先检查 README 并说明项目结构",
        "acceptance_command": [],
        "state": state or {},
    }


def decode_context(messages):
    item=next(message for message in messages if message.get('content','').startswith('Current task context (authoritative current requirements):'))
    return json.loads(item["content"].split("\n", 1)[1])


def test_planner_reads_fresh_session_and_keeps_summary_coverage(tmp_path):
    store = FakeStore()
    store.messages = [
        {"message_id": "m1", "role": "user", "kind": "user", "content": "以前的目标", "seq": 1},
        {"message_id": "m2", "role": "assistant", "kind": "final", "content": "旧答复", "seq": 2},
        {"message_id": "m3", "role": "user", "kind": "user", "content": "这次只看 README", "seq": 8},
    ]
    store.session.update(revision=3, summary="之前讨论过重构", summary_through_seq=5)
    assembler = ContextAssembler(store, UnusedModel(), config())

    messages = asyncio.run(assembler.build(task({"plan": {"constraints": ["保持兼容"]}}), "planner", []))
    payload = decode_context(messages)

    assert store.reads == [("session-1", 0, None, None)]
    assert payload["latest_user_requirement"] == "这次只看 README"
    assert payload["session_revision"] == 3
    assert "Call read_session_messages" in payload["history_reader"]
    assert any("This is context" in message.get('content','') for message in messages)
    assert messages[-1]['content'].startswith('Current task context (authoritative current requirements):')
    event = store.events[-1]
    assert event["event_type"] == "context.assembled"
    assert event["payload"]["role"] == "planner"
    assert event["payload"]["session_revision"] == 3
    artifact_id = event["payload"]["artifact_id"]
    assert store.artifacts[artifact_id]["kind"] == "node_context"
    assert event["payload"]["node_context"]["context_id"] == event["payload"]["context_id"]

    # Each call sees persisted changes made after the previous assembly.
    store.session["revision"] = 4
    store.messages.append({"message_id": "m4", "role": "user", "kind": "user", "content": "现在改看 pyproject", "seq": 9})
    changed = asyncio.run(assembler.build(task(), "planner", []))
    assert decode_context(changed)["latest_user_requirement"] == "现在改看 pyproject"
    assert store.events[-1]["payload"]["session_revision"] == 4


def test_coder_gets_assignment_not_session_transcript_and_raw_rag_is_closed():
    store = FakeStore()
    store.messages = [
        {"message_id": "m1", "role": "user", "kind": "user", "content": "敏感的旧会话细节", "seq": 1},
        {"message_id": "m2", "role": "user", "kind": "user", "content": "只检查 src/main.py", "seq": 2},
    ]
    assignment = {
        "step_id": "inspect",
        "goal": "检查 src/main.py",
        "user_requirements": ["只检查 src/main.py"],
        "constraints": ["不要修改文件"],
        "done_when": ["说明入口"],
        "relevant_context": ["入口文件由用户指定"],
        "previous_results": [],
        "session_revision": 2,
    }
    messages = asyncio.run(ContextAssembler(store, UnusedModel(), config()).build(
        task({"assignment": assignment}), "coder", [],
        evidence=[{"event_id": "must-not-leak", "text": "retrieved"}],
    ))
    rendered = json.dumps(messages, ensure_ascii=False)
    payload = decode_context(messages)

    assert payload["assignment"] == assignment
    assert payload["latest_user_requirement"] == "只检查 src/main.py"
    assert "敏感的旧会话细节" not in rendered
    assert "must-not-leak" not in rendered
    assert "JSON CoderReport" in rendered
    assert store.events[-1]["payload"]["evidence_count"] == 0


def test_session_reads_are_isolated_by_session_id():
    store = SessionScopedStore()
    second_task = task()
    second_task["session_id"] = "session-b"
    second_task["state"] = {"assignment": {"step_id": "b", "goal": "B task"}}

    messages = asyncio.run(ContextAssembler(store, UnusedModel(), config()).build(second_task, "planner", []))
    payload = decode_context(messages)
    transcript = next(item["content"] for item in messages if "Recent persisted session conversation" in item["content"])

    assert store.reads == [("session-b", 0, None, None)]
    assert payload["session_revision"] == 7
    assert "B separate request" in transcript
    assert "A private request" not in json.dumps(messages, ensure_ascii=False)


def test_budget_compacts_only_whole_tool_exchanges_and_reports_coverage():
    store = FakeStore()
    cfg = config()
    cfg.runtime.coder_context_tokens = 6000
    llm = SummaryModel()
    assembler = ContextAssembler(store, llm, cfg)
    messages = []
    for index in range(3):
        call_id = f"call-{index}"
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": f"result-{index}-" + "x" * 5000},
        ])
    coder_task = task({"assignment": {"step_id": "s", "goal": "inspect", "user_requirements": ["current request"]}})
    coder_task["task_id"] = "task-coder"
    assembled = asyncio.run(assembler.build(coder_task, "coder", messages))
    serialized = json.dumps(assembled, ensure_ascii=False)
    compacted_summary = coder_task["state"]["context_execution_summaries"]["coder"]["summary"]
    retained = [item for item in assembled if item.get("role") in ("assistant", "tool")]
    call_ids = [call["id"] for item in retained if item["role"] == "assistant" for call in item.get("tool_calls", [])]
    result_ids = [item["tool_call_id"] for item in retained if item["role"] == "tool"]

    assert call_ids == result_ids
    assert call_ids[-1] == "call-2"  # The newest complete exchange remains available.
    assert compacted_summary in serialized
    coverage = store.events[-1]["payload"]["history_coverage"]
    assert coverage["tool_history_complete"] is False
    assert "older complete exchange(s) omitted" in serialized
    assert store.events[-1]["payload"]["input_size_estimate"] <= store.events[-1]["payload"]["input_budget"]
    assert coder_task["state"]["context_execution_summaries"]["coder"]["summary"]
    assert store.task_states["task-coder"]["context_execution_summaries"]["coder"]["summary"]
    assert any(event["payload"].get("scope") == "execution_history" for event in store.events if event["event_type"] == "context.compacted")

    # The next task receives the durable execution summary without summarizing that same prefix again.
    call_count = len(llm.calls)
    old_summary = coder_task["state"]["context_execution_summaries"]["coder"]["summary"]
    reused = asyncio.run(assembler.build(coder_task, "coder", messages))
    assert old_summary in json.dumps(reused, ensure_ascii=False)
    assert len(llm.calls) in (call_count, call_count + 1)
    if len(llm.calls) > call_count:
        assert old_summary in llm.calls[-1]["payload"]["previous_summary"]
        assert old_summary in coder_task["state"]["context_execution_summaries"]["coder"]["summary"]
    persisted_summary = coder_task["state"]["context_execution_summaries"]["coder"]["summary"]
    llm_call_count = len(llm.calls)
    cfg.runtime.coder_context_tokens = 20000
    third_context = asyncio.run(assembler.build(coder_task, "coder", messages))
    assert persisted_summary in json.dumps(third_context, ensure_ascii=False)
    assert len(llm.calls) == llm_call_count

    unrelated_task = task({"assignment": {"step_id": "new-task", "goal": "new assignment"}})
    unrelated_task["task_id"] = "task-unrelated"
    unrelated_context = asyncio.run(assembler.build(unrelated_task, "coder", []))
    assert "Prior coder execution summary" not in json.dumps(unrelated_context, ensure_ascii=False)
    assert "context_execution_summaries" not in unrelated_task["state"]
    assert "context_execution_summaries" not in store.session["working_state"]


def test_execution_compaction_does_not_retry_an_identical_rejected_input():
    class EmptySummaryModel(SummaryModel):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            result.content = ''
            return result

    async def check():
        store = FakeStore()
        llm = EmptySummaryModel()
        cfg = config()
        cfg.runtime.coder_context_tokens = 6000
        coder_task = task({"assignment": {"step_id": "s", "goal": "inspect"}})
        messages = [{"role": "user", "content": f"{i}-" + "x" * 24000} for i in range(3)]
        await ContextAssembler(store, llm, cfg).build(coder_task, "coder", messages)
        assert len(llm.calls) == 1
        await ContextAssembler(store, llm, cfg).build(coder_task, "coder", messages)
        assert len(llm.calls) == 1
        messages[0]["content"] = "different-" + messages[0]["content"]
        await ContextAssembler(store, llm, cfg).build(coder_task, "coder", messages)
        assert len(llm.calls) == 2

    asyncio.run(check())


def test_session_summary_is_saved_with_cursor_and_reused_on_next_task():
    store = FakeStore()
    llm = SummaryModel()
    store.messages = [
        {"message_id": f"m{seq}", "role": "user", "kind": "user", "content": f"requirement-{seq}", "seq": seq}
        for seq in range(1, 16)
    ]
    assembler = ContextAssembler(store, llm, config())

    asyncio.run(assembler.build(task(), "planner", []))
    assert store.session["summary_through_seq"] == 3
    assert "requirement-1" in store.session["summary"]
    assert "requirement-3" in store.session["summary"]
    assert "requirement-4" not in store.session["summary"]
    assert any(event["payload"].get("scope") == "session_history" for event in store.events if event["event_type"] == "context.compacted")

    next_task = task()
    next_task["task_id"] = "task-2"
    next_context = asyncio.run(assembler.build(next_task, "planner", []))
    assert "Earlier session summary (through sequence 3" in json.dumps(next_context, ensure_ascii=False)
    assert len(llm.calls) == 1
    artifact = store.artifacts[store.events[-1]["payload"]["artifact_id"]]["content"]
    assert json.loads(artifact)["history_complete"] is False
    assert store.events[-1]["payload"]["history_coverage"]["session_summary_is_exact_transcript"] is False


def test_mandatory_current_request_is_never_silently_truncated():
    store = FakeStore()
    cfg = config()
    cfg.runtime.planner_context_tokens = 250
    cfg.models.local.max_context_tokens = 3000
    cfg.models.local.max_output_tokens = 1024

    with pytest.raises(ValueError, match="mandatory current request/constraints"):
        asyncio.run(ContextAssembler(store, UnusedModel(), cfg).build(task(), "planner", []))


def test_default_planner_budget_uses_token_estimate_and_compact_durable_projections():
    store = FakeStore()
    cfg = config()
    constraints = ["必须保留用户的原始约束：不得修改配置、依赖或外部数据库。"]
    plan = {
        "goal": "检查并完成一个中文用户请求",
        "steps": [{"id": "review", "goal": "检查实现和证据", "status": "completed", "extra": "冗余" * 400}],
        "constraints": constraints,
        "done_when": ["结论有证据"],
        "suggested_files": ["ignored.py"],
        "memory_queries": ["ignored"],
        "skill_hints": ["ignored"],
    }
    assignment = {
        "step_id": "review",
        "goal": "检查实现和证据",
        "user_requirements": ["保留用户要求"],
        "constraints": constraints,
        "done_when": ["确认现状"],
        "relevant_context": ["工作区状态"],
        "previous_results": [{"step_id": "old", "status": "completed", "outcome": "completed", "summary": "大段重复叙述" * 500}],
        "session_revision": 4,
        "private_internal_detail": "omit" * 500,
    }
    report = {
        "step_id": "review",
        "outcome": "completed",
        "summary": "已完成检查。" * 250,
        "changed_files": [],
        "checks": [{"name": "只读核对", "result": "passed", "output": "大段检查输出" * 500}],
        "artifact_refs": ["artifact-raw-report"],
        "raw_artifact": "不得内联" * 500,
    }
    state = {"plan": plan, "assignment": assignment, "last_coder_report": report}

    assembled = asyncio.run(ContextAssembler(store, UnusedModel(), cfg).build(task(state), "planner", []))
    payload = decode_context(assembled)
    event = store.events[-1]["payload"]

    assert cfg.runtime.planner_context_tokens == 8192
    assert payload["plan"]["constraints"] == constraints
    assert payload["assignment"]["constraints"] == constraints
    assert payload["assignment"]["previous_results"] == [{"step_id": "old", "status": "completed", "outcome": "completed"}]
    assert payload["last_coder_report"]["artifact_refs"] == ["artifact-raw-report"]
    assert payload["last_coder_report"]["summary_truncated"] is True
    raw_report_artifact_id = payload["last_coder_report"]["raw_report_artifact_id"]
    assert json.loads(store.artifacts[raw_report_artifact_id]["content"]) == report
    assert raw_report_artifact_id in event["artifact_refs"]
    rendered = json.dumps(assembled, ensure_ascii=False)
    assert "冗余" not in rendered
    assert "大段重复叙述" not in rendered
    assert "不得内联" not in rendered
    assert event["input_size_estimate"] <= event["input_budget"]
    assert event["configured_input_budget"] == 8192
    assert event["tool_schema_reserve_tokens"] == 0
    assert event["input_tokens_exact"] is None
    assert event["input_count_method"] == "conservative_estimate"
    assert event["input_size_unit"] == "estimated_token_units"
    assert event["estimate_method"] == "mixed_script_heuristic_with_20pct_margin"
    assert ContextAssembler._size([{"role": "user", "content": "中" * 1000}]) < 8192


def test_working_state_bound_keeps_last_failed_task_diagnostic_record():
    store = FakeStore()
    record = {"task_id": "failed-task", "error": "mandatory context overflow", "diagnostic": "可复现证据"}
    store.session["working_state"] = {"last_failed_task": record, "large_old_state": "旧状态" * 5000}

    assembled = asyncio.run(ContextAssembler(store, UnusedModel(), config()).build(task(), "planner", []))
    payload = decode_context(assembled)

    assert payload["working_state"]["last_failed_task"] == record
    assert payload["working_state"]["coverage"] == "other working state truncated"


def test_backend_exact_count_receives_tools_and_response_format():
    class CountingModel(UnusedModel):
        def __init__(self):
            self.request = None

        async def count_input_tokens(self, messages, **kwargs):
            self.request = {"messages": messages, **kwargs}
            return 123

    store = FakeStore()
    model = CountingModel()
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    response_format = {"type": "json_object"}
    asyncio.run(ContextAssembler(store, model, config()).build(
        task(), "planner", [], tools=tools, response_format=response_format, max_tokens=512,
    ))
    event = store.events[-1]["payload"]
    assert model.request["tools"] == tools
    assert model.request["response_format"] == response_format
    assert model.request["max_tokens"] == 512
    assert event["input_tokens_exact"] == 123
    assert event["input_count_method"] == "backend_exact"
    assert event["counted_tool_schema_count"] == 1


def test_failed_backend_counter_is_recorded_as_estimate_not_exact():
    class UnavailableCounter(UnusedModel):
        async def count_input_tokens(self, *args, **kwargs):
            raise RuntimeError("count route unavailable")

    store = FakeStore()
    asyncio.run(ContextAssembler(store, UnavailableCounter(), config()).build(task(), "planner", []))
    event = store.events[-1]["payload"]
    assert event["input_tokens_exact"] is None
    assert event["input_count_method"] == "conservative_estimate"
    assert "count route unavailable" in event["input_count_error"]


def test_small_planner_budget_keeps_mandatory_request_and_drops_optional_rag():
    store = FakeStore()
    cfg = config()
    cfg.raw_rag.enabled = True
    cfg.runtime.planner_context_tokens = 2200
    requirements = {
        "goal": "检查项目并说明入口",
        "constraints": ["不得修改文件", "只依据当前工作区证据"],
        "done_when": ["指出入口文件"],
    }
    current_task = task({"current_requirements": requirements})
    current_task["state"]["effective_prompt"] = "检查 README 并说明项目结构"

    assembled = asyncio.run(ContextAssembler(store, UnusedModel(), cfg).build(
        current_task, "planner", [], evidence=[{"text": "检索内容" * 5000, "event_id": "e-1"}],
    ))
    payload = decode_context(assembled)
    event = store.events[-1]["payload"]

    assert payload["current_requirements"] == requirements
    assert payload["latest_user_requirement"] == current_task["state"]["effective_prompt"]
    assert "不得修改文件" in json.dumps(assembled, ensure_ascii=False)
    assert "Untrusted retrieved evidence" not in json.dumps(assembled, ensure_ascii=False)
    assert event["input_size_estimate"] <= 2200
    assert event["history_coverage"]["evidence_or_skills_omitted_for_budget"] is True
    assert event["evidence_count"] == 0


def test_mandatory_planner_context_over_hard_limit_fails_with_recoverable_reason():
    store = FakeStore()
    cfg = config()
    cfg.runtime.planner_context_tokens = 100
    cfg.models.local.max_context_tokens = 5000
    cfg.models.local.max_output_tokens = 1000
    cfg.runtime.context_safety_margin_tokens = 500

    with pytest.raises(ValueError, match=r"mandatory current request/constraints.*100 tokens.*Raise the input budget"):
        asyncio.run(ContextAssembler(store, UnusedModel(), cfg).build(
            task({"current_requirements": {"goal": "keep this", "constraints": ["must survive"]}}),
            "planner", [],
        ))
    assert not any(event["event_type"] == "context.assembled" for event in store.events)


def test_input_budget_and_estimate_use_actual_model_request_reserves():
    class CountingModel(UnusedModel):
        def __init__(self):
            self.request = None

        async def count_input_tokens(self, messages, **kwargs):
            self.request = {"messages": messages, **kwargs}
            return 123

    store = FakeStore()
    cfg = config()
    cfg.runtime.planner_context_tokens = 6000
    cfg.models.local.max_context_tokens = 6000
    cfg.models.local.max_output_tokens = 1024
    cfg.runtime.context_safety_margin_tokens = 500
    model = CountingModel()
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    response_format = {"type": "json_object"}

    assembled = asyncio.run(ContextAssembler(store, model, cfg).build(
        task(), "planner", [], tools=tools, response_format=response_format, max_tokens=512,
    ))
    event = store.events[-1]["payload"]

    assert event["input_budget"] == 4988  # 6000 model context - actual 512 output - 500 safety reserve
    assert event["output_token_reserve"] == 512
    assert model.request["messages"] == assembled
    assert model.request["tools"] == tools
    assert model.request["response_format"] == response_format
    assert model.request["max_tokens"] == 512
    assert event["input_size_estimate"] == ContextAssembler._size(
        assembled, tools=tools, response_format=response_format, max_tokens=512,
    )


def test_exact_overflow_discards_optional_history_then_recounts():
    class ResizingCounter(UnusedModel):
        def __init__(self):
            self.counts = []

        async def count_input_tokens(self, messages, **kwargs):
            self.counts.append(messages)
            return 2000 if len(self.counts) == 1 else 100

    cfg = config()
    cfg.runtime.planner_context_tokens = 1000
    cfg.models.local.token_count_url = "http://localhost/token-count"
    store = FakeStore()
    model = ResizingCounter()
    assembled = asyncio.run(ContextAssembler(store, model, cfg).build(task(), "planner", []))
    event = store.events[-1]["payload"]
    assert len(model.counts) == 2
    assert len(assembled) == 2
    assert event["input_tokens_exact"] == 100
    assert event["history_coverage"]["optional_history_omitted_for_exact_budget"] is True


def test_system_prompts_include_decision_and_report_schemas():
    assembler = ContextAssembler(FakeStore(), UnusedModel(), config())
    planner_prompt = assembler._system_prompt("planner")
    schema_text = planner_prompt.split("JSON Schema:\n", 1)[1].split(". Do not add explanations", 1)[0]
    schema = json.loads(schema_text)
    coder_prompt = assembler._system_prompt("coder")

    assert {branch["properties"]["action"]["const"] for branch in schema["oneOf"]} == {"respond", "ask_user", "delegate"}
    assert set(schema["$defs"]) == {"TaskPlan", "ExecutionAssignment", "RequirementsProposal"}
    coder_schema = re.search(r"CoderReport matching (\{.*\})\. This report", coder_prompt).group(1)
    report = json.loads(coder_schema)
    assert report["properties"]["outcome"]["enum"] == ["completed", "blocked", "needs_replan", "failed"]


def test_history_complete_is_false_when_recent_transcript_falls_out_for_budget():
    store = FakeStore()
    store.messages = [
        {"message_id": f"m{seq}", "role": "user", "kind": "user", "content": "detail-" + "x" * 3000, "seq": seq}
        for seq in range(1, 13)
    ]
    cfg = config()
    cfg.runtime.planner_context_tokens = 6000

    asyncio.run(ContextAssembler(store, UnusedModel(), cfg).build(task(), "planner", []))

    context_snapshot = store.artifacts[store.events[-1]["payload"]["artifact_id"]]["content"]
    assert json.loads(context_snapshot)["history_complete"] is False
    assert store.events[-1]["payload"]["history_coverage"]["recent_transcript_included"] is False


def test_planner_latest_input_follows_old_decisions_and_contains_coder_completion():
    store=FakeStore()
    report={'step_id':'s1','outcome':'completed','summary':'Real file edit and checks are complete'}
    state={'last_coder_report':report,'plan':{'goal':'fix','steps':[{'id':'s1','goal':'fix','status':'completed'}]}}
    old=[{'role':'assistant','content':'{"action":"delegate","assignment":{"step_id":"s1","goal":"fix"}}'}]
    messages=asyncio.run(ContextAssembler(store,UnusedModel(),config()).build(task(state),'planner',old))
    assert messages[-1]['role']=='user'
    assert messages[-1]['content'].startswith('Current task context (authoritative current requirements):')
    assert decode_context(messages)['last_coder_report']==report
    assert any(item==old[0] for item in messages[:-1])
