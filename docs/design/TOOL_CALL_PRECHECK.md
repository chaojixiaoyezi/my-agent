# 工具调用审批前的有效性复核

状态：已实现并真实复验通过（2026-09-24，用户批准现稿后实现，合入 main `42f7e57e1` 并双机部署；测试机默认确认模式下"审批等待期间停用"与"思考期间停用"两场景均按预期拦下且账上零执行，见 TESTS.md）。实现与本稿的三处偏差见文末"实现偏差"。改动统一权限门，两条开发线共用。上位合同见 [可装卸插件方案](PLUGIN_LIFECYCLE.md) 与工具运行时合同（`agent/tooling/models.py`）。

## 解决问题

第 10 步真实 TUI（.10，默认确认权限）观察到：模型在一回合内调用某插件工具，审批框弹出等待期间，另一个 TUI 停用了该插件；用户批准后调用才因激活失效而失败（TOOL_EXECUTION_FAILED，零副作用）。
用户被要求审批一件已经不可能执行的事。根因是现行合同"每次运行冻结工具快照，invoke 位于统一权限门之后、不重新调用 availability"
（`tooling/registry_invoke.py` 模块头），而插件代理只在真正执行时才用 `PluginActivationRef.require()` 复核激活。

## 原则（对照铁律）

- 硬门只守客观事实：插件激活是否仍在安装表里、MCP 连接是否仍在运行，都是文件系统/进程事实，可以在审批前硬拦；任务质量之类不进入这里。
- 通用而非专项：复核是工具运行时策略的通用可选能力，不是"如果是插件就……"的分支。内置工具默认"可用"且不做 I/O；MCP 代理用连接存活事实；插件代理用激活复核。任何后续工具种类只需实现同一接口。
- 结构化结果：复核返回结构化可用性（是否可用、错误码、简短原因），不解析文本；失败沿既有 `TOOL_UNAVAILABLE`（category=tool，recommended_action=REQUEST_CAPABILITY）返回模型，让模型换工具或申请能力，不伪装成执行失败。
- 不改变快照身份：运行快照仍然冻结，复核只决定"是否弹审批、是否进入执行器"；快照哈希、loaded 事实、权限模式都不因复核而变化。
- 竞态仍由执行时兜底：复核通过后审批等待期间仍可能被停用，执行时的 `require()` 保持原样。复核减少无谓审批并把客观失效提前暴露，不是唯一保障。

## 挂接位置与接口

调用链（现状）：模型发出 tool_use → `execute_traced_tool_call` → `ToolExecutor.execute`（`tooling/executor.py`）：快照查找 → 规范化 →
`ActionPolicy.decide`（读取快照冻结的 `runtime.availability`、效果解析、审批判定）→ 状态为 `ask` 时返回 approval_required →
`_resolve_tool_approval` → `StreamApproval.request` 写 `permission_requested` → 等待决定 → 再次 `execute_one` → `_execute_authorized`
→ 校验 → `pre_handler_gate` → 权威 → 工具账 claim → `invoke_registry_tool`。执行链上没有任何一步在冻结快照之后重新读取可用性
（唯一的例外是决策线的 `decision_recommendation._tools_current`：它在采用/续用推荐前逐个重读 `availability()`，任一失效就整体丢弃推荐、回到原展示，只影响展示不影响执行）。
插件代理的 `availability()`（`plugin_runtime.py`）已经能把激活失效报成 `TOOL_UNAVAILABLE`，但执行链只在建快照时读它一次。
显式插件命令（`host_command_approval`）两遍都走完整的 `ToolExecutor.execute`：审批前已复核激活，审批后没有复核；挂接点 2 自然覆盖它，不单独挂钩。

挂接点（两处共用同一个辅助函数，不新增第二套判定）：

1. `ToolExecutor.execute`：`ActionPolicy.decide` 返回 `ask` 之后、返回 approval_required 之前。
   调用 `runtime.handler.availability()`（没有该方法的替身处理器直接跳过；抛异常按不可用处理）。不可用时返回
   `ActionDecision("deny", ("TOOL_UNAVAILABLE",), {"failure_stage": "runtime_gate", "precheck": "pre_approval", "reason": <availability.reason>},
   resolved_effect=decision.resolved_effect)`，由现有 `_decision_result` 记为 `handler_executed=False`、效果 `not_started`；
   不写 `permission_requested`，不进入执行器，工具账没有该调用的执行记录。
2. `_execute_authorized`：审批通过后再次进入、工具账 claim 之前，只对"经过审批"的调用再跑同一函数。
   这一处覆盖"审批等待期间被停用"的时序；不对免审批调用逐次复核（`test_tool_runtime_scope.py:251` 钉住了这条现行合同，且避免给内置工具加成本）。
   "经过审批"必须是结构化事实：现在 `ActionPolicy.decide` 返回的 allow 不带审批标记（绑定匹配在 `_approval_decision` 内部完成），
   所以先让 ActionPolicy 在 decision 的 evidence 里输出 `approval_applied=true`，执行器只看这个标记，不重复匹配绑定。
3. 渲染：挂接点 2 拦下的结果会经 `_with_applied_approval_fact` 带上"已获批准、不要等待"的说明，与 `TOOL_UNAVAILABLE` 并列会让模型困惑；
   "批准后复核失败"要单独渲染成"已批准但工具在执行前失效，请换工具"，不复用已批准提示。
4. 具体原因要到模型：`_decision_result` 目前不写 `reported_error_code`，模型只看到通用 `TOOL_UNAVAILABLE`；复核结果的具体原因（激活失效、连接关闭）
   放进 `reported_error_code` 并保证它在错误分类表里；不把 `availability.error_code` 直接塞进 reason codes（未知码会塌成 UNKNOWN_ERROR / REPORT_BLOCKER）。

接口：`_pre_execution_precheck(runtime: ToolRuntime) -> ToolAvailability | None`，只读，不构造连接、不启动进程、不写状态。
可用的结构化输入：`request`（快照、写边界、审批模式、run 上下文、取消令牌）、`runtime`（spec、策略、冻结的处理器及其 activation_ref）、
`call`（run/turn/attempt/operation 编号、参数哈希）、`decision`。

错误码：沿用 `TOOL_UNAVAILABLE`（不可重试、建议申请能力），具体原因放在 `reported_error_code`（`PLUGIN_ACTIVATION_UNAVAILABLE` → `TOOL_UNAVAILABLE` 的映射已经存在于 `mcp_registration.py`，沿用）。
连续两次审批前被拦会触发既有的 tool-failure-channel-hint（`loop_hints.py` 只计 `TOOL_UNAVAILABLE` 与可重试网络错，默认阈值 2）：插件已停用就该换渠道，这是期望行为，写明不改。
不用 `PLUGIN_NOT_ENABLED`（不在错误分类表里，恢复逻辑会当未知码阻塞）和 `PLUGIN_DISABLED`（语义是插件管理总开关关闭）。
顺带修正：现行失败路径报 `TOOL_EXECUTION_FAILED`，分类表标为可重试、建议 RETRY，对已停用插件是错误建议；复核提前拦下后模型收到的是正确的"换工具/申请能力"。

顺带修正（同一片）：今天 TUI 看到的 `TOOL_EXECUTION_FAILED` 链路是"停用写入 stop_requested → 执行时检查到它 → 抛 MCP_CONNECTION_CLOSED → 映射成 TOOL_EXECUTION_FAILED（可重试）"。
把"因停用而关闭"按结构化的 stop 原因报成 `PLUGIN_ACTIVATION_UNAVAILABLE`（不解析文字），这样复核之后、执行之前那个极短竞态窗口也不会再给出错误的重试建议。
插件工具默认 effect=dangerous 不走只读自动重试，所以现在只是提示错、不会真重试。

不做：等待审批期间逐次轮询复核（`permission_bridge` 每次 poll 再查）——目前"不可用"在 `round_execution` 里会回退成 approval_required，要先改那条回退才有意义，另开一片。
非插件 MCP 服务在回合中途死亡的情况，同一复核（连接存活事实）自然覆盖，不单独写分支。

## 实现偏差（2026-09-24，按代码事实调整，不改原则）

- **复核是代理工具的 opt-in，不是对所有 handler 调 `availability()`**：`ShellTool.availability` 在 Linux 上会起 `bwrap --version` 进程，审计/记忆/人格工具的可用性还依赖当前任务属性，逐次复核既有 I/O 又可能与快照时不一致。所以新增 `BaseTool.precheck_availability()` 默认返回 None（不复核、沿用冻结快照），只有 `MCPProxyTool`（内存里的连接状态）和 `PluginProxyTool`（一次有界安装表读取 + 连接状态）覆盖。原则"内置工具零成本、零 I/O"由此成立。
- **具体原因码**：两个代理的 `availability()` 之前只报通用 `TOOL_UNAVAILABLE`；复核入口分别给出 `PLUGIN_ACTIVATION_UNAVAILABLE` / `MCP_CONNECTION_CLOSED`，执行器只把它放进 `reported_error_code`，`error_code` 仍是 `TOOL_UNAVAILABLE`；两码已登记到错误分类表（tool、不可重试、REQUEST_CAPABILITY）。
- **挂点 2 的位置**：`_execute_authorized` 已到 8 个参数的硬上限，复核放在该函数入口、按 allow 裁决证据里的 `approval_applied=true` 判断，不加参数；`approval_applied` 只在 `_approval_decision` 的精确 binding 匹配分支为真（沙箱内自动执行、策略不要求、auto 模式都为假）。
- **渲染**：批准后被拦下的结果在 `_with_applied_approval_fact` 里跳过"已批准且已应用"事实，避免模型误以为执行过；拒绝文案由 `_decision_message` 按 `precheck=pre_approval|post_approval` 点明"审批前"或"批准后、执行前"。
- **停用链**：`mcp_managed_process.require` 改为先核对激活再看进程记录，因停用而关闭的调用报 `PLUGIN_ACTIVATION_UNAVAILABLE`（→`TOOL_UNAVAILABLE`），不再是可重试的 `TOOL_EXECUTION_FAILED`；进程消失后的 EOF 路径仍报 `MCP_CONNECTION_CLOSED`。

## 验收

- 合同单测（fake tool / fake plugin）：快照冻结后、调用前停用插件 → 不写 `permission_requested`、不进入执行器、模型收到 `TOOL_UNAVAILABLE` 与结构化原因；工具账不出现该调用的执行记录（与 TUI236 "审批前拦下即零操作"一致）。
- 合同单测：审批等待期间停用 → 批准后在 claim 之前被第二处复核拦下，同样是 `TOOL_UNAVAILABLE`、无 claim、无执行记录；只有在复核之后、执行之前的极短窗口内失效，才仍走现行执行失败路径（TOOL_EXECUTION_FAILED，not_started）。
- 合同单测：内置工具的复核不产生任何文件/网络 I/O（用计数替身钉住）。
- 真实 TUI（.10 默认确认权限）：模型思考期间停用插件 → 本回合不再弹出该工具的审批框，模型改用其它方式或如实说明。

## 风险与边界

- 每次工具调用多一次复核：插件为一次安装表读取（有界），MCP 为进程状态读取，内置为常量；不允许在复核里启动进程或建立连接。
- 不在复核里做"自动重新启用"或"追随新代次"，失效就是失效。
