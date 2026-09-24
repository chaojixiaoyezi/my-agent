# 工具调用审批前的有效性复核

状态：设计草案，待用户确认后实现；改动统一权限门，两条开发线共用，实现前须经决策线评审。上位合同见 [可装卸插件方案](PLUGIN_LIFECYCLE.md) 与工具运行时合同（`agent/tooling/models.py`）。

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
→ 校验 → `pre_handler_gate` → 权威 → 工具账 claim → `invoke_registry_tool`。今天没有任何一步在冻结快照之后重新读取可用性；
插件代理的 `availability()`（`plugin_runtime.py`）已经能把激活失效报成 `TOOL_UNAVAILABLE`，但只在建快照时被调用一次。

挂接点（两处共用同一个辅助函数，不新增第二套判定）：

1. `ToolExecutor.execute`：`ActionPolicy.decide` 返回 `ask` 之后、返回 approval_required 之前。
   调用 `runtime.handler.availability()`（没有该方法的替身处理器直接跳过；抛异常按不可用处理）。不可用时返回
   `ActionDecision("deny", ("TOOL_UNAVAILABLE",), {"failure_stage": "runtime_gate", "precheck": "pre_approval", "reason": <availability.reason>},
   resolved_effect=decision.resolved_effect)`，由现有 `_decision_result` 记为 `handler_executed=False`、效果 `not_started`；
   不写 `permission_requested`，不进入执行器，工具账没有该调用的执行记录。
2. `_execute_authorized`：审批通过后再次进入、工具账 claim 之前，只对"经过审批"的调用再跑同一函数。
   这一处覆盖"审批等待期间被停用"的时序；不对免审批调用逐次复核（`test_tool_runtime_scope.py:251` 钉住了这条现行合同，且避免给内置工具加成本）。

接口：`_pre_execution_precheck(runtime: ToolRuntime) -> ToolAvailability | None`，只读，不构造连接、不启动进程、不写状态。
可用的结构化输入：`request`（快照、写边界、审批模式、run 上下文、取消令牌）、`runtime`（spec、策略、冻结的处理器及其 activation_ref）、
`call`（run/turn/attempt/operation 编号、参数哈希）、`decision`。

错误码：沿用 `TOOL_UNAVAILABLE`（不可重试、建议申请能力），具体原因放在 reason / `reported_error_code`（例如 PLUGIN_ACTIVATION_UNAVAILABLE、MCP_CONNECTION_CLOSED）。
不用 `PLUGIN_NOT_ENABLED`（不在错误分类表里，恢复逻辑会当未知码阻塞）和 `PLUGIN_DISABLED`（语义是插件管理总开关关闭）。
顺带修正：现行失败路径报 `TOOL_EXECUTION_FAILED`，分类表标为可重试、建议 RETRY，对已停用插件是错误建议；复核提前拦下后模型收到的是正确的"换工具/申请能力"。

不做：等待审批期间逐次轮询复核（`permission_bridge` 每次 poll 再查）——目前"不可用"在 `round_execution` 里会回退成 approval_required，要先改那条回退才有意义，另开一片。
非插件 MCP 服务在回合中途死亡的情况，同一复核（连接存活事实）自然覆盖，不单独写分支。

## 验收

- 合同单测（fake tool / fake plugin）：快照冻结后、调用前停用插件 → 不写 `permission_requested`、不进入执行器、模型收到 `TOOL_UNAVAILABLE` 与结构化原因；工具账不出现该调用的执行记录（与 TUI236 "审批前拦下即零操作"一致）。
- 合同单测：审批等待期间停用 → 批准后在 claim 之前被第二处复核拦下，同样是 `TOOL_UNAVAILABLE`、无 claim、无执行记录；只有在复核之后、执行之前的极短窗口内失效，才仍走现行执行失败路径（TOOL_EXECUTION_FAILED，not_started）。
- 合同单测：内置工具的复核不产生任何文件/网络 I/O（用计数替身钉住）。
- 真实 TUI（.10 默认确认权限）：模型思考期间停用插件 → 本回合不再弹出该工具的审批框，模型改用其它方式或如实说明。

## 风险与边界

- 每次工具调用多一次复核：插件为一次安装表读取（有界），MCP 为进程状态读取，内置为常量；不允许在复核里启动进程或建立连接。
- 不在复核里做"自动重新启用"或"追随新代次"，失效就是失效。
