"""Execute reference repair qualification without any model calls."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'src'))
from runner.repair_qualification import qualify_repairs

if __name__ == '__main__':
    report = qualify_repairs(ROOT)
    target = ROOT / 'reports/repo_api_repair_qualification.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['# repo_api 修复结构与真实工具证据验收', '',
             '本报告为参考修复的离线工程回放；模型调用为 0，未测量基线能力、收益或天花板效应。', '',
             '| 阶段 | 参考修改的业务文件 | 逐文件撤回后失败数 | 自然证据检查 |', '|---|---|---|---|']
    for row in report['stages']:
        names = ', '.join(row['changed_business_files'])
        rejected = sum(item['rejected'] for item in row['ablations'])
        lines.append(f"| {row['stage']} | {names} | {rejected}/{len(row['ablations'])} | {row['natural_required_evidence']} |")
    lines += ['', 'T1/T2 的证据来自实际 ToolExecutor.edit_file 生成的最小差异及结果元数据，未生成声明级假 diff。',
              '消融说明这些参考修复需要多文件协同，不证明所有可能解法的文件数下界。真实模型轨迹仍需后续复核。', '',
              f"整体检查：{'通过' if report['passed'] else '失败'}。详细命令、差异、指纹和日志见对应 JSON。", '']
    target.with_suffix('.md').write_text('\n'.join(lines))
    print('Reference repair qualification:', 'PASS' if report['passed'] else 'FAIL')
    print(target)
    raise SystemExit(0 if report['passed'] else 1)
