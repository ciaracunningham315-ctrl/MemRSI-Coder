"""Validate and persist the Planner's interpretation of the current request."""
from __future__ import annotations

from evocode.contracts import CurrentRequirements, WorkspaceMapping

from .delivery import resolve_delivery_target

_REQUIREMENT_FIELDS = ('goal', 'constraints', 'done_when', 'delivery_required', 'delivery_targets')


def requirements_changed(current, replacement):
    return any(current.get(key) != replacement[key] for key in _REQUIREMENT_FIELDS)


def proposed_requirements(task, state, decision):
    """Return a validated replacement, or None when the accepted snapshot is reusable."""
    current = state.get('current_requirements')
    revision = current['revision'] if current else 0
    user_seq = state.get('last_user_event_seq', 0)
    proposal = decision.requirements
    if proposal is None:
        # A direct answer does not start or steer executable work, so it does
        # not need a task requirements snapshot. Keep any accepted task snapshot
        # intact for later turns in the same session.
        if decision.action == 'respond':
            return None
        if not current or current['source_user_seq'] != user_seq:
            raise ValueError('Return requirements for the latest user message before proceeding')
        return None
    if proposal.expected_revision != revision or proposal.source_user_seq != user_seq:
        raise ValueError(
            f'Requirements must use expected_revision={revision} and source_user_seq={user_seq}'
        )
    if decision.action != 'ask_user' and proposal.delivery_required and not any(
        target.operation in ('create', 'modify') for target in proposal.delivery_targets
    ):
        raise ValueError('A file-delivery requirement needs concrete create or modify delivery targets.')
    mapping = WorkspaceMapping.for_task(task)
    for target in proposal.delivery_targets:
        resolve_delivery_target(target, mapping)
    replacement = CurrentRequirements(
        revision=revision + 1,
        **proposal.model_dump(exclude={'expected_revision'}),
    ).model_dump()
    if current and current['source_user_seq'] == user_seq:
        if requirements_changed(current, replacement):
            raise ValueError('Keep the accepted requirements for this user message. Update the plan separately.')
        return None
    return replacement


def accept_requirements(store, task, state, decision):
    replacement = proposed_requirements(task, state, decision)
    if replacement is None:
        return
    prior = state.get('current_requirements') or {}
    changed = requirements_changed(prior, replacement)
    working = dict(state)
    with store.transaction():
        newer_events = store.read_events(task_id=task['task_id'], after_seq=replacement['source_user_seq'])
        if any(event['event_type'] == 'user.message' for event in newer_events):
            return False
        if changed:
            # Old steps remain in the event log. They cannot drive the revised goal.
            for key in (
                'plan', 'steps', 'assignment', 'current_step_id', 'last_coder_report',
                'completed_step_ids', 'completed_step_goals', 'delivery_verification',
                'delivery_repair_attempts', 'verification_attempts',
                'verified_coder_turns', 'verified_check_selection', 'coder_report_retries',
                'coder_report_retry_pending', 'coder_report_only',
            ):
                working.pop(key, None)
            if not working.get('verification_blocked_by_input'):
                working.pop('verification', None)
            working['coder_messages'] = []
            summaries = dict(working.get('context_execution_summaries', {}))
            summaries.pop('coder', None)
            working['context_execution_summaries'] = summaries
        working['current_requirements'] = replacement
        store.append_event(task['task_id'], 'requirements.updated', {
            'requirements': replacement,
            'changed': changed,
        })
        store.update_task(task['task_id'], status='running', state=working)
    state.clear()
    state.update(working)
    return True
