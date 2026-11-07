"""Resolve only filesystem targets from the accepted requirements snapshot."""
from __future__ import annotations

from pathlib import Path

from evocode.contracts import CurrentRequirements, DeliveryTarget, WorkspaceMapping


def resolve_delivery_target(target: DeliveryTarget, mapping: WorkspaceMapping) -> Path:
    if not target.path.strip() or '\x00' in target.path:
        raise ValueError('Delivery target must contain a valid filesystem path')
    root = Path(mapping.host_root)
    path = Path(target.path).expanduser()
    execution_root = Path(mapping.execution_root)
    if path.is_absolute() and path.is_relative_to(execution_root):
        path = root / path.relative_to(execution_root)
    return (root / path).resolve()


def delivery_targets(task, state):
    snapshot = state.get('current_requirements')
    if not snapshot:
        return []
    requirements = CurrentRequirements.model_validate(snapshot)
    mapping = WorkspaceMapping.for_task(task)
    return [
        (target, resolve_delivery_target(target, mapping))
        for target in requirements.delivery_targets
    ]
