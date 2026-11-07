"""Qualify independent Gate/replay fixtures offline; never invoke models."""
import json
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.gate_tasks import validate_gate_tasks

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository-id', choices=('repo_api', 'repo_cli', 'repo_data'))
    args = parser.parse_args()
    result = validate_gate_tasks(ROOT, repository_id=args.repository_id)
    label = args.repository_id or 'all'
    target = ROOT / f'reports/{label}_gate_validation.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('Independent Gate/replay qualification:', 'PASS' if result['passed'] else 'FAIL')
    print(target)
    raise SystemExit(0 if result['passed'] else 1)
