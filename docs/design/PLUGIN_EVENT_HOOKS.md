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

## 6. 事件目录 v2

公共字段：`event_id`、`type`、`seq`（按「owner × 插件激活」各自计数、从 1 开始单调递增；换代重装后从 1 重新开始——插件从序号里看不出没订阅的类型或别的 owner 发生过什么）、`occurred_at`、`dropped_before`（这个插件的槽建立之后、同类事件被合并掉没送达的条数：中途启用从当前最新一条开始收、第一条为 0；换代丢旧待发后同样从 0 起算）、`channel`（`tui` / `feishu` / …）、`thread_ref`（**会话线程** `ConversationStore thread_id` 的哈希，不是原编号；工具事件与回合事件同源；`prompt_submitted` 在提交时点也解析会话线程、与回合事件同一非空引用，解析不出线程时不发事件）、`channel_conversation_ref`（**渠道会话号**的哈希，同一哈希规则；只有 Gateway 侧事件有值，工具事件为空）、`actor`（`main`、`subagent` 或 `decision`；`decision` 是决策模型的宿主自动执行，J16 片 D 也走同一个执行器）。**默认不含任何正文、路径、参数或输出。**

**v1 → v2（tref2，2026-10-05）**：v1 的 `thread_ref` 在两类事件上混用了两个不同的来源——Gateway 侧（`prompt_submitted` / `turn_started` / `turn_ended` / `command_executed`）取渠道会话号（`channel_conversation_id`），工具侧（`tool_call_started` / `tool_call_finished`）取会话线程（task attribute `conversation_thread_id`），插件想按会话归组会归不上。v2 把 `thread_ref` 统一为**会话线程**的哈希（工具事件原样；Gateway 侧改取回合解析出的线程，`command_executed` 用回执里执行时解析的 `conversation_thread_id`），并新增 `channel_conversation_ref` 承载渠道会话号。已安装插件无需改代码（字段名与值形态不变，新字段可忽略）；若插件按旧 `thread_ref` 建过会话索引，升级切换点会断一次。宿主账本不记录观察事件（设计 D6），无需回填。**tref2b 修正（2026-10-05）**：`prompt_submitted` 在提交时点解析会话线程、不再留空；解析不出线程时不发事件——"每个回合事件都有非空会话引用"是 m1_joint 合同；关闭时零解析不变。

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

- **tref3（2026-10-05）**：所有经 `gateway_event_context` 装配的 Gateway/工具事件都必须有非空 ConversationStore `thread_id`；线程未解析时返回 `None`，只记固定 `PLUGIN_EVENT_THREAD_UNRESOLVED` 结构化诊断（不含渠道、会话号或错误正文）。事件开关关闭时先短路，不读路由、不解析线程。`turn_started` 无上下文时，`turn_ended` 自然不发布；结构化 `restart_resume` 与 `provider_transient_resume` 均令回合状态为 `interrupted`。

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
  - 事实（3a 2026-10-03 补充，m1b5 静态核对）：用户的 `/plugins` 管理命令在 `plugin_management._prepare` 构造请求，`/plugins@` 插件命令在 `plugin_invocation._prepare_invocation` 构造请求；两者都经过同一个 `ToolExecutor`，只是工具对模型不可见。所以“绕开”不能靠“不走执行器”，要靠结构化来源。
  - 做法：`ToolExecutorRequest` 新增 `call_origin`，取值 `model`（主代理、子代理、决策模型自动执行）或 `host_command`（用户发的宿主命令）。只在上述两个宿主命令构造点显式设 `host_command`；`tooling/registry.py` 的日常模型构造点走默认 `model`。没设的一律按 `model`（宁严勿松，新路径忘了设只会多问，不会漏问）；工具参数里的 `call_origin`、`__call_origin` 无权改变来源。收紧钩子只对 `model` 生效。
  - 构造点盘点：日常业务有上述三个构造点；全仓静态 AST 另有 `contracts/main_agent_foundation_contract_cases.py` 的合同自检构造，它保留默认 `model`，不是宿主命令豁免入口。`runtime_db/host_command_execution.py` 接收调用方的 `prepare` 结果并经 `dataclasses.replace` 补操作账与执行上下文，不是 `/plugins@` 的请求构造点，也不能仅凭进入这个模块就升级来源为 `host_command`。
  - `/settings`、`/approve`、`/deny` 是 Gateway 控制命令，不经执行器，本来就不受影响。
  - **前提（9b 复核）**：`call_origin=host_command` 免收紧的可信度，依赖 Gateway 确认“这条请求确实来自用户本人的通道”。Gateway 现有的本机来源信任规则和“模型命令沙箱默认能联网”之间有冲突（早已存在，同样影响 H3、审批和 owner 墙的前提），列为 3a 待定项。定下来之前 B5 按现状验收。
  - 插件管理工具对模型保持不可见（现状 `ToolExposure(model_visible=False)`），模型停用、卸载不了收紧它的插件。
  - 用例：收紧插件对所有危险工具一律 `deny` 时，用户在 TUI 和 IM 里仍能用 `/plugins disable` 停掉它；模型可见工具表里没有任何插件管理工具。

### 8.2 合并规则（只能更严）

**full 参数投影完整性（3a 2026-10-03 裁定；17j截断只能更严补充）**：声明 `arguments: "full"` 的请求在 `call` 同时带 `arguments_truncated` 布尔值；脱敏、去内部键后的紧凑 JSON 超出4000字符预算则为 `true`，未截则为 `false`。`arguments: "none"` 不带该字段。只认布尔 `true`，插件应把可见片段结论与 `ask` 合并取更严：已有 `deny` 不降为 `ask`，已有 `ask` 保留原原因码，只将可见 `allow_as_is` 升为 `ask`；不能把不可见尾部当不存在放行。标记不修改原参数或哈希，不塞进工具参数对象。B8样例/B9模板保留17j实现，B5只迁宿主与本设计。

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
- **ds10 初审、3a 采纳**：全部故障与撤销经唯一 `failure_review` 构造，GateReview 构造处保证非 ok 的回复一律 ask、对应固定原因码；revoked 仍由合并剔除。非法解码、超时、错误、不可用及代次校准都不能手工拼 allow_as_is，merge 不负责补救不一致的 outcome/verdict。

### 8.4 审批：写明是哪个插件，并且不能被自动批准

插件要求确认时，执行器把要求放进结果的结构化元数据 `plugin_requirements`：`[{plugin_id, version, gate_id, reason_code, message}]`。`_resolve_tool_approval` 据此：
- 审批描述前加“[插件 <插件名> 要求确认：<原因码> <消息>]”，TUI 审批框和 IM 那行“代理请求：…”都看得到（IM 摘要取描述前 200 字，前缀放最前）；消息必须与 decode_reply、直接 GateReply 共用 `clean_gate_message`，80 字、去控制/双向控制和 Unicode 换行，直接构造 review 不能绕过（ds10 初审、3a 采纳）；
- `binding` 里加 `plugin_gate_ref`（这次要求的结构化摘要：插件编号、激活编号、gate、原因码，加这一次调用的 `operation_id`、`call_id`、幂等键、参数哈希）。`binding` 是 `dict[str, str]`，构造和 `from_mapping` 都会把值转成字符串，所以引用必须先编码成稳定的 JSON 字符串，不能直接塞 dict/list 或把 Python 字符串表示当 JSON；经 `to_dict` / `from_mapping` 往返后仍能解码并核对同一身份。`permission_id` 先按旧算法算好（保留现有 `grant_key` 的处理与哈希顺序），再附加 `plugin_gate_ref`，不能改变旧审批 ID；
- 选项只给“允许这一次”和“拒绝”，去掉“本会话都允许”和“以后都允许”（照 `host_command_approval` 去掉选项的做法）。

### 8.5 绕开四种自动批准

判断“这次审批带插件要求”只读结构化字段 `plugin_gate_ref`，由同一个函数（`plugin_gate_required(request)`）给出；下面每个自动批准入口都先调它，不各写一份判断（9b 评审）。带 `plugin_gate_ref` 的审批请求：
- 自主模式（auto）也弹框：`ActionPolicy._approval_decision` 对非 `always` 工具在 auto 分支先返回放行，宿主精确批准核对并不一定执行；执行器必须把宿主 `allow` 与插件 `ask` 合并成“强制确认”，不能依赖宿主先产出 `ask`；
- **两条审批入口都要守门**（3a 2026-10-03 补充）：Gateway 的 `StreamApproval.request` 与子代理/后台主代理的 `conversation/agent_tool_approval.AgentToolApprovalSinkMixin.request_permission` 都必须在查会话缓存、长期授权之前先调同一个 `plugin_gate_required(request)`。带引用时不查 Gateway 缓存或 `_approved_session_keys`，不调用长期授权放行，也不把本次批准写入会话缓存或 owner 长期授权；只改 Gateway 不算完成；
- 等待期间自主模式提供者（`autonomous_tool_decision`）也先调这个共同判定，不对插件确认给决定；**四处入口都守门**：两处 request，加 `gateway_parts/permission_bridge.wait_for_gateway_permission_decision` 和 `conversation/agent_tool_approval.wait_for_agent_tool_approval` 的等待轮询。两条等待路必须在共同 `plugin_gate_required` 之后才允许模式提供者放行，即使传入自定义提供者也不能绕过；补 Gateway 等待切自主的反证和独立移除该守门的变异（ds10 初审、3a 采纳）；不通过各自复制字段判断制造第二份合同；
- 不可交互的场合（后台任务、定时任务、非管理员 IM、没有审批通道的子代理）照现有规则返回“无法审批”，工具不执行（宁严勿松）。插件能从 `call.interactive` 看到这一点，可以自己选 `deny` 而不是 `ask`。
- 这种“被插件挡住、又没人能批”要看得见（ae 评审，3a 定 D2）：TUI 和 IM 都显示“插件 X 要求确认，但这里无法审批，所以没执行”，工具结果错误码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`（带插件名和插件给的原因码）；后台任务的失败原因带同样的事实；`/plugins info` 的最近决定里单独计“无法审批”的次数。
- 决策模型自动执行（J16 片 D，`action_candidate_auto_execute_enabled`）的调用同样经收紧钩子（`actor=decision`），插件要求的确认不被它的自动执行跳过（9b 评审）。
- Gateway 重启后续跑（I4）：挂着的插件确认不能丢、也不能当成已批准；续跑回合里同一调用要重新征询插件（9b 评审）。
  - 第3段隔离补验（2026-10-04，worker/m1-b5-tool-gate，接57f465dca）：真实请求处理/重排、假供应商/插件传输、停机故障注入和宿主重建验证重新征询并等新的决定；不当作实际重启/TUI/IM验收。主审批沿原claim，子审批在原scope/行冻结canonical execution_attempt_id，旧挂起/决定不可跨恢复轮；原schema/路径不变，不新增批准账。
- 双入口反证：隔离测试先保存同作用域、同工具/参数的旧会话批准（例如 `rm -rf build`），再提供假的已激活收紧插件；Gateway 与子代理/后台主代理入口都必须发布插件确认并等待本次用户决定，不能因旧缓存直接执行。两条路还各覆盖 owner 长期授权命中、等待时切到自主模式、不写回会话/长期授权；B7 前不用真实 v8 启用。

### 8.6 批准后重跑

用户批准后同一调用重跑（现有机制）。重跑时：
- 对 `plugin_gate_ref` 已被批准的插件，**不再问**（防止插件每次都要确认、永远跑不下去）；仍检查该插件还在启用，停用了就不算它的要求。
- 跳过的条件是 `plugin_gate_ref` 和**这一次**调用的精确身份完全一致：`operation_id`、`call_id`、幂等键、`args_hash`，加插件激活编号和 `gate_id`（ae、9b 评审）。不能只按“这个插件批准过”或“插件 + 参数哈希”就跳过，否则同一个 run 里模型过一会儿再发一条一模一样的 `rm -rf`，会继承上一次的批准。用例：批准后同一调用重跑不再问；之后再发的同参数新调用、不同参数的调用都要重新问。
- **批准数据源**（3a 2026-10-03 补充）：收紧门自己读取宿主 `write_boundary.approved_actions` 里已批准记录的 `plugin_gate_ref`，解码后按上述全部身份核对；不能只依赖 `ActionPolicy` 的 `approval_applied`，因为自主模式或宿主不要求确认时它不会经过宿主的已批准核对。模型回执不能授予批准权，仍沿原用户审批记录，不新增批准账本。补“auto 下宿主 allow → 插件强制 ask → 用户本次批准 → 精确重跑跳门”的完整测试。
- 其它插件照常问（比如批准期间新启用的插件）。
- 第4段本地实施（2026-10-04，`worker/m1-b5-tool-gate`，接第3段 `471b7b4fc`）：只读原宿主批准行，外层必须 `APPROVED`、有 `approval_id` 且工具/run/operation/幂等键/参数哈希匹配；内层逐字段核对上述六身份，另核插件 ID 和包版本。共用池原 review 循环每轮复读安装，版本或激活变动时重新征询，不将未获得 allow 的重跑贴成已应用批准；拒绝回执保留原错误码并带同源清洗的插件/原因前缀。
- 第4/5段联接约定：`tighten_plugin_decision(request, call, host)` 与 `PluginToolGate.merge(host_status, reviews)` 签名不变。`GateCall` 尾字段 `approved_gate_refs` 默认空 tuple；`GateReview` 尾字段 `approval_applied` 默认 False，True 是宿主精确批准跳门、没有协议征询的投影，ds2 的第5段不能虚构对应 `plugin_gate.decided`。`b5s5` 已接原归档信封与唯一决定writer，全部真实review每门一条（排除已批准跳门），不可交互的ask记录 `final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE`；组合从真实临时库读到B6 info。主/子链路仍仅在隔离宿主与假传输下验证，真实 TUI/IM/Gateway 未验证。

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
- **强制沙箱**：v8 插件不管 `plugin_process_sandbox` 开没开，都进插件进程沙箱；本机沙箱或所需网络隔离不可用就启用失败（`sandbox_unavailable`），不进入 `preparing`、不退回无沙箱。
- **断网与 Gateway 端口**：`network:false` 用 Linux `--unshare-net` 或 macOS `(deny network*)` 完全断网，宿主与插件仍通过 MCP 标准输入输出通信。macOS 的 `network:true` 保留普通网络连接，但 v8 规格从 G4 登记表带入 `deny_gateway_ports`：实际绑定的 Gateway 端口始终被拒，非 Gateway 端口不受此规则影响。Linux 在 G5 端口拦截实现前无法按端口拒绝，因此 `network:true` 的 v8 插件在启用阶段以结构化原因 `gateway_port_isolation_unavailable` 拒绝；`network:false` 继续受支持并保持断网。
- **收窄读**：所有 v8 插件（无论 `network` 是 true 还是 false）都拒读运行 Gateway 用户的整个家目录，只放行插件自己的环境目录、插件数据目录，以及启用时由解释器真实路径推出的安装前缀。Python 同时放行 `sys.prefix`、`sys.base_prefix` 报告的原路径及各自 realpath，不能因 symlink 规范化丢掉家目录中的别名；Node 放行可执行文件 realpath 往上两级的安装目录。安装前缀必须是已存在目录且不能位于 my-agent 数据根中；解释器路径或 realpath 落在数据根内时以 `interpreter_inside_hidden_root` 拒绝；隐藏根或前缀推不出来/不存在时按 `sandbox_unavailable` fail-closed。会话、记忆、配置、`.ssh`、云凭据及家目录其它内容读不到；工具结果仍会进入模型上下文，所以 `network:false` 也不等于这些结果不会进入对话。
- **目录可见性与平台实现**：系统目录（如 `/etc`、`/usr`、`/Library`）在家目录外，仍可读。macOS 沿用 `attempt/sandbox._private_read_rules` / `_ancestor_metadata_rules`：拒读家目录后，对插件环境、数据目录、解释器前缀的上级只按 literal 放行元数据，保证严格 realpath 不因 lstat 报 EPERM 失败。Linux 用 tmpfs 覆盖整个家目录，再只读挂回插件环境和解释器前缀、读写挂回插件数据目录；隐藏根必须存在，否则拒绝，不悄悄漏挂。
- **共用受限规格**：`plugin_restricted_sandbox_spec(*, cwd, data_dir, owner_home, policy: PluginRestrictedSandbox) -> AttemptSandboxSpec` 是旧插件权限接入点；`PluginRestrictedSandbox` 由宿主核验的 `read_roots`、`write_roots`、`execute_roots`、`network`、`hidden_read_root` 构成。`plugin_sandbox_spec(..., sandbox_policy=...)` 把 v8 和显式受限策略统一投影到它；候选预检经 `plugin_sandbox_problem` 走同一工厂，业务连接由 `PluginMCPClient` 走该工厂，面板复用该客户端。路径须严格存在且为普通文件/目录；目录根开放该目录，文件型程序根开放原文件和 strict realpath，不顺带开放父目录；若任何授权根覆盖隐藏根，规格构造失败关闭。数据目录是宿主固定的基础写根，策略额外写根逐项叠加；网络规则和 G4/G5 边界仍由统一 `AttemptExecutionSandbox` 实现，调用方不得另写 Seatbelt/bwrap 规则。`execute_roots` 指定宿主核验的启动程序可见根，不构成禁止系统目录中其它子进程 `execve` 的独立 allowlist。
- **网络用例**（9b 评审）：`network:false` 的插件连本机回环也被拒（macOS `deny network*`、Linux `--unshare-net`）；macOS `network:true` 必须能连普通回环端口、连不上登记的 Gateway 端口。Linux `network:true` 目前在启用阶段拒绝，待 G5 端口隔离落地后再放开。
- **确认码覆盖订阅**：`confirmation_details` 在 v8 时加四项事实：`events`（类型和正文范围）、`tool_gates`（工具、效果、参数范围）、`network`、`sandbox`（固定为强制）。改了订阅就是换了确认码，必须重新确认。
- **H2 / H3 不受影响**：插件进程自己的文件访问仍受沙箱只读覆盖；插件拿到的事件和参数只是数据，不带任何宿主能力。

## 11. 配置项

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `plugin_events_enabled` | `false` | M 线总开关：开了才允许启用 v8 插件、才投事件和问收紧钩子 |
`plugin_events_enabled` 是 B7 唯一新增配置；`plugin_tool_gate_timeout_ms` 属于 B5，待真实消费点落实时再加回，不能提前作为无读取方配置。

观察的 3 秒超时、5 秒退避、120 秒空闲沿用面板服务的常数，不新加配置。两项都按 AGENTS.md 同步 `agent_config.yaml`（中文注释）、`AgentConfig`、参数中心登记和测试。

两项都属于管理员参数边界：模型经设置工具（`user_config`）改会被拒（`PARAMETER_BOUNDARY`），只有用户本人用 `/settings` 能改。**3a 2026-10-03 新裁定**：超时配置从 B7 移属 B5，实际登记在 `settings/parameter_registry._BOUNDARY_NAMES`；这是整数，`classify_safety` 跳过 plugin 目标词，必须显式登记才能防止模型放大预算。B5 同步 YAML 中文说明、AgentConfig 默认值和规范化范围，征询真实读取该值，覆盖 metadata 与模型 user_config 拒绝。`plugin_events_enabled` 仍由 B7 实施，总开关/local-main/强制沙箱及 B1 关闭门不由 B5 改动。

`plugin_events_enabled` 进管理员边界项 `USER_SETTINGS_BOUNDARY_KEYS`（`settings/user_config_capability.py`）：模型经设置工具（`user_config`）改会被拒（`PARAMETER_BOUNDARY`），只有用户本人用 `/settings` 能改。否则模型调一次设置工具把开关关掉，收紧钩子就全失效。B7 用例锁定这项边界。

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
| B6 账本与展示（查询与展示半，已实施：`worker/m1-b6-ledger-display`，m1b6，提交 `d9f158e8d`、文档补丁 `e80db5918`，基于 B3 返工头 `0a3064078`；写账半待 B5 合入） | 不依赖 B5 的部分：`plugin_gate_decisions`（最近 N 条、默认 10、`seq` 倒序、字段白名单，`message` 不外泄）、`plugin_gate_unavailable_count`（累计，不设窗口）、`/plugins info` 四段（订阅/收紧/网络沙箱/最近决定+计数）、观察计数读 B3 `hub.stats`（缺 hub 或读失败写“暂无记录”）、hub 经 `plugin_command_service → control_service → control_operation_service → http_handlers` 全链透传 | `runtime_db/repository.py`、`plugin_commands.py`、`plugin_management.py`、`plugin_events/confirmation.py`、`gateway_parts/{plugin_command_service,control_service,control_operation_service,http_handlers}.py` | 见 `tests/test_plugin_event_display.py` 12 项与 6 个变异；测试库直插第 9 节字段代 B5 写入 | B5（仅写账半） |
| B8 样例与验收（样例已实施：m1b8，`worker/m1-b8-samples`，基于 `claude/3a-step17i` `c47d023b6`；删除门改拦 `apply_patch` 删除段：b8dg，2026-10-03；真实验收待 B3–B7 合入后另派） | 样例 A：观察全部 6 类事件并在面板显示计数；样例 B：`run_command` 含 `rm -rf` 时要求确认、对 `apply_patch` 补丁里的删除文件段（`*** Delete File: `）直接拒绝；Python 和 Node 各一份 | `plugins/event-watch/`、`plugins/rm-guard/`、`plugins/rm-guard-node/` | 假模型真进程：TUI 与 IM 都看到“插件 X 要求确认 / 拒绝”；真实验收见第 14 节 | B3–B7 |
| B9 写插件的技能（M5；已实现（`worker/sol2-m5`，`de395322e`），待 be 复审） | 内置“写 my-agent 插件”技能、作者合同和 v8 文件模板（Python、Node 各一个）；安装启用仍只能由用户输确认码 | `agent_py_agent/skills/builtin/plugins/write-my-agent-plugin/`、`test_write_my_agent_plugin_skill.py` | 两语言 v8 ZIP 真构建/读回/包内 stdio；只订阅合法、模型管理入口不可见；四类双语言变异全部抓到；旧新构建器字节回归保留。B7 生产启用与隔离未验证，门禁例外见 TESTS | B1 `add244a92` |
| B5 收紧钩子（分段实施：第 1 段 `9cf60731d`，第 2 段补审至 `6ec0fc7f3`，第 3 段主/子及隔离 I4 已提交 `471b7b4fc`（父 `57f465dca`）；第4段来源头 `ff5d761d9`；第1–4段由b5r三方迁到 `worker/m1-b5-on17j`（基线 `ebe621d87`），B4真实handler事件和B5宿主decide后收紧两边保留；历史b5r联合697项678通过、13失败、5准备错误（真正17j基线相同）、1账本strict xfail；b5s5已将ds2第5段 `ff5d761d9..d517a8b7b` 三方叠到b5r交付 `65f47f2c6`，修真实review与canonical信封两接缝，组合三项转正、最终27显式文件563通过（含11文件guards187项）；无交互混合门保真实deny、不误计无法审批，未代称历史18项失败已通过；整体WIP，3a先叠sol2/b4g再挑本线，完整非作者交叉初审、9b终审、3a外部复跑仍待做） | `PluginToolGate`、合并规则（同严按插件 ID 排序）、超时按确认、代次撤销与新实例重问、重跑只对同一次调用不重问；`call_origin`；审批前缀、去掉会话/长期选项、单一 `plugin_gate_required` 判定绕开缓存/授权/自主；`PLUGIN_GATE_*` 错误码登记 | `tooling/executor.py`、新 `plugin_events/tool_gate.py`、`agent_core/tool_loop/round_execution.py`、`contracts/tool_approval.py`、`gateway_parts/stream_approval.py`、`gateway_parts/permission_bridge.py`（`wait_for_gateway_permission_decision` 必须共同守门）、`conversation/agent_tool_approval.py`（request 和 wait 都守门）、`user_space/approval_mode.py`、`contracts/error_taxonomy.py`、`plugin_management.py` 与 `plugin_invocation.py`（两处显式设 `call_origin=host_command`）；`tooling/registry.py` 保持默认 `model`；`runtime_db/host_command_execution.py` 仅联测调用方来源经 replace 保留，不在那里授予豁免 | 第 8.2 表 7 种组合；Gateway 与子代理/后台主代理两条路在已有同操作会话缓存、长期授权、自主等待轮询下仍弹插件确认，不写入会话/长期授权；决策模型自动执行同样受收紧；`plugin_gate_ref` 字符串往返、附加前后的旧 `permission_id` 不变；auto 下宿主 allow 仍转强制 ask，并由收紧门自行核对已批准引用；I4 续跑回合重新征询；超时 → 确认；两个并行调用撞上同一慢插件，排队那条超时按确认；插件回复多带字段（如 `arguments`）被忽略、参数不变；征询中停用 → 不算、换新实例 → 问新实例；批准后同一调用重跑不再问，之后同参数新调用、不同参数照样问；不可交互 → 不执行、错误码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`、账本记 `final_status`；收紧插件一律 deny 时用户仍能 `/plugins disable`、模型工具表里没有插件管理工具；模型发起的调用在工具参数里塞 `"call_origin": "host_command"` 或 `__call_origin` 照样被收紧（来源只读宿主设在 `ToolExecutorRequest` 上的字段，不从参数读；ae 补，配变异“执行器从参数读来源”）；拒绝时模型看到 `PLUGIN_GATE_DENIED` 且不是“结果未知” | B1、B2；H3 已合入 |
| B7 安全底座 | 将 B1 暂时拒绝与构造期 v8 计划排除换成真正总开关、local/main 和强制沙箱判定；v8 强制沙箱、断网、隐藏 Gateway 家目录并只放行插件目录/解释器前缀；macOS 拒绝 Gateway 绑定端口，Linux 在插件沙箱接入 G5 Landlock 端口拒绝前拒绝 `network:true`；启用拒绝必须在 `preparing` 前完成。B7 只登记 `plugin_events_enabled` | `plugin_sandbox.py`、`plugin_runtime.py`、`plugin_enable_tool.py`、`plugin_management.py`、`attempt/sandbox.py`、`contracts/error_taxonomy.py`、配置登记文件 | 真进程生产布局：Python、Node chdir/严格 realpath 与插件目录/数据目录读写成功；`.ssh`、其它家目录文件、别的插件数据、会话和配置读不到；Linux 列目录只见挂载骨架；macOS `network:true` 可连普通端口但不能连实际 Gateway 绑定端口；Linux `network:true` 启用以结构化错误码拒绝、`network:false` 仍按真实断网自检；沙箱或隐藏根不可用时启用不留激活；总开关与 local/main 反例及 `user_config` 边界有端到端用例 | B1；H3 已合入 |

## 14. 验收（goal 第四节）

- 假模型真进程：B8 两个样例在 TUI、IM 两条路径各跑一遍。
- 真实验收：隔离 home、MiniMax M2.7，TUI 和飞书各一遍：让模型删目录时弹出“[插件 rm-guard 要求确认：RM_RF …]”，选拒绝后模型收到 `APPROVAL_REJECTED`；让模型用补丁删一个文件（`apply_patch` 的 `*** Delete File: ` 段）时直接收到 `PLUGIN_GATE_DENIED`；`/plugins info` 能看到最近决定和观察计数。
- 全量 + Linux 车道全绿；每块变异全部抓到。

## 15. 决定点（3a 2026-10-03 已定）

| 编号 | 问题 | 结论（3a 2026-10-03 定） |
| --- | --- | --- |
| D1 | v1–v5 Python 轮子包能不能订阅 | 不能；用 v6 解释器入口打包，过确认码 |
| D2 | 不可交互场合插件要求确认怎么办 | 照现有规则“无法审批、不执行”；TUI 和 IM 看得到“插件 X 要求确认，但这里无法审批，所以没执行”，码 `PLUGIN_GATE_APPROVAL_UNAVAILABLE` |
| D3 | 钩子超时默认值 | 2000 ms，可配 200–10000；管理员可经 /settings 调（调大只会多等，超时仍收紧成确认），模型不能改 |
| D4 | 正文给不给、给多少 | 3a 2026-10-03 裁定：观察正文只允许 `prompt_submitted` 的提示文字，截 4000 字，要声明且确认码同意；`tool_call_started` 只能 none。工具参数只通过 `tool_gates.arguments: "full"` 对精确列出的工具给，确认码列明工具，去内部键、统一脱敏后截 4000 字；第一期不给工具输出 |
| D5 | `host_api` 能否和事件同时用 | 第一期不能 |
| D6 | 观察计数要不要落盘 | 不落盘；决定落 `runtime_events` |
| D7 | 本地直连 TUI 的提示、回合、命令事件 | 第一期只有工具两类 |
| D8 | 收紧钩子能不能看子代理的调用 | 能，`actor` 分 `main` / `subagent` / `decision` |
| D9 | 第一期开放范围 | 只 local/main；别的 owner 启用直接拒绝，码 `plugin_events_owner_not_allowed` |

**订阅贡献补充决定（3a 2026-10-03 裁定）**：v8 非空 `events` 或 `tool_gates` 算贡献，允许没有工具和面板的观察插件或 rm-guard；v1–v7 不改，v8 四项全空仍由“必须声明订阅”门拒绝。

## 16. 已知边界

- **观察事件不带工具参数（第二期再议）**（3a 2026-10-03 裁定）：第 6 节原要求只向精确列出的工具给正文，但 `events` 没有工具列表；第一期验收不要求观察参数，也不新增 `events[].tools`。需要参数的插件只能用精确工具的 full 收紧门；第二期有真实需求再扩展观察格式。
- **家目录读边界**（b7f，2026-10-03）：所有 v8 插件都隐藏 Gateway 用户整个家目录，只放行插件自己的目录和解释器安装前缀；系统目录（`/etc`、`/usr`、`/Library` 等）仍可读。插件进程能拿到它订阅的事实和（同意过的）正文，这些数据在插件那里怎么用，宿主管不到；工具结果也会进入模型上下文，因此 `network:false` 不能阻止工具结果中的内容进入对话。
- **Gateway 端口边界**（b7f/b7r，2026-10-04）：macOS v8 `network:true` 复用 G4 的实际绑定端口表，拒绝连接 Gateway 端口但允许其它网络端口；Linux v8 `network:true` 继续在启用阶段以结构化原因 `gateway_port_isolation_unavailable` 拒绝。原因是 G5 Landlock `CONNECT_TCP` 端口拒绝尚未接入 v8 插件进程沙箱；待接入后先在 Linux 车道验证目标端口被拒、其它端口仍可达，再考虑放开。`network:false` 不受影响。未来 v8 若允许声明 `host_api`，必须同时要求 `network:true` 或单独放行到其回环端口。
- **开关关闭后的投递/征询**：已启用插件在 `plugin_events_enabled` 关闭后不投事件、不问收紧，由 B4/B5 落实；B7 只负责启用门和沙箱，不在本块实现事件中心或收紧钩子行为。
- 收紧钩子的超时配置应由 B5 的真实消费点一并登记；B7 不增加未被读取的 `plugin_tool_gate_timeout_ms`。
- 观察事件会合并丢弃（只留最新），插件不能把它当完整审计日志；需要完整记录的应读宿主账本（第二期可开放只读查询）。
- **发送任务的线程数是全局上界**：每个（owner, 激活）一个在途任务、线程池 4 个（与面板服务同档）；挂住的插件数到达 4 个时，后面的发送任务要排队，仍会互相影响。这是面板服务已有的同类边界。
- **hub 不看 `plugin_events_enabled`**（给 B4、B7 的接线提示）：开关关掉时 hub 仍会读安装表（每次发布触发一轮）。B4 在事件点应先看开关再调 `publish_plugin_event`；B7 的「开关关闭就不能启用」保证不会真的投递，但读表的开销省不掉。
- Windows 上宿主命令不进沙箱，插件进程沙箱同样不可用，v8 插件在 Windows 上启用失败。
- **收紧钩子是护栏，不是安全边界**（9b 评审）：插件拒了补丁里的删除段，模型还能用 `run_command rm` 达到同样效果。插件作者要按 `effects` 订阅才能盖住同类操作；宿主的安全边界仍是 H2、H3、审批策略和沙箱。
- **宿主命令来源的可信度**（9b 复核，3a 待定）：用户命令免收紧，前提是 Gateway 能确认调用方是用户本人；本机来源信任与模型沙箱可联网之间的冲突，由 3a 定方向（比如收紧本机来源信任条件，或让模型沙箱连不到 Gateway）。
- **账本可信依赖 H3**（9b 评审）：第 9 节的 `runtime_events` 和审批决定文件（`workspace/runtime/services/gateway` 下）不被模型篡改，是靠 H3“宿主运行状态对模型只读”保证的。所以 M 线 B5–B8 排在 H3 合入之后。

## 16.1 B4/B5 预热协同（b5b7wire，2026-10-04）

B4 的事件点与 B5 的收紧征询共用同一个 B2 连接池（`plugin_channel_pool`），所以插件进程是**懒启动 + 常驻复用**：
第一次用到时启动，之后复用连接，空闲 120 秒才关。由此得出冷启动多发生在两个时刻——**本回合第一次工具调用**，
或**空闲 120 秒后的第一次**。中间的多次征询都是热的，不会各自付一次启动成本。

## 17. 评审记录

- **3a 转述 ae 备审补充（2026-10-03，m1b5 已静态核对并纳入合同，代码仍待第 2–4 段）**：补 `AgentToolApprovalSinkMixin.request_permission` 的会话缓存、长期授权、等待自主轮询三条路，与 Gateway 共用 `plugin_gate_required`（8.5）；`/plugins@` 来源改在 `plugin_invocation` 的真实构造点设置（8.1/13）；审批引用用 JSON 字符串、旧 permission 算法不变（8.4），收紧门自行读原 `approved_actions` 做精确重跑（8.6）。这是合同修订，不是安全复审通过或链路已验证。

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
| B6 账本与展示 | `worker/m1-b6-ledger-display`（查询与展示半已实施，头 `d9f158e8d`、文档补丁 `e80db5918`，基于 B3 返工头 `0a3064078`；写账半b5s5已沿原归档接入迁移树，待3a集成） | 查询与展示半已先行实施；b5s5组合真写临时库与info已验，生产全链仍待B5/B7集成验收 | 9b |
| B5 收紧钩子（b5r第1–4段迁移、b5s5第5段叠入与六项补强已实施，整体WIP待集成） | `worker/m1-b5-on17j`，真正17j基线 `ebe621d87`；前四段来源 `b6ede99e0..ff5d761d9`，第五段来源 `ff5d761d9..d517a8b7b` | B1、B2、H3已合入；B4真实handler观察/B6展示/B8样例保留；组合三项转正，原归档/唯一writer/真实临时库/B6 info链已验，批准跳门不记决定 | 3a先叠sol2/b4g再挑本线 → 完整非作者交叉初审/9b安全终审 → 3a沙箱外复跑；历史18项启动/启用失败未重跑，不记通过 |
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
