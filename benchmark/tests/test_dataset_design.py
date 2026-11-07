"""Guard the reviewed task boundaries against answer hints and single-file regressions."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('t1_seed', 't2_transfer', 't3_stale', 't4_heldout')


def test_prompts_do_not_disclose_internal_repairs_and_dispatch_imports_are_used():
    for stage in STAGES:
        base = ROOT / 'scenarios/repo_api' / stage
        prompt = (base / 'task.md').read_text()
        for hint in ('require_count', 'read_count', 'InputFault', 'api/errors.py', 'api/faults.py', 'bool'):
            assert hint not in prompt, (stage, hint)
        tree = ast.parse((base / 'code/api/app.py').read_text())
        imports = {alias.asname or alias.name for node in tree.body
                   if isinstance(node, ast.ImportFrom) for alias in node.names}
        loads = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        assert imports <= loads, (stage, imports - loads)


def test_cumulative_routes_remain_and_reference_repairs_span_multiple_modules():
    required = []
    for stage, route in zip(STAGES, ('/reservations', '/returns', '/holds', '/batch-reservations')):
        required.append(route)
        base = ROOT / 'scenarios/repo_api' / stage
        manifest = json.loads((base / 'manifest.json').read_text())
        app = (base / 'code/api/app.py').read_text()
        assert all(route in app for route in required), stage
        before = {p.relative_to(base / 'code').as_posix(): p.read_bytes()
                  for p in (base / 'code/api').rglob('*.py')}
        reference = ROOT / manifest['reference_dir']
        after = {p.relative_to(reference).as_posix(): p.read_bytes() for p in (reference / 'api').rglob('*.py')}
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        assert len(changed) >= 2, (stage, changed)
        if stage in ('t1_seed', 't2_transfer'):
            assert {'api/validation.py', 'api/errors.py'} <= changed
