from __future__ import annotations

import json
from pathlib import Path


class RecoveryManager:
    """Reconcile interrupted tool calls without repeating uncertain side effects."""

    def __init__(self, store):
        self.store=store

    def uncertain(self, task_id):
        started={};finished=set()
        for event in self.store.read_events(task_id=task_id):
            payload=event['payload'];call_id=payload.get('tool_call_id')
            if event['event_type']=='tool.started' and call_id is not None:started[call_id]=payload
            elif event['event_type'] in ('tool.completed','tool.failed','tool.resolved'):finished.add(call_id)
        return {key:value for key,value in started.items() if key not in finished}

    def rebuild_coder_messages(self, task_id, messages):
        responses={m.get('tool_call_id') for m in messages if m['role']=='tool'}
        resolutions={e['payload']['tool_call_id']:e['payload'] for e in self.store.read_events(task_id=task_id) if e['event_type'] in ('tool.resolved','tool.completed','tool.failed')}
        rebuilt=[]
        for message in messages:
            rebuilt.append(message)
            for call in message.get('tool_calls',[]):
                if call['id'] not in responses:
                    resolution=resolutions.get(call['id'])
                    rebuilt.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(resolution or {'status':'not_executed','reason':'interrupted before execution; make a fresh tool call if still needed'})})
        return rebuilt

    def resolve_edit_if_observable(self, task_id, call_id, info, resolve):
        prepared=info.get('prepared',{})
        if info.get('tool')!='edit_file' or not prepared:
            return False
        path=Path(prepared['path'])
        import hashlib
        current=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else hashlib.sha256(b'').hexdigest()
        if current==prepared['after_hash']:
            resolve(task_id,call_id,'completed')
            return True
        if current==prepared['before_hash']:
            resolve(task_id,call_id,'failed')
            return True
        return False
