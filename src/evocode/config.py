from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model_id: str = ""
    api_key_env: str = "EVOCODE_LOCAL_API_KEY"
    max_context_tokens: int = Field(default=131072, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    timeout: float = 120
    thinking: Literal['enabled', 'disabled'] | None = None
    # Optional backend-specific API that returns an exact count for the complete
    # chat request (messages and tools). No endpoint is guessed or probed.
    token_count_url: str | None = None

    @model_validator(mode="after")
    def output_fits_context(self):
        if self.max_output_tokens >= self.max_context_tokens:
            raise ValueError("max_output_tokens must be smaller than max_context_tokens")
        return self


class ModelsConfig(BaseModel):
    local: ModelConfig = Field(default_factory=ModelConfig)
    evolution: ModelConfig = Field(default_factory=lambda: ModelConfig(base_url="https://api.deepseek.com/v1", model_id="deepseek-flash", api_key_env="DEEPSEEK_API_KEY"))


class MemoryConfig(BaseModel):
    enabled: bool = True
    index_when_disabled: bool = True
    background_indexing: bool = True
    qdrant_url: str = "http://127.0.0.1:6333"
    collection: str = "evocode_events"
    embedding_base_url: str = "http://127.0.0.1:8001/v1"
    embedding_model: str = ""
    embedding_api_key_env: str = "EVOCODE_EMBEDDING_API_KEY"
    embedding_dimensions: int | None = None
    rerank_enabled: bool = False
    rerank_url: str = "http://127.0.0.1:8002/rerank"
    rerank_model: str = ""
    rerank_api_key_env: str = "EVOCODE_RERANK_API_KEY"
    rerank_provider: str = "compatible"
    rerank_top_k: int = 5
    dense_top_k: int = 20
    final_evidence_count: int = 5
    max_context_tokens: int = 4096
    # Optional frozen history allowlists, captured in each task configuration.
    history_allowed_task_ids: list[str] | None = None
    history_allowed_event_ids: list[str] | None = None


class SkillsConfig(BaseModel):
    enabled: bool = True
    max_loaded_skills: int = 3


class EvolutionConfig(BaseModel):
    enabled: bool = True
    auto_trigger_enabled: bool = True
    auto_evaluate: bool = False
    auto_publish: bool = False
    freeze_meta_skill: bool = False
    proposal_format: Literal["json_object", "json_schema", "strict_tool"] = "json_object"
    selection_task_specs: list[dict] = Field(default_factory=list)
    allow_remote_code_context: bool = False
    batch_task_count: int = 8
    similar_issue_threshold: int = 3
    meta_review_after_runs: int = 25
    sandbox_image: str = "python:3.12-slim"
    timeout: int = 300
    target_metric: str = "tool_calls"


class RuntimeConfig(BaseModel):
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_turns: int = 40
    max_replans: int = 3
    tool_timeout: int = 120
    planner_context_tokens: int = 8192
    coder_context_tokens: int = 24576
    checkpoint_every_events: int = 30
    execution_backend: str = "docker"
    docker_image: str = "python:3.12-slim"
    max_output_chars: int = 24000
    context_safety_margin_tokens: int = Field(default=2048, ge=0)


class AppConfig(BaseModel):
    data_dir: str = "data"
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    raw_rag: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @property
    def sqlite_path(self) -> Path:
        return Path(self.data_dir).resolve() / "evocode.db"

    def snapshot(self) -> dict:
        # Selection specifications are instance-only orchestration inputs. They
        # are passed to the evaluator directly and should not be copied into
        # task snapshots or remote proposal context.
        return self.model_dump(exclude={'evolution': {'selection_task_specs'}})


def load_config(path: str | None = None) -> AppConfig:
    # Local credentials are never included in config snapshots.
    env_file = Path('.env')
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())
    source = Path(path or os.getenv("EVOCODE_CONFIG", "config/local.yaml"))
    data = yaml.safe_load(source.read_text()) if source.exists() else {}
    config = AppConfig.model_validate(data or {})
    for env, attr in (("EVOCODE_MODEL_URL", "base_url"), ("EVOCODE_MODEL_ID", "model_id")):
        if os.getenv(env):
            setattr(config.models.local, attr, os.environ[env])
    if os.getenv("EVOCODE_DATA_DIR"):
        config.data_dir = os.environ["EVOCODE_DATA_DIR"]
    if os.getenv("EVOCODE_DOCKER_IMAGE"):
        config.runtime.docker_image = os.environ["EVOCODE_DOCKER_IMAGE"]
    return config

