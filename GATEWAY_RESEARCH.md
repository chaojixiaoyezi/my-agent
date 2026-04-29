# Gateway Research

这份调研面向下一步 `my-agent gateway`。重点不是“找一个现成框架直接套”，而是拆出长期后台运行、可重启恢复、可 attach/detach、可并发执行、多层代理任务树这几类需求背后的成熟模式。

## 我们的问题定义

目标体验：

```text
my-agent gateway restart
my-agent chat
```

用户把任务交给主代理后，可以退出 TUI、重启 gateway、刷新聊天会话。父代理、子代理、孙代理继续靠落盘任务账本恢复现场，而不是靠某个聊天上下文或 Python 线程活着。

## 方案族谱

### 1. 进程守护型 daemon

代表：Ollama、PM2、Supervisor、systemd/launchd、Windows Task Scheduler。

这类系统解决的是“进程要一直活着”。例如 Ollama 可以手动 `ollama serve`，也推荐用 systemd service，配置 `Restart=always`、`RestartSec=3`，并通过 `journalctl` 看日志。PM2 可以 `pm2 save` / `pm2 resurrect` 保存进程列表，Supervisor 有明确的进程状态机和日志。

优点：
- 非常适合做 `gateway start/stop/restart/status/logs`。
- 用户理解成本低：服务活着，客户端就能连。
- 跨平台可分层实现：Windows 先用普通后台进程/Task Scheduler，Linux 用 systemd，macOS 用 launchd。

不足：
- 它只保证进程恢复，不保证“任务恢复”。
- 如果 runner 在进程内同步跑，进程卡住时 gateway 也会卡住。
- Windows 的后台任务常有非交互会话、路径、权限和环境变量问题。

对我们的启发：
- 进程守护是 gateway 外壳，不是任务系统本体。
- `gateway restart` 后必须从任务账本恢复，而不是指望 Python 内存还在。
- 第一版可以先做 PID、lock、heartbeat、logs；后续再接系统服务安装。

参考：
- Ollama Linux service: https://docs.ollama.com/linux
- PM2 process management: https://pm2.io/docs/runtime/guide/process-management/
- Supervisor subprocess states: https://www.supervisord.org/subprocess.html
- Windows `schtasks`: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create

### 2. Messaging gateway / agent gateway

代表：OpenClaw、Hermes。

OpenClaw 把 gateway 定义为 24/7 后台服务，负责通道连接、消息路由、定时任务和工具执行，并提供 start/stop/restart/status/logs。Hermes 的 gateway 是长运行进程，用 PID 文件做 profile 级跟踪，还处理 cron、session expiry、memory flush、模型/provider 状态刷新。

优点：
- 和我们的最终用户体验最接近：TUI/外部渠道只是客户端，gateway 是本体。
- 适合把聊天、定时任务、后台维护、工具执行放到一个控制面。
- Hermes 的“每条消息创建 agent + session id + memory flush”给多用户/多会话隔离提供了参考。

不足：
- 如果 heartbeat 是 runtime 短路逻辑，而不触发完整 LLM turn，就会出现“只报 OK，不推进任务”。
- 如果只有 session transcript，没有任务账本，刷新会话后仍可能失去项目级上下文。
- gateway 容易变成大杂烩：通道、任务、工具、记忆、调度全部混在一起。

对我们的启发：
- gateway 必须有两层：控制面负责接消息和调度，执行面负责 runner。
- heartbeat 必须读任务账本和 gate，不允许直接固定 OK。
- 每次 planner/runner/acceptance 都要写审计记录。
- 本项目第一版先用本地文件 inbox/response 模拟 messaging gateway：`gateway ask` 写 pending request，后台 worker 触发完整 LLM turn，再把响应写回 response 文件。后续可以把这个通道替换成 SQLite / HTTP / WebSocket。

参考：
- OpenClaw gateway: https://www.getopenclaw.ai/docs/gateway
- OpenClaw runbook: https://docs.openclaw.ai/gateway
- Hermes gateway internals: https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals
- Hermes messaging gateway: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

### 3. Detached UI + persistent kernel/session

代表：JupyterLab/Jupyter Server、Claude Code session。

Jupyter 的浏览器页面可以关闭，底层 kernel/terminal 仍在 server 上继续跑；Jupyter Server 通过 Kernel Manager 管理 kernel 生命周期。Claude Code / Agent SDK 持久化 conversation history，可以 resume/continue/fork，但官方也强调 session 持久化的是 conversation，不是文件系统状态。

优点：
- 很贴近“UI 只是窗口，后台工作继续”的体验。
- Kernel/session 有 ID，可重新打开、聚焦、关闭。
- 对 TUI attach/detach 很有启发。

不足：
- Jupyter kernel 继续跑不等于任务状态可恢复；server 进程死了，正在执行的 kernel 也可能丢。
- Claude session 恢复的是上下文，不是可靠任务账本。
- 长输出、断线期间的消息缓冲、资源泄漏都需要额外治理。

对我们的启发：
- chat/TUI 应该只 attach 到 gateway，不拥有任务。
- 每个 runner 要有 run id、日志文件、状态文件，TUI 重连后从账本读取，而不是等旧 stdout。
- transcript 可以辅助恢复，但不能作为唯一事实源。

参考：
- JupyterLab running kernels: https://jupyterlab.readthedocs.io/en/stable/user/running.html
- Jupyter Server architecture: https://jupyter-server.readthedocs.io/en/latest/developers/architecture.html
- Claude Code sessions: https://code.claude.com/docs/en/agent-sdk/sessions

### 4. 任务队列 + worker pool

代表：Celery、RQ、n8n queue mode。

Celery 用 broker 传递任务，worker 并发处理，结果 backend 记录状态；生产环境建议用平台工具或 supervisord daemonize worker。RQ 是更轻量的 Redis Queue，worker 信息、状态、heartbeat 等保存在 Redis，并支持 worker pool。n8n queue mode 把主进程、webhook 进程、worker 分开，用 Redis 做 broker、数据库保存执行数据，worker concurrency 默认 10，可横向扩展。

优点：
- 这是“100 个孙代理同时工作”的核心模式。
- 控制面和执行面天然分离：gateway 不被单个 runner 阻塞。
- 可以做 worker_concurrency、start_rate、timeout、retry、dead letter。

不足：
- 引入 Redis/RabbitMQ/Postgres 会显著增加安装复杂度。
- 任务必须设计成幂等，否则重试会重复写文件、重复调用 API。
- 结果 backend、broker、worker 版本不一致会带来隐蔽问题。

对我们的启发：
- 我们第一版不急着引 Redis，可以先做 SQLite/job 表 + 子进程 worker。
- 要早早区分：`runner_concurrency`、`runner_start_rate`、`acceptance_limit`、`planner_interval`。
- 所有 runner 的副作用要有 idempotency key 或“先检查再写”。

参考：
- Celery first steps: https://docs.celeryq.dev/en/main/getting-started/first-steps-with-celery.html
- Celery workers: https://docs.celeryq.dev/en/v5.4.0/userguide/workers.html
- RQ workers: https://python-rq.org/docs/workers/
- n8n queue mode: https://docs.n8n.io/hosting/scaling/queue-mode/

### 5. Durable workflow engine

代表：Temporal、LangGraph durable execution、CrewAI Flows。

Temporal 的核心卖点是 crash-proof execution：崩溃、网络故障、基础设施中断后仍能从上次状态继续。LangGraph durable execution 通过 checkpointer 保存每步状态，要求 thread id、确定性、把副作用包进 task，并提供不同 durability 模式。CrewAI Flows 有 `@persist`，默认 SQLite 后端，保存 flow 状态。

优点：
- 最适合长任务、人类验收、暂停/恢复、失败重试。
- 可以把“父代理拆任务 -> 子代理执行 -> 验收 -> 继续”建成显式状态机。
- 对 LLM 超时、工具失败、人工介入很友好。

不足：
- 成本是复杂度：确定性、幂等、checkpoint、replay 都要认真设计。
- Temporal 对个人本地小工具来说偏重。
- LangGraph/CrewAI 适合 workflow，但我们还需要自己的任务树、权限、文件边界和多层代理账本。

对我们的启发：
- `my-agent` 应该实现轻量 durable execution，而不是一开始引大框架。
- 状态机要显式：CREATED / READY / RUNNING / AWAITING_ACCEPTANCE / DONE / BLOCKED / FAILED / CANCELLED。
- 每个步骤写 event log，重启后从 event log + current state 恢复。

参考：
- Temporal docs: https://docs.temporal.io/
- LangGraph durable execution: https://docs.langchain.com/oss/python/langgraph/durable-execution
- CrewAI flows: https://docs.crewai.com/en/concepts/flows

### 6. 自动化平台 / supervisor OS

代表：Node-RED、Home Assistant。

Node-RED 把 flows 和 credentials 放在 userDir，context 可以用本地文件存储，默认带缓存并每 30 秒 flush；如果进程异常退出，未 flush 的数据会丢。Home Assistant Supervisor 负责运行 Core、更新、失败回滚、备份恢复、安装和运行 add-ons。

优点：
- 给“普通用户不关心底层参数”提供了范式：用户看的是 flow/add-on/automation，不是 worker 参数。
- Supervisor 层很有价值：安装、更新、备份、回滚、插件都由它管。
- Node-RED 的 context store 提醒我们：持久化频率会影响可靠性和磁盘磨损。

不足：
- 自动化平台的流程通常比 agent 决策更确定；LLM agent 的不确定性更高。
- 低代码平台容易隐藏失败细节，需要更强的审计和可解释日志。
- flush 间隔和缓存策略如果不清楚，会让用户误以为“已经保存”。

对我们的启发：
- 未来可以有 `my-agent doctor` / `backup` / `restore` / `upgrade`。
- 配置默认走 auto，用户只看任务树和状态。
- 关键任务状态必须同步写；低价值 telemetry 才能异步 flush。

参考：
- Node-RED runtime config: https://nodered.org/docs/user-guide/runtime/configuration
- Node-RED local filesystem context: https://nodered.org/docs/api/context/store/localfilesystem
- Home Assistant Supervisor: https://developers.home-assistant.io/docs/supervisor/

### 7. Cloud sandbox task platform

代表：OpenAI Codex 云端任务。

Codex 把每个 coding task 放在独立云 sandbox，并行执行，完成后提供 terminal log、测试输出和可审查结果。它不是本地 gateway，但它把“任务隔离、并行、证据链”做成了一等公民。

优点：
- 每个任务独立环境，互不污染。
- 天然并行，用户可以同时派多个任务。
- 证据链清楚：日志、测试、diff、结果。

不足：
- 云端 sandbox 不等于本地工具链常驻。
- 本地私有文件、桌面工具、长期 gateway、TUI attach/detach 不是同一个问题。
- 成本、权限和环境同步是主要挑战。

对我们的启发：
- 子代理/孙代理最好有隔离工作目录、明确允许写入边界和证据输出。
- 父代理不应该只看最终文本，要看日志、测试和 artifact。
- 并行任务必须可审查、可回滚、可重新派工。

参考：
- OpenAI Codex: https://openai.com/index/introducing-codex/

## 对 my-agent 的推荐架构

### 先定身份模型

`my-agent` 未来不是单个进程，也不是单个聊天 session，而是一个长期身份。每个 gateway 都是完整独立 agent，不因为处在下级位置而减少功能。上下级关系只通过授权、委托和汇报形成：

- identity 决定所有权。
- grant 决定访问权。
- delegation 决定协调权。

下级 gateway 默认不能读取上级的全部任务、现状、记忆、密钥和工具状态；但它可以在授权范围内独立完成任意任务，也可以继续管理自己的下级 gateway。root gateway 保留 reclaim 协调权的能力，但 root reclaim 不等于无限读取其他 gateway 的私有状态。

组织扩展通过 invite key 完成：上级 gateway 可以在授权范围内邀请新的 gateway 加入自己的下级关系，新的 gateway 仍是完整 my-agent，只是在组织里有受限成员身份。组织结构需要独立账本记录 parent/children、membership、grants、delegations、last_seen 和 coordination_epoch，并通过事件日志随时重建。

### 第一阶段：本地 gateway control plane

先做：
- `my-agent gateway start`
- `my-agent gateway stop`
- `my-agent gateway restart`
- `my-agent gateway status`
- `my-agent gateway logs`

实现重点：
- PID 文件、lock 文件、heartbeat 文件。
- stdout/stderr 日志落盘。
- gateway 只做控制面，不在主循环里长时间同步跑 runner。
- `daemon` 保留为前台调试入口。

### 第二阶段：SQLite 任务账本

不要一开始引 Redis。先用 SQLite + 文件目录：
- `tasks`：任务树和当前状态。
- `events`：每次调度、runner、验收、能力授权、失败都追加事件。
- `jobs`：待执行 runner / planner / acceptance job。
- `leases`：worker 租约，防止两个 worker 抢同一任务。

文件目录继续保存：
- prompt
- response
- runner_result.json
- evidence
- test logs
- acceptance report

### 第三阶段：worker pool

把概念拆清楚：
- `runner_concurrency`: 同时最多有多少 runner 在跑。
- `runner_start_rate`: 每分钟最多启动多少新 runner。
- `planner_interval`: 父代理多久做一次全局判断。
- `acceptance_limit`: 每轮最多验收多少个已交付任务。
- `runner_timeout_seconds`: 单个 runner 超时。

当前 `daemon_max_runners` 只是过渡期参数，未来不要让普通用户看见。

### 第四阶段：自适应调度

`auto` 策略应该观察：
- API 延迟。
- API 错误率。
- runner 平均耗时。
- 队列长度。
- 卡住任务数量。
- 验收积压。
- 用户是否设置预算/规模上限。

根据这些自动：
- 降低并发。
- 延长超时。
- 暂停启动新孙代理。
- 优先验收已有结果。
- 上报阻塞摘要。

### 第五阶段：系统服务安装

最后再做：
- Windows Task Scheduler / 服务包装。
- macOS launchd。
- Linux systemd user service。
- `my-agent doctor` 检查 PATH、Python、配置、端口、PID、日志、lock、权限。

## 我们不该做的事

- 不要把 LLM conversation transcript 当作任务状态。
- 不要让 gateway 主进程同步跑 30 分钟 runner。
- 不要让 heartbeat 有固定 OK 快速通道。
- 不要默认暴露一堆启动参数给普通用户。
- 不要在没有幂等策略前盲目做大并发。
- 不要把“进程重启恢复”和“任务恢复”混为一谈。

## 最小可落地设计

第一版 gateway 可以非常朴素：

```text
gateway process
  - reads config
  - owns heartbeat / pid / lock / logs
  - polls SQLite jobs
  - starts worker subprocesses up to runner_concurrency
  - watches worker heartbeats
  - writes events
  - exposes local status through files first, later HTTP/WebSocket

chat/TUI
  - reads status
  - submits user messages / tasks
  - can exit anytime
```

这样就能满足最重要的承诺：重启 gateway 不掉任务状态。
