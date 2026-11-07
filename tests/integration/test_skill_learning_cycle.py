"""Exercise Skill promotion and subsequent use with real tools and a scripted model."""

import json
import os
import subprocess

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, TaskSpec, ToolCall

CHECK = ["python", "-m", "unittest", "discover", "-s", "tests"]
SKILL = "For an arithmetic regression, inspect calc.py and change only the wrong operator."
SKILL_PREFIX = "Applicable versioned skills (subject to task constraints):\n"


class ScriptedModel:
    def __init__(self, config=None):
        self.executed = False
        self.coder_turn = 0
        self.coder_skills = []

    async def complete(self, messages, *, role, **kwargs):
        if role == "evolution":
            trace = json.loads(messages[-1]["content"])["traces"][0]
            edit = next(event for event in trace["events"] if event["event_type"] == "code.patch")
            return LLMResponse(content=json.dumps({
                "operation": "CREATE", "name": "arithmetic repair", "content": SKILL,
                "level": "project", "scope_id": trace["task"]["repository_id"],
                "reason": "reuse a verified narrow edit", "evidence_event_ids": [edit["event_id"]],
                "trigger_terms": ["arithmetic regression"],
            }))

        if role == "planner":
            if self.executed:
                self.executed = False
                self.coder_turn = 0
                return LLMResponse(content='{"action":"respond","answer":"Repaired."}')
            context = json.loads(messages[-1]["content"].split("\n", 1)[1])
            goal = context["latest_user_requirement"]
            self.replacement = goal.rsplit(" ", 1)[-1]
            return LLMResponse(content=json.dumps({
                "action": "delegate", "assignment": {"step_id": "repair", "goal": goal},
                "requirements": {"source_user_seq": context["consumed_user_seq"], "goal": goal},
            }))

        self.coder_turn += 1
        skills = [skill for message in messages if message.get("content", "").startswith(SKILL_PREFIX)
                  for skill in json.loads(message["content"][len(SKILL_PREFIX):])]
        self.coder_skills.extend(skills)
        # Both branches inspect the target. The baseline also reads the tests.
        if self.coder_turn == 1 or (self.coder_turn == 2 and not skills):
            path = "calc.py" if self.coder_turn == 1 else "tests/test_calc.py"
            return LLMResponse(tool_calls=[ToolCall(id=f"read-{self.coder_turn}", name="read_file",
                                                    arguments={"path": path})])
        if self.coder_turn == (2 if skills else 3):
            return LLMResponse(tool_calls=[ToolCall(id="repair", name="edit_file", arguments={
                "path": "calc.py", "old_text": "a - b", "new_text": f"a {self.replacement} b",
            })])
        self.executed = True
        return LLMResponse(content='{"step_id":"repair","outcome":"completed",'
                                   '"summary":"Repaired calculation","changed_files":["calc.py"]}')


def fixture(root, expected):
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def calc(a, b): return a - b\n")
    test = ("import unittest\nfrom calc import calc\nclass TestCalc(unittest.TestCase):\n"
            f"    def test_calc(self): self.assertEqual(calc(6, 2), {expected})\n")
    (root / "tests" / "test_calc.py").write_text(test)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    return test


@pytest.mark.skipif(os.getenv("EVOCODE_DOCKER_TESTS") != "1", reason="explicit Docker integration opt-in")
async def test_learning_from_execution_promotes_and_loads_exact_version_in_next_task(tmp_path, monkeypatch):
    training, validation, next_repo = (tmp_path / name for name in ("train", "validate", "next"))
    fixture(training, 8)
    protected = fixture(validation, 12)
    fixture(next_repo, 3)
    config = AppConfig(data_dir=str(tmp_path / "state"))
    config.raw_rag.enabled = config.raw_rag.background_indexing = False
    config.evolution.allow_remote_code_context = True
    config.evolution.auto_trigger_enabled = False
    config.runtime.execution_backend = "docker"
    repository_id = "arithmetic-fixture"
    model = ScriptedModel()
    monkeypatch.setattr("evocode.app.ModelClient", ScriptedModel)
    app = Application(config, model)
    try:
        def spec(repo, operator):
            return TaskSpec(repo_path=str(repo), repository_id=repository_id,
                            prompt=f"Fix arithmetic regression using operator {operator}",
                            acceptance_command=CHECK)

        training_task = app.create_task(spec(training, "+"))
        trained = await app.run(training_task["task_id"])
        assert trained["status"] == "completed", trained["state"].get("error")
        run = await app.evolution.trigger(training_task["task_id"])
        assert run["status"] == "candidate", run["payload"].get("error")
        assert not app.skills.snapshot(repository_id)

        evaluation_spec = spec(validation, "*").model_dump()
        evaluation_spec["acceptance_files"] = ["tests/test_calc.py"]
        run = await app.evolution.evaluate(run["run_id"], [evaluation_spec])
        assert run["status"] == "evaluated", run["payload"].get("evaluation", run["payload"].get("error"))
        pair = run["payload"]["evaluation"]["rows"][0]
        assert pair["baseline"]["passed"] and pair["candidate"]["passed"]
        assert pair["baseline"]["acceptance_protected"] and pair["candidate"]["acceptance_protected"]
        assert pair["candidate"]["tool_calls"] == pair["baseline"]["tool_calls"] - 1
        assert (validation / "tests" / "test_calc.py").read_text() == protected
        assert "a - b" in (validation / "calc.py").read_text()

        published = app.evolution.publish(run["run_id"])["payload"]["published_version"]
        next_task = app.create_task(spec(next_repo, "//"))
        result = await app.run(next_task["task_id"])
        assert result["status"] == "completed", result["state"].get("error")
        assert result["state"]["verification"]["status"] == "passed"
        assert "a // b" in (next_repo / "calc.py").read_text()
        assert any(skill["version_id"] == published["version_id"] and skill["content"] == SKILL
                   for skill in model.coder_skills)
        events = app.store.read_events(task_id=next_task["task_id"])
        assert any(event["event_type"] == "skill.loaded"
                   and event["payload"]["version_id"] == published["version_id"] for event in events)
    finally:
        await app.close()
