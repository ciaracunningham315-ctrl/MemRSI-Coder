from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


StepStatus = Literal["pending", "in_progress", "completed", "blocked", "skipped"]


@dataclass
class StepState:
    id: str
    goal: str
    status: StepStatus = "pending"
    attempts: int = 0
    result: str | None = None

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "StepState":
        status = value.get("status", "pending")
        if status not in {"pending", "in_progress", "completed", "blocked", "skipped"}:
            status = "pending"
        return cls(str(value["id"]), str(value["goal"]), status, int(value.get("attempts", 0)), value.get("result"))


@dataclass
class RuntimeState:
    """Typed view over the JSON state stored by EventStore."""

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, value: dict[str, Any] | None) -> "RuntimeState":
        raw = dict(value or {})
        plan = raw.get("plan") or {}
        if plan:
            steps = [StepState.from_value(item) for item in plan.get("steps", [])]
            raw["plan"]["steps"] = [asdict(step) for step in steps]
            raw["steps"] = {step.id: asdict(step) for step in steps}
            if raw.get("current_step_id") not in raw["steps"]:
                raw["current_step_id"] = next((s.id for s in steps if s.status != "completed"), None)
        return cls(raw)

    @property
    def steps(self) -> dict[str, StepState]:
        return {key: StepState.from_value(value) for key, value in self.raw.get("steps", {}).items()}

    def activate_next_step(self) -> StepState | None:
        steps = self.steps
        active = next((step for step in steps.values() if step.status == "in_progress"), None)
        if active:
            self.raw["current_step_id"] = active.id
            return active
        pending = next((step for step in steps.values() if step.status == "pending"), None)
        if pending:
            pending.status = "in_progress"
            pending.attempts += 1
            self._write_step(pending)
            self.raw["current_step_id"] = pending.id
        return pending

    def complete_current_step(self, result: str | None = None) -> StepState | None:
        step = self.steps.get(self.raw.get("current_step_id"))
        if not step:
            return None
        step.status, step.result = "completed", result
        self._write_step(step)
        self.raw["current_step_id"] = None
        return step

    def _write_step(self, step: StepState) -> None:
        self.raw.setdefault("steps", {})[step.id] = asdict(step)
        for index, value in enumerate(self.raw.get("plan", {}).get("steps", [])):
            if value.get("id") == step.id:
                self.raw["plan"]["steps"][index] = asdict(step)
                break

    def to_dict(self) -> dict[str, Any]:
        return self.raw
