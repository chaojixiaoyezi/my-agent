# Gateway Design Notes

这份文档记录 gateway 常驻方向的外部参考和本项目目标。它不是实现完成清单，而是避免我们把“前台 daemon”“TUI 会话”“后台 runtime”“任务状态”混在一起。

更完整的外部方案调研、优缺点拆解和阶段建议见 [GATEWAY_RESEARCH.md](GATEWAY_RESEARCH.md)。

## 我们要的形态

目标是：

```text
my-agent
my-agent gateway restart
my-agent chat
```

`gateway` 是本地常驻 runtime，`chat` / TUI 只是客户端。用户可以退出聊天界面、重启 gateway、刷新会话；任务树、runner 输出、验收、能力授权、失败原因和调度日志都要落盘，gateway 重启后从任务账本恢复，而不是依赖某个长聊天上下文活着。当前 `my-agent` 不带子命令时已经会自动启动 gateway 并进入 `chat --gateway`，这是默认用户体验。

一个 Gateway 可以服务多个 TUI。后台续跑的串行边界是 durable conversation thread，不是整个 owner：
同 thread 的前台和后台继续共用一条 run claim，不同 thread 在全局/单 owner 两层上限内并发。
这与 会话运行时 每个 Thread/Session 自有 active turn 的边界一致，但仍保留本项目的跨进程持久队列和恢复。

gateway 的本地后台控制面入口：

```text
my-agent gateway start
my-agent gateway status
my-agent gateway stop
my-agent gateway restart
my-agent gateway logs
my-agent gateway ask "继续推进当前任务"
my-agent gateway result <request_id>
```

它先负责后台进程、pid、state、heartbeat、stop request、日志和本地消息入口，内部暂时复用现有 daemon/watch 调度。SQLite 任务账本、worker pool、跨机器通信和组织模型会在这个入口上逐步接入。

第二步已经补上本地 inbox / response 通道：

```text
client CLI -> data/gateway/requests/pending/<request_id>.json
gateway worker -> data/gateway/responses/<request_id>.json
gateway audit -> data/gateway/gateway_requests.jsonl
```

`gateway ask` 是最小客户端协议。它还不是完整 TUI attach，也不是完整 HTTP/WebSocket gateway；当前代码已有可选本机 HTTP 控制面，但请求事实源仍是文件队列。它已经把“用户消息进入常驻 gateway 并触发完整 LLM turn”这件事从前台 chat 里拆了出来。`chat --gateway` 已经开始复用这条请求队列：chat 只做前台客户端，普通消息交给后台 gateway 处理。后续 TUI 可以继续复用同一条请求队列，或者把底层从文件队列替换成 SQLite / HTTP，而不改变用户命令面。

### 大白话解释：gateway ask / result

这三个命令不是最终普通用户每天必须敲的命令，而是现在给 gateway 留出来的“本地消息入口”和“调试口”：

```text
my-agent gateway ask "继续推进当前任务"
my-agent gateway ask "长任务" --no-wait
my-agent gateway result <request_id>
```

可以这样理解：

- `gateway start`：先把本地常驻的主代理后台启动起来。
- `gateway ask "一句话"`：把这句话发给后台主代理，并在当前终端等它回话。
- `gateway ask "长任务" --no-wait`：把任务发给后台主代理，但当前终端不等结果，只拿一个 `request_id`。
- `gateway result <request_id>`：以后根据这个 `request_id` 去拿结果。
- `chat --gateway`：在交互界面里自动做 `ask/result`，用户只像正常聊天一样输入。

未来如果接入微信、Telegram、飞书、Web TUI 或桌面客户端，用户不会手动敲这些命令。聊天工具会替用户做同样的事：

```text
用户在聊天工具里发消息
-> 聊天适配器把消息写入 gateway inbox
-> gateway 触发完整 LLM turn
-> gateway 写出 response
-> 聊天适配器把 response 发回给用户
```

所以这些 CLI 命令的定位是：

- 普通用户：以后基本不用直接关心。
- 开发者/高级用户：用来验证 gateway 本体是否工作。
- 排错时：如果聊天工具没回复，可以先用 `gateway ask` 判断是 gateway 坏了，还是聊天适配器坏了。
- 架构上：这是未来 TUI、聊天工具、HTTP/WebSocket adapter 都会复用的最小协议雏形。

### 用户回复与原生附件交付

运行时的 `[MAIN_AGENT_*]`、`[RUN_*]`、`[SUBAGENT_*]` 是机器协议，不是用户文案。Gateway response、
所有 IM 最终回复和持久化 assistant transcript 在出口统一经过 `project_user_reply`：普通文本原样保留，
完成协议只转换成“文件已经生成”等简短正文；绝对路径、`validated` 和内部验收字段不出站。

外部发送使用两份不可混合的 typed 结构：`DeliveryContext` 由入站 adapter、owner 配置或会话绑定提供
可信 channel/target/reply_to；`ReplyEnvelope` 只放正文和已校验附件，永远没有收件人。普通最终回复、
后台主动消息和显式工具发送统一进入 `DeliveryService`。provider adapter、能力和目标地址合同由
`ChannelAdapterRegistry` 注册，投递主流程不维护平台 if/else。

文件交付不靠模型说“服务器上没有飞书工具”，也不新增 `feishu_send_file` 等平台专用重叠工具。
唯一模型入口是 `send_message`：目标从当前 scoped owner 的可信配置取得；附件只能引用该 owner 已登记
的 artifact，发送前重新核对 registry 状态、真实路径边界和 SHA-256，然后由当前通道 adapter 调用
原生 `send_image` / `send_file`。同一外部调用有持久化幂等回执，重试不得重复发送。

完成轮次会把最小产物引用写入 assistant message metadata。下一轮的 `Recent Artifact Refs` 只提供给
模型和工具；用户说“发我/把上一个文件给我”时直接调用 `send_message` 复用原文件，不把上一任务重做
一遍。旧 transcript 若仍保存完整完成协议，会在读取时投影成人话并迁移式恢复产物引用。

完整对象合同、新 IM 接入步骤和幂等边界见 `docs/design/CHANNEL_DELIVERY_DESIGN.md`。

### 本地队列目录怎么理解

当前先用文件队列，原因是简单、跨平台、容易看见和调试。每个目录的含义如下：

```text
data/gateway/requests/pending/
```

等待处理的请求。`gateway ask` 会先把请求 JSON 写到这里。可以把它理解成“收件箱”。

```text
data/gateway/requests/processing/
```

gateway 正在处理的请求。后台 worker 取走 pending 请求时，会先移动到这里。可以把它理解成“正在办”。

```text
data/gateway/requests/done/
```

已经处理完的请求原件。处理完成后，请求 JSON 会移动到这里。可以把它理解成“已归档的原始工单”。

```text
data/gateway/responses/
```

处理结果。每个 request id 对应一个响应 JSON。`gateway result <request_id>` 读取的就是这里。

```text
data/gateway/gateway_requests.jsonl
```

请求审计日志。每处理完一次请求，就追加一行，方便以后追踪“谁什么时候给 gateway 发了什么，结果是什么”。

如果 gateway 崩溃时有请求停在 `processing/`，下一次 gateway 启动时会把这些请求退回 `pending/`，避免任务半路卡死。

## 多 Gateway 组织模型

长期目标不是“一个主 gateway 拥有所有下级”，而是“多个完整独立 gateway 通过授权、委托和汇报形成组织关系”。

每个 gateway 都是完整 agent 实体：

- 有自己的身份、任务账本、记忆、配置、工具、密钥和能力目录。
- 可以独立聊天、拆任务、开自己的子代理/孙代理、运行 planner 和 runner。
- 可以作为上级协调其他 gateway，也可以作为下级接收其他 gateway 的委托。
- 不因为成为“副 gateway”而减少自身能力。

上下级关系只定义访问边界和协调关系：

- 上级可以把某个任务、某段上下文、某些能力卡授权给下级。
- 下级默认不能读取上级的全部任务、现状、记忆、密钥、工具状态或权限。
- 下级完成任务后回报授权范围内的结果和证据。
- 下级仍然可以在自己的本体内独立承担任意工作。

所以这里的核心不是 role-based capability reduction，而是：

```text
identity 决定所有权
grant 决定访问权
delegation 决定协调权
```

主 gateway 和副 gateway 的区别，不是“谁功能更多”，而是“谁在当前组织关系里承担协调责任，谁能访问哪些被授权资源”。

### Root 与协调权

一个 my-agent identity 可以有 root gateway。root 是创始身份或最高恢复者，拥有一键 reclaim 的权利。root 可以把当前协调权委托给另一个 gateway，让它成为 active coordinator；root 自己仍然是完整 gateway，不是低配下级。

委托后的约束：

- active coordinator 可以调度全局任务和协调下级 gateway。
- active coordinator 不会自动获得 root 的全部私有记忆、密钥、任务账本和工具状态。
- root 可以 reclaim 协调权。
- 被委托出来的 coordinator 不能把 root reclaim 权限转授给别人。

每次协调权变更都应该增加 epoch，避免旧 coordinator 在网络延迟或恢复后继续写入旧命令。

```yaml
agent_identity_id: "my-agent-001"
root_gateway_id: "gateway-a"
active_coordinator_gateway_id: "gateway-b"
coordination_epoch: 42
root_reclaim_enabled: true
```

### 授权与委托

gateway 之间传递任务时，不传“整个自己”，只传明确 scope：

```yaml
delegation:
  from_gateway_id: "gateway-a"
  to_gateway_id: "gateway-b"
  task_id: "task-123"
  scope:
    artifacts:
      - "task-123/context.md"
      - "task-123/evidence/"
    capability_cards:
      - "python-testing"
      - "repo-readonly"
    allowed_actions:
      - "run"
      - "test"
      - "report"
  expires: "after_task"
  epoch: 42
```

这样可以同时满足：

- 公司模式：CEO gateway 可以委托部门 gateway；部门 gateway 仍是完整主体。
- 多机器模式：一台机器挂了，其他 gateway 可在授权范围内接替任务。
- 隐私和安全：下级不能非法获取上级全部状态和权限。
- 迁移和备份：本体可以迁移，但每个 gateway 的私有状态和共享状态要区分。

### Organization Gateway Model

组织模型可以理解成：用户创建一个组织，root gateway 生成组织身份和邀请凭证；员工或其他机器使用被授予的 key / invite 安装并注册自己的 my-agent。注册后的 gateway 加入组织关系，但仍然是完整独立的 my-agent。

示例：

```text
Company / Organization
  root gateway: founder-my-agent
    |
    |-- department gateway: engineering-lead-my-agent
    |     |
    |     |-- member gateway: backend-dev-my-agent
    |     `-- member gateway: qa-my-agent
    |
    `-- department gateway: ops-lead-my-agent
          |
          `-- member gateway: build-machine-my-agent
```

这里每个节点都能完整工作：

- root gateway 可以做自己的任务。
- department gateway 可以独立工作，也可以管理自己下面的 gateway。
- member gateway 可以独立工作，也可以在被授权时继续邀请自己的下级。
- 组织关系可以随时调整：提升、降级、转移团队、撤销授权、重新指定 coordinator。

“降级”不是能力减少，而是组织职责变化。例如某个部门 gateway 不再负责协调某个团队，它仍然是完整 my-agent，只是该团队的 coordinator role 被移交给另一个 gateway。

### Invite Key 与注册

组织扩展不应该靠共享 root 的 API key，而应该靠邀请凭证。invite key 只证明“被谁邀请、允许加入哪里、初始权限是什么、是否允许继续邀请下级”。

```yaml
invite:
  org_id: "org-001"
  issued_by_gateway_id: "founder-my-agent"
  parent_gateway_id: "engineering-lead-my-agent"
  invitee_label: "backend-dev"
  initial_role: "member"
  allowed_scopes:
    - "receive_tasks"
    - "report_results"
    - "request_capabilities"
  can_invite_children: false
  expires_at: "2026-05-30T00:00:00Z"
```

副 gateway 也可以签发自己的 invite，把新人加入到自己的下级关系里，但只能在自己被授权的组织范围内做这件事。它不能借此读取上级的私有账本，也不能绕过上级拿到未授权能力。

### 组织架构状态

组织关系必须是可查询、可审计、可恢复的状态，不应该只存在聊天里。后续应该有一个组织账本，至少记录：

- `org_id`
- `gateway_id`
- `parent_gateway_id`
- `children_gateway_ids`
- `active_coordinator_for`
- `membership_status`: active / suspended / revoked / left
- `capability_summary`
- `last_seen_at`
- `trust_level`
- `can_invite_children`
- `grants`
- `delegations`
- `coordination_epoch`

组织架构变化必须追加事件：

- gateway joined
- gateway left
- gateway suspended
- gateway revoked
- coordinator delegated
- coordinator reclaimed
- parent changed
- invite issued
- invite revoked
- grant issued
- grant revoked

这让组织随时可重建，也能回答“现在谁归谁管”“谁有权看什么”“谁正在负责哪个任务子树”。

### 第一版如何留地基

第一版仍然先做单机 gateway，但 schema 和日志要提前留下这些概念：

- `gateway_id`
- `agent_identity_id`
- `root_gateway_id`
- `active_coordinator_gateway_id`
- `coordination_epoch`
- `delegation_id`
- `delegated_by_gateway_id`
- `assigned_gateway_id`
- `parent_gateway_id`
- `org_id`
- `membership_status`
- `can_invite_children`
- `grant_scope`
- `attempt_id`

先不做跨机器通信，也不做真正组织树；但任务、事件、runner attempt 和 gateway 状态里先带这些字段，后续扩展时不会推倒重来。

### 仍需补充的设计点

开工前还要继续补齐这些边界，但不需要第一版全部实现：

- Invite key 的签名、撤销和过期机制。
- root reclaim 与 active coordinator 的冲突处理。
- 下级 gateway 离线后，它负责的任务如何超时、重派或转交。
- 跨机器 artifact 同步：哪些内容共享，哪些内容只留本机。
- 组织备份和迁移：导出 org ledger、task ledger、artifact bundle，但不导出本机 secret。
- Secret 管理：API key、私有工具凭证、员工本机密钥必须本地保存，不进入上级账本。
- 版本协作：不同 my-agent 版本之间如何明确协议和升级边界。
- 权限审计：每次 grant、delegation、revoke、reclaim 都要有事件记录。
- 隐私策略：上级能看到下级汇报，但不能默认看到下级全部私有记忆。

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
- 高级用户可以在配置里调策略：任务规模默认 0 表示不设硬上限，runner 并发/启动速率默认 `auto`；runner 外层总超时默认 `off`，需要压测或自动抢救卡死 runner 时再显式设成 `auto` 或固定秒数。
- 真实 worker pool 做好前，当前 `daemon_max_runners: "auto"` 先映射成保守值 1，避免前台进程一次性同步阻塞太多 runner。
