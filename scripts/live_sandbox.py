import asyncio,json,uuid
from pathlib import Path
from evocode.app import Application
from evocode.config import load_config
async def main():
 c=load_config();c.evolution.enabled=False;c.raw_rag.enabled=False;c.raw_rag.index_when_disabled=False;c.runtime.max_turns=12
 a=Application(c)
 try:
  root=(Path('data/sandbox-fixtures')/str(uuid.uuid4())).resolve();(root/'tests').mkdir(parents=True)
  (root/'calc.py').write_text('def add(a,b):\n    return a+b\n')
  (root/'tests/test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_positive(self): self.assertEqual(add(2,3),5)\n def test_negative(self): self.assertEqual(add(-1,-2),-3)\n')
  # A real isolated execution with protected test content; source fixture is already fixed.
  tests={'tests/test_calc.py':(root/'tests/test_calc.py').read_text()}
  spec={'repo_path':str(root),'prompt':'Read calc.py and run python -m unittest discover -s tests to verify add. Do not modify tests.','acceptance_command':['python','-m','unittest','discover','-s','tests']}
  result=await a.run_isolated(spec,{'config':c.snapshot(),'acceptance_files':tests,'skill_versions':[]},str(root))
  Path('docs/live-sandbox-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='result'},ensure_ascii=False))
 finally:await a.close()
asyncio.run(main())
