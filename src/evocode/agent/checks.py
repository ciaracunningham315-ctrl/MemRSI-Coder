"""Discover executable project checks from workspace evidence, without model calls."""
from __future__ import annotations

import hashlib
import json
import sys
from itertools import islice
from pathlib import Path

from evocode.contracts import ProjectCheck


class ProjectCheckDiscovery:
    def discover(self, root, changed_files=(), backend='docker'):
        root=Path(root).resolve()
        scopes={root}
        for name in changed_files:
            path=(root/name).resolve()
            if path.is_relative_to(root):
                scopes.update(parent for parent in path.parents if parent.is_relative_to(root))
        # Cover common front/backend subprojects without scanning dependency trees.
        for child in sorted(root.iterdir()):
            if (child.is_dir() and not child.name.startswith('.') and child.name not in {'node_modules','data','dist','build','vendor'}
                    and any((child/name).is_file() for name in ('package.json','pyproject.toml','go.mod','Cargo.toml'))):
                scopes.add(child.resolve())
        checks=[]
        python=sys.executable if backend=='host' else 'python'

        def add(scope,name,kind,argv,source):
            if not scope.is_relative_to(root):
                return
            cwd=scope.relative_to(root).as_posix()
            identifier='check-'+hashlib.sha256(json.dumps([cwd,argv]).encode()).hexdigest()[:12]
            checks.append(ProjectCheck(id=identifier,name=name,kind=kind,command=argv,cwd=cwd,source=source))

        for scope in sorted(scopes,key=lambda p:p.relative_to(root).as_posix())[:12]:
            if not scope.is_relative_to(root) or not scope.is_dir():
                continue
            package=self._read(scope/'package.json')
            if package:
                try:
                    manifest=json.loads(package)
                    scripts=manifest.get('scripts',{})
                    if not isinstance(scripts,dict):
                        scripts={}
                except (ValueError,AttributeError):
                    scripts={}
                manager='pnpm' if (scope/'pnpm-lock.yaml').exists() else 'yarn' if (scope/'yarn.lock').exists() else 'bun' if any((scope/n).exists() for n in ('bun.lock','bun.lockb')) else 'npm'
                for script in ('test:ci','test:unit','test','typecheck','build'):
                    text=scripts.get(script)
                    if not isinstance(text,str) or not text.strip() or 'no test specified' in text.lower():
                        continue
                    is_test=script.startswith('test')
                    # Avoid interactive/watch-only scripts; use a CI or run mode.
                    if is_test and ('--watch' in text or script=='test' and any(c.kind=='test' and c.cwd==scope.relative_to(root).as_posix() for c in checks)):
                        continue
                    argv=[manager,'run',script]
                    if is_test and 'vitest' in text and '--run' not in text:
                        argv+=['--','--run'] if manager=='npm' else ['--run']
                    if is_test and 'react-scripts test' in text:
                        argv+=['--','--watchAll=false'] if manager=='npm' else ['--watchAll=false']
                    add(scope,'项目测试' if is_test else '类型检查' if script=='typecheck' else '项目构建',
                        'test' if is_test else 'typecheck' if script=='typecheck' else 'build',argv,
                        (scope/'package.json').relative_to(root).as_posix()+':scripts.'+script)
            files=[]
            for folder in ('tests','test','.'):
                location=scope/folder
                if location.is_dir():
                    matches=location.glob('test*.py') if folder=='.' else location.rglob('test*.py')
                    files.extend(islice((path for path in matches if path.is_file() and path.resolve().is_relative_to(root)),50))
            pyproject=self._read(scope/'pyproject.toml')
            pytest_config=next((name for name in ('pytest.ini','tox.ini','setup.cfg') if 'pytest' in self._read(scope/name)),None)
            if 'tool.pytest' in pyproject or pytest_config:
                add(scope,'Python 测试','test',[python,'-m','pytest','-q'],(scope/('pyproject.toml' if 'tool.pytest' in pyproject else pytest_config)).relative_to(root).as_posix())
            elif files:
                unittest=any('unittest' in self._read(path) for path in files[:12])
                if unittest:
                    folder='tests' if (scope/'tests').is_dir() else 'test' if (scope/'test').is_dir() else '.'
                    add(scope,'Python 测试','test',[python,'-m','unittest','discover','-s',folder],files[0].relative_to(root).as_posix())
                else:
                    add(scope,'Python 测试','test',[python,'-m','pytest','-q'],files[0].relative_to(root).as_posix())
            if (scope/'go.mod').is_file():
                add(scope,'Go 测试','test',['go','test','./...'],(scope/'go.mod').relative_to(root).as_posix())
            if (scope/'Cargo.toml').is_file():
                add(scope,'Rust 测试','test',['cargo','test','--offline'],(scope/'Cargo.toml').relative_to(root).as_posix())

        python_files=[name for name in changed_files if str(name).endswith('.py') and (root/name).resolve().is_relative_to(root) and (root/name).is_file()]
        if python_files and not any(check.kind=='test' for check in checks):
            add(root,'Python 语法检查（不代表功能测试）','syntax',
                [python,'-c','import ast,sys; from pathlib import Path; [ast.parse(Path(p).read_text(encoding="utf-8"), filename=p) for p in sys.argv[1:]]; print("Syntax checks passed; no functional tests were run")',*python_files[:30]],'本次修改的 Python 文件')
        return list({check.id:check for check in checks}.values())[:20]

    @staticmethod
    def select(checks,changed_files=(),selected_ids=()):
        by_id={check.id:check for check in checks}
        if selected_ids:
            return [by_id[key] for key in selected_ids if key in by_id][:3]
        relevant=[check for check in checks if check.cwd=='.' or any(str(path).startswith(check.cwd+'/') for path in changed_files)]
        if not relevant:
            relevant=checks
        order={'test':0,'typecheck':1,'build':2,'syntax':3}
        return sorted(relevant,key=lambda check:(order[check.kind],check.cwd))[:3]

    @staticmethod
    def _read(path):
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_size<=256000:
                return path.read_text(encoding='utf-8')
        except (OSError,UnicodeError):
            pass
        return ''
