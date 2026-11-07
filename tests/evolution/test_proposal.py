import pytest
import json
from pydantic import ValidationError

from evocode.evolution.proposal import parse_proposal, proposal_schema


def test_strict_tool_schema_leaves_length_validation_to_local_parser():
    rendered = json.dumps(proposal_schema('task', strict_tool=True))
    assert all(f'"{keyword}"' not in rendered
               for keyword in ('minLength', 'maxLength', 'minItems', 'maxItems'))
    with pytest.raises(ValidationError):
        parse_proposal(task_proposal(body={
            'applicability': 'Applies here', 'steps': [], 'limitations': [],
        }), 'task')


def task_proposal(**overrides):
    return {
        'operation': 'CREATE',
        'name': 'retry safely',
        'level': 'general',
        'scope_id': None,
        'trigger_terms': ['retry', 'transient error'],
        'skill_id': None,
        'parent_version_id': None,
        'source_skill_ids': [],
        'evidence_event_ids': ['event-1'],
        'reason': 'The trace shows a transient failure was retried successfully.',
        'body': {
            'applicability': 'Use after a retryable network failure.',
            'steps': ['Check whether the operation is safe to repeat.', 'Retry with bounded backoff.'],
            'limitations': ['Do not retry non-idempotent writes without an idempotency key.'],
        },
        'content': None,
        **overrides,
    }


def test_structured_task_proposal_renders_stable_registry_content_and_keeps_metadata():
    proposal = parse_proposal(task_proposal(), 'task')

    assert proposal['content'] == (
        '## Applicability\n\n'
        'Use after a retryable network failure.\n\n'
        '## Steps\n\n'
        '1. Check whether the operation is safe to repeat.\n'
        '2. Retry with bounded backoff.\n\n'
        '## Limitations\n\n'
        '- Do not retry non-idempotent writes without an idempotency key.\n'
    )
    assert proposal['body']['steps'][1] == 'Retry with bounded backoff.'
    assert proposal['evidence_event_ids'] == ['event-1']
    assert proposal['trigger_terms'] == ['retry', 'transient error']
    assert proposal['operation'] == 'CREATE'


def test_legacy_flat_content_is_accepted_and_optional_fields_are_normalized():
    proposal = parse_proposal({
        'name': 'legacy skill',
        'level': 'general',
        'reason': 'Existing clients returned a flat content string.',
        'content': '## Existing instructions\n\nDo the safe thing.',
    }, 'task')

    assert proposal['content'] == '## Existing instructions\n\nDo the safe thing.'
    assert proposal['body'] is None
    assert proposal['operation'] == 'CREATE'
    assert proposal['source_skill_ids'] == []
    assert proposal['evidence_event_ids'] == []
    assert proposal['scope_id'] is None


def test_meta_schema_uses_meta_operation_and_structured_body():
    proposal = parse_proposal({
        'operation': 'REVISE',
        'name': 'learning strategy',
        'reason': 'Use a repeatable evidence review.',
        'body': {
            'applicability': 'When proposing or revising a skill.',
            'steps': ['Compare outcomes with the source trace.'],
            'limitations': [],
        },
    }, 'meta')

    assert proposal['level'] == 'meta'
    assert proposal['scope_id'] is None
    assert proposal['operation'] == 'REVISE'
    assert proposal['content'].endswith('## Limitations\n\n- None specified.\n')


@pytest.mark.parametrize('patch', [
    {'operation': 'INVALID'},
    {'reason': '   '},
    {'body': {'applicability': 'Use it', 'steps': [], 'limitations': []}},
    {'body': {'applicability': '', 'steps': ['Do something'], 'limitations': []}},
    {'body': {'applicability': 'Use it', 'steps': ['Do it'], 'limitations': []}, 'content': 'also supplied'},
    {'unexpected': True},
])
def test_invalid_proposals_are_rejected(patch):
    with pytest.raises(ValidationError):
        parse_proposal(task_proposal(**patch), 'task')


def test_proposal_schema_is_strict_and_all_properties_are_required():
    schema = proposal_schema('task')
    properties = schema['properties']

    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(properties)
    assert set(properties['body']['anyOf'][0]['$ref'].split('/')[-1:] ) == {'SkillBody'}
    assert properties['content']['anyOf'][1]['type'] == 'null'
    operation_schema = schema['$defs'][properties['operation']['$ref'].split('/')[-1]]
    assert operation_schema['enum'] == ['CREATE', 'REVISE', 'MERGE', 'GENERALIZE', 'DEPRECATE']
    assert schema['$defs']['SkillBody']['additionalProperties'] is False
    assert set(schema['$defs']['SkillBody']['required']) == {'applicability', 'steps', 'limitations'}


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match='kind must be'):
        proposal_schema('other')
