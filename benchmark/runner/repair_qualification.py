"""Offline reference-edit replay and leave-one-file-out admission checks.

These are actual tool executions, not model-generated trajectories or ability
results. They establish that ordinary edits can yield the required evidence.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import uuid

from runner.dataset_validation import STAGES, check_variant, inventory, passed
from runner.stage_packages import materialize_stage_package


def qualify_repairs(root: Path) -> dict:
    from evocode.agent.tools import ToolExecutor
    from evocode.config import AppConfig
    from evocode.contracts import ToolCall
    from evocode.memory.service import MemoryService
    from evocode.persistence import EventStore

    root = root.resolve()
    work = root / '.runtime' / ('repair_qualification_' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    rows = []
    for stage in STAGES:
        manifest = json.loads((root / 'scenarios/repo_api' / stage / 'manifest.json').read_text())
        baseline, reference = root / manifest['code_dir'], root / manifest['reference_dir']
        before, after = inventory(baseline), inventory(reference)
        changed = sorted(name for name in before.keys() | after.keys()
                         if before.get(name) != after.get(name) and name.startswith('api/') and name.endswith('.py'))
        ablations = []
        for number, name in enumerate(changed):
            overlay = work / stage / ('omit_' + str(number))
            shutil.copytree(reference, overlay)
            target = overlay / name
            if name in before:
                shutil.copyfile(baseline / name, target)
            elif target.exists():
                target.unlink()
            checks = check_variant(root, work / stage / 'ablations', manifest,
                                   str(number), overlay, sys.executable)
            ablations.append({'omitted_file': name, 'commands': checks,
                              'rejected': not passed(checks['combined'])})

        edits = []
        evidence = {}
        if stage in ('t1_seed', 't2_transfer'):
            repo = work / stage / 'tool_replay'
            materialize_stage_package(root, manifest, repo)
            config = AppConfig(data_dir=str(work / stage / 'tool_state'))
            store = EventStore(config.sqlite_path)
            executor = ToolExecutor(store, config)
            memory = MemoryService(store, config)
            try:
                task = {'task_id': 'offline_reference_replay', 'repository_id': 'repo_api', 'repo_path': str(repo)}
                for number, name in enumerate(changed):
                    if name not in after:
                        raise ValueError('reference replay currently requires existing/new files, not deletions')
                    old = (repo / name).read_text() if (repo / name).exists() else ''
                    call = ToolCall(id=str(number), name='edit_file', arguments={
                        'path': name, 'old_text': old, 'new_text': (reference / name).read_text()})
                    result = asyncio.run(executor.execute(task, call))
                    if result.status != 'completed':
                        raise ValueError(result.output)
                    artifact = store.get_artifact(result.metadata['patch_artifact_id'])
                    patch = Path(artifact['storage_path']).read_text()
                    metadata = memory._evidence_metadata([{'payload': {**result.metadata, 'metadata': result.metadata}}], patch)
                    metadata['repository_id'] = 'repo_api'
                    evidence[name] = metadata
                    edits.append({'file': name, 'actual_patch': patch, 'metadata': metadata,
                                  'tool_result': result.model_dump()})
            finally:
                store.close()
        natural = (stage not in ('t1_seed', 't2_transfer') or
                   all(name in evidence and evidence[name]['content_evidence'] for name in ('api/validation.py', 'api/errors.py'))
                   and 'require_count' in evidence['api/validation.py']['symbols'])
        rows.append({'stage': stage, 'changed_business_files': changed, 'ablations': ablations,
                     'actual_reference_edits': edits, 'natural_required_evidence': natural,
                     'source_fingerprint': {'baseline': before, 'reference': after},
                     'passed': len(changed) >= 2 and sum(x['rejected'] for x in ablations) >= 2 and natural})
    return {'kind': 'offline_reference_repair_qualification', 'model_calls': 0,
            'model_trajectory_revalidation': 'not_run', 'formal_results': False,
            'boundary': 'Reference edit replay, not an Agent solution. Ablation establishes multi-file reference necessity, not a lower bound for every possible solution or absence of ceiling effects.',
            'passed': all(row['passed'] for row in rows), 'stages': rows}
