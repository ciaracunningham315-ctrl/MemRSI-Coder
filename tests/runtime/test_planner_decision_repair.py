import json

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.context.assembler import ContextAssembler
from evocode.contracts import LLMResponse, TaskSpec


async def test_malformed_planner_step_is_repaired_in_current_context(tmp_path):
    class PlannerModel:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            context = json.loads(messages[-1]["content"].split("\n", 1)[1])
            step = {"id": "inspect", "goaL": "Inspect the package"}
            if self.calls > 1:
                repair = context["planner_decision_repair"]
                assert "plan.steps.0.goal" in repair["errors"][0]
                assert context["latest_user_requirement"] == "Inspect and then update the package."
                step["goal"] = step.pop("goaL")
            return LLMResponse(content=json.dumps({
                "action": "delegate",
                "requirements": {
                    "source_user_seq": context["consumed_user_seq"],
                    "goal": context["latest_user_requirement"],
                },
                "plan": {"goal": "Update the package", "steps": [
                    step, {"id": "update", "goal": "Update the package"},
                ]},
                "assignment": {"step_id": "inspect", "goal": "Inspect the package"},
            }))

    config = AppConfig(data_dir=str(tmp_path / "store"))
    model = PlannerModel()
    app = Application(config, model)
    try:
        task = app.create_task(TaskSpec(repo_path=str(tmp_path), prompt="Inspect and then update the package."))
        state = task["state"]
        state["last_user_event_seq"] = 1
        assembler = ContextAssembler(app.store, model, config)
        decision = await app.controller.planner.plan(task, state, assembler, model)
        assert [step.id for step in decision.plan.steps] == ["inspect", "update"]
        assert model.calls == 2
        events = app.store.read_events(task_id=task["task_id"])
        rejected = [event["payload"] for event in events if event["event_type"] == "planner.decision_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["attempt"] == 1
        assert rejected[0]["context_id"]
        assert "plan.steps.0.goal" in rejected[0]["errors"][0]
    finally:
        await app.close()


@pytest.mark.parametrize("first_plan", [
    None,
    {"goal": "Repair the delivered file", "steps": [
        {"id": "step-1", "goal": "Create the file"},
    ]},
])
async def test_delegate_outside_existing_plan_is_repaired_before_return(tmp_path, first_plan):
    class PlannerModel:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            context = json.loads(messages[-1]["content"].split("\n", 1)[1])
            if self.calls == 1:
                step_id = "step-2"
                plan = first_plan
            else:
                assert context["assignment"]["step_id"] == "step-1"
                assert "assignment.step_id" in context["planner_decision_repair"]["errors"][0]
                step_id = "step-1"
                plan = None
            return LLMResponse(content=json.dumps({
                "action": "delegate",
                "assignment": {"step_id": step_id, "goal": "Repair the delivered file"},
                "requirements": None,
                "plan": plan,
            }))

    config = AppConfig(data_dir=str(tmp_path / "store"))
    model = PlannerModel()
    app = Application(config, model)
    try:
        task = app.create_task(TaskSpec(repo_path=str(tmp_path), prompt="Create the delivered file."))
        state = task["state"]
        state.update({
            "last_user_event_seq": 1,
            "current_requirements": {
                "revision": 1,
                "source_user_seq": 1,
                "goal": "Create the delivered file.",
                "constraints": [],
                "done_when": [],
                "delivery_required": False,
                "delivery_targets": [],
            },
            "plan": {"goal": "Create the delivered file.", "steps": [
                {"id": "step-1", "goal": "Create the file", "status": "pending"},
            ]},
            "steps": {"step-1": {"id": "step-1", "goal": "Create the file", "status": "pending"}},
            "assignment": {"step_id": "step-1", "goal": "Create the file", "requirements_revision": 1},
        })
        assembler = ContextAssembler(app.store, model, config)
        decision = await app.controller.planner.plan(task, state, assembler, model, reason="Delivery verification failed")
        assert decision.assignment.step_id == "step-1"
        assert decision.plan is None
        assert model.calls == 2
        events = app.store.read_events(task_id=task["task_id"])
        rejected = [event["payload"] for event in events if event["event_type"] == "planner.decision_rejected"]
        assert len(rejected) == 1
        assert "step-2" in rejected[0]["errors"][0]
    finally:
        await app.close()
