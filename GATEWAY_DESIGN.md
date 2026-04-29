# Gateway Design Notes

这份文档记录 gateway 常驻方向的外部参考和本项目目标。它不是实现完成清单，而是避免我们把“前台 daemon”“TUI 会话”“后台 runtime”“任务状态”混在一起。

## 我们要的形态

目标是：

```text
my-agent gateway restart
my-agent chat
```

`gateway` 是本地常驻 runtime，`chat` / TUI 只是客户端。用户可以退出聊天界面、重启 gateway、刷新会话；任务树、runner 输出、验收、能力授权、失败原因和调度日志都要落盘，gateway 重启后从任务账本恢复，而不是依赖某个长聊天上下文活着。

## 外部方案参考

### 通道运行时

通道运行时 把 gateway 定义成 24/7 后台服务，负责连接 Telegram / WhatsApp 等通道、路由消息、处理定时任务和工具执行。它提供 `通道运行时 gateway start/stop/restart/status/logs`，也支持后台 daemon 和系统服务。

它的优点是用户入口清楚：gateway 是本体，TUI / 外部渠道是客户端；重启、日志、状态也有明确命令。风险点是：如果 heartbeat 或定时事件只是健康检查，没有真的进入 LLM 决策链，就会出现“只报 OK，不推进任务”的问题，所以需要任务状态机和 planner gate。

参考：
- （外部资料链接已移出发布文档）
- （外部资料链接已移出发布文档）

### 长期助手

长期助手 也采用 messaging gateway。公开文档里 gateway 是长运行进程，用 PID 文件跟踪，支持 `长期助手 gateway start/stop`，也可以交给 systemd / launchd 管理。它的 gateway 还负责后台维护：cron tick、session expiry、memory flush、模型列表和 provider 状态刷新。

这个方向对我们很有借鉴意义：gateway 不只负责“活着”，还要负责后台维护、记忆落盘和不同客户端消息接入。但 长期助手 的设计更偏多平台消息网关；我们这里第一阶段更偏本地任务 runtime 和任务树恢复。

参考：
- （外部资料链接已移出发布文档）
- （外部资料链接已移出发布文档）

### 模型助手 Code / 模型助手 Agent SDK

模型助手 的 session 文档强调：session 会把会话历史、工具调用、工具结果和模型响应写到磁盘，之后可以 continue / resume / fork。它适合恢复对话上下文，但官方也明确区分：session 持久化的是 conversation，不是文件系统状态。

这对我们是一个提醒：只靠恢复 LLM 会话不够。我们的长任务状态必须是应用层任务账本，conversation transcript 只能作为辅助材料。

参考：
- （外部资料链接已移出发布文档）

### OpenAI 会话运行时

会话运行时 云端形态把每个任务放进独立云 sandbox，可以并行执行多个任务，完成后给出 terminal log、测试输出等可验证证据。它更像“云端任务队列 + 隔离 worker + 审核面板”，不是本地 gateway。

对我们的启发是：任务并行和可验证证据应该是一等公民；每个 runner 要有自己的环境边界、日志、测试证据和最终交付，而不是只把结果塞进聊天上下文。

参考：
- （外部资料链接已移出发布文档）

## 本项目决策

- `daemon` 是当前过渡入口：前台常驻调度，不是最终 gateway。
- 未来 `gateway` 才是本地常驻 runtime；`chat` / TUI 应该能 attach / detach。
- 任务状态以落盘账本为准：子代理、孙代理、runner 输出、验收、能力授权和阻塞原因都必须可恢复。
- 用户层不暴露底层 tick 参数；普通用户只说目标和可选规模限制。
- 高级用户可以在配置里调策略：任务规模默认 0 表示不设硬上限，runner 并发/启动速率/超时默认 `auto`。
- 真实 worker pool 做好前，当前 `daemon_max_runners: "auto"` 先映射成保守值 1，避免前台进程一次性同步阻塞太多 runner。
