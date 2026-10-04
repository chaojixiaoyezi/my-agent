# 插件事件订阅与“只收紧”的工具调用钩子（M 线第一期，M1 设计稿）

- **状态**：设计稿（2026-10-03，be）。ae、9b 评审已吸收（第 17 节），3a 已定 D1–D9 和开放范围；派活清单见第 18 节。分支 `claude/be-m1-design`，基于 `claude/3a-step17h` `b453f8883`。
- **来源**：goal 2026-10-03 第四节（对标 Claude Code Mods）。本期只做“只看、只收紧”：插件能看到宿主流程里的事件，能在工具调用前要求“再确认一次”或“拒绝”，**不能放宽、改写、接管**。
- **改了原立场**：[PLUGIN_LIFECYCLE](PLUGIN_LIFECYCLE.md) 第 187 行“事件响应”原写“后续从既有结构化事件发布只读订阅……不先开放任意核心状态拦截或常驻回调”。本稿保留“只读订阅”，新增一种受限拦截：工具调用前的收紧钩子，结果只有三种（照原样、加一道确认、拒绝），只能比宿主更严。“常驻回调”仍不开放：钩子是宿主发起的一问一答，有超时，插件不能主动推任何东西进宿主。
- 本稿写成可拆块交给 my-agent 会话实现的样子，第 13 节是块清单。

## 1. 解决什么，不做什么

**要做到**（第一期验收，goal 原文）：
- 一个样例插件观察到全部 6 类事件；
- 另一个样例插件在 `rm -rf` 前强制要求确认、在指定工具上直接拒绝；
- 两个都在 TUI 和 IM 里看得到“是哪个插件动的手”。

**这期不做**（第二期，等用户看了第一期再定）：
- 改写提示词或工具参数、接管工具调用（违反“唯一执行器、唯一账本”，用“拒绝 + 插件自己的工具”代替）；
- 插件加 UI 按钮、输入框；
- 把模型请求转给别的模型；
- 插件市场、URL 安装。

**硬性边界**（goal 第五节）：
- 默认关（总开关，第 11 节）；
- 只在插件进程沙箱里跑，断网（声明了网络权限的除外，见第 10 节）；
- 安装、启用要过确认码，确认码覆盖订阅了哪些事件、收紧哪些工具、要不要正文、要不要网络；
- 不能放宽宿主的 H2 隐藏路径、H3 宿主只读、审批策略和沙箱；
- 每次“拒绝 / 要求确认”都写进结构化账本，TUI 和 IM 都看得到。

## 2. 现状（盘点事实，基于 `b453f8883`）

- **没有事件总线或钩子**。插件对宿主唯一的“推送”是面板轮询（`plugin_display`）；`ExtensionPlugin` 只有启动时的注册（`agent/extensions/plugin.py`）。
- **面板服务已有一套可复用的投递机制**（`agent/plugin_display/service.py`）：
  - 每个（owner, 激活代次）一条连接，`_Connection.pending` 按（会话, 面板）只留最新一份；
  - 每个插件同时一个在途请求（`running` 标志 + `_drain`）；
  - 请求超时 3 秒，出错退避 5 秒，空闲 120 秒关连接；
  - 每次渲染前后复核激活代次（`_require_current`），代次失效就丢结果、关连接；结果只在连接对象没换时才写回。
  - 缺口：连接是第一次渲染时才启动，启动只受 MCP 连接超时（30 秒）约束，不受 3 秒渲染超时约束。
- **MCP 握手**：宿主 `initialize` 时不声明任何能力；插件在回复的 `capabilities.experimental` 里声明扩展和版本（`my-agent/display`、`my-agent/workspace-read-context`、`my-agent/workspace-write-context`），宿主按声明决定发不发（`agent/tooling/mcp_client.py` `_handshake`，`agent/plugin_runtime.py` `_negotiated`）。
- **工具调用链**：`ToolExecutor.execute`（`agent/tooling/executor.py`）→ `ActionPolicy.decide` → `ask` 时先复核可用性 → 非放行就返回 `approval_required` / `failed` → 放行进 `_execute_authorized`（其中 `pre_handler_gate` 只能拒，不能要求确认）。
  - `ask` 变成审批：`round_execution._resolve_tool_approval` → `build_tool_approval_request`（`agent/contracts/tool_approval.py`），用户批准后**同一调用重跑一遍**，`ActionPolicy` 按绑定（工具、run、operation、幂等键、参数哈希）认出批准。
  - **这几种情况下，`ask` 不弹框就被批准**：自主模式（工具不是 `ApprovalPolicy("always")`）、会话级已批准缓存（`StreamApproval` 按“工具名 + 参数哈希”）、owner 长期授权（`grant_key`）、等待中由自主模式提供者给出决定。插件要求的确认必须绕开这四条，见 8.5。
  - 已有“谁要求确认”的先例：`_with_request_actor` 给决策模型自动执行的审批加 `binding.actor` 和描述前缀“[决策自动执行 actor=…]”。`ActionDecision` 的原因码和 evidence 目前不进 `ToolApprovalRequest`。
  - IM 没有审批卡片，只有一行文字“代理请求：…。回复 /approve <管理员密码> 允许本次，/deny 拒绝。”（`agent/adapter/manager.py`），摘要取审批描述前 200 字。
- **插件工具一律按 dangerous 处理**（`plugin_runtime.discover_tools` 不传 effect）。清单里的 `requested_effect` 只决定发不发写入上下文。
- **插件进程沙箱**（`plugin_sandbox.py`）：开关 `plugin_process_sandbox` 默认关；开了也只限写（只能写插件自己的数据目录），**读和宿主一样、网络不限**（`docs/design/PLUGIN_PROCESS_SANDBOX.md`：“这个沙箱不是网络边界”）。
- **启用确认码**（`plugin_runtime_facts.confirmation_details`）只覆盖程序和文件（插件、版本、包哈希、入口、平台、文件清单、解释器），不覆盖工具、权限、网络；**只有 v6（任意语言）包要确认码**，Python 包（v1–v5）启用不要确认码。
- **宿主内部的事件源**：
  - 提交提示：Gateway `handle_ask` 写请求队列（带正文）；
  - 回合开始/结束：`request_execution._handle_gateway_request`；主活动行 `GatewayMainActivitySink`；
  - 工具开始/结束：`round_execution._emit_tool_progress`（直播，带输出摘要）；`runtime_events` 的 `tool_completed`（持久，不带参数和输出）；
  - 命令：`control_operation_service.execute_gateway_control_operation` 的回执（命令原文，密码已脱敏）。
  - 子代理在 Gateway 进程内的线程里跑，工具调用走同一个执行器。
- **账本**：`runtime_events`（每个 owner 的 runtime.db，按 `event_type, seq` 有索引）已有 `tool_completed` 带 `actor` / `decision_ref`，最适合放插件决定。

## 3. 总体结构

```text
观察（M2）：
  宿主事件点（提示提交、回合开始/结束、工具开始/结束、命令执行）
    → PluginEventHub（每个 Gateway 进程一个，按 owner 分区，只投给该 owner 已启用、已订阅的插件）
        → 插件通道（与面板共用的连接池，按 owner + 激活代次）
            → my-agent/events.observe（只读，宿主发、插件回执，单在途、只留最新、3 秒超时）

收紧（M3）：
  ToolExecutor.execute：ActionPolicy.decide 之后、执行之前
    → PluginToolGate.review（只问订阅了这个工具的插件，并行，总预算 = 钩子超时）
        → my-agent/tool-gate.review → allow_as_is / ask / deny
    → 与宿主决定取更严的那个 → 拒绝：结构化错误；要求确认：强制审批（绕开自主、缓存、长期授权）
    → 每次征询写 runtime_events（plugin_gate.decided）
```

一条原则贯穿全稿：**插件只能让结果更严**。宿主已经拒绝的调用不问插件；插件的回答只会把“放行”变成“确认”或“拒绝”，把“确认”变成“拒绝”。

## 4. 清单 v8：声明订阅与收紧

v8 在 v6（任意语言：`entry` + `files` + `platforms`）基础上加三个字段。只有 v8 包能订阅事件或收紧工具：v6 已有程序和文件的确认码，任意语言都能写，Python 插件用 v6 的解释器入口打包即可。v1–v5（宿主环境里的 Python 轮子）不加这个能力，理由是它们没有确认码，且第一期不想再开一条确认路径（待评审决定点 D1）。

```json
{
  "schema_version": "plugin_package.v8",
  "...": "v6 的全部字段原样",
  "events": [
    {"type": "tool_call_started", "content": "none"},
    {"type": "prompt_submitted", "content": "text"}
  ],
  "tool_gates": [
    {"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"},
    {"id": "guard-delete", "tools": ["apply_patch"], "effects": [], "arguments": "full"}
  ],
  "permissions": {"network": false}
}
```

示例里的 `guard-delete` 看的是 `apply_patch` 的补丁文本：宿主没有 `delete_file` 工具，模型删文件走补丁里的 `*** Delete File: ` 段或 `run_command`（3a 2026-10-03 裁定）。

校验规则（`PluginManifest.__post_init__`，照 `PanelDeclaration` 的严格写法）：
- `events`：0–6 项，`type` 取第 6 节的固定 6 种之一、不重复；`content` 只能是 `none`（默认）或 `text`。**3a 2026-10-03 裁定**：`text` 只允许用在 `prompt_submitted`，`tool_call_started` 只能是 `none`；工具参数只从精确工具的 `tool_gates.arguments: "full"` 提供，不新增 `events[].tools`。
- `tool_gates`：0–4 项；`id` 小写字母数字连字符 1–32 位、包内唯一；`tools` 是宿主工具名的精确列表（0–16 项，不支持通配）；`effects` 只能取 `mutating`、`dangerous`（表示“所有这类效果的工具”）；`tools` 和 `effects` 至少一个非空；`arguments` 只能是 `none` 或 `full`。
- `arguments: "full"` 时 `tools` 必须非空、`effects` 必须为空（ae 评审）：按效果收紧覆盖的工具太多（含别的插件的工具、`write_file` 的内容），用户在确认码里读不出这么大的范围，所以按效果收紧只能拿 `args_hash`。
- `permissions.network`：布尔，默认 false。
- `events` 和 `tool_gates` 都为空的 v8 包无效（`invalid_manifest`），没订阅就该用 v6。
- **3a 2026-10-03 裁定**：v8 的非空 `events` 或 `tool_gates` 算作贡献，工具、面板、事件、收紧四者至少一项非空；允许只订阅事件或只声明收紧门，不用硬塞工具或面板。上述“必须有订阅”门继续保留；v1–v7 原规则完全不变，v6 没有工具也没有面板仍被拒。
- 第一期 `host_api` 不能和 `events` / `tool_gates` 同时声明（`events_with_host_api_unsupported`）：宿主 API 走本机回环，和第 10 节的断网冲突，留到第二期再定。
- 清单不认识的工具名不在装包时拒绝（工具会随别的插件、版本变化），在 `/plugins info` 里标“当前不存在”。
- 旧版本（v1–v7）序列化逐字节不变（沿用 `plugin_manifest.py` 的既有约定）。

## 5. 握手与两个协议方法

插件在 `initialize` 回复的 `capabilities.experimental` 里声明：
- `"my-agent/events": {"versions": ["1"]}`：能接观察事件；
- `"my-agent/tool-gate": {"versions": ["1"]}`：能回答收紧征询。

清单声明了、握手没声明：
- 事件：不投递，计入“不可用”计数，`/plugins info` 显示原因；
- 收紧：**按“要求确认”处理**（宁严勿松），每次都记账，`/plugins info` 显示“收紧钩子不可用”。

**观察**：宿主 → 插件 `my-agent/events.observe`（JSON-RPC request，插件只回空结果当回执）：

```json
{"events": [
  {"event_id": "ev-…", "type": "tool_call_finished", "seq": 812, "occurred_at": 1759480000.12,
   "dropped_before": 3, "channel": "tui", "thread_ref": "thread-…", "actor": "main",
   "facts": {"tool": "run_command", "ok": false, "error_code": "COMMAND_FAILED", "...": "..."}}
]}
```

**收紧**：宿主 → 插件 `my-agent/tool-gate.review`：

```json
{"gate_id": "guard-rm", "call": {"call_id": "…", "tool": "run_command", "effect": "dangerous", "actor": "main",
 "interactive": true, "args_hash": "…", "arguments": {"command": "rm -rf build"}}}
```

`arguments` 只在声明 `full` 时给，并且先做两步投影（ae 评审）：去掉所有 `__` 开头的宿主内部键（`__run_scope`、`__sandbox_*`、`__operation_id` 等，里面有路径和身份）；再经宿主现有的统一脱敏 `common/log_redaction.redact_sensitive_value`（工具输出投影用的同一套），不另写规则。然后截到 4000 字。

插件回：

```json
{"verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次"}
```

- `verdict` 只能是 `allow_as_is`、`ask`、`deny`；
- `reason_code` 大写字母数字下划线 1–40 位；
- `message` 可选，纯文本，宿主截到 80 字、去掉控制字符和换行后才展示；
- 其它字段一律忽略，不能借回复改参数。

## 6. 事件目录 v1

公共字段：`event_id`、`type`、`seq`（按「owner × 插件激活」各自计数、从 1 开始单调递增；换代重装后从 1 重新开始——插件从序号里看不出没订阅的类型或别的 owner 发生过什么）、`occurred_at`、`dropped_before`（这个插件的槽建立之后、同类事件被合并掉没送达的条数：中途启用从当前最新一条开始收、第一条为 0；换代丢旧待发后同样从 0 起算）、`channel`（`tui` / `feishu` / …）、`thread_ref`（会话编号的哈希，不是原编号）、`actor`（`main`、`subagent` 或 `decision`；`decision` 是决策模型的宿主自动执行，J16 片 D 也走同一个执行器）。**默认不含任何正文、路径、参数或输出。**

| 事件 | 结构化事实（facts） | 可选正文（声明 `content: "text"` 且确认码同意） | 事件点 |
| --- | --- | --- | --- |
| `prompt_submitted` | `request_id`、`chars`、`has_attachments` | 提示文字，截到 4000 字 | Gateway `handle_ask` 写队列成功后 |
| `turn_started` | `request_id`、`model_name`（不含服务商和密钥） | 无 | `_handle_gateway_request` 开始执行时 |
| `turn_ended` | `request_id`、`status`（done / failed / stopped / interrupted）、`duration_ms`、`tool_calls`、`error_code` | 无 | 同上，收口时 |
| `tool_call_started` | `call_id`、`tool`、`effect`、`args_hash` | 无（3a 2026-10-03 裁定；参数只走精确工具的 `tool_gates.arguments: "full"`，见第 5 节） | 执行器放行之后、真正执行之前 |
| `tool_call_finished` | `call_id`、`tool`、`ok`、`error_code`、`failure_stage`、`duration_ms`、`handler_executed` | 无（第一期不给输出） | 执行器拿到结果之后 |
| `command_executed` | `command`（只给命令名，如 `/model`）、`operation_id`、`state`（ok / failed） | 无 | Gateway 控制命令回执落定后 |

- 只投给事件所属 owner 已启用、已订阅、握手通过的插件。多用户 Gateway 里 A 的事件永远不到 B 的插件。
- 被拒绝、要求确认后被用户拒绝的工具调用，不发 `tool_call_started` / `finished`（它们没执行）；收紧征询本身记在账本里（第 9 节）。
- 本地直连 TUI（不经 Gateway）第一期只有工具两类事件，没有提示、回合、命令三类（已知边界）。

## 7. 观察的投递语义（M2）

照面板服务的做法，并把它的连接管理抽成两边共用的通道（块 B2），不再写第二套：
- **通道**：每个（owner, 激活代次）一条 MCP 连接，事件和面板共用。事件中心要连接时用 `gateway_parts/plugin_panels_http.plugin_channel_pool(server)` 取那个挂在 server 上的唯一池（B2 复审返工加的入口），不要自己 new；池由 Gateway 在停止时关闭。
- **只留最新**：每个插件按事件类型各留一份待发（新的覆盖旧的），被覆盖的条数计入下一条的 `dropped_before`（只算这个插件的槽建立之后被合并的；中途启用从当前最新一条开始收，第一条为 0）。一次请求把所有类型的待发打成一批（最多 6 条）。
- **单在途**：每个（owner, 激活）同时只有一个 `events.observe` 请求；在途时新事件只进待发。发送任务按（owner, 激活）独立调度、线程池 4 个——一个插件挂住只占自己的线程，不拖同 owner 或其它 owner 的插件；挂住的插件数到达线程数仍是全局上界（与面板服务同款边界）。
- **有界**：请求超时 3 秒；出错退避 5 秒；空闲 120 秒关连接。连接启动也计入超时：启动超过 3 秒就把这批记为“超时”，连接在后台继续启动（补上面板服务的同一个缺口）。
- **撤销**：每次发送前后复核激活代次；代次失效就丢掉待发、关连接。停用即撤销，不会有“停用后还收到事件”。
- **不影响主流程**：事件点只做“把事实放进待发表”（加锁、拷贝几个字段），发送在宿主后台线程池里做；插件慢、挂、崩，主流程都感觉不到。
- **计数**：每个（owner, 插件, 事件类型）在内存里记 `delivered`、`coalesced`、`failed`、`unavailable`、`last_error_code`、`last_delivered_at`。不写盘（观察不是决定），`/plugins info` 显示。

## 8. 工具调用前的收紧钩子（M3）

### 8.1 挂在哪

`ToolExecutor.execute`：`ActionPolicy.decide` 返回之后、`_precheck_before_approval` 之前。
- 宿主决定是 `deny`：不问插件，直接拒。
- 宿主决定是 `allow` 或 `ask`：问所有“收紧了这个工具”的插件（工具名精确命中，或工具效果命中 `effects`）。没有插件命中时，一行代码都不多走。
- 子代理和决策模型自动执行的工具调用走同一个执行器，同样受收紧，`actor` 分别是 `subagent`、`decision`。
- **只收紧模型发起的调用，用户自己发的宿主命令永远不受收紧**（ae、9b 评审）：
  - 事实：用户的 `/plugins` 管理命令（`plugin_management._prepare`）和 `/plugins@` 插件命令（`runtime_db/host_command_execution`）也经过同一个 `ToolExecutor`，只是工具对模型不可见。所以“绕开”不能靠“不走执行器”，要靠结构化来源。
  - 做法：`ToolExecutorRequest` 新增 `call_origin`，取值 `model`（主代理、子代理、决策模型自动执行）或 `host_command`（用户发的宿主命令）。上面两条宿主命令路径显式设成 `host_command`；没设的一律按 `model`（宁严勿松，新路径忘了设只会多问，不会漏问）。收紧钩子只对 `model` 生效。
  - `/settings`、`/approve`、`/deny` 是 Gateway 控制命令，不经执行器，本来就不受影响。
  - **前提（9b 复核）**：`call_origin=host_command` 免收紧的可信度，依赖 Gateway 确认“这条请求确实来自用户本人的通道”。Gateway 现有的本机来源信任规则和“模型命令沙箱默认能联网”之间有冲突（早已存在，同样影响 H3、审批和 owner 墙的前提），列为 3a 待定项。定下来之前 B5 按现状验收。
  - 插件管理工具对模型保持不可见（现状 `ToolExposure(model_visible=False)`），模型停用、卸载不了收紧它的插件。
  - 用例：收紧插件对所有危险工具一律 `deny` 时，用户在 TUI 和 IM 里仍能用 `/plugins disable` 停掉它；模型可见工具表里没有任何插件管理工具。

### 8.2 合并规则（只能更严）

| 宿主 | 插件里最严的 | 结果 |
| --- | --- | --- |
| allow | allow_as_is | allow |
| allow | ask | ask（插件要求的确认） |
| allow | deny | deny（`PLUGIN_GATE_DENIED`） |
| ask | allow_as_is | ask（宿主原有的确认） |
| ask | ask | ask（一次弹框，同时写明宿主和插件的要求） |
| ask | deny | deny |
| deny | 不问 | deny |

多个插件并行问，取最严；同样严时按插件 ID 排序取第一个做主原因（结构化顺序，不看并行返回的先后，账本才稳定），账本里每个插件都记。

### 8.3 超时和失败

- 钩子总预算 `plugin_tool_gate_timeout_ms`（默认 2000，第 11 节），连接启动也算在内。
- 超时、插件报错、回复不合规、握手没声明 → 这个插件按 `ask` 处理，原因码 `PLUGIN_GATE_TIMEOUT` / `PLUGIN_GATE_ERROR` / `PLUGIN_GATE_MALFORMED` / `PLUGIN_GATE_UNAVAILABLE`（goal：收紧类超时就按“加确认”处理）。
- 征询途中插件代次失效：
  - 停用或卸载 → 它的回答作废，**不算它的要求**（停用即撤销）；
  - 换成了新的激活实例（升级、重新启用）且新实例仍收紧这个工具 → 在剩余预算里问新实例，预算不够按 `ask`（9b 评审），不能就此回到宿主决定把它放过去。
- 每个插件同时一个在途征询；排队等待也算在预算里。

### 8.4 审批：写明是哪个插件，并且不能被自动批准

插件要求确认时，执行器把要求放进结果的结构化元数据 `plugin_requirements`：`[{plugin_id, version, gate_id, reason_code, message}]`。`_resolve_tool_approval` 据此：
- 审批描述前加“[插件 <插件名> 要求确认：<原因码> <消息>]”，TUI 审批框和 IM 那行“代理请求：…”都看得到（IM 摘要取描述前 200 字，前缀放最前）；
- `binding` 里加 `plugin_gate_ref`（这次要求的结构化摘要：插件编号、激活编号、gate、原因码，加这一次调用的 `operation_id`、`call_id`、幂等键、参数哈希），`permission_id` 照旧在加之前算好；
- 选项只给“允许这一次”和“拒绝”，去掉“本会话都允许”和“以后都允许”（照 `host_command_approval` 去掉选项的做法）。

### 8.5 绕开四种自动批准

判断“这次审批带插件要求”只读结构化字段 `plugin_gate_ref`，由同一个函数（`plugin_gate_required(request)`）给出；下面每个自动批准入口都先调它，不各写一份判断（9b 评审）。带 `plugin_gate_ref` 的审批请求：
- 自主模式（auto）也弹框：`ActionPolicy` 层不能把它当普通 `ask` 跳过，执行器在合并后直接产出“强制确认”；
- `StreamApproval` 不查会话缓存、不查长期授权，也不把这次批准写进会话缓存；
- 等待期间自主模式提供者（`autonomous_tool_decision`）不对它给决定；
- 不可交互的场合（后台任务、定时任务、非管理员 IM、没有审批通道的子代理）照现有规则返回“无法审批”，工具不执行（宁严勿松）。插件能从 `call.interactive` 看到这一点，可以自己选 `deny` 而不是 `ask`。
- 这种“被插件挡住、又没人能批”要看得见（ae 评审，3a 定 D2）：TUI 和 IM 都显示“插件 X 要求确认，但这里无法审批，所以没执行”，工具结果错误码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`（带插件名和插件给的原因码）；后台任务的失败原因带同样的事实；`/plugins info` 的最近决定里单独计“无法审批”的次数。
- 决策模型自动执行（J16 片 D，`action_candidate_auto_execute_enabled`）的调用同样经收紧钩子（`actor=decision`），插件要求的确认不被它的自动执行跳过（9b 评审）。
- Gateway 重启后续跑（I4）：挂着的插件确认不能丢、也不能当成已批准；续跑回合里同一调用要重新征询插件（9b 评审）。

### 8.6 批准后重跑

用户批准后同一调用重跑（现有机制）。重跑时：
- 对 `plugin_gate_ref` 已被批准的插件，**不再问**（防止插件每次都要确认、永远跑不下去）；仍检查该插件还在启用，停用了就不算它的要求。
- 跳过的条件是 `plugin_gate_ref` 和**这一次**调用的精确身份完全一致：`operation_id`、`call_id`、幂等键、`args_hash`，加插件激活编号和 `gate_id`（ae、9b 评审）。不能只按“这个插件批准过”或“插件 + 参数哈希”就跳过，否则同一个 run 里模型过一会儿再发一条一模一样的 `rm -rf`，会继承上一次的批准。用例：批准后同一调用重跑不再问；之后再发的同参数新调用、不同参数的调用都要重新问。
- 其它插件照常问（比如批准期间新启用的插件）。

### 8.7 拒绝时模型看到什么

- 工具结果 `error_code = PLUGIN_GATE_DENIED`（登记进错误合同：权限类、不可重试、建议换策略），输出文字“插件 <插件名> 拒绝了这次调用（原因码 <码>）”；
- 拒绝发生在执行器决定阶段（写账前），不会被工具协调器归成“结果未知”；
- 用户拒绝插件要求的确认，和现在一样是 `APPROVAL_REJECTED`。

## 9. 账本与展示

**决定账本**：每次收紧征询一条 `runtime_events`，`event_type = "plugin_gate.decided"`，挂在调用所在的 attempt / run 上（工具调用一定在 run 里）：

```json
{"plugin_id": "rm-guard", "version": "1.0.0", "activation_id": "…", "gate_id": "guard-rm",
 "tool": "run_command", "call_id": "…", "operation_id": "…", "args_hash": "…", "actor": "main",
 "outcome": "ok", "verdict": "ask", "reason_code": "RM_RF", "latency_ms": 41,
 "host_status": "allow", "final_status": "ask"}
```

- `outcome` 取 `ok` / `timeout` / `error` / `malformed` / `unavailable` / `revoked`；
- 用户对这次确认的选择，沿用现有审批事实（批准时 evidence 带 `approval_applied` 和 `plugin_gate_ref`）；
- 不写 `message` 原文，只写原因码（消息只用于当次展示）。

**展示**（TUI 和 IM 同一份文字）：
- `/plugins info <插件>` 新增四段：订阅了哪些事件（含正文范围）、收紧哪些工具（含参数范围）、网络和沙箱状态、最近 10 次收紧决定（时间、工具、结果、原因码）和观察计数。
- 审批框和 IM 审批文字带“[插件 X 要求确认：…]”。
- 被插件拒绝的调用，在活动行和工具结果里显示“插件 X 拒绝”。

## 10. 安全底座（M4）

- **第一期只对本机管理员（local/main）开放**（ae 建议，3a 定 D9）：别的 owner 启用 v8 插件直接拒绝，结构化原因码 `plugin_events_owner_not_allowed`；别的 owner 的事件一律不投、调用一律不问。理由：多用户 Gateway 里管理员给别的 owner 装事件插件就能读到那个用户的提示词，这是隐私边界；要放开得单独设计、由被观察的用户自己决定。口径和 J16 的属主范围一样。
- **总开关** `plugin_events_enabled` 默认 false。关着时 v8 包可以安装，但启用被拒（`plugin_events_disabled`，提示去 `/settings` 打开）；已启用的 v8 插件在开关关掉后，事件不投、收紧不问（相当于停用这两项能力），`/plugins info` 显示原因。
- **B1 过渡关闭门（ae 复审、3a 2026-10-03 要求）**：B7 尚未落地时一律按总开关关闭处理；`PluginEnableTool` 对 `manifest.permissions` 非空的包在索取确认码之前返回 `plugin_events_disabled`、`not_started/not_committed`，不解析运行时、不生成候选计划、不准备环境或提交激活；即使带正确码也拒绝，安装仍允许。B7 必须同时替换这道拒绝与构造期计划排除，接入实际开关、local/main 和强制沙箱判定，不能只删除关闭门后让 v8 走无隔离的 v6 链。
- **强制沙箱**：v8 插件不管 `plugin_process_sandbox` 开没开，都进插件进程沙箱；本机沙箱不可用就启用失败（`sandbox_unavailable`），不退回无沙箱。
- **断网**：沙箱规格 `network_access=False`（Linux `--unshare-net`，macOS 最后一条 `(deny network*)`，两边都有现成实现和自检）。声明了 `permissions.network: true` 的才放开，并在确认码事实里列出。MCP 走标准输入输出，断网不影响宿主和插件之间的通信。
- **收窄读**：拒读整个 my-agent 数据根，只放行插件自己的包目录、数据目录和运行环境，宿主配置、会话、记忆都读不到；事件里给什么，插件才知道什么。macOS 用现有的私有读拒绝（`private_read_roots`）加放行即可；Linux 的插件沙箱现在是“整根只读、读范围与宿主相同”，要先隐藏数据根再把插件自己的目录挂回去，具体挂法在 B7 里实测后定（风险项）。macOS 上必须沿用 `attempt/sandbox._private_read_rules` / `_ancestor_metadata_rules`：Seatbelt 对数据根按 subpath 拒读时，会连同放行目录的每一级上级目录的 lstat 一起拒掉，而插件包目录正好嵌在被拒读的数据根里（`data/plugins` 下），Python、node 的 realpath 和 venv 会报 EPERM 起不来（09-26 生产出过，step12s 热修）。两个平台都要有真进程用例，布局必须是“放行目录嵌在拒读根里”：真实解释器（Python、node）启动、realpath、读自己的包和数据目录都成功，读会话和记忆失败；不能只用 cat、printf。
- **网络用例**（9b 评审）：`network: false` 的插件连本机回环也被拒（macOS `deny network*`、Linux `--unshare-net`），用例钉住；`network: true` 的插件能连上本机 Gateway 端口，但读不到令牌（收窄读），调不了要令牌的接口；不要令牌的接口（已知本机 `/status`）返回什么，B7 先列清单并断言不含正文。
- **确认码覆盖订阅**：`confirmation_details` 在 v8 时加四项事实：`events`（类型和正文范围）、`tool_gates`（工具、效果、参数范围）、`network`、`sandbox`（固定为强制）。改了订阅就是换了确认码，必须重新确认。
- **H2 / H3 不受影响**：插件进程自己的文件访问仍受沙箱只读覆盖；插件拿到的事件和参数只是数据，不带任何宿主能力。

## 11. 配置项

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `plugin_events_enabled` | `false` | M 线总开关：开了才允许启用 v8 插件、才投事件和问收紧钩子 |
| `plugin_tool_gate_timeout_ms` | `2000` | 一次工具调用等插件回答的总时间（含连接启动），超时按“要求确认”处理；范围 200–10000 |

观察的 3 秒超时、5 秒退避、120 秒空闲沿用面板服务的常数，不新加配置。两项都按 AGENTS.md 同步 `agent_config.yaml`（中文注释）、`AgentConfig`、参数中心登记和测试。

两项都进管理员边界项 `USER_SETTINGS_BOUNDARY_KEYS`（`settings/user_config_capability.py`，ae、9b 评审，3a 定）：模型经设置工具（`user_config`）改会被拒（`PARAMETER_BOUNDARY`），只有用户本人用 `/settings` 能改，和宿主核验开关同一组。否则模型调一次设置工具把开关关掉，收紧钩子就全失效。B7 加用例。

## 12. 对其它文档的改动

- [PLUGIN_LIFECYCLE](PLUGIN_LIFECYCLE.md) 第 187 行改为：订阅宿主公开事件（只读、结构化、默认无正文）；工具调用前可加只收紧的钩子（照原样 / 加确认 / 拒绝），不能放宽、改写或接管；见本稿。
- [PLUGIN_PROCESS_SANDBOX](PLUGIN_PROCESS_SANDBOX.md)：补“v8 插件强制进沙箱并断网”。
- [PLUGIN_DISPLAY](PLUGIN_DISPLAY.md)：连接管理抽到共用通道后，注明面板和事件共用。
- DESIGN_LEDGER 登记本稿。

## 13. 拆块（给 my-agent 会话实现）

每块独立一条 `worker/*` 分支，按顺序合；每块都要：合同单测 → 假插件进程 → 相关回归 → 变异（至少 4 个）→ 严格门禁。B5、B7 涉及安全裁定，由 be 或 9b 复审。

| 块 | 做什么 | 主要改的文件 | 关键用例 | 依赖 |
| --- | --- | --- | --- | --- |
| B1 清单 v8（已实施：`worker/m1-b1`，原提交 `20a930cd2`，裁定提交 `ad6007106`；ae 意见修订 `d6e9daccb`，ae 复审通过，并入 step17i） | `events` / `tool_gates` / `permissions` 字段、校验、序列化；构建脚本出 v8；确认码事实加四项，写成人能看懂的话（如“能看到这些工具的完整参数：…”仅指 full 收紧门） | `plugin_manifest.py`、`plugin_runtime_facts.py`、`plugin_enable_tool.py`、`scripts/build_plugin_files_package.py` | 每条校验规则正反例（含工具开始 text 被拒、v8 只订阅合法、`arguments: full` 配 `effects` 被拒）；v1–v7 序列化逐字节不变；改订阅确认码就变；v8 + `host_api` 被拒；v8 安装允许但正确码也按关闭拒绝，未开始/未提交，v6 原确认链保持 | 无 |
| B2 共用插件通道（已实施：`worker/ds1-b2-channel`，ds1 `e62593fc6`，ds4b2 复审返工至 `7b21bfe12`；be、ae、9b 复审通过，并入 step17i；实施与返工记录见第 19–21 节） | 把面板服务的连接、代次复核、单在途、空闲关闭抽成 `PluginChannelPool`，面板改用它；启动计入超时 | 新 `plugin_display/channel.py`（或 `plugin_channel/`）、`plugin_display/service.py` | 面板原有用例全部不改照过；启动超过超时记超时、后台继续启动；代次失效丢结果 | 无 |
| B3 事件中心（已实施：`worker/m1-b3-event-hub`，m1b3，提交 `c9571b037`、返工 `11a56269a`、返工 2 `d8970996c`、拆平 `815151369`；9b 复审待做） | `PluginEventHub`：按 owner 分区、按类型只留最新、`dropped_before`、单在途、退避、撤销、计数；`my-agent/events` 握手；`events.observe` | 新 `plugin_events/hub.py`、`plugin_events/protocol.py`，Gateway 组装处 | 假插件：合并与丢弃计数准确；慢插件不拖主线程；停用后不再收到；跨 owner 不串；握手没声明不投 | B1、B2 |
| B4 事件点（m1b4，已实施，待复审） | 6 类事件的投影与接线；只有提示事件声明且同意时给提示正文，观察事件不带工具参数（3a 2026-10-03 裁定）；提示投影统一脱敏；提示 owner 复用请求 worker 的结构化解析，不按频道猜归属 | `gateway_parts/http_handlers.py`（提示）、`gateway_parts/request_execution.py`（回合）、`tooling/executor.py`（工具）、`gateway_parts/control_operation_service.py`（命令） | 每类事件字段齐全；默认不含正文（带标记正文反证）；工具观察不含参数；带标记的凭据和 `__` 内部键反证；被拒的调用不发工具事件；`actor` 三种取值；随机回环 HTTP 服务→真实 worker 认领三事件顺序及 local/tui 跨 owner 不发提示 | B3 |
| B5 收紧钩子 | `PluginToolGate`、合并规则（同严按插件 ID 排序）、超时按确认、代次撤销与新实例重问、重跑只对同一次调用不重问；`call_origin`；审批前缀、去掉会话/长期选项、单一 `plugin_gate_required` 判定绕开缓存/授权/自主；`PLUGIN_GATE_*` 错误码登记 | `tooling/executor.py`、新 `plugin_events/tool_gate.py`、`agent_core/tool_loop/round_execution.py`、`contracts/tool_approval.py`、`gateway_parts/stream_approval.py`、`user_space/approval_mode.py`、`contracts/error_taxonomy.py`、`plugin_management.py` 与 `runtime_db/host_command_execution.py`（设 `call_origin=host_command`） | 第 8.2 表 7 种组合；自主模式、会话缓存、长期授权、决策模型自动执行下仍弹框；I4 续跑回合重新征询；超时 → 确认；两个并行调用撞上同一慢插件，排队那条超时按确认；插件回复多带字段（如 `arguments`）被忽略、参数不变；征询中停用 → 不算、换新实例 → 问新实例；批准后同一调用重跑不再问，之后同参数新调用、不同参数照样问；不可交互 → 不执行、错误码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`、账本记 `final_status`；收紧插件一律 deny 时用户仍能 `/plugins disable`、模型工具表里没有插件管理工具；模型发起的调用在工具参数里塞 `"call_origin": "host_command"` 或 `__call_origin` 照样被收紧（来源只读宿主设在 `ToolExecutorRequest` 上的字段，不从参数读；ae 补，配变异“执行器从参数读来源”）；拒绝时模型看到 `PLUGIN_GATE_DENIED` 且不是“结果未知” | B1、B2；H3 已合入 |
| B6 账本与展示 | `plugin_gate.decided` 写入与查询；`/plugins info` 四段（含“无法审批”计数）；IM 同文 | `runtime_db/repository.py`（查询方法）、`plugin_commands.py`、`plugin_management.py` | 每种 outcome 一条且字段齐全；不写消息原文；TUI 与 IM 输出相同；最近 10 条按时间倒序；“无法审批”单独计数 | B5 |
| B6 账本与展示（查询与展示半，已实施：`worker/m1-b6-ledger-display`，m1b6，提交 `d9f158e8d`、文档补丁 `e80db5918`，基于 B3 返工头 `0a3064078`；写账半待 B5 合入） | 不依赖 B5 的部分：`plugin_gate_decisions`（最近 N 条、默认 10、`seq` 倒序、字段白名单，`message` 不外泄）、`plugin_gate_unavailable_count`（累计，不设窗口）、`/plugins info` 四段（订阅/收紧/网络沙箱/最近决定+计数）、观察计数读 B3 `hub.stats`（缺 hub 或读失败写“暂无记录”）、hub 经 `plugin_command_service → control_service → control_operation_service → http_handlers` 全链透传 | `runtime_db/repository.py`、`plugin_commands.py`、`plugin_management.py`、`plugin_events/confirmation.py`、`gateway_parts/{plugin_command_service,control_service,control_operation_service,http_handlers}.py` | 见 `tests/test_plugin_event_display.py` 12 项与 6 个变异；测试库直插第 9 节字段代 B5 写入 | B5（仅写账半） |
| B7 安全底座 | 将 B1 暂时拒绝与构造期 v8 计划排除换成真正总开关、local/main 和强制沙箱判定，不能只删关闭门；v8 强制沙箱、断网、收窄读（Linux 挂法先实测，macOS 沿用 `_ancestor_metadata_rules`）；开关关时拒绝启用；沙箱不可用时失败；两个配置项进管理员边界项 | `plugin_sandbox.py`、`plugin_runtime.py`、`plugin_enable_tool.py`、`settings/config.py`、`settings/user_config_capability.py`、`config/agent_config.yaml` | 真实沙箱（macOS / Linux 车道）真进程，布局为“放行目录嵌在拒读根里”：Python、node 解释器启动和 realpath 成功，读得到自己的包和数据目录、读不到会话和记忆；`network: false` 连外网和本机回环都被拒；`network: true` 能连 Gateway 端口但读不到令牌、调不了要令牌的接口，不要令牌的接口列清单；开关关 → `plugin_events_disabled`；模型经 `user_config` 改两个配置被拒（`PARAMETER_BOUNDARY`）；非 local/main 启用被拒（`plugin_events_owner_not_allowed`） | B1；H3 已合入 |
| B8 样例与验收（样例已实施：m1b8，`worker/m1-b8-samples`，基于 `claude/3a-step17i` `c47d023b6`；删除门改拦 `apply_patch` 删除段：b8dg，2026-10-03；真实验收待 B3–B7 合入后另派） | 样例 A：观察全部 6 类事件并在面板显示计数；样例 B：`run_command` 含 `rm -rf` 时要求确认、对 `apply_patch` 补丁里的删除文件段（`*** Delete File: `）直接拒绝；Python 和 Node 各一份 | `plugins/event-watch/`、`plugins/rm-guard/`、`plugins/rm-guard-node/` | 假模型真进程：TUI 与 IM 都看到“插件 X 要求确认 / 拒绝”；真实验收见第 14 节 | B3–B7 |
| B9 写插件的技能（M5；已实现（`worker/sol2-m5`，`de395322e`），待 be 复审） | 内置“写 my-agent 插件”技能、作者合同和 v8 文件模板（Python、Node 各一个）；安装启用仍只能由用户输确认码 | `agent_py_agent/skills/builtin/plugins/write-my-agent-plugin/`、`test_write_my_agent_plugin_skill.py` | 两语言 v8 ZIP 真构建/读回/包内 stdio；只订阅合法、模型管理入口不可见；四类双语言变异全部抓到；旧新构建器字节回归保留。B7 生产启用与隔离未验证，门禁例外见 TESTS | B1 `add244a92` |

## 14. 验收（goal 第四节）

- 假模型真进程：B8 两个样例在 TUI、IM 两条路径各跑一遍。
- 真实验收：隔离 home、MiniMax M2.7，TUI 和飞书各一遍：让模型删目录时弹出“[插件 rm-guard 要求确认：RM_RF …]”，选拒绝后模型收到 `APPROVAL_REJECTED`；让模型用补丁删一个文件（`apply_patch` 的 `*** Delete File: ` 段）时直接收到 `PLUGIN_GATE_DENIED`；`/plugins info` 能看到最近决定和观察计数。
- 全量 + Linux 车道全绿；每块变异全部抓到。

## 15. 决定点（3a 2026-10-03 已定）

| 编号 | 问题 | 结论（3a 2026-10-03 定） |
| --- | --- | --- |
| D1 | v1–v5 Python 轮子包能不能订阅 | 不能；用 v6 解释器入口打包，过确认码 |
| D2 | 不可交互场合插件要求确认怎么办 | 照现有规则“无法审批、不执行”；TUI 和 IM 看得到“插件 X 要求确认，但这里无法审批，所以没执行”，码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE` |
| D3 | 钩子超时默认值 | 2000 ms，可配 200–10000，管理员边界项，模型不能改 |
| D4 | 正文给不给、给多少 | 3a 2026-10-03 裁定：观察正文只允许 `prompt_submitted` 的提示文字，截 4000 字，要声明且确认码同意；`tool_call_started` 只能 none。工具参数只通过 `tool_gates.arguments: "full"` 对精确列出的工具给，确认码列明工具，去内部键、统一脱敏后截 4000 字；第一期不给工具输出 |
| D5 | `host_api` 能否和事件同时用 | 第一期不能 |
| D6 | 观察计数要不要落盘 | 不落盘；决定落 `runtime_events` |
| D7 | 本地直连 TUI 的提示、回合、命令事件 | 第一期只有工具两类 |
| D8 | 收紧钩子能不能看子代理的调用 | 能，`actor` 分 `main` / `subagent` / `decision` |
| D9 | 第一期开放范围 | 只 local/main；别的 owner 启用直接拒绝，码 `plugin_events_owner_not_allowed` |

**订阅贡献补充决定（3a 2026-10-03 裁定）**：v8 非空 `events` 或 `tool_gates` 算贡献，允许没有工具和面板的观察插件或 rm-guard；v1–v7 不改，v8 四项全空仍由“必须声明订阅”门拒绝。

## 16. 已知边界

- **观察事件不带工具参数（第二期再议）**（3a 2026-10-03 裁定）：第 6 节原要求只向精确列出的工具给正文，但 `events` 没有工具列表；第一期验收不要求观察参数，也不新增 `events[].tools`。需要参数的插件只能用精确工具的 full 收紧门；第二期有真实需求再扩展观察格式。
- 插件进程能拿到它订阅的事实和（同意过的）正文，这些数据在插件那里怎么用，宿主管不到；断网和收窄读是为了让它带不出去、也读不到更多。
- 收紧钩子增加的是延迟：每个命中的调用最多多等 `plugin_tool_gate_timeout_ms`。只订阅需要的工具，不订阅读工具，影响很小。
- 观察事件会合并丢弃（只留最新），插件不能把它当完整审计日志；需要完整记录的应读宿主账本（第二期可开放只读查询）。
- **发送任务的线程数是全局上界**：每个（owner, 激活）一个在途任务、线程池 4 个（与面板服务同档）；挂住的插件数到达 4 个时，后面的发送任务要排队，仍会互相影响。这是面板服务已有的同类边界。
- **hub 不看 `plugin_events_enabled`**（给 B4、B7 的接线提示）：开关关掉时 hub 仍会读安装表（每次发布触发一轮）。B4 在事件点应先看开关再调 `publish_plugin_event`；B7 的「开关关闭就不能启用」保证不会真的投递，但读表的开销省不掉。
- Windows 上宿主命令不进沙箱，插件进程沙箱同样不可用，v8 插件在 Windows 上启用失败。
- **收紧钩子是护栏，不是安全边界**（9b 评审）：插件拒了补丁里的删除段，模型还能用 `run_command rm` 达到同样效果。插件作者要按 `effects` 订阅才能盖住同类操作；宿主的安全边界仍是 H2、H3、审批策略和沙箱。
- **宿主命令来源的可信度**（9b 复核，3a 待定）：用户命令免收紧，前提是 Gateway 能确认调用方是用户本人；本机来源信任与模型沙箱可联网之间的冲突，由 3a 定方向（比如收紧本机来源信任条件，或让模型沙箱连不到 Gateway）。
- **账本可信依赖 H3**（9b 评审）：第 9 节的 `runtime_events` 和审批决定文件（`workspace/runtime/services/gateway` 下）不被模型篡改，是靠 H3“宿主运行状态对模型只读”保证的。所以 M 线 B5–B8 排在 H3 合入之后。

## 17. 评审记录

- **ae 的 B1 复审（3a 2026-10-03 转述，修订已由 ae 复审通过）**：常数目录重新生成并同时过 `--check` 与测试；B7 前 v8 启用按总开关关闭拒绝；探针和分支增量不保存本机路径。ae 沙箱外核对 `test_plugin_any_language.py`：基线 `274cedb1e` 和 B1 头 `bf520e911` 均 **35 passed**，六失败来自 my-agent 命令沙箱环境；旧版字节兼容和原四变异已独立确认，本线不重复调查、不冒充亲自沙箱外验证。

- **ae（2026-10-03）**：总体同意（只看、只收紧；面板通道共用；收紧决定进 `runtime_events`；v8 强制沙箱）。必须改 5 条已改：
  1. 工具参数投影去 `__` 内部键、走统一脱敏（第 5、6 节）；
  2. `arguments: full` 只能配精确工具名（第 4 节）；
  3. 用户的管理命令永远不受收紧，加 `/plugins disable` 用例（8.1）；
  4. 两个配置项进管理员边界项（第 11 节）；
  5. 重跑不重问只对同一次调用（8.6）。
  建议 5 条已吸收：`actor` 加 `decision`（第 6 节）；第一期只 local/main（第 10 节，D9）；“无法审批”可见（8.5）；macOS 上级目录元数据与两平台真进程读用例（第 10 节）；同严按插件 ID 排序取主原因（8.2）。D1–D8 ae 全部同意。
- **9b（2026-10-03，评审的是初稿 8b6344045）**：方向对，可以按稿拆块。必须改 4 条已改：
  1. 8.6 批准后不重问绑定到这一次调用的精确身份（`operation_id`、`call_id`、幂等键、参数哈希、激活编号、`gate_id`），同参数新调用重新问；
  2. 两个配置项是参数中心边界键，模型经 `user_config` 改被拒；插件管理工具对模型不可见。评审时发现用户的 `/plugins` 管理命令也经执行器，所以 8.1 改为按结构化来源 `call_origin` 只收紧模型发起的调用；
  3. macOS 收窄读沿用 `_ancestor_metadata_rules`，B7 用“放行目录嵌在拒读根里”布局跑真实解释器启动和 realpath；
  4. 8.5 单一 `plugin_gate_required` 判定，决策模型自动执行和 I4 续跑两条入口钉住。
  建议 6 条：代次换成新实例时问新实例（8.3）；护栏不是安全边界（第 16 节）；参数统一脱敏（与 ae 第 1 条相同，已做）；B5 补三条用例（并行慢插件、多余字段、不可交互记 `final_status`）；B7 补网络和令牌用例（第 10 节）；账本可信依赖 H3、M 线排在 H3 之后（第 16 节）。全部吸收。
- **3a（2026-10-03）**：D1–D9 按上表定；ae 的 5 条必须改全部接受；第一期只开放 local/main；B2 先派。
- **ae 复核 991b198b3（2026-10-03）**：同意；补 B5 用例“参数里伪造来源照样被收紧”（已写进第 13 节 B5 行）。
- **9b 复核 991b198b3（2026-10-03）**：通过；请写明 `host_command` 免收紧依赖 Gateway 的调用方身份确认（已写进 8.1 和第 16 节，3a 待定项）。

## 18. 派活清单

- **基线**：B1、B2、B3、B4、B9 用派活时 `claude/3a-step17h` 的最新头；B5、B6、B7、B8 用 H3 合入之后的 `claude/3a-step17h` 头（账本和审批文件的完整性依赖 H3）。
- **分支**：每块一条 `worker/m1-<块>`，在各自 `~/my-agent-worktrees/worker-*` 工作树上做，按 myagent-dispatch/tasks/common.md 的规则（不碰其它目录、不推远端、不启停 Gateway、不读真实会话和记忆正文）。
- **每块交付**：合同单测 → 假插件进程 → 相关回归 → 变异（至少 4 个）→ 严格门禁，交复审人，3a 挑。

| 块 | 分支 | 可开工条件 | 复审 |
| --- | --- | --- | --- |
| B1 清单 v8 | `worker/m1-b1`（已实施，原提交 `20a930cd2`，裁定提交 `ad6007106`，追加 ae 意见修订） | 已并入 step17i | ae（原提交、裁定和本轮修订一并复审） |
| B2 共用插件通道 | `worker/ds1-b2-channel`（已实施，头 `7b21bfe12`） | 已并入 step17i | be、ae、9b |
| B3 事件中心 | `worker/m1-b3-event-hub`（已实施，头 `815151369`，待 9b 复审） | 已合入（B1、B2） | 9b |
| B4 事件点（m1b4，已实施，待复审） | `worker/m1-b4-event-points` | 已变基至 `0ae0efe5e`（含 B3 最终版）；Gateway 与三来源装配、拒绝组合已实施；luna6 完整初审的 HTTP 覆盖与 owner 错投尾补见 TESTS | luna6 初审尾补待复审，3a 把关 |
| B5 收紧钩子 | `worker/m1-b5-tool-gate` | B1、B2、H3 合入 | be、ae |
| B6 账本与展示 | `worker/m1-b6-ledger-display`（查询与展示半已实施，头 `d9f158e8d`、文档补丁 `e80db5918`，基于 B3 返工头 `0a3064078`；写账半等 B5 合入后接） | B5 合入（写账半）；查询与展示半已先行实施 | 9b |
| B7 安全底座 | `worker/m1-b7-sandbox` | B1、H3 合入 | 9b、ae |
| B8 样例与验收 | `worker/m1-b8-samples` | B3–B7 合入 | be（真实验收 be 做，TUI 和飞书各一遍） |
| B9 写插件的技能 | `worker/sol2-m5`（已实现（`worker/sol2-m5`，`de395322e`）） | 已变基到 B1 合入头 `add244a92`；独立包/stdio 已验，生产启用仍由 B7 接线 | be（待复审；clean-package 运行产物例外见 TESTS） |

## 19. B2 实施记录（ds1，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `e62593fc6`；复审返工见第 20 节）

- **选了哪个目录**：用 `plugin_channel/` 包（不是 `plugin_display/channel.py`）。理由：B3 事件中心、B5 收紧钩子都要用它，放在 `plugin_display/` 下会让事件线反向依赖展示包；独立包保持单向依赖（`plugin_display` → `plugin_channel`，通道不依赖任何调用方），也方便后面按 `plugin_channel` 做导入边界检查。
- **文件**：新增 `agent_py_agent/agent/plugin_channel/{__init__.py,pool.py}`，新增 `agent_py_agent/tests/test_plugin_channel_pool.py`；`plugin_display/service.py` 改为持有 `PluginChannelPool`；常数（3 秒请求超时、5 秒退避、120 秒空闲）取值不变，改为在通道里定义、面板用别名引用。
- **对外接口（B3 复用）**：`PluginChannelPool(client_factory, clock)`；`acquire(owner_key, installation, now)` 取或建连接；`request(connection, ChannelCall(owner, method, params, timeout, before_send))` 一次完整请求（含启动）；`retire_stale`、`close_idle`、`close` 管回收；异常 `PluginChannelTimeout` / `PluginChannelRevoked` / `PluginChannelBackoff` 分别表示超时、代次失效、退避期内。面板专属的待发合并、结果缓存、退避重试都留在 `plugin_display/service.py`。
- **补的缺口**：连接启动在后台线程里做，等待只到本次超时预算；超时就按超时返回，后台继续启动，下一次请求复用同一条连接（不再被 30 秒 MCP 连接超时挡住 3 秒的渲染预算）。
- **验证**：面板原用例 21 项一行未改全过（另加 1 项服务侧撤销用例，共 22 项）；4 个真实插件包用例 22 项通过；新增池用例 7 项；5 个定点变异（不复核代次、允许多在途、启动不计超时、空闲不关、撤销不关连接）全部被检出，源码哈希还原一致。真实插件进程、真实 TUI/IM 与宿主启用链未验证（本沙箱不起真插件）。

## 20. B2 复审返工（ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，接 `af3dd299b` 之后）

be 复审（`~/.my-agent/decision-evidence/m1-b2-review-20261003/`）判定不能挑：4 个竞态探针全失败、10 个变异只杀死 4 个。返工内容：

- **关闭竞态（探针 1–4）**：`_publish_transport` 改成在池锁内核对"连接仍在表里且客户端未换"，失败就返回 False；`_start_worker` 拿到 False 在锁外停掉刚启动的客户端并按撤销返回。池加 `_closed` 终态标记，`acquire` 与 `_ensure_started` 见到它、或连接不在表里，一律抛 `PluginChannelRevoked`。`close()` 像 `retire_stale` 一样逐条 `_detach`（置空 client/transport）。`_require_current` 对已回收（client 为 None）或无 `activation_ref` 的连接按撤销处理，不再抛 `AttributeError`。
- **共用池（第 4 条）**：`plugin_panels_http.plugin_channel_pool(server)` 建进程内唯一池挂在 server 上（创建服务走锁内不重入的内部函数，避免自死锁），池注入 `PluginDisplayService`；`GatewayHTTPServer` 在 `stop()` 关池，面板服务 close 只关自建的池。**给 B3 的入口**：事件中心要连接时用 `plugin_panels_http.plugin_channel_pool(server)` 取同一个池，不要自己 new。
- **退避语义变化**：退避状态本来就存在连接上，但返工把"共用一个池"落到实处后，同一插件的两个面板真的共用一条连接，所以一个面板遇到**连接级**故障会让该插件的其它面板一起等 5 秒（请求级错误不在此列，见第 21 节）。
- **验证**：4 个探针原样文件全过并转正为 `tests/test_plugin_channel_lifecycle.py`；10 个变异全部被杀死；相关 4 个用例文件 55 项通过；guards9 172 项通过；详见 TESTS.md 顶部「B2 复审返工」小节。


## 21. B2 二轮返工（ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，接 `fcb638182` 之后）

ae 复审（`~/.my-agent/decision-evidence/m1-b2-rereview-20261003/`）判定 B2 要真正被第二个调用方（B3 事件、B5 收紧钩子）用起来，还差两处，另有三条小修：

- **面板服务不再摘别人管的连接**：`PluginDisplayService.panels` 交给共用池的"有效激活集合"改成**这个 owner 全部已启用的激活**（不再只有带面板的插件）；安装表读不到时返回 `None`，`_retire` 收到 `None` 就**一条连接都不回收**（不清缓存、不 `retire_stale`、不 `close_idle`）。以前这两种情况都会把共用池里事件中心的连接摘掉、进程停掉。
- **退避只针对连接级故障**：`PluginChannelPool.request` 只在异常属于连接级时才给整条连接退避；判定走 `_is_connection_failure(exc)`，只看结构化类型（`PluginChannelTimeout`、`OSError` 家族）与错误码（`_CONNECTION_FAILURE_CODES`），不读错误文字。为此：
  - 远端对单个请求回的 JSON-RPC 错误改用**独立的** `MCP_REMOTE_ERROR` 码（`mcp_protocol.unwrap_jsonrpc`，原先是 `MCP_PROTOCOL_ERROR`，与本地帧错误混在一起没法区分）；该码已在 `mcp_registration._ERROR_CODE_MAP` 登记。
  - 连接启动失败统一包成 `PluginChannelStartFailed`（新异常类，连接级），让退避判定不必猜原始异常类别。
  - 请求级失败（`MCP_REMOTE_ERROR`、调用方 `before_send` 校验拒绝）只算这一次请求失败，抛给调用方，由面板服务按面板 `retry_after` 自己退避。
- **`_exchange` 锁内取客户端**：新增 `_current_client(connection)`，在池锁内取一次 client 并核对"连接仍是表中当前对象且 client 非空"，不成立抛 `PluginChannelRevoked`；`before_send` 改用它的返回值。以前在锁外读 `connection.client`，并发摘除会传 None，让面板把"连接被回收"误判成"插件未声明展示能力"。
- **`last_used` 使用约定（B3 必读）**：`request()` **不刷新** `last_used`，空闲回收只认 `acquire`。所以 **B3 每次请求前必须按这个顺序用**：
  1. `pool = plugin_panels_http.plugin_channel_pool(server)` —— 取进程内唯一的共用池，不要自己 new；
  2. `conn = pool.acquire(owner_key, installation, now)` —— 取/建连接，**同时刷新使用时间**；
  3. `pool.request(conn, ChannelCall(owner, method, params, timeout=...))` —— 发请求；
  4. 停用/换代由池自己按撤销处理；空闲回收由 Gateway 的既有轮询调 `close_idle`。
  直接用旧连接对象反复 `request`（跳过 `acquire`）会让连接被空闲关闭提前判成长期未用。
- **回收责任划分（B3 必读）**：面板服务只回收**自己管的**失效连接（靠它自己的展示槽表判断"这条连接是我建的"），
  别人建的连接它一律不碰。所以 **B3 建的连接，B3 必须自己回收自己的失效连接**（激活停用/换代时调池的
  `retire_stale`，或让请求前后的代次复核把失效连接按撤销关掉），**不能指望面板服务替它回收**——
  面板服务看不到、也不该猜别人的连接归属。等 B3 有了真实需求，再考虑在池上加结构化的调用方标记。
- **取快照，不要遍历池的活字典**：需要遍历某个 owner 的连接键时用 `pool.owner_keys(owner_key)`，
  它在池锁内返回不可变元组；直接遍历 `pool.connections` 会撞上并发撤销/新建抛
  `RuntimeError: dictionary changed size during iteration`。
- **停机后池拒绝新建（B3 必读）**：`GatewayHTTPServer.stop()` 会在 `_SERVICE_LOCK` 内给 server 置
  "插件通道已关闭"标记，之后 `plugin_channel_pool(server)` 与 `plugin_display_service(server)` 一律抛
  `PluginChannelRevoked`，不会再建出一个没人关的池。`server_close()` 不等还在跑的工作线程，
  所以 B3 在停机排空阶段取池会撞到这个标记——这是预期行为，按撤销处理并停止投递即可，不要重试。
- **回收要有正面过期证据（B3 必读）**：池的 `retire_stale(owner_key, valid_ids, scope)` 带回一个
  `RetireScope`：`managed` 是本调用方管的激活、`created_before` 是读有效集合那一刻的池内创建序号。
  只有"归自己管 **且** 建在这个时间界之前 **且** 不在 valid 里"的连接才会被摘。B3 回收自己的连接时
  也照这个来（先 `pool.current_seq()` 取界，再读自己的有效集合），否则会摘掉别的调用方刚为新启用
  激活建的连接。
- **删掉 `_new_pool` 转发**：`plugin_panels_http` 里只保留 `plugin_channel_pool(server)`（对外、自己拿锁）与 `_locked_shared_pool(server, ...)`（持锁内部用），服务创建处直接调后者。
- **验证**：ae 两条探针原样文件全过并转正为仓库用例；be 的 10 个变异连同本轮新增的 8 个共 18 个全部被杀死（含"`owner_keys` 交出活字典""服务改回直接遍历池活字典"）；详见 TESTS.md 顶部「B2 三轮返工」小节。
