# repo_data 四阶段主任务离线准入

本报告使用统一 dataset validator；模型调用数为 0。结果仅证明题集与判分夹具可运行，不是 Agent 能力或正式实验成绩。

从 `benchmark/` 目录运行：

```bash
uv run --project .. --extra dev python scripts/validate_dataset.py --repository-id repo_data
```

| 阶段 | 准入 | 公开基线 | 目标隐藏验收失败 | 固定回归 | 参考修复 | 三类坏补丁 |
|---|---|---|---|---|---|---|
| seed | True | True | True | True | True | True / True |
| transfer | True | True | True | True | True | True / True |
| stale | True | True | True | True | True | True / True |
| heldout | True | True | True | True | True | True / True |

JSON 保存逐题命令、指纹和失败/错误数；本地原始命令日志不纳入版本控制。T3 适用性只提供固定代码/参考修改的结构对照，没有真实模型轨迹或在线 MemoryService 适用性调用。

工作副本在日志归档后清理。
