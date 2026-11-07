import subprocess

import pytest

from evocode.config import AppConfig
from evocode.sandbox.evaluator import SandboxEvaluator


@pytest.mark.asyncio
async def test_evaluator_passes_explicit_frozen_collection_to_isolated_runner(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'test_example.py').write_text('def test_example(): pass\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    state = tmp_path / 'state'
    state.mkdir()
    calls = []

    async def runner(spec, snapshot, workspace, *, frozen_memory_collection=None):
        calls.append(frozen_memory_collection)
        return {'passed': True, 'acceptance_protected': True}

    evaluator = SandboxEvaluator(None, AppConfig(data_dir=str(state)), runner=runner)
    snapshot = {'source_data_dir': str(state), 'config': {}, 'frozen_memory_collection': 'stage_frozen'}
    result = await evaluator._execute({'repo_path': str(repo), 'prompt': 'fix',
                                       'acceptance_command': ['python', '-m', 'pytest']}, snapshot)
    assert result['passed'] is True
    assert calls == ['stage_frozen']


@pytest.mark.asyncio
async def test_paired_evaluation_rejects_different_frozen_collections(tmp_path):
    evaluator = SandboxEvaluator(None, AppConfig(data_dir=str(tmp_path)), runner=None)
    with pytest.raises(ValueError, match='same frozen memory collection'):
        await evaluator.compare('run', 'candidate', [], {'frozen_memory_collection': 'one'},
                                {'frozen_memory_collection': 'two'})
