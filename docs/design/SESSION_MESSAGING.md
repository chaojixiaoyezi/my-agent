# 会话间消息与派活（SESSION_MESSAGING）

## 解决问题

同一个 owner 下同时开着多个 TUI 会话（thread）时，会话之间不能互相传消息，也不能派活。
管理员只能在一个会话里干完所有事，无法"让 A 会话把活派给 B 会话、B 干完回报 A"。

本切片是第一期：**管理员（`owner_kind=main`）的同 owner 会话之间**可以互发消息、可以派任务；
普通用户（`owner_kind=user`）两种能力都关闭；跨 owner 一律拒绝并返回明确结构化错误码。

**状态：第一期已实现并已上线**（模型工具、TUI 命令、权威存储、防循环守卫、结果回报与取消都已交付，
见本文件末尾的"实现落点"）。后续期（IM 目标、普通用户开关、跨 owner 授权）见 `DESIGN_LEDGER.md`。

## 权威与行为

先复用已有底座，不另起第二套状态。唯一权威划分如下：

| 需求 | 复用的既有权威 | 为什么 | 其余只是投影 |
| --- | --- | --- | --- |
| 会话登记 | `ConversationStore.threads`（canonical `ConversationThread`，`conversation_thread.v10`，`conversation/models.py:410`） | 已是历史/压缩/模型/owner 的唯一权威；自带 `owner_id/status/updated_at/title` | `SessionManager`（CLI 恢复用的 `session.json`，`agent/session/manager.py`）只是恢复投影，**不是**消息目标 |
| message 的正文与状态 | `GuidanceStore`（`conversation/store_guidance.py`，`append_once` + `dedupe_key` + 四态状态机） | 已有幂等写入、状态机、按 target 定位；`GUIDANCE_TARGET_TYPES` 已含 `thread`（`conversation/models.py:862`） | —— |
| 目标的唤醒 | `WakeStore`（`conversation/store_wakes.py`：`raise_signal`/`publish_deduped`/`pending`/`mark_handled`） | 已有 urgent/normal 队列、去重发布、handled 回执，是"叫醒空闲会话"的既有机制 | —— |
| 回合触发 | `TurnTrigger`（`agent_core/runtime/turn_trigger.py:42`）新增一种 kind | 既有 `lifecycle_wake` 已证明"结构化触发 ≠ 用户轮"的模式（`current_turn_text` 用 `# Host Event` 开头） | —— |
| task 的状态与结果 | **新增** `SessionTaskStore`（`conversation/session_tasks.py`） | `ThreadTaskLink` 是"会话↔自己的任务"，不是"派给别的会话的任务"，语义不同 | `CollaborationStore`（`collaboration/models.py`：case/请求/证据）是跨代理协作账本，**不**做派活权威 |

要点：
- message 正文**不**新建消息表，直接落 `GuidanceStore`（`target_type="thread"`）；
- task 只存结构字段与 refs，不存对话正文；
- 唤醒只经 `WakeStore`；task 的结果回报作为 message 走同一条 guidance 链。

## 消息与任务的结构化身份

### message（kind = `session_message`）

| 字段 | 来源 |
| --- | --- |
| `message_id` | 复用 `guidance_id` |
| 发送方 | 当前 runner 上下文（`owner_id` + `thread_id`），不读显式参数 |
| 接收方 | `target_thread_id`（显式参数） |
| `created_at` | `GuidanceEntry.created_at` |
| `dedupe_key` | 幂等键，重发返回原 `message_id` |
| `status` | `queued` / `delivered` / `failed`，映射既有 `GuidanceOnceReceipt` 状态：`pending`→queued、`consumed`→delivered、`rejected`→failed |
| **`origin_kind`** | **`user_steering`（既有用户插话）/ `session_message`（会话消息），落 `GuidanceEntry.metadata`** |
| **`origin_thread_id`** | **发送方 thread_id，落 metadata；注入时据此渲染“来自会话 X 的消息”** |

**来源不得混同用户插话（dev 审阅点 1）**：`GuidanceEntry` 必须有结构化来源类型（用户插话 / 会话消息）
和发送方 thread_id。注入时，会话消息以"来自会话 X 的消息"这种**宿主事件**形式呈现，
**绝不能渲染成用户原话**，否则目标模型会把别的会话的话当成用户指令。

### task（kind = `session_task`）

| 字段 | 说明 |
| --- | --- |
| `task_id` | 新 ID |
| 发送方 / 接收方 | 同 message |
| 正文 | 结构化输入（来源明确的 input ref），不存对话正文 |
| `status` | `queued` / `accepted` / `done` / `failed` / `cancelled` |
| 产物 `refs` | 结束时由目标会话填入 |
| `summary` | 结束时填入，回报给发送方 |
| `origin_task_id` | 防循环：由对端 task 触发的 task 记下上级 task |

**task 状态只能有一个权威（dev 审阅点 2）**：`SessionTaskStore` 只存派活记录
（发送方、接收方、任务正文的引用、`dedupe_key`、`origin_task_id`），加上指向目标执行的链接
（`conversation_request_id` 或 `task_run_id`）。
- `accepted`：目标 `TurnTrigger` 被消费时写入；
- `done` / `failed` / `cancelled`：**只由目标请求的终态事件写入，写入方只有一个**；
- 不允许出现"SessionTaskStore 说 done，但目标请求其实 failed"这种双账。

**任务正文只存一份（dev 审阅点 3）**：正文放一条 guidance 条目（或 artifact），
`SessionTaskStore` 只存它的 id。目标轮从这个 id 取正文来呈现，不再复制第二份正文。

身份冲突：发送方身份只从当前 runner 上下文取；显式参数与上下文冲突时返回 `scope_warnings`，
不静默猜、不冒充别的会话（遵循架构铁律"并发代理身份必须显式"）。

## 投递语义

- **message**
  - 写入目标 thread 的 guidance 队列（`GuidanceStore.append_once`，key=`(target_thread_id, message_id)`）。
  - 目标**空闲**：由该 message 触发一次 `WakeStore.raise_signal`，以宿主事件呈现，写明来源会话。
  - 目标**正忙**：只排队，在下一个回合边界由 `inject_pending_guidance` 在安全点注入
    （`agent_core/runtime/guidance.py`，注入点是"当前活动轮的安全点"，这是既有语义）。
- **task**
  - 目标会话以新的 `TurnTrigger` kind（`session_task`）开一轮（`TurnTrigger` 新增 kind，
    复用 `current_turn_text` 的"非用户轮"渲染路径）。
  - 任务正文是来源明确的输入；结束时把结构化结果（状态、产物 refs、摘要）作为 message 回给发送方。
- **取消/停止已派任务**：走 `SessionTaskStore` 的结构化状态变更 + 控制事件，不解析自然语言。

## 权限矩阵（只看结构化字段，不看文本）

判定输入只有：发送方 `owner_kind`、`kind`（message/task）、是否同 owner、是否同 thread、开关。

**判定用结构化身份 `OwnerIdentity`**（`agent/user_space/identity_store.py`，含 `provider/owner_kind/owner_id`），
不从当前 runner 上下文之外的裸字符串推断。

注意：`owner_kind=main` 不只本机 TUI——IM 管理员身份（`/admin <密码>` 绑定，见
[ADMIN_CHANNEL_IDENTITY.md](ADMIN_CHANNEL_IDENTITY.md)）让飞书私聊也按 `local/main` 运行。
所以**发送方**可以是任意 local/main 会话，包括 IM 管理员私聊。

**接收方（第一期）只限本地渠道**：用 `channel_bindings` 的结构化字段判断，**白名单**放行
（`chat`/`cli`/`local`/`tui`/`gateway-cli`/`http`）；任何不在白名单里的渠道——包括 feishu/qq/wecom/dingtalk
以及**以后新增的渠道**——一律返回 `SESSION_TARGET_CHANNEL_UNSUPPORTED`（fail closed）。
原因：给 IM 会话发消息或派活会让那边开一轮并把回复真的发到 IM，属于对外副作用，第一期先不开。
这条也避免了"封闭枚举"——名单只列允许的，不列要拦的。

**身份 fail closed**：结构化 owner 身份三元组（`provider`/`owner_kind`/`owner_id`）任一为空时，
一律返回 `SESSION_IDENTITY_UNAVAILABLE`，**绝不默认成 main 或 local**。

| 发送方 owner_kind | kind | 目标 | 结果 |
| --- | --- | --- | --- |
| main | message | 同 owner 同 thread | 允许（等于给自己插话，复用 guidance） |
| main | message | 同 owner 其他 thread | 允许（开关默认开） |
| main | message | 跨 owner | 拒绝 `SESSION_TARGET_OUT_OF_SCOPE` |
| main | task | 同 owner 其他 thread | 允许（开关默认开） |
| main | task | 同 owner 同 thread | 拒绝 `SESSION_TASK_TARGET_SELF` |
| main | task | 跨 owner | 拒绝 `SESSION_TARGET_OUT_OF_SCOPE` |
| user | message | 任意 | 拒绝 `SESSION_MESSAGING_DISABLED`（开关默认关） |
| user | task | 任意 | 拒绝 `SESSION_TASK_NOT_ALLOWED`（无开关，直接拒） |

错误码集合：`SESSION_MESSAGING_DISABLED`、`SESSION_TASK_NOT_ALLOWED`、
`SESSION_TARGET_OUT_OF_SCOPE`、`SESSION_TASK_TARGET_SELF`、`SESSION_TASK_CHAIN_LIMIT`、
`SESSION_PAIR_RATE_LIMIT`、`SESSION_TARGET_NOT_FOUND`、`SESSION_TARGET_CHANNEL_UNSUPPORTED`、
`SESSION_IDENTITY_UNAVAILABLE`、`SESSION_NO_CURRENT_THREAD`。

（自定义错误码必须登记进 `contracts/error_taxonomy.ERROR_CONTRACTS`，否则会被归一成 `UNKNOWN_ERROR`；
守卫 `test_recovery_code_policy.py` 会检查所有在用的错误码都已登记，定向测试要带上它。）

**不泄露存在性（dev 审阅点 4）**：目标 thread 不存在，或属于别的 owner，
都返回**同一个错误码** `SESSION_TARGET_OUT_OF_SCOPE` 和**同样的** `scope_warnings`，
不区分"不存在"和"没权限"。发给自己也要拒绝。

## 防循环

- **派活链深度上限**：由对端 task 触发的 task 携带 `origin_task_id` 链；
  深度超过 `session_task_max_chain_depth` 拒绝 `SESSION_TASK_CHAIN_LIMIT`。
  **深度按 `origin_task_id` 链结构化计算，不信任模型传入的深度。**
  **链来源由宿主提供**：派活唤醒信封的 `metadata.session_task_id` 由 conversation 运行时写入
  `task_attributes` 的结构化属性 `conversation_session_task_id`，工具只从那里读；
  不依赖任何可被手工赋值的 agent 属性（这正是 2026-09-28 修掉的缺陷——原实现读一个从未被写入的属性，
  守卫在生产里一次都不会触发）。
- **派活回合的开头是宿主事件**：派活唤醒产出 `kind=session_task` 的 `TurnTrigger`
  （`session_task_turn_trigger`，只从唤醒信封的结构化 metadata 构造；缺 `session_task_id` 时不发触发、
  退化成普通后台片）。回合开头因此是 `# Host Event` + 固定首句
  「宿主事件：另一个会话派来一个任务……」，而不是用户轮。
- **每对会话每小时上限**：按 `(发送 thread, 接收 thread)` 分桶计数；
  超过 `session_pair_hourly_limit` 拒绝 `SESSION_PAIR_RATE_LIMIT`。
  **task 完成后的回报消息也计入这个上限。**
- **回报不得升级为 task**：task 完成后回报给发送方的是 message，**不能自动变成新 task**。
- 两数字 `0` 表示不限制（能力配置统一约定）。

## 配置

放 `agent_py_agent/config/capability_config.yaml` + `agent_py_agent/agent/capability/config.py`，
因为能力授权类参数不进主配置（AGENTS.md 约定）。中文注释。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `session_messaging_admin_enabled` | `true` | 管理员会话之间发消息开关 |
| `session_messaging_user_enabled` | `false` | 普通用户发消息开关（默认关，用户隔离期再开） |
| `session_task_admin_enabled` | `true` | 管理员会话之间派任务开关 |
| `session_task_max_chain_depth` | `4` | 派活链深度上限；0 不限制 |
| `session_pair_hourly_limit` | `60` | 每对会话每小时上限（任务回报与取消通知也计入）；0 不限制 |

## 入口

- **模型工具**（注册在 `agent/core.py` `_register_orchestration_tools`，
  须在 `enable_subagents` 门控**之前**，因为会话间消息是 owner 级能力）：
  - `list_owner_sessions`：列出本 owner 的会话（`thread_id`、状态、最近活动时间、渠道、`is_current`、
    `allowed_kinds`），只读、不含标题/摘要/正文；调用方自己的会话标 `is_current` 且 `allowed_kinds` 为空；
  - `send_session_message`：发消息；
  - `create_session_task`：派任务；
  - `get_session_task`：查任务状态。

**关闭时的可见性（dev 审阅点 6）**：普通用户或开关关闭时，这几个工具**不进模型工具列表**
（用结构化可用性控制，不靠模型自觉）；TUI 命令返回 `SESSION_MESSAGING_DISABLED`。
两边的 TUI 都要显示：目标 TUI 显示收到的消息或任务及来源，发送方 TUI 显示投递和任务状态
（显示放第 2、3 片）。
- **TUI 命令**：扩展 `/sessions`（列会话，需把目标列表从 Session 改成 ConversationThread 或建立映射）、
  新增 `/tell <thread> <msg>`、`/delegate <thread> <task>`。
- **飞书入口**：第一期不做，记入 DESIGN_LEDGER 缺口。

## 落地坐标（已核对）

- **工具注册**：`agent/core.py:1074-1081` `_register_orchestration_tools`；
  边界在 `core.py:1072-1073`（`if not agent.config.enable_subagents: return`）。
  样板：`agent_core/runtime/guidance_tool.py`（`SendGuidanceTool`：BaseTool + `runtime_policy` + `model_spec` + 稳定 error_code）。
- **capability_config Python 侧**：`agent/capability/config.py` 的 `CapabilityConfig` + `load_capability_config()`。
- **存储组装**：`agent/conversation/store.py:41-48`（`self.threads`/`self.messages`/`self.guidance`/`self.wakes`）；
  新增 `self.session_tasks` 挂同一 `ConversationStore`。
- **TUI 命令**：声明在 `agent/command_catalog.py` 的 `COMMAND_CATALOG`（`/sessions` 在 line 36）；
  处理器在 `cli/chat_parts/slash_commands.py`（`_handle_sessions_command` line 135，`handlers` 元组 line 49-57）。
- **会话枚举**：`ConversationStore.threads.list_report(limit)`（`conversation/store_threads.py:232`）；
  注意 `/sessions` 当前用的是 `SessionManager.list_sessions_report`（`agent/session/manager.py:94`），
  与 `ConversationThread` 是两套记录——第 2 片必须处理这个差异。
- **owner_kind 来源**：`home_paths.owner_kind`（`settings/config.py:164` 默认 `"main"`）。
- **唤醒消费**：`owner_wake_discovery.py`（跨 owner 磁盘发现，第二期用）+ 后台循环 tick。

## 测试计划

按 `合同单测 → fake tool → fake LLM → replay` 顺序：

1. **权限矩阵合同单测**：管理员/普通用户 × message/task × 同 owner/跨 owner，8 格全覆盖，
   逐格核对返回码或成功。
2. **幂等**：同 `dedupe_key` 重发返回原 `message_id`，不产生第二条。
3. **防循环**：链深超限、每小时超限各一条；`0` 表示不限制的分支；
   回报 message 不能升级成新 task；链深按 `origin_task_id` 结构化计算（改坏模型传的深度不影响判定）。
4. **不泄露存在性**：目标不存在 vs 目标属于别的 owner 返回同一错误码与同样 warnings。
5. **空闲目标唤醒注入（dev 审阅点 1 附加测试）**：空闲目标被唤醒的**新一轮**，
   开头能取到排队的消息，确认**回合开始时也会注入**，而不是只在中途的安全点注入。
6. **来源不混同用户插话**：会话消息以"来自会话 X 的消息"宿主事件呈现，测试断言它
   **不会被渲染成用户原话**（`# User Task`）。
7. **fake tool**：写入后目标 guidance 队列可见；状态机 `queued→delivered`、task `queued→accepted→done` 流转。
8. **fake LLM 端到端**：一个 Gateway 两个会话，A 派活 → B 执行 → B 回报（既有 fake LLM 脚手架）。
9. **变异验证**：人为改坏权限判定/去重/链深，确认对应测试会红。
10. 定向回归：`test_wake_queue.py`、`test_runtime_guidance.py`、`test_lifecycle_wake_host_event.py`、
    `test_collaboration_owner_isolation.py` 作为调用链样板。
11. **真实链路门禁**（2026-09-28 起，会话互通的每一次交付先用它把关）：`test_session_task_real_chain.py`。
    - 走真实 Gateway ask、Gateway 同款后台调度器，以及真实的 唤醒 → 认领 → run_claimed → 回合装配，只替换供应商传输。
    - 已知未修的缺陷用 strict xfail 标出，修好后必须转正。
    - 其中“空闲目标的消息唤醒回合没有认领或确认消息，下一回合会再收到一遍”违反第 5 条，是 2026-09-28 新发现的缺陷。
    - 2026-09-29 起包含 `list_owner_sessions` 的正向对照：管理员列会话 → 只凭清单挑目标派活或发消息 → 目标执行；
      并断言清单不含别的 owner 的会话，也不含正文。
    - 2026-09-29 起，取消场景除了在模型调用中打断（HOLD-FIRST、HOLD-AFTER-TOOL），还有两窗：
      - 后端不响应停止：迟到的答复先由模型调用等待关卡丢弃，旗还在时还有既有防线；
      - 停止旗丢失（故障注入）：只剩交付前按任务已取消的持久检查。
      取消修复并入前，两窗都按 strict xfail 挂在 `stop_confirmed`。
    - 2026-09-29 起有“同一对会话连发三条”一窗：第二条内容不同，第三条与第一条相同。
      - 断言：三条都独立入队、各有回执；工具返回真实状态；每条都送到；发送方不触发结果未知收口。
      - 现状（缺陷）：`send_session_message` 的去重键只按会话对区分（`session_message:{发送方}->{目标}`），`append_once` 同键同文返回旧记录、同键异文报错。所以第二条报结果未知，第三条返回第一条的旧 id、写死 `status: pending`，都送不到。
      - 已修（分支 `claude/75-session-message-dedupe`）：键 = 会话对 + 这次发送的身份（`session_message_dedupe_key`；模型工具用
        `__operation_id`，同一次调用重试不变；`/tell` 每次新生成），唤醒 metadata 的 `message_dedupe_key` 带上它，已消费判据按它查回执，
        工具返回回执真实状态；这一窗已转正。
    - 目标回合在消费前被停止：回执不再永久 rejected，释放给下一回合正式认领（方案 A，最多释放 5 次），
      门禁断言消息出现在之后回合自己的输入里、回执最终 consumed；细节见 DESIGN_LEDGER 对应条目。
    - 消息唤醒回合认领后失败：后台片异常结束时同样收尾，消息释放给重跑回合（释放按次计数），门禁两窗：失败一次后送到并 consumed；
      每次都失败时第 6 次转 rejected 带码、唤醒结案。
    - 派活回合认领正文后失败（不是取消）：正文退回给同一任务号的重跑（不授权跨回合），门禁一窗：断一次后重跑完成、正文 consumed；
      取消用例断言正文不回到 pending；先断线、后取消时，取消路径把已退回的正文撤成 rejected，门禁一窗断言重试不再注入它。
    - 细节见 TESTS.md。

## 交付切片

| 片 | 内容 |
| --- | --- |
| 0 | 本设计文档 + DESIGN_LEDGER 摘要（贴板等 dev 审） |
| 1 | 权威存储、权限判定、管理员发消息工具、目标会话呈现；权限矩阵合同单测 |
| 2 | TUI 命令（列会话、发消息） |
| 3 | 管理员派任务、结果回报、取消；fake LLM 端到端 |
| 4 | 防循环守卫、配置开关、文档收尾（CODEBASE_TREE、TESTS.md） |

**端到端验收（2026-09-28，脚本模型 + 真实 Gateway／TUI，被测 `b938b2a98`）：未通过。**A 能派活并唤醒 B，B 的回合开头是宿主事件；但派活回合的工具集里没有会话工具，所以 B 无法再往下派，链深守卫在真实链路上仍到不了。空闲目标的回合从不接单、不收尾、不回报，任务停在 queued。没确认消费的正文会在后来的回合里被再次注入。目标正忙时，在安全点注入正文会让它的请求以 `DATACORRUPTIONERROR`（guidance submission reservation mismatch）失败。取消只能走“从队列撤回”，停止控制一次也没有发出。证据批次 `session-task-chain-e2e`，详见 TESTS.md。

**复验（2026-09-28，脚本模型 + 真实 Gateway／TUI，被测 `a646a4885`）：未通过。**上一轮的 4 个缺陷已修好：A→B→C 两层派活都能接单、收尾和回报；C 再往下派时返回 `SESSION_TASK_CHAIN_LIMIT`（depth=2，limit=2）；给正忙的会话发普通会话消息，它的请求不再失败，消息也被确认消费。新发现三个问题：一是会话在派活回合里时，邮箱中别的任务的完成回报（metadata 带 `session_task_id`）会被 `claim_for_turn` 按归属拒绝认领，而 `available_for_turn` 不看归属、仍判定“有新输入”，模型的最终回复被反复作废，回合对模型无上限空转（实测 3.2 秒 88 次、95 秒 3849 次，只能停 Gateway）；二是 agent 根目录下缺少 capability 配置文件时，链深和每对限额按“不限制”处理，而不是默认值 4 和 60（`workspace_root` 为空时 agent 根目录是 owner home）；三是取消仍停不下目标回合，因为任务要等第一次模型调用返回才绑定回合，绑定前取消只能撤队列，绑定后停止控制只在会话窗口的活动 Gateway 请求里找回合，找不到后台派活片。集成方决定：生产上继续暂停会话互通，`a646a4885` 不部署。证据批次 `session-task-chain-e2e/recheck-a646a4885`，详见 TESTS.md。

## 缺口与后续

- 飞书入口第一期不做。
- **接收方渠道**：第一期只允许本地渠道白名单；给 IM 会话发消息/派活（会把回复真的发到 IM，属对外副作用）
  列为后续项，需要先设计对外副作用的确认与审计。
- 普通用户发消息开关默认关；用户隔离（跨 owner 如何安全开放）留待后续设计。
- 跨 owner 发现机制（`owner_wake_discovery.py`）第二期才用。

## 实现落点（第一期，已上线）

| 关注点 | 实现位置 |
| --- | --- |
| 权限判定（纯函数，只读结构化字段） | `agent_py_agent/agent/conversation/session_messaging.py`（`decide_session_messaging`、两个可见性判定） |
| 发消息（模型工具） | `agent_py_agent/agent/agent_core/orchestration/tools/send_session_message.py` |
| 派活（模型工具） | `agent_py_agent/agent/agent_core/orchestration/tools/create_session_task.py` |
| 查询 / 取消（模型工具） | `agent_py_agent/agent/agent_core/orchestration/tools/session_task_control.py` |
| 列本 owner 会话（模型工具） | `agent_py_agent/agent/agent_core/orchestration/tools/list_owner_sessions.py`（含自己的可见性判定 `list_owner_sessions_tool_visible`） |
| 派活权威记录与状态机 | `agent_py_agent/agent/conversation/session_tasks.py` |
| 取消时对目标回合发精确停止控制 | `agent_py_agent/agent/gateway_parts/session_task_stop.py` |
| 目标回合结束时的结构化回报 | `agent_py_agent/agent/conversation/session_task_report.py` |
| 每对会话每小时限额 | `agent_py_agent/agent/conversation/session_pair_rate.py`（消息、派活、任务回报、取消通知都计入） |
| 宿主事件呈现 | `agent_py_agent/agent/agent_core/runtime/loop_support.py`（`_native_turn_opener` / `_host_event_source`）；派活回合触发由 `agent_py_agent/agent/agent_core/runtime/turn_trigger.py` 的 `session_task_turn_trigger` 构造 |
| 派活回合的结构化身份 | `agent_py_agent/agent/conversation/runtime.py`（`_session_task_id_from_wake` 写入 `conversation_session_task_id`；`_background_model_inputs` 分流派活触发） |
| 派活回合的精确回合号 | 同文件 `_session_task_run_id`（只读唤醒信封 `metadata.session_task_id`）→ `_run_params` 的 `request_id/run_id/task_id`；收尾用同一编号 |
| 宿主投递的回合绑定 | `agent_py_agent/agent/conversation/store_guidance.py`（`claim_for_turn` 在认领时补记接收回合的 `expected_turn_id`，并做正文归属核对） |
| 后台工具档（含会话工具） | `agent_py_agent/agent/conversation/background_tool_policy.py`（`SESSION_TASK_WAKE_ALLOWED_TOOLS`） |
| TUI 命令 | `/sessions threads`、`/tell`、`/sessions inbox`（`agent_py_agent/cli/chat_parts/slash_commands.py`） |
| 配置键 | `agent_py_agent/config/capability_config.yaml` + `agent_py_agent/agent/capability/config.py` |

**`list_owner_sessions`（2026-09-28 补上，原第一期缺口）**：
- 可见性：只给管理员（`owner_kind=main`），`session_messaging_admin_enabled` 与 `session_task_admin_enabled`
  至少开一个才注册；普通用户即使打开了 `session_messaging_user_enabled` 也看不到它（第一期只给管理员）。
  配置经 `capability_config_for_agent(...) or CapabilityConfig()` 读，直接读字段，不自带兜底值。
- 数据只读本 owner 的 `conversation_store`，并逐条核对记录的 `owner_home` 与当前 owner 家目录的规范路径；
  写了别的 owner 家目录的记录不列出、不计数，不泄露其存在。子代理内部线程（`metadata.thread_kind=agent`）不列出。
- 每行只有 `thread_id / status / last_activity_at / channel / is_current / allowed_kinds`；`allowed_kinds` 由
  `decide_session_messaging` 对 message、task 逐类判定，IM 渠道会话照常列出但为空。损坏记录只计入
  `unreadable_records`，不回显内容。按最近活动倒序，`limit` 取 1–100（默认 20），超出时 `truncated=true`。
- 身份三元组任一缺失返回 `SESSION_IDENTITY_UNAVAILABLE`，不列任何会话。派活唤醒档
  `SESSION_TASK_WAKE_ALLOWED_TOOLS` 同时放行它。
- 真实链路验证：`test_session_task_real_chain.py::test_admin_lists_owner_sessions_and_reaches_the_listed_target`
  （派活、发消息两条）。对普通用户开放之前，必须先补权限用例。

限额语义：`session_pair_hourly_limit` 按「发送会话 → 接收会话」分桶，小时窗口固定、跨窗口归零；
模型发起的发送与派活在投递前判断，超限返回 `SESSION_TASK_RATE_LIMIT` 且不投递、不占配额；
宿主自动发出的任务回报与取消通知**不会被拒**，但同样计入配额（避免回报绕过限额）。
