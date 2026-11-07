"""Validated proposal contracts and deterministic SKILL.md rendering."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonEmptyText = Annotated[str, Field(min_length=1)]
TriggerTerm = Annotated[str, Field(min_length=2, max_length=60)]


class TaskOperation(StrEnum):
    CREATE = 'CREATE'
    REVISE = 'REVISE'
    MERGE = 'MERGE'
    GENERALIZE = 'GENERALIZE'
    DEPRECATE = 'DEPRECATE'


class MetaOperation(StrEnum):
    CREATE = 'CREATE'
    REVISE = 'REVISE'


class SkillBody(BaseModel):
    """Structured instructions that can be rendered as a stable Markdown body."""

    model_config = ConfigDict(extra='forbid', strict=True)

    applicability: NonEmptyText = Field(description='When this skill applies')
    steps: list[NonEmptyText] = Field(min_length=1, description='Ordered procedure steps')
    limitations: list[NonEmptyText] = Field(description='Known limits and cases where the procedure may fail')

    @field_validator('applicability', mode='before')
    @classmethod
    def validate_applicability(cls, value):
        return _nonempty_text(value, 'applicability')

    @field_validator('steps', 'limitations')
    @classmethod
    def validate_text_list(cls, values, info):
        if info.field_name == 'steps' and not values:
            raise ValueError('steps must contain at least one instruction')
        return [_nonempty_text(value, info.field_name) for value in values]


def _nonempty_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a non-empty string')
    return value.strip()


class _ProposalBase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    name: NonEmptyText
    level: str
    scope_id: str | None
    skill_id: str | None
    parent_version_id: str | None
    source_skill_ids: list[NonEmptyText]
    evidence_event_ids: list[NonEmptyText]
    reason: NonEmptyText
    # Both keys are required in strict JSON Schema. Exactly one representation
    # is accepted: structured body for new proposals, or content for legacy ones.
    body: SkillBody | None
    content: str | None

    @field_validator('name', 'reason', mode='before')
    @classmethod
    def validate_required_text(cls, value, info):
        return _nonempty_text(value, info.field_name)

    @field_validator('scope_id', 'skill_id', 'parent_version_id', mode='before')
    @classmethod
    def validate_optional_ids(cls, value, info):
        if value is None:
            return None
        return _nonempty_text(value, info.field_name)

    @field_validator('source_skill_ids', 'evidence_event_ids')
    @classmethod
    def validate_ids(cls, values, info):
        return [_nonempty_text(value, info.field_name) for value in values]

    @field_validator('content', mode='before')
    @classmethod
    def validate_legacy_content(cls, value):
        if value is None:
            return None
        return _nonempty_text(value, 'content')

    @model_validator(mode='after')
    def require_one_body_representation(self):
        if (self.body is None) == (self.content is None):
            raise ValueError('provide exactly one of body or legacy content')
        return self


class TaskProposal(_ProposalBase):
    operation: TaskOperation = Field(strict=False)
    level: Literal['project', 'general']
    scope_id: str | None
    trigger_terms: list[TriggerTerm] = Field(max_length=8)

    @field_validator('trigger_terms')
    @classmethod
    def validate_trigger_terms(cls, values):
        if len(values) > 8:
            raise ValueError('trigger_terms must contain at most 8 values')
        terms = [_nonempty_text(term, 'trigger_terms') for term in values]
        if any(not 2 <= len(term) <= 60 for term in terms):
            raise ValueError('trigger_terms must be between 2 and 60 characters')
        return terms


class MetaProposal(_ProposalBase):
    operation: MetaOperation = Field(strict=False)
    level: Literal['meta']
    scope_id: None


def render_skill_body(body: SkillBody) -> str:
    """Render structured content in a deterministic form for SkillRegistry."""
    steps = '\n'.join(f'{index}. {step}' for index, step in enumerate(body.steps, start=1))
    limitations = '\n'.join(f'- {item}' for item in body.limitations) or '- None specified.'
    return (
        '## Applicability\n\n'
        f'{body.applicability}\n\n'
        '## Steps\n\n'
        f'{steps}\n\n'
        '## Limitations\n\n'
        f'{limitations}\n'
    )


def proposal_schema(kind: str, *, strict_tool: bool = False) -> dict:
    """Return a strict, all-fields-required JSON Schema for the proposal kind."""
    if kind == 'task':
        model = TaskProposal
    elif kind == 'meta':
        model = MetaProposal
    else:
        raise ValueError("kind must be 'task' or 'meta'")
    schema = model.model_json_schema()
    schema['additionalProperties'] = False
    schema['required'] = list(schema['properties'])
    if strict_tool:
        _remove_unsupported_tool_constraints(schema)
    return schema


def _remove_unsupported_tool_constraints(value):
    """Keep length validation local for strict-tool providers with a smaller schema dialect."""
    if isinstance(value, dict):
        for keyword in ('minLength', 'maxLength', 'minItems', 'maxItems'):
            value.pop(keyword, None)
        for item in value.values():
            _remove_unsupported_tool_constraints(item)
    elif isinstance(value, list):
        for item in value:
            _remove_unsupported_tool_constraints(item)


def parse_proposal(value, kind: str) -> dict:
    """Validate and normalize a model proposal, retaining its metadata.

    Pydantic's ``ValidationError`` (a ``ValueError`` subclass) is allowed to
    propagate so callers can use its actionable field-level diagnostics.
    """
    if not isinstance(value, dict):
        raise TypeError('Proposal must be a JSON object')
    if kind == 'task':
        model = TaskProposal
        defaults = {
            'operation': TaskOperation.CREATE,
            'scope_id': None,
            'trigger_terms': [],
            'skill_id': None,
            'parent_version_id': None,
            'source_skill_ids': [],
            'evidence_event_ids': [],
            'body': None,
            'content': None,
        }
    elif kind == 'meta':
        model = MetaProposal
        defaults = {
            'operation': MetaOperation.CREATE,
            'level': 'meta',
            'scope_id': None,
            'skill_id': None,
            'parent_version_id': None,
            'source_skill_ids': [],
            'evidence_event_ids': [],
            'body': None,
            'content': None,
        }
    else:
        raise ValueError("kind must be 'task' or 'meta'")
    # Older model prompts returned flat content and omitted fields which were
    # optional in the original service contract. Normalize only known fields;
    # model validation still rejects every unknown key.
    normalized = {**defaults, **value}
    proposal = model.model_validate(normalized)
    result = proposal.model_dump(mode='json')
    if proposal.body is not None:
        result['content'] = render_skill_body(proposal.body)
    return result
