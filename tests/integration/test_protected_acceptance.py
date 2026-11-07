import hashlib
import os
import json
import pytest
from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, ToolCall

@pytest.mark.skipif(os.getenv('EVOCODE_DOCKER_TESTS')!='1',reason='explicit Docker integration opt-in')
async def test_modified_tests_cannot_pass_protected_oracle(tmp_path,monkeypatch):
    repo=tmp_path/'repo';(repo/'tests').mkdir(parents=True)
    (repo/'calc.py').write_text('def add(a,b): return a-b\n')
    original='import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n'
    (repo/'tests/test_calc.py').write_text(original)
    class Model:
        def __init__(self,*a):self.turn=0
        async def complete(self,messages,*,role='coder',**kw):
            if role=='planner':
                decision={'action':'respond','answer':'Tests pass'} if self.turn else {'action':'delegate','assignment':{'step_id':'s1','goal':'fix'}}
                if decision['action'] == 'delegate':
                    decision['requirements'] = {'source_user_seq': 1, 'goal': 'Run the protected check'}
                return LLMResponse(content=json.dumps(decision))
            self.turn+=1
            if self.turn==1:return LLMResponse(tool_calls=[ToolCall(id='tamper',name='edit_file',arguments={'path':'tests/test_calc.py','old_text':original,'new_text':'import unittest\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertTrue(True)\n'})])
            return LLMResponse(content='{"step_id":"s1","outcome":"completed","summary":"Tests pass"}')
    monkeypatch.setattr('evocode.app.ModelClient',Model)
    c=AppConfig(data_dir=str(tmp_path/'data'));c.raw_rag.enabled=True;c.raw_rag.embedding_model='offline-test';c.raw_rag.background_indexing=False;c.evolution.enabled=False
    async def forbidden_rebuild(*a,**kw): raise AssertionError('Unexpected paid reindex in manual mode')
    monkeypatch.setattr('evocode.memory.MemoryService.rebuild_index',forbidden_rebuild)
    app=Application(c,Model())
    try:
        result=await app.run_isolated({'repo_path':str(repo),'prompt':'fix','acceptance_command':['python','-m','unittest','discover','-s','tests']},{'config':c.snapshot(),'acceptance_files':{'tests/test_calc.py':original}},str(repo))
        assert result['result']['state']['verification']['status']=='passed'
        assert result['acceptance_protected'] is True
        assert result["acceptance_file_sha256"] == {
            "tests/test_calc.py": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        }
        assert result['passed'] is False
        assert 'FAIL' in result['acceptance_output']
    finally:await app.close()


@pytest.mark.skipif(os.getenv("EVOCODE_DOCKER_TESTS") != "1", reason="explicit Docker integration opt-in")
async def test_private_oracle_never_enters_agent_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("correct\n")
    protected_path = "tests/private_acceptance.py"
    protected_source = (
        "from pathlib import Path\n"
        "assert Path('/workspace/answer.txt').read_text() == 'correct\\n'\n"
    )
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.raw_rag.enabled = False

    class FakeController:
        def __init__(self, store, *_args):
            self.store = store

        async def run(self, task_id):
            assert self.store.get_task(task_id)["state"].get("acceptance_command", []) == []
            assert not (workspace / protected_path).exists()
            return {"status": "completed"}

    monkeypatch.setattr("evocode.agent.AgentController", FakeController)
    app = Application(config)
    try:
        result = await app.run_isolated(
            {
                "repo_path": str(workspace),
                "prompt": "Write the answer file",
                "acceptance_command": ["python", "-I", "-B", protected_path],
            },
            {
                "oracle_only_acceptance": True,
                "acceptance_files": {protected_path: protected_source},
            },
            str(workspace),
            llm_factory=lambda _config: object(),
        )
    finally:
        await app.close()
    assert result["passed"] is True
    assert result["acceptance_protected"] is True
    assert not (workspace / protected_path).exists()
