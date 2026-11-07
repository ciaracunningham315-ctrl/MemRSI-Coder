from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

from evocode.config import AppConfig
from evocode.context import ContextAssembler
from evocode.contracts import ExecutionAssignment, PlanStep, TaskPlan, ToolResult

from .coder import Coder, CoderReportParseError
from .delivery import delivery_targets
from .execution_stop import redundant_inspections
from .planner import Planner
from .recovery import RecoveryManager
from .requirements import accept_requirements
from .tools import TOOLS, ToolExecutor, UncertainToolExecution
from .verification import AcceptanceVerifier


class AgentController:
    def __init__(self, store, llm, config: AppConfig, memory=None, skills=None, *, automatic_memory_query=None):
        self.store,self.llm,self.config,self.memory,self.skills=store,llm,config,memory,skills
        # Isolated evaluations may opt into one deterministic request-derived
        # query. Production callers leave this unset and retain on-demand routing.
        self.automatic_memory_query = automatic_memory_query
        self._cancelled=set();self._stop_requested=set();self._active={};self._executors={}
        self._message_lock=threading.Lock();self._queued_user_messages={}
        self.recovery=RecoveryManager(store);self.planner=Planner(store,config)
        self.coder=Coder();self.verifier=AcceptanceVerifier(store)

    def enqueue_user_message(self, task_id, event):
        """Signal an already-persisted message; event persistence remains the caller's job."""
        with self._message_lock:
            self._queued_user_messages.setdefault(task_id,[]).append(dict(event))

    def _consume_user_messages(self, task, state):
        task_id=task['task_id']
        working=copy.deepcopy(state)
        events=self.store.read_events(task_id=task_id,after_seq=working.get('last_user_event_seq',0))
        consumed=[]
        if working.get('last_user_event_seq', 0) == 0:
            initial=next((e for e in events if e['event_type']=='user.message' and not e['payload'].get('target_task_id') and e['payload'].get('content')==task['prompt']),None)
            if initial:
                working['last_user_event_seq']=initial['seq']
                working.setdefault('processed_user_event_ids',[]).append(initial['event_id'])
                consumed.append((initial,'initial'))
                events=[e for e in events if e['seq']>initial['seq']]
            else:
                working['last_user_event_seq']=0
        steering=[]
        redirected=False
        for event in events:
            if event['event_type']!='user.message':
                continue
            processed=set(working.setdefault('processed_user_event_ids',[]))
            if event['event_id'] in processed:
                working['last_user_event_seq']=max(working.get('last_user_event_seq',0),event['seq'])
                continue
            payload=event['payload']
            target=payload.get('target_task_id')
            if target and target!=task_id:
                working['last_user_event_seq']=max(working.get('last_user_event_seq',0),event['seq'])
                working['processed_user_event_ids'].append(event['event_id'])
                consumed.append((event,'ignored'))
                continue
            content=str(payload.get('content','')).strip()
            if not content:
                working['last_user_event_seq']=max(working.get('last_user_event_seq',0),event['seq'])
                working['processed_user_event_ids'].append(event['event_id'])
                consumed.append((event,payload.get('intent','addendum')))
                continue
            intent=payload.get('intent','auto')
            if intent=='redirect':
                working['effective_prompt']=content
                working.setdefault('user_addenda',[]).append({'intent':'redirect','content':content,'event_id':event['event_id']})
                redirected=True
            else:
                working.setdefault('user_addenda',[]).append({'intent':intent,'content':content,'event_id':event['event_id']})
            steering.append(content)
            working['processed_user_event_ids'].append(event['event_id'])
            working['last_user_event_seq']=max(working.get('last_user_event_seq',0),event['seq'])
            consumed.append((event,intent))
        if redirected and working.get('plan'):
            for step in working['plan'].get('steps', []):
                if step.get('status') in ('pending', 'in_progress'):
                    step['status'] = 'skipped'
                    if 'steps' in working and step.get('id') in working['steps']:
                        working['steps'][step['id']]['status'] = 'skipped'
            working['current_step_id'] = None
        changed=working!=state
        if changed or consumed:
            # Acknowledgment, durable Coder history, dedupe IDs, and cursor advance
            # commit together. A replay after a crash either sees all of them or none.
            with self.store.transaction():
                for event,intent in consumed:
                    self.store.append_event(task_id,'user.message.consumed',{
                        'source_event_id':event['event_id'],'intent':intent,
                    })
                self.store.update_task(task_id,state=working)
            state.clear();state.update(working)
        with self._message_lock:
            self._queued_user_messages.pop(task_id,None)
        return '\n'.join(steering) if steering else None

    def cancel(self, task_id):
        self._cancelled.add(task_id)
        future=self._active.get(task_id)
        if future:future.cancel()
        else:self.store.update_task(task_id,status='cancelled')

    def stop(self, task_id):
        """Request a resumable stop, including before a scheduled run starts."""
        task=self.store.get_task(task_id)
        if task['status'] in ('completed','failed','cancelled','needs_review','interrupted'):
            return task
        first_request=task_id not in self._stop_requested
        self._stop_requested.add(task_id)
        future=self._active.get(task_id)
        if future and first_request:
            future.cancel()
        elif not future:
            self.store.update_task(task_id,status='interrupted',state=task['state'])
        return self.store.get_task(task_id)

    def _uncertain(self, task_id):
        return self.recovery.uncertain(task_id)

    def resolve(self, task_id, tool_call_id, resolution):
        if resolution not in ('completed','failed'):raise ValueError('Resolution must be completed or failed')
        if tool_call_id not in self._uncertain(task_id):raise ValueError('No uncertain tool with this id')
        self.store.append_event(task_id,'tool.resolved',{'tool_call_id':tool_call_id,'resolution':resolution,'source':'operator'})
        return self.store.get_task(task_id)

    async def resume(self, task_id):
        task=self.store.get_task(task_id)
        if task_id in self._active:raise ValueError('Task is already running')
        checkpoint=self.store.latest_checkpoint(task_id)
        # The append-only task events are authoritative. A checkpoint is only a
        # compatibility fallback for older logs that predate task.created/events.
        fallback=(checkpoint or {}).get('state') or task['state']
        task['state']=self.store.replay_task_state(task_id, fallback=fallback)
        for call_id,info in self._uncertain(task_id).items():
            prepared=info.get('prepared',{})
            if info.get('tool')=='edit_file' and prepared:
                self.recovery.resolve_edit_if_observable(task_id,call_id,info,self.resolve)
            elif info.get('tool') in ('read_file','memory_search'):
                self.resolve(task_id,call_id,'failed')
        pending=self._uncertain(task_id)
        if pending:
            task['state']['uncertain_tools']=list(pending.values())
            return self.store.update_task(task_id,status='needs_review',state=task['state'])
        messages=task['state'].get('coder_messages',[])
        task['state']['coder_messages']=self.recovery.rebuild_coder_messages(task_id,messages)
        if task['state'].get('next_node') == 'tool':
            # Recovery has resolved or synthesized every outstanding tool result;
            # continue the same Coder exchange without replaying side effects.
            self._transition(task_id, task['state'], 'tool', 'coder', 'recovery_tool_results')
        task['state'].pop('uncertain_tools',None)
        task['state'].pop('error',None)
        self.store.update_task(task_id,status='running',state=task['state'])
        self._cancelled.discard(task_id)
        self._stop_requested.discard(task_id)
        return await self.run(task_id)

    async def _plan(self, task, state, assembler, llm, reason=None, skills=None):
        return await self.planner.plan(task, state, assembler, llm, reason, skills=skills)

    def _refresh_skills(self, task, state, config, selected, phase):
        """Select from the task's pinned versions as new context becomes available."""
        if not (config.skills.enabled and self.skills):
            return
        report = self.skills.select_with_reasons({**task, 'state': state})
        pinned = state.get('skill_snapshot', {})
        loaded_ids = list(state.get('loaded_skill_ids', []))
        limit = max(0, config.skills.max_loaded_skills)
        loaded_ids = [skill_id for skill_id in loaded_ids if skill_id in pinned][:limit]
        newly_loaded = []
        selected_by_id = {item['skill_id']: item for item in selected}
        load_status = {}
        for skill_id in loaded_ids:
            version_id = pinned.get(skill_id)
            if version_id and skill_id not in selected_by_id:
                selected_by_id[skill_id] = self.skills.load(version_id)
            load_status[skill_id] = 'already_loaded'
        for item in report['selected']:
            skill_id = item['skill_id']
            if skill_id in loaded_ids:
                continue
            if len(loaded_ids) >= limit:
                load_status[skill_id] = 'limit_retained_existing'
                continue
            version_id = pinned.get(skill_id)
            if not version_id:
                load_status[skill_id] = 'not_in_task_snapshot'
                continue
            loaded = self.skills.load(version_id)
            loaded_ids.append(skill_id)
            selected_by_id[skill_id] = loaded
            load_status[skill_id] = 'loaded'
            newly_loaded.append({
                'skill_id': skill_id,
                'version_id': version_id,
                'reason': item['selection_reason'],
                'matches': item['matched_triggers'],
            })
            self.store.append_event(task['task_id'], 'skill.loaded', {
                'skill_id': skill_id, 'version_id': version_id,
                'reason': item['selection_reason'], 'matches': item['matched_triggers'],
                'selection_phase': phase,
            })
        state['loaded_skill_ids'] = loaded_ids
        # Keep previous loads stable across replans and resumes, then append
        # new matches in the registry's deterministic rank order.
        selected[:] = [selected_by_id[skill_id] for skill_id in loaded_ids if skill_id in selected_by_id]

        for decision in report['decisions']:
            decision['load_status'] = load_status.get(
                decision['skill_id'], 'not_selected' if not decision.get('selected') else 'not_loaded')
        self.store.append_event(task['task_id'], 'skill.selection', {
            'phase': phase,
            'candidates': report['decisions'],
            'loaded_skill_ids': loaded_ids,
            'newly_loaded': newly_loaded,
            'limit': limit,
        })
        if newly_loaded:
            self.store.update_task(task['task_id'], state=state)

    @staticmethod
    def _workspace_question(task, targets):
        selected = str(Path(task['repo_path']).resolve())
        return ('请求的输出路径不在当前会话选择的工作区内。'
                f'当前工作区：{selected}；请求路径：{", ".join(targets)}。'
                '请将目标目录选择为会话工作区，或在该目标目录中新建会话后重新提交任务。')

    async def run(self, task_id):
        if task_id in self._active:
            raise ValueError('Task is already running')
        task = self.store.get_task(task_id)
        if task_id in self._stop_requested or task['status'] == 'interrupted':
            return task
        if self._uncertain(task_id):
            return self.store.update_task(task_id, status='needs_review')
        if task['status'] in ('completed', 'cancelled'):
            return task

        config = AppConfig.model_validate(task['config'])
        factory = getattr(self.llm, 'for_config', None)
        llm = factory(config) if callable(factory) else self.llm
        assembler = ContextAssembler(self.store, llm, config)
        executor = ToolExecutor(self.store, config, self.memory)
        self._active[task_id] = asyncio.current_task()
        self._executors[task_id] = executor
        state = task['state']
        started = time.monotonic()
        try:
            initial_steering = self._consume_user_messages(task, state)
            if initial_steering:
                state['next_node'] = 'planner'
                state['planner_decision'] = None
                state['planner_reason'] = initial_steering
            if 'skill_snapshot' not in state:
                state['skill_snapshot'] = self.skills.snapshot(task['repository_id']) if config.skills.enabled and self.skills else {}
            selected = []
            self._refresh_skills(task, state, config, selected, 'task_start')

            evidence = []

            failure_streak = 0
            planner_turns = 0
            loop_budget = config.runtime.max_turns * 2 + config.runtime.max_replans + 4
            for turn in range(loop_budget):
                if task_id in self._cancelled:
                    raise asyncio.CancelledError()
                state['session_revision'] = self._current_session_revision(task)
                steering = self._consume_user_messages(task, state)
                if steering:
                    state['next_node'] = 'planner'
                    state['planner_decision'] = None
                    state['agent_finished'] = False

                next_node = state.get('next_node', 'planner')
                if next_node in ('end', 'waiting_user'):
                    return self.store.get_task(task_id)

                if next_node == 'planner':
                    self.verifier.refresh(task,state)
                    planner_turns += 1
                    if planner_turns > config.runtime.max_turns + config.runtime.max_replans:
                        raise RuntimeError('Planner turn budget exhausted')
                    reason = steering or state.get('planner_reason')
                    decision = await self._plan(task, state, assembler, llm, reason, skills=selected)
                    state.pop('planner_reason', None)
                    if decision.check_ids:
                        state['selected_check_ids']=decision.check_ids
                    # Do not act on a Planner result if the user steered while it
                    # was being generated. The next Planner turn sees that input.
                    newer_input = self._consume_user_messages(task, state)
                    if newer_input:
                        state['next_node'] = 'planner'
                        state['planner_decision'] = None
                        state['planner_reason'] = newer_input
                        self.store.update_task(task_id, status='running', state=state)
                        continue
                    if accept_requirements(self.store, task, state, decision) is False:
                        state['planner_decision'] = None
                        continue
                    root = Path(task['repo_path']).resolve()
                    external_targets = [
                        target.path for target, path in delivery_targets(task, state)
                        if not path.is_relative_to(root)
                    ]
                    if external_targets and decision.action != 'ask_user':
                        question = self._workspace_question(task, external_targets)
                        self.store.append_event(task_id, 'delivery.target_blocked', {
                            'workspace': str(Path(task['repo_path']).resolve()),
                            'requested_paths': external_targets,
                            'source': 'current_requirements',
                        })
                        decision = decision.model_copy(update={'action': 'ask_user', 'question': question,
                                                               'answer': None, 'assignment': None})
                    state['planner_decision'] = decision.model_dump()
                    if decision.action == 'respond':
                        acceptance = await self._verify_before_final(task, state, executor, config)
                        if acceptance == 'repair':
                            continue
                        if acceptance == 'continue':
                            continue
                        if acceptance == 'failed':
                            return self.store.fail_task(task_id, state)
                        state['final_answer'] = decision.answer
                        state['agent_finished'] = True
                        state['duration_seconds'] = state.get('duration_seconds', 0) + time.monotonic() - started
                        published = self.store.publish_final(task_id, decision.answer, state)
                        if published is None:
                            steering = self._consume_user_messages(task, state)
                            state['next_node'] = 'planner'
                            state['planner_decision'] = None
                            state['planner_reason'] = steering or 'A newer user message superseded the final response.'
                            state.pop('final_answer', None)
                            state['agent_finished'] = False
                            self.store.update_task(task_id, status='running', state=state)
                            continue
                        return published
                    if decision.action == 'ask_user':
                        state['agent_finished'] = False
                        state['duration_seconds'] = state.get('duration_seconds', 0) + time.monotonic() - started
                        published = self.store.publish_question(task_id, decision.question, state)
                        if published is None:
                            steering = self._consume_user_messages(task, state)
                            state['next_node'] = 'planner'
                            state['planner_decision'] = None
                            state['planner_reason'] = steering or 'A newer user message superseded the question.'
                            self.store.update_task(task_id, status='running', state=state)
                            continue
                        return published
                    assignment = decision.assignment
                    prior_plan = state.get('plan')
                    self._install_plan_and_assignment(state, decision.plan, assignment)
                    self._refresh_skills(task, state, config, selected, 'planner')
                    if state.get('plan') != prior_plan:
                        self.store.append_event(task_id, 'planner.replanned' if prior_plan else 'planner.plan', {
                            'plan': state['plan'], 'reason': reason,
                        })
                    self.store.append_event(task_id, 'plan.step.started', {
                        'step_id': assignment.step_id,
                        'goal': state['steps'][assignment.step_id]['goal'],
                        'attempt': state['steps'][assignment.step_id].get('attempts', 0) + 1,
                    })
                    state['steps'][assignment.step_id]['attempts'] = state['steps'][assignment.step_id].get('attempts', 0) + 1
                    for plan_step in state['plan']['steps']:
                        if plan_step['id'] == assignment.step_id:
                            plan_step['attempts'] = state['steps'][assignment.step_id]['attempts']
                            break
                    if config.raw_rag.enabled and self.memory:
                        evidence = []
                        planned_memory_queries = list(state.get('plan', {}).get('memory_queries', [])[:2])
                        memory_queries = planned_memory_queries
                        if self.automatic_memory_query is not None and not state.get('automatic_memory_query_attempted'):
                            state['automatic_memory_query_attempted'] = True
                            memory_queries = [self.automatic_memory_query]
                            memory_queries.extend(
                                query for query in planned_memory_queries
                                if query != self.automatic_memory_query
                            )
                        for query in memory_queries[:2]:
                            result = await self.memory.search(query, {**task, 'state': state})
                            self.store.append_event(task_id, 'memory.retrieved', {
                                'query': query, **result.model_dump(exclude={'evidence'}),
                                'evidence_event_ids': [item.event_id for item in result.evidence],
                            })
                            evidence.extend(item.model_dump() for item in result.evidence)
                    self._transition(task_id, state, 'planner', 'coder', 'delegate')
                    state['coder_messages'] = []
                    for key in ('coder_report_only', 'coder_report_retries', 'coder_report_retry_pending',
                                'coder_report_retry_tool_call_count'):
                        state.pop(key, None)
                    state['coder_turns'] = state.get('coder_turns', 0)
                    self.store.update_task(task_id, status='running', state=state)
                    continue

                if next_node != 'coder':
                    raise ValueError(f'Unsupported runtime node: {next_node}')
                closing_report = state.get('coder_report_only', False)
                if state.get('coder_turns', 0) >= config.runtime.max_turns and not closing_report:
                    raise RuntimeError('Coder turn budget exhausted')
                assignment_data = state.get('assignment')
                requirements = state.get('current_requirements')
                if (
                    not assignment_data or not requirements
                    or assignment_data.get('requirements_revision') != requirements['revision']
                    or requirements['source_user_seq'] != state.get('last_user_event_seq', 0)
                ):
                    state['next_node'] = 'planner'
                    state['planner_reason'] = 'Establish current requirements and a matching assignment before executing.'
                    continue
                assignment = ExecutionAssignment.model_validate(assignment_data)
                state['coder_turns'] = state.get('coder_turns', 0) + 1
                self._refresh_skills(task, state, config, selected, 'execution')
                messages = state.setdefault('coder_messages', [])
                tools = [tool for tool in TOOLS if config.raw_rag.enabled or tool['function']['name'] != 'memory_search']
                report_only = closing_report or state.get('coder_report_retry_pending')
                if report_only:
                    tools = []
                response = await self.coder.complete(task, state, assembler, llm, messages, evidence, selected, tools, assignment)
                assistant_message = {'role': 'assistant', 'content': response.content}
                if response.reasoning_content is not None:
                    assistant_message['reasoning_content'] = response.reasoning_content
                if response.tool_calls:
                    assistant_message['tool_calls'] = [
                        {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': json.dumps(call.arguments)}}
                        for call in response.tool_calls
                    ]
                messages.append(assistant_message)
                with self.store.transaction():
                    self.store.append_event(task_id, 'coder.message', {'message': assistant_message, 'usage': response.usage})
                    self.store.update_task(task_id, state=state)

                steering = self._consume_user_messages(task, state)
                messages = state.setdefault('coder_messages', [])
                if steering:
                    self._append_skipped_tools(messages, response.tool_calls, 'A newer user message arrived before tool dispatch')
                    self._transition(task_id, state, 'coder', 'planner', 'new_user_input')
                    state['planner_decision'] = None
                    state['planner_reason'] = steering
                    self.store.update_task(task_id, status='running', state=state)
                    continue

                if response.tool_calls and report_only:
                    rejected = state.get('coder_report_retry_tool_call_count', 0) + 1
                    state['coder_report_retry_tool_call_count'] = rejected
                    self._append_skipped_tools(messages, response.tool_calls,
                                               'Tool calls are disabled during the report-only turn.')
                    self.store.append_event(task_id, 'coder.report.retry_tool_calls_rejected', {
                        'step_id': assignment.step_id,
                        'tool_call_ids': [call.id for call in response.tool_calls],
                        'attempt': rejected,
                    })
                    if rejected >= 2:
                        raise CoderReportParseError('Coder attempted tool calls during both report-only correction attempts')
                    messages.append({'role': 'user', 'content': (
                        'Tool calls are not allowed for this repair. Return only one valid CoderReport JSON object.'
                    )})
                    self.store.update_task(task_id, status='running', state=state)
                    continue

                if response.tool_calls and redundant_inspections(
                    self.store, self.verifier, task, state, executor, assignment.step_id, response.tool_calls
                ):
                    reason = ('Delivery is evidenced. The requested calls add no evidence. '
                              'Independent project verification still applies.')
                    self._append_skipped_tools(messages, response.tool_calls, reason)
                    messages.append({'role': 'user', 'content': reason + (
                        ' Return a CoderReport now. State the actual scope of the check and report any remaining work'
                        ' or blocker. Do not call tools.'
                    )})
                    state['coder_report_only'] = True
                    with self.store.transaction():
                        self.store.append_event(task_id, 'coder.report.only_requested', {
                            'step_id': assignment.step_id, 'reason': 'redundant_inspections',
                            'tools': [call.name for call in response.tool_calls],
                            'tool_call_ids': [call.id for call in response.tool_calls],
                        })
                        self.store.update_task(task_id, state=state)
                    continue

                if response.tool_calls:
                    replan_reason = None
                    self._transition(task_id, state, 'coder', 'tool', 'tool_calls')
                    for index, call in enumerate(response.tool_calls):
                        if call.name == 'replan':
                            result = ToolResult(tool_call_id=call.id, status='completed', output='Replanning requested')
                            replan_reason = str(call.arguments.get('reason', 'Observed deviation'))
                        else:
                            try:
                                result = await self._execute(task, state, executor, call)
                            except UncertainToolExecution as exc:
                                state['uncertain_tools'] = [dict(item) for item in self._uncertain(task_id).values()]
                                self.store.append_event(task_id, 'tool.interrupted', {
                                    'tool_call_id': call.id, 'tool': call.name, 'reason': str(exc), **exc.metadata,
                                })
                                self.store.update_task(task_id, status='needs_review', state=state)
                                self.store.save_checkpoint(task_id, state)
                                return self.store.get_task(task_id)
                            failure_streak = failure_streak + 1 if result.status == 'failed' else 0
                        messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': result.model_dump_json()})
                        self.store.update_task(task_id, state=state)
                        steering = self._consume_user_messages(task, state)
                        if steering:
                            self._append_skipped_tools(messages, response.tool_calls[index + 1:], 'A newer user message arrived at the safe tool boundary')
                            replan_reason = steering
                            break
                    if failure_streak >= 3:
                        replan_reason = 'Repeated tool failures: ' + messages[-1]['content']
                    if replan_reason:
                        self._transition(task_id, state, 'tool', 'planner', 'replan_or_user_input')
                        state['planner_decision'] = None
                        state['planner_reason'] = replan_reason
                    else:
                        self._transition(task_id, state, 'tool', 'coder', 'tools_completed')
                    self.store.update_task(task_id, status='running', state=state)
                    continue

                try:
                    report = self.coder.parse_report(response.content, assignment.step_id)
                except CoderReportParseError as exc:
                    retry_count = state.get('coder_report_retries', 0)
                    self.store.append_event(task_id, 'coder.report.invalid', {
                        'step_id': assignment.step_id,
                        'raw_content': response.content,
                        'error': str(exc),
                        'retry': retry_count,
                    })
                    if retry_count >= 1:
                        raise
                    state['coder_report_retries'] = retry_count + 1
                    state['coder_report_retry_pending'] = True
                    messages.append({'role': 'user', 'content': (
                        'Your previous response did not contain exactly one valid CoderReport JSON object '
                        f'for step {assignment.step_id!r} ({exc}). Return only a corrected CoderReport. '
                        'Do not repeat or dispatch any tool calls.'
                    )})
                    state['next_node'] = 'coder'
                    self.store.append_event(task_id, 'coder.report.retry_requested', {
                        'step_id': assignment.step_id, 'attempt': retry_count + 1,
                    })
                    self.store.update_task(task_id, status='running', state=state)
                    continue
                state.pop('coder_report_retries', None)
                state.pop('coder_report_only', None)
                state.pop('coder_report_retry_pending', None)
                state.pop('coder_report_retry_tool_call_count', None)
                state['last_coder_report'] = report.model_dump()
                self.store.append_event(task_id, 'coder.report', {'report': report.model_dump(), 'usage': response.usage})
                self._update_step_from_report(task_id, state, report)
                self._transition(task_id, state, 'coder', 'planner', 'coder_report')
                state['planner_decision'] = None
                state['coder_messages'] = []
                self.store.update_task(task_id, status='running', state=state)
                events = self.store.read_events(task_id=task_id)
                last = state.get('checkpoint_seq', 0)
                if events and events[-1]['seq'] - last >= config.runtime.checkpoint_every_events:
                    state['checkpoint_seq'] = events[-1]['seq']
                    self.store.save_checkpoint(task_id, state)

            raise RuntimeError('Execution turn budget exhausted')
        except asyncio.CancelledError:
            await executor.cancel(task_id)
            uncertain=self._uncertain(task_id)
            if task_id in self._stop_requested and uncertain:
                state['uncertain_tools']=list(uncertain.values())
                status='needs_review'
            elif task_id in self._stop_requested:
                status='interrupted'
            else:
                status='cancelled'
            self.store.update_task(task_id, status=status, state=state)
            self.store.save_checkpoint(task_id, state)
            return self.store.get_task(task_id)
        except Exception as exc:
            state['error'] = str(exc)
            self.store.fail_task(task_id, state, error=f'{type(exc).__name__}: {exc}')
            self.store.save_checkpoint(task_id, state)
            return self.store.get_task(task_id)
        finally:
            self._active.pop(task_id, None)
            self._executors.pop(task_id, None)
            self._stop_requested.discard(task_id)

    @staticmethod
    def _append_skipped_tools(messages, calls, reason):
        for pending in calls or []:
            messages.append({'role': 'tool', 'tool_call_id': pending.id, 'content': ToolResult(
                tool_call_id=pending.id, status='skipped', output=reason,
            ).model_dump_json()})

    def _current_session_revision(self, task):
        data = self.store.get_session(task['session_id'])
        session = data.get('session', data) if isinstance(data, dict) else {}
        return int(session.get('revision', 0))

    def _transition(self, task_id, state, source, target, reason):
        state['next_node'] = target
        with self.store.transaction():
            self.store.append_event(task_id, 'node.transition', {
                'from': source, 'to': target, 'reason': reason,
            })
            self.store.update_task(task_id, status='running', state=state)

    def _install_plan_and_assignment(self, state, proposed_plan, assignment):
        previous = state.get('plan') or {}
        previous_steps = {item['id']: item for item in previous.get('steps', []) if isinstance(item, dict) and item.get('id')}
        if proposed_plan is not None:
            plan_data = proposed_plan.model_dump()
            ids = [item['id'] for item in plan_data['steps']]
            if len(ids) != len(set(ids)):
                raise ValueError('Planner plan step ids must be unique')
            observed_completed = dict(state.get('completed_step_goals', {}))
            for item in plan_data['steps']:
                old = previous_steps.get(item['id'])
                if old and old.get('status') == 'completed':
                    item['goal'] = old['goal']
                    observed_completed[item['id']] = old['goal']
                if observed_completed.get(item['id']) == item.get('goal'):
                    item['status'] = 'completed'
                else:
                    item['status'] = 'pending'
        elif previous.get('steps'):
            plan_data = TaskPlan.model_validate(previous).model_dump()
        else:
            plan_data = TaskPlan(
                goal=assignment.goal,
                steps=[PlanStep(id=assignment.step_id, goal=assignment.goal)],
                constraints=assignment.constraints,
                done_when=assignment.done_when,
            ).model_dump()

        step_by_id = {step['id']: step for step in plan_data['steps']}
        if assignment.step_id not in step_by_id:
            raise ValueError(f'Planner assignment step {assignment.step_id!r} is not part of its active plan')
        step = step_by_id[assignment.step_id]
        if step.get('status') == 'completed':
            raise ValueError(f'Planner assigned already completed step {assignment.step_id!r}')
        for item in plan_data['steps']:
            if item['id'] != assignment.step_id and item.get('status') == 'in_progress':
                item['status'] = 'pending'
        step['status'] = 'in_progress'
        state['plan'] = plan_data
        state['steps'] = {item['id']: dict(item) for item in plan_data['steps']}
        state['current_step_id'] = assignment.step_id
        assignment_data = assignment.model_dump()
        assignment_data['session_revision'] = state.get('session_revision', 0)
        assignment_data['requirements_revision'] = state['current_requirements']['revision']
        state['assignment'] = assignment_data

    def _update_step_from_report(self, task_id, state, report):
        state.pop('verification_blocked_by_input', None)
        step = state.get('steps', {}).get(report.step_id)
        if step is None:
            raise ValueError(f'CoderReport step {report.step_id!r} is not in the active plan')
        status = {
            'completed': 'completed',
            'blocked': 'blocked',
            'failed': 'blocked',
            'needs_replan': 'pending',
        }[report.outcome]
        step['status'] = status
        step['result'] = report.summary
        if status == 'completed':
            completed = set(state.get('completed_step_ids', []))
            completed.add(report.step_id)
            state['completed_step_ids'] = sorted(completed)
            state.setdefault('completed_step_goals', {})[report.step_id] = step['goal']
        else:
            state['completed_step_ids'] = [item for item in state.get('completed_step_ids', []) if item != report.step_id]
            state.get('completed_step_goals', {}).pop(report.step_id, None)
        state['steps'][report.step_id] = step
        for item in state['plan']['steps']:
            if item['id'] == report.step_id:
                item.update(step)
                break
        state['current_step_id'] = None
        self.store.append_event(task_id, 'plan.step.' + ('completed' if status == 'completed' else status), {
            'step_id': report.step_id, 'result': report.summary,
        })

    async def _verify_before_final(self, task, state, executor, config):
        plan = state.get('plan') or {}
        if plan:
            remaining = next((step for step in plan.get('steps', []) if step.get('status') in ('pending', 'in_progress')), None)
            if remaining:
                assignment = ExecutionAssignment(
                    step_id=remaining['id'], goal=remaining['goal'],
                    constraints=plan.get('constraints', []), done_when=plan.get('done_when', []),
                    session_revision=state.get('session_revision', 0),
                )
                self._install_plan_and_assignment(state, TaskPlan.model_validate(plan), assignment)
                self._transition(task['task_id'], state, 'planner', 'coder', 'unfinished_plan_step')
                return 'continue'

        targets = delivery_targets(task, state)
        requirements = state.get('current_requirements') or {}
        missing_target = requirements.get('delivery_required', False) and not any(
            target.operation in ('create', 'modify') for target, _ in targets
        )
        if missing_target:
            delivery = {'status': 'failed', 'targets': [], 'missing_writes': [],
                        'reason': 'missing_write_target'}
        else:
            delivery = self.verifier.delivery_result(task, state)
        state['delivery_verification'] = delivery
        self.store.append_event(task['task_id'], 'delivery.verification', delivery)
        if delivery['status'] == 'failed':
            attempts = state.get('delivery_repair_attempts', 0) + 1
            state['delivery_repair_attempts'] = attempts
            if attempts > config.runtime.max_replans:
                state['error'] = ('未取得工作区文件交付的证据。' if missing_target else
                                  'Explicit requested output paths were not written in the selected workspace.')
                self.store.fail_task(task['task_id'], state)
                self.store.save_checkpoint(task['task_id'], state)
                return 'failed'
            state['planner_reason'] = (
                'The accepted requirements say a file deliverable is required, but no create or modify target was '
                'recorded. Do not claim completion. Return a delegate decision with concrete workspace-relative '
                'delivery targets and have Coder write the files, or ask the user if the destination is unclear.'
            ) if missing_target else (
                'Delivery verification failed for these targets: '
                + ', '.join(delivery['missing_writes'])
                + '. Delegate the missing work with a fresh step ID. Keep the accepted requirements.'
            )
            state['planner_decision'] = None
            self.store.append_event(task['task_id'], 'delivery.repair_requested', {
                'attempt': attempts, 'missing_writes': delivery['missing_writes'],
                'route': 'planner', 'reason': delivery.get('reason', 'missing_writes'),
            })
            self.store.update_task(task['task_id'], status='running', state=state)
            return 'continue'

        if state.get('verification_blocked_by_input'):
            state['error'] = 'Acceptance verification failed; a newer user request arrived before repair could be assigned.'
            self.store.fail_task(task['task_id'], state)
            self.store.save_checkpoint(task['task_id'], state)
            return 'failed'
        command = state.get('acceptance_command') or task.get('state', {}).get('acceptance_command', [])
        report = state.get('last_coder_report') or {}
        if report.get('outcome') != 'completed':
            return 'ok'
        self.verifier.refresh(task,state)
        if not command and not self.verifier.needs_checks(task,state):
            return 'ok'
        if (state.get('verified_coder_turns') == state.get('coder_turns')
                and state.get('verified_check_selection',[]) == state.get('selected_check_ids',[])
                and state.get('verification', {}).get('status') in ('passed','blocked','unknown')):
            return 'ok'

        verification = await self.verifier.verify(self, task, state, executor, state.get('coder_turns', 0))
        state['verification'] = verification
        state['last_coder_report'].setdefault('checks', []).append({'independent_check_status':verification['status'],'coverage':verification.get('coverage')})
        state['verified_coder_turns'] = state.get('coder_turns', 0)
        state['verified_check_selection']=list(state.get('selected_check_ids',[]))
        steering = self._consume_user_messages(task, state)
        if steering:
            state['next_node'] = 'planner'
            state['planner_decision'] = None
            state['planner_reason'] = steering
            if verification['status'] == 'failed':
                state['verification_blocked_by_input'] = True
            self.store.update_task(task['task_id'], status='running', state=state)
            return 'continue'
        if verification['status'] == 'passed':
            if not command:
                state['planner_reason']='Automatic project checks finished. Produce the final answer using verification in the current task context; distinguish syntax-only checks from functional tests.'
                state['next_node']='planner'
                state['planner_decision']=None
                return 'continue'
            return 'ok'
        if verification['status'] in ('unknown','blocked'):
            state['planner_reason']='Automatic verification could not be completed. Read verification in the current task context and explain its limitation without claiming tests passed; do not repeatedly run unavailable tools or change test assertions to pass.'
            state['next_node']='planner'
            state['planner_decision']=None
            return 'continue'

        attempts = state.get('verification_attempts', 0) + 1
        state['verification_attempts'] = attempts
        if attempts > config.runtime.max_replans:
            state['error'] = 'Acceptance verification failed and repair budget is exhausted.'
            self.store.append_event(task['task_id'], 'verification.budget_exhausted', {
                'attempts': attempts, 'verification': verification,
            })
            self.store.fail_task(task['task_id'], state)
            self.store.save_checkpoint(task['task_id'], state)
            return 'failed'

        feedback = self.verifier.repair_message(verification)
        state['verification_feedback'] = feedback
        assignment = ExecutionAssignment.model_validate(state['assignment'])
        state['assignment'] = assignment.model_copy(update={
            'goal': 'Repair the implementation so the acceptance command passes.\n' + feedback,
            'session_revision': state.get('session_revision', 0),
        }).model_dump()
        step_id = assignment.step_id
        state['steps'][step_id]['status'] = 'in_progress'
        state['plan']['steps'] = [
            {**step, 'status': 'in_progress'} if step['id'] == step_id else step
            for step in state['plan']['steps']
        ]
        state['current_step_id'] = step_id
        state['coder_messages'] = []
        self.store.append_event(task['task_id'], 'verification.repair_requested', {
            'attempt': attempts, 'feedback': feedback,
        })
        self._transition(task['task_id'], state, 'planner', 'coder', 'acceptance_repair')
        return 'repair'

    async def _execute(self, task, state, executor, call):
        # Count persisted admissions, including calls interrupted before completion.
        # This check runs before prepare/execute and survives task recovery.
        cap = AppConfig.model_validate(task.get('config') or self.config.snapshot()).runtime.max_tool_calls
        if cap is not None:
            admitted = sum(event['event_type'] == 'tool.started'
                           for event in self.store.read_events(task_id=task['task_id']))
            if admitted >= cap:
                self.store.append_event(task['task_id'], 'tool.budget_exhausted',
                                        {'max_tool_calls': cap, 'admitted': admitted})
                raise RuntimeError('Tool call budget exhausted')
        prepared={}
        try:prepared=executor.prepare(task,call)
        except (ValueError,KeyError,OSError) as exc:
            return ToolResult(tool_call_id=call.id,status='failed',output=str(exc))
        git=executor._git_snapshot(task['repo_path'])
        paths=list(git['file_paths'])
        if call.name in ('read_file','edit_file'):
            raw=prepared.get('path',call.arguments.get('path'))
            if raw:
                try:paths=[executor._relative_path(task['repo_path'],raw)]
                except (OSError,ValueError):paths=[]
        self.store.append_event(task['task_id'],'tool.started',{
            'tool_call_id':call.id,'tool':call.name,'arguments':call.arguments,'prepared':prepared,
            'git_commit':git['git_commit'],'file_paths':paths,
        })
        result=await executor.execute({**task,'state':state},call,prepared)
        state['tool_calls']=state.get('tool_calls',0)+1
        with self.store.transaction():
            metadata=result.metadata
            file_paths=metadata.get('file_paths',paths)
            git_commit=metadata.get('git_commit',git['git_commit'])
            if metadata.get('patch_artifact_id'):
                patch_artifact=executor.store.get_artifact(metadata['patch_artifact_id'])
                patch=Path(patch_artifact['path']).read_text()
                self.store.append_event(task['task_id'],'code.patch',{
                    'tool_call_id':call.id,'artifact_id':metadata['patch_artifact_id'],
                    'patch':patch,'file_paths':file_paths,'git_commit':git_commit,
                })
            self.store.append_event(task['task_id'],'tool.completed' if result.status=='completed' else 'tool.failed',{
                **result.model_dump(),'git_commit':git_commit,'file_paths':file_paths,
            })
            self.store.update_task(task['task_id'],state=state)
        return result
