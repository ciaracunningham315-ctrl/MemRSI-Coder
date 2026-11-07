# 状态服务契约

入口 `api.status.StatusAPI.dispatch(method, path)`。
GET /status 返回 `(200, metadata)`。响应必须为独立深拷贝；调用方对任何层级的响应修改不得改变服务 metadata、另一个响应或另一个服务实例。默认 metadata 为 `{"service":"dispatch-tools","labels":["stable"],"limits":{"workers":2}}`，实际响应以实例当前 metadata 为准。
其他路径返回 `(404,{"error":{"code":"not_found"}})`；已知路径其他方法返回 `(405,{"error":{"code":"method_not_allowed"}})`；错误路由不改变 metadata。
