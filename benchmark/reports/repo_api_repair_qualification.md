# repo_api 修复结构与真实工具证据验收

本报告为参考修复的离线工程回放；模型调用为 0，未测量基线能力、收益或天花板效应。

| 阶段 | 参考修改的业务文件 | 逐文件撤回后失败数 | 自然证据检查 |
|---|---|---|---|
| t1_seed | api/errors.py, api/reservations.py, api/validation.py | 3/3 | True |
| t2_transfer | api/errors.py, api/returns.py, api/validation.py | 3/3 | True |
| t3_stale | api/holds.py, api/validation.py | 2/2 | True |
| t4_heldout | api/batch.py, api/stock.py, api/validation.py | 3/3 | True |

T1/T2 的证据来自实际 ToolExecutor.edit_file 生成的最小差异及结果元数据，未生成声明级假 diff。
消融说明这些参考修复需要多文件协同，不证明所有可能解法的文件数下界。真实模型轨迹仍需后续复核。

整体检查：通过。详细命令、差异、指纹和日志见对应 JSON。
