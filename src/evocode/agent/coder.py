from __future__ import annotations

import json

from evocode.contracts import CoderReport


class CoderReportParseError(ValueError):
    """The coder response does not contain one valid report for this step."""


class Coder:
    """Inner execution node. Its narrative is an internal structured report."""

    async def complete(self, task, state, assembler, llm, messages, evidence, skills, tools, assignment=None):
        if assignment is not None:
            state['assignment'] = assignment.model_dump() if hasattr(assignment, 'model_dump') else assignment
        context=await assembler.build(
            {**task,'state':state}, 'coder', messages, evidence, skills,
            tools=tools,
            response_format={'type': 'json_object'} if (state.get('coder_report_retry_pending') or state.get('coder_report_only')) else None,
            max_tokens=assembler.config.models.local.max_output_tokens,
        )
        context_id = assembler.last_context_id
        report_only = state.get('coder_report_retry_pending') or state.get('coder_report_only')
        report_format = {'response_format': {'type': 'json_object'}} if report_only else {}
        response=await llm.complete(context,role='coder',tools=tools,max_tokens=assembler.config.models.local.max_output_tokens,**report_format)
        assembler.record_model_usage(task['task_id'], 'coder', response.usage, context_id=context_id)
        state['model_responses']=state.get('model_responses',0)+1
        state['tokens']=state.get('tokens',0)+response.usage.get('total_tokens',0)
        usage=state.setdefault('token_usage',{})
        usage['coder']=usage.get('coder',0)+response.usage.get('total_tokens',0)
        return response

    @staticmethod
    def parse_report(content, step_id):
        content = (content or '').strip()
        decoder = json.JSONDecoder()
        objects = []
        index = 0

        # Find complete top-level JSON objects within prose or Markdown fences.
        # Advancing to the end of each decoded object avoids counting its nested
        # dictionaries (for example, entries in `checks`) as separate reports.
        while index < len(content):
            start = content.find('{', index)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(content, start)
            except json.JSONDecodeError:
                if objects and content[start + 1:].lstrip().startswith('"'):
                    raise CoderReportParseError('Coder response has a trailing incomplete JSON object')
                index = start + 1
                continue
            if isinstance(value, dict):
                objects.append(value)
            index = end

        if len(objects) != 1:
            if not objects:
                raise CoderReportParseError('Coder response contains no complete JSON object')
            raise CoderReportParseError('Coder response contains multiple JSON objects; expected exactly one')

        try:
            report = CoderReport.model_validate(objects[0])
        except Exception as exc:
            raise CoderReportParseError(f'Invalid CoderReport schema: {exc}') from exc
        if report.step_id != step_id:
            raise CoderReportParseError(f'CoderReport step_id must be {step_id!r}')
        return report
