# repo_cli 独立 Gate 与回归重放准入

本报告仅验证离线夹具可执行性、故障隔离与参考修复；模型调用为 0，不代表模型效果或正式结果。

总体准入：通过。

| 任务 ID | 阶段 | 类型 | 公开检查 | 目标验收失败 | 基线回归 | 参考修复 |
|---|---|---|---:|---:|---:|---:|
| repo_cli_gate_seed_bundle | seed | task | True | True | True | True |
| repo_cli_gate_transfer_cache | transfer | task | True | True | True | True |
| repo_cli_gate_stale_release | stale | task | True | True | True | True |
| repo_cli_gate_meta_transfer_manifest | transfer | meta | True | True | True | True |
| repo_cli_replay_profile_summary | seed | regression | True | True | True | True |

独立工作副本与原始命令日志保存在本地 `.runtime/`，不纳入版本控制；本报告保留准入摘要和指纹。
