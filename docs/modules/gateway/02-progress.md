# Gateway Progress

## 2026-06-06 入口收敛

- 删除 `agent/gateway_parts/runtime.py` 聚合层。
- adapter 直接调用 `request_worker`，processing 恢复直接调用 `lease_service`。
- `agent/gateway_parts/__init__.py` 仍是包级公开入口；内部实现不再通过 `runtime.py` 多跳转发。

## 2026-06-06 会话上下文精确化

- gateway conversation follow-up 只把 `status=active` 的 task link 当作活跃任务。
- `completed`、`done`、`closed` 等旧/展示状态不会再通过“非关闭即活跃”的逻辑污染后续请求。

## 2026-06-04 收敛

- gateway 文档改为只描述当前 `gateway_parts/` 队列、worker、lease、HTTP 和 renderer。
- request/response/history/status/PID 读取错误必须结构化显示，不能吞成空状态。
- 后台启动失败需要早期健康确认并写明确失败状态。
- chat/gateway 多客户端共享队列时，应减少本地膨胀和重复读写，避免本地成为模型之外的瓶颈。
