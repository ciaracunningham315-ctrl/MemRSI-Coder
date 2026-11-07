import json

import pytest

from evocode.agent.coder import Coder, CoderReportParseError


def test_parse_report_from_preamble_and_json_fence():
    content = '核验与运行验证已完成。\n\n```json\n' + json.dumps({
        'step_id': 's1',
        'outcome': 'completed',
        'summary': 'Finished.',
        'checks': [{'name': 'unit', 'result': {'passed': True}}],
    }, ensure_ascii=False) + '\n```'

    report = Coder.parse_report(content, 's1')

    assert report.step_id == 's1'
    assert report.outcome == 'completed'
    assert report.checks[0]['result']['passed'] is True


def test_parse_report_rejects_multiple_json_objects():
    first = {'step_id': 's1', 'outcome': 'completed', 'summary': 'Finished.'}
    second = {'step_id': 's1', 'outcome': 'failed', 'summary': 'Actually failed.'}

    with pytest.raises(CoderReportParseError, match='multiple JSON objects'):
        Coder.parse_report(json.dumps(first) + '\n' + json.dumps(second), 's1')


def test_parse_report_rejects_trailing_incomplete_json_object():
    report = '{"step_id":"s1","outcome":"completed","summary":"Finished."}'

    with pytest.raises(CoderReportParseError, match='trailing incomplete JSON object'):
        Coder.parse_report(report + '\n{"step_id":', 's1')


def test_parse_report_rejects_invalid_schema():
    with pytest.raises(CoderReportParseError, match='Invalid CoderReport schema'):
        Coder.parse_report('{"step_id":"s1","outcome":"unknown","summary":"x"}', 's1')


def test_parse_report_checks_expected_step_id():
    with pytest.raises(CoderReportParseError, match="step_id must be 's2'"):
        Coder.parse_report('{"step_id":"s1","outcome":"completed","summary":"x"}', 's2')


def test_parse_report_reproduces_recorded_preamble_failure():
    content = '''核验与运行验证已完成。

```json
{"step_id":"s1","outcome":"completed","summary":"Finished.","changed_files":[],"checks":[],"artifact_refs":[]}
```'''

    assert Coder.parse_report(content, 's1').summary == 'Finished.'
