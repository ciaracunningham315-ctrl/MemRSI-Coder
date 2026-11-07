# 批量预约的一致性

批量预约中出现空批次被接受、错误优先级不一致以及失败后库存变化的问题。修复 POST /batch-reservations，使其遵循整个请求的业务契约，并保留既有路由的行为。

从仓库的 README、公开接口规范及现有成功路由确认完整的输入输出要求。
当前说明与仓库内容足以独立求解，无须历史任务补丁。入口为
`from api.app import InventoryAPI`，调用 `dispatch(method, path, payload=None)`。
所有响应、状态副作用、字段路径及兼容接口以本版本公开契约为准。

请修复业务实现，可按需要重构内部结构。运行
`python -m unittest discover -s tests` 检查基本可用性，并自行覆盖业务边界。
Python 3.10+，仅标准库；各题使用独立固定代码起点。
