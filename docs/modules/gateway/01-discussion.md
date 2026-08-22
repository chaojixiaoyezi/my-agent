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

## 2026-08-22 为什么前台不能询问“恢复全机任务”

前台 TUI 只是 Gateway 的一个会话客户端。普通启动等于新建会话，恢复旧会话必须点名 session；历史
subagent 的调和需要 runner session、心跳、attempt 和父会话状态，只有 Gateway 掌握这条结构化链。
因此启动页不再扫全局 board，也不再让普通用户面对一个实际上不会派工的 `[Y/n]` 选择。

同轮真机还发现旧 JSON 文件锁可能在进程崩溃时留下 0 字节文件，并把恢复线程永久挡住。最终选择与
会话运行时 同类的 OS advisory lock：文件正文只给人排障，内核锁才决定是否有人在岗。
