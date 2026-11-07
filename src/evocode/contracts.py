from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    status: str
    output: str = ""
    exit_code: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    model: str = ""
    reasoning_content: str | None = None
    finish_reason: str | None = None
    response_id: str | None = None
    raw_message: dict[str, Any] = Field(default_factory=dict)


class LLMClient(Protocol):
    async def complete(self, messages: list[dict], *, role: str = "coder", tools: list[dict] | None = None, response_format: dict | None = None, max_tokens: int | None = None) -> LLMResponse: ...


class PlanStep(BaseModel):
    id: str
    goal: str
    status: str = "pending"


class TaskPlan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    done_when: list[str] = Field(default_factory=list)
    suggested_files: list[str] = Field(default_factory=list)
    memory_queries: list[str] = Field(default_factory=list)
    skill_hints: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    event_id: str
    session_id: str
    task_id: str | None = None
    chunk_id: str | None = None
    git_commit: str | None = None
    state_validity: str = "unknown"
    text: str = ""
    score: float = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    status: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    mode: str = "none"
    reason: str | None = None
    fts_hits: int = 0
    dense_hits: int = 0
    reranked: bool = False


class EvaluationReport(BaseModel):
    evaluation_id: str
    passed: bool = False
    status: str = "needs_review"
    baseline: dict[str, Any] = Field(default_factory=dict)
    candidate: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    repo_path: str
    prompt: str
    acceptance_command: list[str] = Field(default_factory=list)
    session_id: str | None = None
    repository_id: str | None = None


class SessionSpec(BaseModel):
    repo_path: str
    title: str | None = None


class SessionMessageSpec(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    intent: Literal["auto", "addendum", "redirect", "new_task"] = "auto"
    client_message_id: str | None = None
    acceptance_command: str | list[str] = ""


class WorkspaceMapping(BaseModel):
    """Authoritative path translation for one execution, derived from its session root."""

    host_root: str
    execution_root: str
    working_directory: str = "."
    target_source: Literal["selected_session_workspace"] = "selected_session_workspace"
    target_path: str = "."
    fresh_container_per_command: bool = True
    network_available: bool = False

    @classmethod
    def for_workspace(cls, host_root: str, execution_backend: str = "docker") -> WorkspaceMapping:
        from pathlib import Path

        resolved_root = str(Path(host_root).expanduser().resolve())
        if execution_backend == "docker":
            execution_root = "/workspace"
            isolated = True
            network_available = False
        elif execution_backend == "host":
            execution_root = resolved_root
            isolated = False
            network_available = True
        else:
            raise ValueError(f"Unknown execution backend: {execution_backend}")
        return cls(
            host_root=resolved_root,
            execution_root=execution_root,
            fresh_container_per_command=isolated,
            network_available=network_available,
        )

    @classmethod
    def for_task(cls, task: dict[str, Any]) -> WorkspaceMapping:
        root = task.get("repo_path")
        if not root:
            # Legacy in-memory tasks used by context tests have no repository root.
            return cls(host_root="", execution_root="/workspace")
        config = task.get("config") or {}
        runtime = config.get("runtime") if isinstance(config, dict) else {}
        backend = runtime.get("execution_backend", "docker") if isinstance(runtime, dict) else "docker"
        return cls.for_workspace(str(root), backend)


class Session(BaseModel):
    session_id: str
    repository_id: str
    repo_path: str
    active_task_id: str | None = None
    summary: str = ""
    summary_through_seq: int = 0
    revision: int = 0
    working_state: dict[str, Any] = Field(default_factory=dict)


class ConversationMessage(BaseModel):
    message_id: str
    session_id: str
    task_id: str | None = None
    role: Literal["user", "assistant"]
    kind: Literal["user", "progress", "question", "final"]
    content: str
    seq: int
    created_at: str = ""


class DeliveryTarget(BaseModel):
    """A filesystem deliverable explicitly selected by Planner, never prose or a route."""
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    kind: Literal["file", "directory"] = "file"
    operation: Literal["create", "modify", "delete", "verify"] = "create"


class RequirementsProposal(BaseModel):
    """Full replacement proposed by the LLM against an accepted revision."""
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(default=0, ge=0)
    source_user_seq: int = Field(ge=0)
    goal: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    done_when: list[str] = Field(default_factory=list)
    delivery_required: bool = False
    delivery_targets: list[DeliveryTarget] = Field(default_factory=list)


class CurrentRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    source_user_seq: int = Field(ge=0)
    goal: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    done_when: list[str] = Field(default_factory=list)
    delivery_required: bool = False
    delivery_targets: list[DeliveryTarget] = Field(default_factory=list)


class TaskState(BaseModel):
    """One execution, including steering messages; legacy state keys are preserved."""
    model_config = ConfigDict(extra="allow")
    task_id: str
    session_id: str
    status: str = "created"
    next_node: Literal["planner", "coder", "tool", "waiting_user", "end"] = "planner"
    plan: TaskPlan | None = None
    current_step_id: str | None = None
    last_user_event_seq: int = 0
    session_revision: int = 0
    current_requirements: CurrentRequirements | None = None
    final_message_id: str | None = None
    assignment: ExecutionAssignment | None = None
    planner_decision: PlannerDecision | None = None
    last_coder_report: CoderReport | None = None
    planner_messages: list[dict[str, Any]] = Field(default_factory=list)
    coder_messages: list[dict[str, Any]] = Field(default_factory=list)
    context_execution_summaries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    acceptance_command: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    project_checks: list[ProjectCheck] = Field(default_factory=list)
    selected_check_ids: list[str] = Field(default_factory=list)
    skill_snapshot: dict[str, str] | None = None
    tokens: int = 0
    model_responses: int = 0
    coder_turns: int = 0


class ExecutionAssignment(BaseModel):
    step_id: str
    goal: str
    user_requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    done_when: list[str] = Field(default_factory=list)
    relevant_context: list[str] = Field(default_factory=list)
    previous_results: list[str] = Field(default_factory=list)
    session_revision: int = 0
    requirements_revision: int = 0


class PlannerDecision(BaseModel):
    action: Literal["respond", "delegate", "ask_user"]
    answer: str | None = None
    question: str | None = None
    plan: TaskPlan | None = None
    assignment: ExecutionAssignment | None = None
    check_ids: list[str] = Field(default_factory=list, max_length=3)
    requirements: RequirementsProposal | None = None

    @model_validator(mode="after")
    def validate_action(self):
        required = {"respond": self.answer, "ask_user": self.question, "delegate": self.assignment}[self.action]
        if required is None or isinstance(required, str) and not required.strip():
            raise ValueError(f"{self.action} requires its nonempty output")
        if self.action != "delegate" and self.assignment is not None:
            raise ValueError("Only delegate may contain an assignment")
        return self


class ProjectCheck(BaseModel):
    id: str
    name: str
    kind: Literal['test', 'build', 'typecheck', 'syntax']
    command: list[str] = Field(min_length=1)
    cwd: str = '.'
    source: str


class NodeContext(BaseModel):
    context_id: str
    task_id: str
    session_id: str
    node: Literal["planner", "coder"]
    session_revision: int = 0
    workspace: WorkspaceMapping | None = None
    messages: list[dict[str, Any]]
    evidence_refs: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    history_complete: bool = False
    artifact_id: str | None = None


class CoderReport(BaseModel):
    step_id: str
    outcome: Literal["completed", "blocked", "needs_replan", "failed"]
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)


class ExecutionEvent(BaseModel):
    event_id: str
    session_id: str
    task_id: str | None = None
    seq: int
    event_type: str
    node: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""

