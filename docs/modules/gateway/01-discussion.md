# Gateway：灵感碰撞 / 功能讨论记录

## 为什么想做

gateway 的目标是让 my-agent 不只是一次性 CLI 进程，而是可以后台值班：前台客户端退出后，后台仍能处理请求、调度 subagent、恢复卡住任务，并给外部工具一个稳定入口。

## 讨论过什么

- gateway 先从单机文件协议做起，用 pending/processing/done/responses 目录表达请求生命周期。
- `gateway start/status/stop/restart/logs` 管后台进程，`gateway ask/result` 做开发者可见协议入口。
- chat 可以 attach 到 gateway，普通用户未来不需要直接理解 ask/result。
- gateway 崩溃后，processing 请求要能退回 pending 或归档 failed。
- 多 gateway 组织模型要预留身份、授权、委托和协调字段，但第一版先稳住单机。

## 痛点

- 纯前台 CLI 一退出，长任务和 watch 调度就断。
- 请求、响应、日志、heartbeat 如果没有统一协议，恢复和调试都困难。
- 多 worker 或多客户端同时写文件时，必须有清楚的目录和 lease 边界。
- 普通用户不应该先学习一串 gateway 命令才能聊天。

## 方向

第一版优先把单机后台进程和本地文件队列跑稳：可启动、可停止、可投递请求、可恢复 processing、可索引 LocalStore。更高级的 TUI、HTTP/WebSocket、多 gateway 组织后续再接。

## 相关旧文档

- [GATEWAY_DESIGN.md](../../../GATEWAY_DESIGN.md)
- [GATEWAY_RESEARCH.md](../../../GATEWAY_RESEARCH.md)
- [ARCHITECTURE_GUIDE.md](../../../ARCHITECTURE_GUIDE.md)
- [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)
- [TESTS.md](../../../TESTS.md)

## 当前第一版索引 / 待补齐

本页先收拢 gateway 讨论入口。后续需要把本地队列协议、失败恢复样本、chat attach 体验和多 gateway 组织设计拆成更细的时间线。
