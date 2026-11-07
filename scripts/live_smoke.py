import asyncio,json,uuid
from pathlib import Path
from evocode.app import Application
from evocode.config import load_config
from evocode.contracts import TaskSpec
async def main():
 c=load_config();c.runtime.execution_backend='host';c.runtime.max_turns=12;c.raw_rag.enabled=False;c.raw_rag.index_when_disabled=False;c.evolution.enabled=False
 root=Path('data/smoke-fixtures')/str(uuid.uuid4());(root/'tests').mkdir(parents=True)
 (root/'calc.py').write_text('def add(a,b):\n    return a-b\n')
 (root/'tests/test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_positive(self): self.assertEqual(add(2,3),5)\n def test_negative(self): self.assertEqual(add(-1,-2),-3)\n')
 app=Application(c)
 try:
  task=app.create_task(TaskSpec(repo_path=str(root.resolve()),prompt='Fix add(a,b) so it adds the two numbers. Read the code and test first, make the smallest fix, run python3 -m unittest discover -s tests. Do not edit tests.',acceptance_command=['python3','-m','unittest','discover','-s','tests']))
  print(task['task_id'],flush=True)
  result=await app.run(task['task_id'])
  Path('docs/live-smoke-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
  print(json.dumps(result,ensure_ascii=False),flush=True)
 finally: await app.close()
asyncio.run(main())
