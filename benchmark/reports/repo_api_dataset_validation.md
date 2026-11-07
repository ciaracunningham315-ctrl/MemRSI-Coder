# repo_api 离线题集验收

本报告仅验证题目和判分工程链路；未调用模型，不是 Agent 能力、正式实验或模型效果成绩。

整体离线验收：通过。模型调用：0。

| 阶段 | 通过 | 基线目标失败 | 基线回归 | 参考修复 | 错误补丁 |
|---|---|---|---|---|---|
| seed | True | True | True | True | True |
| transfer | True | True | True | True | True |
| stale | True | True | True | True | True |
| heldout | True | True | True | True | True |

T3 实际适用性工程探针：True。
真实模型历史产生后的证据提取复核尚未运行。符号检查只判断声明名称，不判断参数签名。

| T3 探针 | 预期状态 | 实际状态 | 通过 |
|---|---|---|---|
| T2_to_T3_renamed_symbol | paths=valid, symbols=stale, content=stale, git=valid | paths=valid, symbols=stale, content=stale, git=valid | True |
| T2_to_T3_moved_path | paths=stale, symbols=unknown, content=stale, git=valid | paths=stale, symbols=unknown, content=stale, git=valid | True |
| T2_to_T3_unaffected | paths=valid, symbols=valid, content=valid, git=valid | paths=valid, symbols=valid, content=valid, git=valid | True |
| unrelated_commit_only | paths=valid, symbols=valid, content=valid, git=valid | paths=valid, symbols=valid, content=valid, git=valid | True |
| signature_only_name_unchanged | paths=valid, symbols=valid, content=stale, git=valid | paths=valid, symbols=valid, content=stale, git=valid | True |

JSON 包含所有实际命令、退出码、完整日志路径、代码/私测/参考/错误补丁逐文件 SHA256。文件变化后本报告需重新生成。
所有产物路径以 Benchmark 根目录为基准解析。

独立工作副本与原始命令日志保存在本地 `.runtime/`，不纳入版本控制；本报告保留准入摘要和指纹。
