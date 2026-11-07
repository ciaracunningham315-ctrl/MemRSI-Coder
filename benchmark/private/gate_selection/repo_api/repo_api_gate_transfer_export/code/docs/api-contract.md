# 独立服务接口契约

这是打印/导出/通知工具服务的一条独立维护任务，不连接主测业务数据。
入口 `api.service.ServiceAPI.dispatch(method, path, body=None)`。

- `GET /health` 固定返回 `(200, {"status":"ok"})`，不改变任务记录。
- `POST /exports` 的 body 必须是字典；`days` 必填，必须为 Python 整数（布尔值、浮点数、字符串均不接受），区间为闭区间 `[1,90]`。

- 可选 `compressed` 缺省 `False`；必须是布尔值，不能用数字或字符串代替。
- 成功状态为 201，返回字典键为 `id`、`days`、`compressed`，且 records 中追加相同记录。id 从 1 连续递增。
- 验证失败状态为 400，返回 `{"error":{"code":"invalid_request","field":字段路径}}`。非字典 body 对应 `body`；缺失或无效数量对应 `days`；可选参数无效对应 `compressed`。
- 按 body 结构、数量、可选参数顺序验证，失败时 records、next_id 必须保持原值；不得吞成成功，也不得返回 500。
- 已知路径其他方法返回 405 和 `{"error":{"code":"method_not_allowed"}}`；未知路径返回 404 和 `{"error":{"code":"not_found"}}`，均无副作用。
