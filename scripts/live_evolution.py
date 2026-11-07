"""Real local-model proposal check over a synthetic coding task's actual trace.
DeepSeek default configuration and remote authorization are not changed.
"""
import asyncio,json
from pathlib import Path
from evocode.app import Application
from evocode.config import load_config
async def main():
 c=load_config();c.models.evolution=c.models.local.model_copy(deep=True)
 c.evolution.allow_remote_code_context=True;c.evolution.enabled=True
 a=Application(c)
 try:
  tasks=[t for t in a.store.list_tasks() if t['status']=='completed']
  if not tasks:raise RuntimeError('Run live_smoke.py first')
  run=await a.evolution.trigger(tasks[0]['task_id'])
  summary={'model':c.models.evolution.model_id,'provider':'local smoke override','run':run}
  Path('docs/live-evolution-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
  print(json.dumps({'run_id':run['run_id'],'status':run['status'],'reason':run['payload'].get('reason'),'error':run['payload'].get('error')},ensure_ascii=False))
 finally:await a.close()
asyncio.run(main())
