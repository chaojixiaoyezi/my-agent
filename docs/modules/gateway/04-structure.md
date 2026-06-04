# Gateway Structure

Gateway 负责把外部请求落成可审计队列，并由 worker 调用 SimpleAgent。它不负责模型业务决策。

## 核心文件

- `agent/gateway_parts/request_execution.py`：执行单个 gateway request。
- `agent/gateway_parts/request_worker.py`：worker loop、认领、完成、失败写回。
- `agent/gateway_parts/queue_service.py`：request/response/history/index 文件队列。
- `agent/gateway_parts/lease_service.py`：processing lease 和 heartbeat。
- `agent/gateway_parts/http_handlers.py`：HTTP 入口。
- `agent/gateway_parts/response_renderer.py`：响应渲染。
- `cli/_gateway_*`、`cli/gateway_*`：启动、停止、状态、客户端命令。

## 路径

gateway 运行态写当前 owner workspace runtime：

```text
owner_home/workspace/runtime/workspaces/<workspace-scope>/gateway/
|-- requests/
|-- responses/
|-- history/
|-- workers/
|-- leases/
`-- index/
```

## 规则

- request/response/history 损坏要显式报告 load_error，不能渲染成“没有记录”。
- worker 秒退、参数错、import 错要立即标记失败状态，不能伪装成 processing/planning。
- 多 chat/gateway client 共享同一队列时，本地 IO 不应成为瓶颈；慢点应主要来自模型或外部服务。
