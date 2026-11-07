# 退货验收与可用库存

退货支持登记损坏件，但当前部分验收数据被拒绝，部分错误指向不清晰，入库数量也可能不正确。修复 POST /returns 的验收与入库行为，保留预约服务的兼容性。

从仓库的 README、公开接口规范及现有成功路由确认完整的输入输出要求。
当前说明与仓库内容足以独立求解，无须历史任务补丁。入口为
`from api.app import InventoryAPI`，调用 `dispatch(method, path, payload=None)`。
所有响应、状态副作用、字段路径及兼容接口以本版本公开契约为准。

请修复业务实现，可按需要重构内部结构。运行
`python -m unittest discover -s tests` 检查基本可用性，并自行覆盖业务边界。
Python 3.10+，仅标准库；各题使用独立固定代码起点。
