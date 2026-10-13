# Gateway：初衷和想法

本轮 IM 入口继续共用原命令目录与可信 owner：帮助可见性不能替代执行授权，群的 bot 提及规范化不能把昵称变成管理员身份。stream 短窗口只投影过程，不裁剪模型候选、工具数据或终态；配置由同一有效 owner 下发，投递端不维护另一份用户状态。实施与剩余范围见 [记录](../../tasks/IM_MONITOR_PERMISSIONS_IMPLEMENTATION.md)。

## 初心

让 my-agent 有一个能长期值班的本地“本体”：前台只是客户端，后台负责接请求、跑调度、保留状态、恢复异常。

## 服务谁

- 给普通用户：以后可以像使用常驻助手一样使用 my-agent，不用每次都重新启动完整链路。
- 给开发者：有稳定的文件协议和命令，可以观察请求生命周期和失败恢复。
- 给外部工具/TUI：可以通过 adapter 或未来 HTTP/WebSocket 接入，不直接耦合内部 Python 对象。

## 为什么这样设计

- 本地文件协议比一开始上 HTTP 服务更容易调试和恢复。
- pending/processing/done/failed 目录让请求状态肉眼可见。
- heartbeat、pid、stop request 让后台生命周期可控。
- gateway_parts 按路径、IO、进程控制、恢复、运行时、adapter 拆分，避免一个巨型 gateway 文件继续膨胀。

## 不做什么

- 第一版不直接做分布式调度或多租户服务。
- 不让 gateway 隐式获得所有私有状态；未来多 gateway 要靠授权和委托边界。
- 不把 `gateway ask/result` 当作最终普通用户必须学习的入口，它们主要是开发者协议命令。
- 不让 TUI/status 成为第二个恢复执行器；前台只看当前会话和只读状态，所有崩溃调和由单 Gateway 完成。
- 不用锁文件是否存在或里面的 PID 文本冒充锁归属；跨线程/进程互斥只认操作系统锁。
