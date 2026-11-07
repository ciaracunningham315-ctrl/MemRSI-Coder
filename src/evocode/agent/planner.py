from __future__ import annotations

import json

from pydantic import ValidationError

from evocode.contracts import PlannerDecision

from .requirements import proposed_requirements, requirements_changed


class Planner:
    """Outer loop: interpret user intent and decide whether to answer, ask, or delegate."""

    def __init__(self, store, config):
        self.store, self.config = store, config

    @staticmethod
    def tool_schema():
        return [{
            'type': 'function',
            'function': {
                'name': 'read_session_messages',
                'description': 'Read persisted user and assistant messages from this task session. Use this for conversation history, not RawRAG.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'after_seq': {'type': 'integer', 'minimum': 0},
                        'before_seq': {'type': ['integer', 'null'], 'minimum': 1},
                        'limit': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 100, 'default': 50},
                    },
                    'additionalProperties': False,
                },
            },
        }]

    def read_session_messages(self, session_id, after_seq=0, before_seq=None, limit=None):
        """Planner's local transcript tool; intentionally separate from RawRAG."""
        return self.store.read_conversation_messages(session_id, after_seq=after_seq, before_seq=before_seq, limit=limit)

    @staticmethod
    def _history_query(arguments):
        after = arguments.get('after_seq', 0)
        before = arguments.get('before_seq')
        limit = arguments.get('limit')
        if limit is None:
            limit = 50
        if type(after) is not int or after < 0:
            raise ValueError('after_seq must be a nonnegative integer')
        if before is not None and (type(before) is not int or before <= after):
            raise ValueError('before_seq must be greater than after_seq')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        return after, before, limit

    def _read_history_page(self, task, call, revision, pages, visible_results):
        if call.name != 'read_session_messages':
            raise ValueError(f'Unsupported Planner local tool: {call.name}')
        after, before, limit = self._history_query(call.arguments)
        key = (revision, after, before, limit)
        if key in pages:
            prior_id, result = pages[key]
            if result['messages'] and prior_id not in visible_results:
                # The assembler dropped the older tool result. Restore its exact text
                # from this decision's cache, without querying the database again.
                pages[key] = (call.id, result)
                return {**result, 'cached': True}
            return {'status': 'skipped', 'reason': 'This page was already read. Use its previous result.',
                    'next_after_seq': result['next_after_seq'], 'has_more': result['has_more']}
        for (page_revision, page_after, page_before, _), (_, prior) in pages.items():
            if (page_revision != revision or prior['has_more'] or after < page_after
                    or (page_before is not None and (before is None or before > page_before))):
                continue
            matches = [item for item in prior['messages'] if item['seq'] > after and (before is None or item['seq'] < before)]
            if not matches:
                return {'status': 'skipped', 'reason': 'The end of this range was already reached.',
                        'next_after_seq': after, 'has_more': False}
            result = {**prior, 'messages': matches[:limit], 'has_more': len(matches) > limit,
                      'next_after_seq': matches[min(len(matches), limit) - 1]['seq']}
            pages[key] = (call.id, result)
            return {**result, 'cached': True}
        self.store.append_event(task['task_id'], 'planner.tool.started', {
            'tool_call_id': call.id, 'tool': call.name, 'arguments': call.arguments,
        })
        fetched = self.read_session_messages(task['session_id'], after, before, limit + 1)
        messages = fetched[:limit]
        result = {'status': 'completed', 'session_id': task['session_id'], 'messages': messages,
                  'next_after_seq': messages[-1]['seq'] if messages else after, 'has_more': len(fetched) > limit}
        pages[key] = (call.id, result)
        return result

    async def plan(self, task, state, assembler, llm, reason=None, skills=None):
        self.config = assembler.config
        task_id = task['task_id']
        self.store.update_task(task_id,status='replanning' if reason else 'planning',state=state)
        planner_messages = state.setdefault('planner_messages', [])
        if not state.get('assignment') and not state.get('planner_decision'):
            # Before any delegation was dispatched, an older delegate response
            # can only be a superseded proposal (e.g. user steering or recovery).
            retained=[]
            for message in planner_messages:
                if message.get('role') == 'assistant' and not message.get('tool_calls'):
                    try:
                        if json.loads(message.get('content','{}')).get('action') == 'delegate':
                            continue
                    except (ValueError,AttributeError):
                        pass
                retained.append(message)
            planner_messages[:] = retained
        # The accepted requirements, assignment, and handoff reason already
        # describe this replan. Keep older exchanges in the trace.
        requirements = state.get('current_requirements')
        execution_handoff = (
            reason and state.get('assignment') and requirements
            and requirements['source_user_seq'] == state.get('last_user_event_seq', 0)
        )
        fresh_context = state.get('last_coder_report') or execution_handoff
        turn_start = len(planner_messages) if fresh_context else 0
        if reason:
            planner_messages.append({'role': 'user', 'content': 'New user input or execution update:\n' + str(reason)})

        errors = []
        decision_repair = None
        tool_rounds = 0
        schema_attempts = 0
        tool_budget_exhausted = False
        pages = {}
        for _ in range(6):
            if tool_rounds >= 3 and not tool_budget_exhausted:
                tool_budget_exhausted = True
                planner_messages.append({
                    'role': 'user',
                    'content': (
                        'History lookup budget reached. The transcript results already provided are your available '
                        'evidence for this turn. Do not request any more tools. Return one valid PlannerDecision '
                        'JSON now, using the available evidence and asking the user if essential information is missing.'
                    ),
                })
            messages = await assembler.build(
                {**task, 'state': state}, 'planner', planner_messages[turn_start:],
                skills=skills,
                decision_repair=decision_repair,
                tools=None if tool_budget_exhausted else self.tool_schema(),
                omit_tools_when_history_complete=not tool_budget_exhausted,
                response_format={'type': 'json_object'},
                max_tokens=self.config.models.local.max_output_tokens,
            )
            context_id = assembler.last_context_id
            tools_closed = tool_budget_exhausted or assembler.last_history_complete
            response = await llm.complete(
                messages,
                role='planner',
                tools=None if tools_closed else self.tool_schema(),
                response_format={'type': 'json_object'},
                max_tokens=self.config.models.local.max_output_tokens,
            )
            self._account(state, response.usage)
            assembler.record_model_usage(task_id, 'planner', response.usage, context_id=context_id)
            if response.tool_calls:
                if tools_closed:
                    schema_attempts += 1
                    closed_reason = ('The complete session transcript is already in this input.'
                                     if assembler.last_history_complete else 'The history lookup budget is closed.')
                    errors.append('Planner requested an unavailable history tool. ' + closed_reason)
                    for call in response.tool_calls:
                        self.store.append_event(task_id, 'planner.tool.rejected', {
                            'tool_call_id': call.id,
                            'tool': call.name,
                            'reason': closed_reason,
                        })
                    if schema_attempts >= 3:
                        break
                    planner_messages.append({
                        'role': 'user',
                        'content': (
                            closed_reason + ' The tool request was not executed. '
                            'Return only a valid PlannerDecision JSON without tool calls.'
                        ),
                    })
                    continue
                tool_rounds += 1
                visible_results = {message.get('tool_call_id') for message in messages if message.get('role') == 'tool'}
                planner_messages.append({
                    'role': 'assistant',
                    **({'reasoning_content': response.reasoning_content} if response.reasoning_content is not None else {}),
                    'tool_calls': [{'id': call.id, 'type': 'function', 'function': {
                        'name': call.name, 'arguments': json.dumps(call.arguments),
                    }} for call in response.tool_calls],
                })
                for call in response.tool_calls:
                    try:
                        result = self._read_history_page(task, call, assembler.last_session_revision, pages, visible_results)
                    except (KeyError, TypeError, ValueError) as exc:
                        result = {'status': 'failed', 'error': str(exc)}
                    self.store.append_event(task_id, 'planner.tool.completed', {
                        'tool_call_id': call.id, 'tool': call.name, 'result': result, 'arguments': call.arguments,
                    })
                    planner_messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps(result, ensure_ascii=False)})
                    visible_results.add(call.id)
                continue
            schema_attempts += 1
            try:
                decision = PlannerDecision.model_validate_json(self._json(response.content))
                replacement = proposed_requirements(task, state, decision)
                known={check['id'] for check in state.get('project_checks',[])}
                if any(identifier not in known for identifier in decision.check_ids):
                    raise ValueError('check_ids must reference the discovered project_checks; do not invent test commands or IDs')
                if decision.action == 'delegate':
                    current = state.get('current_requirements') or {}
                    goal_changed = replacement and requirements_changed(current, replacement)
                    if decision.plan is not None:
                        active_step_ids = {step.id for step in decision.plan.steps}
                    elif not goal_changed:
                        active_step_ids = {
                            step.get('id') for step in (state.get('plan') or {}).get('steps', [])
                            if isinstance(step, dict) and step.get('id')
                        }
                    else:
                        # accept_requirements clears the previous plan when a new
                        # goal is accepted; a plan-less assignment then becomes
                        # the controller's implicit one-step plan.
                        active_step_ids = set()
                    if active_step_ids and decision.assignment.step_id not in active_step_ids:
                        raise ValueError(
                            f"assignment.step_id {decision.assignment.step_id!r} must match a step in the active plan "
                            f"({', '.join(sorted(active_step_ids))})"
                        )
                    existing=state.get('steps',{}).get(decision.assignment.step_id,{})
                    if existing.get('status') == 'completed' and not goal_changed:
                        raise ValueError('This step is already completed by a CoderReport. Respond with the result or delegate genuinely unfinished work using its own step ID; do not repeat the completed step.')
                planner_messages.append({'role': 'assistant', 'content': response.content,
                    **({'reasoning_content': response.reasoning_content} if response.reasoning_content is not None else {})})
                state['planner_decision'] = decision.model_dump()
                # Keep the persisted node at Planner until publication or the
                # controller commits a transition to Coder.
                state['next_node'] = 'planner'
                self.store.append_event(task_id, 'planner.decision', {
                    'decision': decision.model_dump(),
                    'reason': reason,
                    'usage': response.usage,
                })
                self.store.update_task(task_id, status='running', state=state)
                return decision
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
                problems = self._decision_errors(exc)
                self.store.append_event(task_id, 'planner.decision_rejected', {
                    'attempt': schema_attempts,
                    'context_id': context_id,
                    'errors': problems,
                })
                if schema_attempts >= 3:
                    break
                decision_repair = {
                    'errors': problems,
                    'instruction': (
                        'Your previous decision was rejected. Return a complete corrected PlannerDecision '
                        'for the same latest request using the exact case-sensitive keys. For a one-step '
                        'delegate omit the optional plan. If several steps are needed, keep them and give '
                        'each step both an id and a goal.'
                    ),
                }
        if errors:
            raise ValueError('Planner failed structured output validation: ' + errors[-1])
        raise ValueError('Planner did not return a structured decision within its bounded response budget')

    @staticmethod
    def _decision_errors(exc):
        if not isinstance(exc, ValidationError):
            return [str(exc)[:300]]
        return [
            f"{'.'.join(map(str, error['loc'])) or 'decision'}: {error['msg']}"[:300]
            for error in exc.errors(include_input=False, include_url=False)[:4]
        ]

    @staticmethod
    def _json(content: str) -> str:
        content = (content or '').strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0]
        return content

    @staticmethod
    def _account(state, usage):
        state['model_responses'] = state.get('model_responses', 0) + 1
        total = usage.get('total_tokens', 0)
        state['tokens'] = state.get('tokens', 0) + total
        by_role = state.setdefault('token_usage', {})
        by_role['planner'] = by_role.get('planner', 0) + total
