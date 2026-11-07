from __future__ import annotations

from .state import RuntimeState


class StepManager:
    """Manage typed plan step transitions and emit append-only transition events."""

    def __init__(self, store):
        self.store = store

    def activate(self, task_id, state):
        runtime = RuntimeState.load(state)
        was_active = any(step.status == "in_progress" for step in runtime.steps.values())
        step = runtime.activate_next_step()
        if step:
            state.update(runtime.to_dict())
            if not was_active:
                self.store.append_event(task_id, 'plan.step.started', {'step_id': step.id, 'goal': step.goal, 'attempt': step.attempts})
        return step

    def complete(self, task_id, state, result=None):
        runtime = RuntimeState.load(state)
        step = runtime.complete_current_step(result)
        if step:
            state.update(runtime.to_dict())
            self.store.append_event(task_id, 'plan.step.completed', {'step_id': step.id, 'result': result})
        return step
