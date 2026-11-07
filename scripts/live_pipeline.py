"""Optional live local-model candidate and meta evaluation. No automatic publish."""
import asyncio,json,subprocess,uuid
from pathlib import Path
from evocode.app import Application
from evocode.config import load_config
async def main():
 c=load_config();c.models.evolution=c.models.local.model_copy(deep=True);c.evolution.allow_remote_code_context=True;c.evolution.enabled=True;c.raw_rag.enabled=False;c.raw_rag.index_when_disabled=False;c.runtime.max_turns=12
 a=Application(c)
 try:
  summary=json.loads(Path('docs/live-evolution-result.json').read_text()); run=a.evolution.get_run(summary['run']['run_id'])
  root=(Path('data/validation-fixtures')/str(uuid.uuid4())).resolve();(root/'tests').mkdir(parents=True)
  (root/'calc.py').write_text('def add(a,b):\n    return a-b\n')
  (root/'tests/test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_positive(self): self.assertEqual(add(7,4),11)\n def test_negative(self): self.assertEqual(add(-2,-3),-5)\n')
  subprocess.run(['git','init','-q',str(root)],check=True)
  subprocess.run(['git','-C',str(root),'add','.'],check=True)
  subprocess.run(['git','-C',str(root),'-c','user.name=EvoCode fixture','-c','user.email=fixture@example.invalid','commit','-qm','Synthetic validation fixture'],check=True)
  scope=run['payload']['candidate'].get('scope_id') or a.store.get_task(run['task_id'])['repository_id']
  specs=[{'repo_path':str(root),'repository_id':scope,'prompt':'Fix add in calc.py using the smallest change and run python -m unittest discover -s tests. Do not edit tests.','acceptance_command':['python','-m','unittest','discover','-s','tests']}]
  result=await a.evolution.evaluate(run['run_id'],specs)
  Path('docs/live-pipeline-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
  print('task evaluation:',result['status'],flush=True)
  meta=await a.evolution.trigger(run['task_id'],kind='meta')
  print('meta proposal:',meta['status'],flush=True)
  if meta['status']=='candidate':
   meta=await a.evolution.evaluate(meta['run_id'],specs)
  Path('docs/live-meta-result.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
  print('meta evaluation:',meta['status'],flush=True)
 finally:await a.close()
asyncio.run(main())
