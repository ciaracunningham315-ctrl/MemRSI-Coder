# repo_cli 离线题集验收

本报告仅验证题目和判分工程链路；未调用模型，不是 Agent 能力、正式实验或模型效果成绩。

整体离线验收：通过。模型调用：0。

| 阶段 | 通过 | 基线目标失败 | 基线回归 | 参考修复 | 错误补丁 |
|---|---|---|---|---|---|
| seed | True | True | True | True | True |
| transfer | True | True | True | True | True |
| stale | True | True | True | True | True |
| heldout | True | True | True | True | True |

JSON 包含所有实际命令、退出码、完整日志路径、代码/私测/参考/错误补丁逐文件 SHA256。文件变化后本报告需重新生成。
所有产物路径以 Benchmark 根目录为基准解析。

独立工作副本与原始命令日志保存在本地 `.runtime/`，不纳入版本控制；本报告保留准入摘要和指纹。
