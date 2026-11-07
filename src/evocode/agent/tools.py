from __future__ import annotations
import asyncio
import difflib
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from evocode.contracts import ToolResult
from .delivery import delivery_targets

DEPENDENCY_MANIFESTS = (
    'pyproject.toml', 'requirements.txt', 'requirements-dev.txt', 'poetry.lock', 'uv.lock',
    'Pipfile.lock', 'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
    'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum',
)


class UncertainToolExecution(RuntimeError):
    """Raised when an interrupted command may have left partial side effects."""

    def __init__(self, message, metadata=None):
        super().__init__(message)
        self.metadata = metadata or {}


def schema(name, description, properties, required):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}


TOOLS=[
 schema('read_file','Read a UTF-8 file inside the repository. Defaults to start_line=1 and max_lines=200; request later ranges to inspect the rest of a large file.',{'path':{'type':'string'},'start_line':{'type':'integer','minimum':1},'max_lines':{'type':'integer','minimum':1}},['path']),
 schema('edit_file','Replace exact unique old_text in a file, or create a new file with old_text empty.',{'path':{'type':'string'},'old_text':{'type':'string'},'new_text':{'type':'string'}},['path','old_text','new_text']),
 schema('run_command','Run an argv command in the isolated repository.',{'command':{'type':'array','items':{'type':'string'}},'cwd':{'type':'string','description':'Optional workspace-relative working directory'}},['command']),
 schema('run_tests','Run an argv test command.',{'command':{'type':'array','items':{'type':'string'}},'cwd':{'type':'string','description':'Optional workspace-relative working directory'}},['command']),
 schema('memory_search','Search historical execution evidence.',{'query':{'type':'string'}},['query']),
 schema('replan','Request a revised plan with concrete observed deviation.',{'reason':{'type':'string'}},['reason'])
]


class ToolExecutor:
    def __init__(self, store, config, memory=None):
        self.store,self.config,self.memory=store,config,memory
        self.processes={}; self.containers={}

    def path(self, root, value):
        root=Path(root).resolve()
        candidate=Path(value).expanduser()
        # Docker exposes this task's selected workspace at /workspace. Translate
        # only that declared execution root; never reinterpret arbitrary host paths.
        if (candidate.is_absolute() and self.config.runtime.execution_backend=='docker'
                and candidate.is_relative_to(Path('/workspace'))):
            candidate=root/candidate.relative_to(Path('/workspace'))
        path=(candidate if candidate.is_absolute() else root/candidate).resolve()
        if not path.is_relative_to(root) or '.git' in path.relative_to(root).parts:
            raise ValueError('Path outside permitted workspace')
        return path

    def _git_snapshot(self, root):
        """Return observable Git identity and changed paths without inventing provenance."""
        root = str(Path(root).resolve())
        try:
            commit = subprocess.run(
                ['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True,
                text=True, timeout=2, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = None
        try:
            prefix = subprocess.run(
                ['git', '-C', root, 'rev-parse', '--show-prefix'], capture_output=True,
                text=True, timeout=2, check=True,
            ).stdout.strip()
            raw = subprocess.run(
                ['git', '-C', root, 'status', '--porcelain=v1', '-z', '--untracked-files=all','--','.'],
                capture_output=True, timeout=2, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return {'git_commit': commit or None, 'file_paths': []}
        paths = set()
        entries = raw.split(b'\0')
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            # Porcelain v1 -z puts the two status columns and a space before PATH.
            path_bytes = entry[3:]
            if path_bytes:
                decoded=os.fsdecode(path_bytes)
                if not prefix or decoded.startswith(prefix):
                    paths.add(decoded[len(prefix):])
            # Rename/copy entries carry the original path as the next NUL item.
            if entry[:2] in (b'R ', b' R', b'C ', b' C', b'RM', b' R') and index < len(entries):
                original = entries[index]
                index += 1
                if original:
                    decoded=os.fsdecode(original)
                    if not prefix or decoded.startswith(prefix):
                        paths.add(decoded[len(prefix):])
        return {'git_commit': commit or None, 'file_paths': sorted(paths)}

    @staticmethod
    def _relative_path(root, path):
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()

    def _command_paths(self, before, after):
        # Report only paths whose working-tree status appeared or changed during
        # this command. Existing dirty paths are kept when they remain dirty.
        return sorted(set(after['file_paths']) - set(before['file_paths']))

    @staticmethod
    def _dependency_fingerprints(root):
        root = Path(root).resolve()
        evidence = []
        for name in DEPENDENCY_MANIFESTS:
            try:
                path = (root / name).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            except (OSError, RuntimeError):
                continue
            evidence.append({'path': name, 'sha256': digest, 'source': 'workspace_at_tool_execution'})
        return evidence

    @staticmethod
    def _delivery_snapshot(task):
        """Observe explicit output targets even in a workspace without Git."""
        root = Path(task['repo_path']).resolve()
        snapshot = {}
        for target, path in delivery_targets(task, task.get('state', {})):
            if not path.is_relative_to(root) or target.operation not in ('create', 'modify'):
                continue
            try:
                if target.kind == 'directory' and path.is_dir():
                    stat = path.stat()
                    fingerprint = ('directory', stat.st_dev, stat.st_ino)
                elif target.kind == 'file' and path.is_file():
                    with path.open('rb') as stream:
                        fingerprint = ('file', hashlib.file_digest(stream, 'sha256').hexdigest())
                else:
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                fingerprint = None
            snapshot[path.relative_to(root).as_posix()] = fingerprint
        return snapshot

    def prepare(self, task, call):
        info={}
        if call.name=='edit_file':
            path=self.path(task['repo_path'],call.arguments['path'])
            text=path.read_text() if path.exists() else ''
            old=call.arguments['old_text']; new=call.arguments['new_text']
            if old:
                if text.count(old)!=1: raise ValueError('old_text must match exactly once')
                updated=text.replace(old,new,1)
            else:
                if path.exists(): raise ValueError('Empty old_text only creates a new file')
                updated=new
            info={'path':str(path),'before_hash':hashlib.sha256(text.encode()).hexdigest(),'after_hash':hashlib.sha256(updated.encode()).hexdigest(),'existed':path.exists()}
        return info

    async def cancel(self, task_id):
        proc=self.processes.get(task_id)
        if proc and proc.returncode is None:
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            await proc.wait()
        container=self.containers.get(task_id)
        if container:
            try:
                cleaner=await asyncio.create_subprocess_exec('docker','rm','-f',container,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(cleaner.wait(),10)
            except (OSError,asyncio.TimeoutError): pass

    async def command(self, task, argv, cwd='.'):
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and '\x00' not in x for x in argv):
            raise ValueError('command must be a nonempty argv string list')
        cache_prefix=str(Path(tempfile.gettempdir())/('evocode-pycache-'+uuid.uuid4().hex))
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','PYTHONUNBUFFERED':'1',
             'PYTHONDONTWRITEBYTECODE':'1','PYTHONPYCACHEPREFIX':cache_prefix}
        command=argv
        directory=self.path(task['repo_path'],cwd)
        if not directory.is_dir():raise ValueError('Command working directory does not exist')
        if self.config.runtime.execution_backend=='docker':
            if self._is_dependency_install(argv):
                image = self.config.runtime.docker_image
                return 2, (
                    'Package installation is unavailable in isolated Docker tools: '
                    'containers use --network none and are removed after each command. '
                    'Prepare dependencies in a reusable image first with '
                    '`python scripts/prepare_docker_environment.py --tag '
                    f'{image} --requirements <requirements.txt>`, then set '
                    f'config.runtime.docker_image to `{image}` and rerun the command.'
                )
            name='evocode-'+uuid.uuid4().hex
            self.containers[task['task_id']]=name
            command=['docker','run','--rm','--name',name,'--network','none','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128','--memory','2g','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={Path(task["repo_path"]).resolve()},dst=/workspace','--workdir','/workspace',self.config.runtime.docker_image]+argv
            command[command.index('--workdir')+1]='/workspace/'+directory.relative_to(Path(task['repo_path']).resolve()).as_posix()
            command[2:2]=['-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPYCACHEPREFIX=/tmp/evocode-pycache']
        elif self.config.runtime.execution_backend!='host': raise ValueError('Unknown execution backend')
        chunks=[]; size=0; cap=4*1024*1024
        proc=await asyncio.create_subprocess_exec(*command,cwd=directory,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
        self.processes[task['task_id']]=proc
        async def drain():
            nonlocal size
            while True:
                part=await proc.stdout.read(65536)
                if not part:break
                if size<cap:chunks.append(part[:cap-size])
                size+=len(part)
            await proc.wait()
        try:
            await asyncio.wait_for(drain(),self.config.runtime.tool_timeout)
            output=b''.join(chunks).decode(errors='replace')
            if size>cap:output+='\n[output exceeded 4 MiB capture limit]'
            return proc.returncode,output
        except asyncio.TimeoutError as exc:
            await self.cancel(task['task_id'])
            raise UncertainToolExecution('Command timed out after it may have started; inspect side effects before resuming') from exc
        except asyncio.CancelledError:
            await self.cancel(task['task_id'])
            raise
        finally:
            self.processes.pop(task['task_id'],None)
            self.containers.pop(task['task_id'],None)

    @staticmethod
    def _is_dependency_install(argv):
        """Recognize common package-manager install forms before an offline run."""
        command = ' '.join(argv)
        return bool(re.search(
            r'(?<![\w.-])(?:python(?:\d+(?:\.\d+)*)?\s+-m\s+pip\s+install|'
            r'pip(?:\d+(?:\.\d+)*)?\s+install|uv\s+(?:pip\s+install|add|sync))(?=\s|$)',
            command,
            re.IGNORECASE,
        ))

    async def execute(self, task, call, prepared=None):
        name,args=call.name,call.arguments
        root=task['repo_path']
        metadata=self._git_snapshot(root)
        try:
            if name=='read_file':
                start_line=args.get('start_line',1)
                max_lines=args.get('max_lines',200)
                if isinstance(start_line,bool) or not isinstance(start_line,int) or start_line<1:
                    raise ValueError('start_line must be a positive integer')
                if isinstance(max_lines,bool) or not isinstance(max_lines,int) or max_lines<1:
                    raise ValueError('max_lines must be a positive integer')
                path=self.path(root,args['path'])
                raw=path.read_bytes()
                full_text=raw.decode('utf-8').replace('\r\n','\n').replace('\r','\n')
                lines=full_text.splitlines(keepends=True)
                total_lines=len(lines)
                if start_line>total_lines:
                    output=f'Requested start_line {start_line} exceeds file length ({total_lines} lines).'
                    line_start=line_end=0
                    line_truncated=True
                else:
                    line_start=start_line
                    line_end=min(total_lines,start_line+max_lines-1)
                    output=''.join(lines[line_start-1:line_end])
                    line_truncated=(line_start>1 or line_end<total_lines)
                code=0
                metadata['file_paths']=[self._relative_path(root,path)]
                metadata.update({
                    'total_lines':total_lines,'start_line':start_line,'max_lines':max_lines,
                    'line_start':line_start,'line_end':line_end,'truncated':line_truncated,
                    '_file_snapshot':full_text,
                    'content_evidence':[{'path':metadata['file_paths'][0],
                        'sha256':hashlib.sha256(raw).hexdigest(),'source':'read_file_snapshot'}],
                })
            elif name=='edit_file':
                info=prepared or self.prepare(task,call); path=Path(info['path'])
                text=path.read_text() if path.exists() else ''
                if hashlib.sha256(text.encode()).hexdigest()!=info['before_hash']:raise ValueError('File changed since edit preparation')
                updated=text.replace(args['old_text'],args['new_text'],1) if args['old_text'] else args['new_text']
                relative=self._relative_path(root,path)
                patch=''.join(difflib.unified_diff(
                    text.splitlines(keepends=True),updated.splitlines(keepends=True),
                    fromfile='a/'+relative,tofile='b/'+relative,
                ))
                path.parent.mkdir(parents=True,exist_ok=True)
                temp=path.with_name(path.name+'.evocode-'+uuid.uuid4().hex)
                updated_bytes=updated.encode('utf-8')
                temp.write_bytes(updated_bytes);temp.replace(path)
                patch_artifact=self.store.save_artifact(patch,'code_patch')
                metadata.update({
                    'file_paths':[relative],'patch_artifact_id':patch_artifact['artifact_id'],
                    'content_evidence':[{'path':relative,'sha256':hashlib.sha256(updated_bytes).hexdigest(),
                                         'source':'edit_file_after'}],
                    'dependency_evidence':self._dependency_fingerprints(root),
                })
                output='Edited '+relative;code=0
            elif name in ('run_command','run_tests'):
                before=metadata
                delivery_before = await asyncio.to_thread(self._delivery_snapshot, task)
                try:
                    code,output=await self.command(task,args['command'],args.get('cwd','.'))
                except UncertainToolExecution as exc:
                    after=self._git_snapshot(root)
                    exc.metadata={**after,'file_paths':self._command_paths(before,after)}
                    raise
                except asyncio.CancelledError:
                    after=self._git_snapshot(root)
                    self.store.append_event(task['task_id'],'tool.interrupted',{
                        'tool_call_id':call.id,'tool':name,'reason':'Command was cancelled after it may have started',
                        **after,'file_paths':self._command_paths(before,after),
                    })
                    raise
                after=self._git_snapshot(root)
                changed = set(self._command_paths(before, after))
                delivery_after = await asyncio.to_thread(self._delivery_snapshot, task)
                changed.update(path for path, fingerprint in delivery_after.items()
                               if fingerprint is not None and delivery_before.get(path, ()) not in (None, fingerprint))
                metadata={**after,'file_paths':sorted(changed)}
            elif name=='memory_search':
                if not self.config.raw_rag.enabled: return ToolResult(tool_call_id=call.id,status='disabled',output='memory_disabled')
                if self.memory is None: return ToolResult(tool_call_id=call.id,status='unavailable',output='memory service unavailable')
                result=await self.memory.search(args['query'],task)
                metadata['memory_retrieval'] = {
                    **result.model_dump(exclude={'evidence'}),
                    'evidence_event_ids': [item.event_id for item in result.evidence],
                }
                output=result.model_dump_json();code=0
            else:raise ValueError('Unknown tool: '+name)
            file_snapshot=metadata.pop('_file_snapshot',None)
            if name=='read_file':
                artifact=self.store.save_artifact(file_snapshot,'file_snapshot')
                metadata['truncated']=metadata['truncated'] or len(output)>self.config.runtime.max_output_chars
            else:
                artifact=self.store.save_artifact(output,'tool_output')
                metadata['truncated']=len(output)>self.config.runtime.max_output_chars
            return ToolResult(tool_call_id=call.id,status='completed' if code==0 else 'failed',output=output[:self.config.runtime.max_output_chars],exit_code=code,metadata={**metadata,'artifact_id':artifact['artifact_id']})
        except asyncio.TimeoutError as exc:
            return ToolResult(tool_call_id=call.id,status='failed',output='Tool execution timed out',metadata={'timeout':True})
        except UncertainToolExecution:
            raise
        except (ValueError,KeyError,OSError,TypeError) as exc:
            return ToolResult(tool_call_id=call.id,status='failed',output=str(exc))
