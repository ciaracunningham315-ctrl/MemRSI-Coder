from __future__ import annotations

from pathlib import Path

from evocode.contracts import ProjectCheck, ToolCall

from .checks import ProjectCheckDiscovery
from .delivery import delivery_targets


class AcceptanceVerifier:
    """Discover project checks, execute the selected checks, and report evidence."""

    def __init__(self,store):
        self.store=store

    def refresh(self,task,state):
        changed=set()
        for event in self.store.read_events(task_id=task['task_id']):
            if event['event_type']=='code.patch' and event['payload'].get('patch','').strip():
                changed.update(event['payload'].get('file_paths',[]))
        # Reported paths are hints, never evidence that a test passed.
        changed.update((state.get('last_coder_report') or {}).get('changed_files',[]))
        root=Path(task['repo_path']).resolve()
        changed=sorted(path for path in changed if isinstance(path,str) and (root/path).resolve().is_relative_to(root))
        checks=ProjectCheckDiscovery().discover(root,changed,task.get('config',{}).get('runtime',{}).get('execution_backend','docker'))
        state['project_checks']=[check.model_dump() for check in checks]
        state['changed_files_for_checks']=changed
        return checks

    def needs_checks(self,task,state):
        code_changes=any(Path(path).suffix.lower() not in {'.md','.rst','.txt','.png','.jpg','.jpeg','.svg','.gif'} for path in state.get('changed_files_for_checks',[]))
        if state.get('acceptance_command') or code_changes:
            return True
        return any(e['event_type']=='tool.started' and e['payload'].get('tool')=='run_tests' for e in self.store.read_events(task_id=task['task_id']))

    def delivery_result(self, task, state=None):
        """Verify the accepted filesystem outcomes using observed paths and tool events."""
        state = task['state'] if state is None else state
        targets = delivery_targets(task, state)
        if not targets:
            return {'status': 'not_applicable', 'targets': [], 'missing_writes': []}

        root = Path(task['repo_path']).resolve()
        events = self.store.read_events(task_id=task['task_id'])
        accepted_seq = max(
            (event['seq'] for event in events
             if event['event_type'] == 'requirements.updated' and event['payload']['changed']),
            default=0,
        )
        events = [event for event in events if event['seq'] > accepted_seq]
        write_calls = {
            event['payload'].get('tool_call_id') for event in events
            if event['event_type'] == 'tool.started'
            and event['payload'].get('tool') in ('edit_file', 'run_command', 'run_tests')
        }
        written = {
            (root / path).resolve() for event in events
            if event['event_type'] == 'code.patch' or (
                event['event_type'] == 'tool.completed'
                and event['payload'].get('tool_call_id') in write_calls
            )
            for path in event['payload'].get('file_paths', [])
            if isinstance(path, str)
        }

        def satisfied(target, path):
            if not path.is_relative_to(root):
                return False
            if target.operation == 'delete':
                return not path.exists() and not path.is_symlink()
            exists = path.is_dir() if target.kind == 'directory' else path.is_file()
            if target.operation == 'verify':
                return exists
            return exists and any(
                item.exists() and (item == path or (
                    target.kind == 'directory' and item.is_relative_to(path)
                ))
                for item in written
            )

        missing = [str(path) for target, path in targets if not satisfied(target, path)]
        return {'status': 'failed' if missing else 'passed',
                'targets': [str(path) for _, path in targets], 'missing_writes': missing}

    async def verify(self, controller, task, state, executor, turn):
        command=state.get('acceptance_command') or task.get('state',{}).get('acceptance_command',[])
        if command:
            checks=[ProjectCheck(id='explicit',name='固定项目检查',kind='test',command=command,source='任务固定验收规则')]
        else:
            checks=ProjectCheckDiscovery.select(self.refresh(task,state),state.get('changed_files_for_checks',[]),state.get('selected_check_ids',[]))
        controller.store.append_event(task['task_id'],'verification.selected',{'source':'explicit' if command else 'automatic','checks':[check.model_dump() for check in checks]})
        results=[]
        for index,check in enumerate(checks):
            call=ToolCall(id=f'acceptance-{turn}-{index}',name='run_tests',arguments={'command':check.command,'cwd':check.cwd})
            tool_result=await controller._execute(task,state,executor,call)
            output=tool_result.output.lower()
            missing=(tool_result.exit_code in (126,127) or any(value in output for value in
                ('no module named pytest','command not found','executable file not found')) or
                tool_result.exit_code is None and 'no such file or directory' in output or
                'cannot find module' in output and 'node_modules' in output)
            status='passed' if tool_result.exit_code==0 and tool_result.status=='completed' else 'blocked' if missing else 'failed'
            results.append({'check':check.model_dump(),'status':status,'result':tool_result.model_dump()})
            if status!='passed':
                break
        if not results:
            result={'status':'unknown','reason':'没有找到可运行的项目测试或构建配置；尚未完成独立验证。','checks':[]}
        else:
            unsuccessful=next((item for item in results if item['status']!='passed'),None)
            result={'status':unsuccessful['status'] if unsuccessful else 'passed','checks':results,
                    'result':(unsuccessful or results[-1])['result'],
                    'coverage':'syntax_only' if all(item['check']['kind']=='syntax' for item in results) else 'project_checks',
                    'reason':'缺少运行检查所需的依赖或工具，尚未验证通过。' if unsuccessful and unsuccessful['status']=='blocked' else ''}
        result['source']='explicit' if command else 'automatic'
        state['verification']=result
        controller.store.append_event(task['task_id'],'verification.result',result)
        return result

    @staticmethod
    def repair_message(result):
        details=result.get('result',{})
        output=details.get('output','')
        return ('Acceptance verification failed. Repair the implementation using this actual command result, then run the acceptance command again.\n'
                f"Exit code: {details.get('exit_code')}\n{output}")
