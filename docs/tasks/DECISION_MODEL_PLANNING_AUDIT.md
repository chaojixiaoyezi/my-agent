# P5-C 规划与派工建议：只读接缝审计

状态：2026-09-22 只读审计；未实现接入点，未发起 Jev 或 Gateway 请求。本文件只界定首片可实施范围，不代表规划质量已通过真实验收。

## 当前四条权威链

| 事实 | 当前唯一入口与约束 | 对 Jev 的含义 |
| --- | --- | --- |
| 普通任务身份 | `conversation/task_promotion.py::promote_current_conversation_task` 在真实工作工具触发时按当前请求、显式绑定 Goal 或 active task 建立/复用 task link；不能从旧目录、文本建议选择任务。`agent_core/tool_call_runtime.py` 按工具的 `promotes_task` 进入该链。 | Jev 建议本身不能晋升任务、生成 task ID 或选择旧 workspace。 |
| 持久 Goal | `conversation/goal_tools.py::CreateGoalTool.execute` 只为用户或 system/developer 显式要求的目标使用；当前 thread/task 绑定、`transition_guard` 和“至多一个未完成 Goal”均在原服务内。名称不是第二执行器。 | 建议不得变成 `create_goal` 参数或提供任何新的授权依据。 |
| 软计划 | `agent_core/task_progress_tool.py::TaskProgressTool.execute` 的 `update` 才写 canonical 进度账本；`read` 返回原账本。`items[].id` 是稳定身份，`generation_id/plan_revision` 供显示与变更检测；Todo 不决定任务生命周期。 | 仅可把已存在、仍 open 的 exact ID 当排序候选；建议不得直接改 Todo 状态或伪造已完成证据。无账本时首片不让 Jev 自造 ID。 |
| 子代理创建 | `agent_core/orchestration_tools.py::CreateSubagentsTool` 是模型可调用的直属创建入口；根级 `execute_create_subagents_service` 在 manager 创建事务中准备、可选网络建议后重新准备，再由 `_materialize_create_subagents` 经原幂等/保存/发布链落盘；递归创建走 `execute_child_creation`。 | Jev 不能直接调用服务或发布 child。由主代理决定是否调用原工具；原容量、权限、取消、替换、输出路径与幂等门必须照常执行。 |

`orchestration/planned_delegation.py::planned_delegation_failure` 和 `orchestration/dispatch_progress_seed.py::planned_dispatch_contract` 在物化前核对 `covers` 是否对应原账本 open exact ID、是否已被 active child 占用；未绑定 child 合法但不能冒充原 Todo。`orchestration/create_constraints.py::find_reusable_named_child` 只按显式 repair/idempotency/work-scope 合同复用；普通同文 goal 不等于重试。`create_subagents` 工具运行策略为 mutating、operation 幂等、现有工具快照权限上界。当前 `orchestration/decision_subagent.py` 的 Jev 选择是“**已决定创建的 child 用哪个执行模型**”，不是“是否创建 child/创建什么工作”的授权。

另有 `agent_core/planner_service.py::build_parent_planner_state` 读取既有 child、board、due/action、runner/capability 候选，供父级子代理调度；`subagents/services/dispatch/parent_planner_service.py` 是这条既有父级 planner 的运行/报告边界。它不能代替主会话的 Goal、Todo 或 `create_subagents` 创建合同。P5-C 首片不改其既有状态机，也不复制第二套 planner。

## 最小 observe/apply 接缝

先只处理**已有 task_progress open 项的继续工作顺序**，不让 Jev 提出新的目标、子任务正文或新能力。宿主在主代理本轮准备上下文时，从原 run/task 身份、进度账本与同一工具快照构建有界候选：`{ledger_run_id, generation_id, plan_revision, open_item_ids, active_cover_owner_run_ids, available_create_subagents}`。候选只包括原账本 exact ID；若无可信 run/账本/open ID、工具不可用或候选过多无法有界收缩，则按原流程，不请求或不消费建议。不能靠任务标题、用户措辞或 child goal 猜 `covers`。

建议把调用放在 `agent_core/runtime/loop_support.py::_prepare_runtime_context` 已冻结工具快照、准备原运行注入之后，主模型请求之前；采用现有 `conversation/decision_service.py::{begin_decision_stage,decide,decision_outcome_is_current}` 的身份、设置、连接、超时和取消边界。**这是待实现的接缝建议**：目前该函数没有 planning 决策调用，实际插入位置还需在开发片对 `build_runtime_main_context_bundle` 与 prompt 注入重复性做精确设计。首片问 Jev 在宿主有限 ID 中给出一个“优先处理 ID”或 `not_needed/need_data/abstain`，原来源 refs 与候选摘要绑定；响应必须能按 exact ID 校验，且应用前重读原账本 revision、open 状态、active cover owner、当前工具快照与 `decision_outcome_is_current`。TypeSafe 当前仅有 choice/score/null 题型；用候选选择表达优先级，不要求它生成自由文本计划。

`observe` 只记录本次建议/拒绝原因，不改变主模型可见上下文；`apply` 只加一条短的、明确标成“可选建议”的主模型动态上下文，例如“当前原 Todo 的 `item-2` 可优先评估；仍由你按原工具合同决定下一步”。它既不调用 `task_progress(update)`，也不合成 `create_subagents` 工具调用，不把建议 ID 自动写入 `covers`。主代理若决定派工，仍须自己提供具体 child goal/边界，并在原创建工具里接受全部核验。此路径不需要用户逐次选择，用户仅按既有设置开关控制接入点。

默认关闭；只在 `settings/decision_settings_schema.py::POINT_RUNTIME_SCOPES` 注册新的 thread 接入点并复用原 owner/thread 覆盖、Jev profile、总/点超时与 `off/observe/apply` 语义，不另建配置仓库、价格账本或执行器。`off`、超时、断连、冷却、`need_data`、无可消费选择、候选/账本/设置过期均返回原规划输入与原工具行为；用户取消仍向原链传播，不伪装为普通失败。该时间成本受现有阶段/点预算约束，不能把规划建议做成每个模型 token/每个工具调用都触发的网络请求。

## 首片不能做的事与待补合同

- 没有 canonical Todo 的普通新任务、需要从开放问题创造全新步骤、复杂多 child 依赖图、跨 sibling 资源协调，都不能用“已有 open ID 排序”覆盖。主模型按原规则规划即可。未来若要让 Jev 建议新步骤，必须先有宿主生成并授权的有限候选及可验证出处；自由文本不能成为 task/Goal/派工机器事实。
- 当前设置 schema 没有 `planning` 点；当前 runtime main context 没有此 hint 的稳定版本字段。实现前要确定 `operation_id`、账本 revision/candidate digest、每轮最多一次触发与 compact/resume 时的过期规则，避免旧建议越过新用户输入或更新后的 Todo。
- `need_data` 只能指出缺的结构化材料（如 ledger、候选、能力快照），不能要求用户在每次派工前操作。可由主代理按原工具补读；补不齐便走原规划。不得因建议断言 `create_subagents` 可用或扩大 allowed tools/write roots。
- P5-C 是否值得扩到“直接派工建议”，需先用离线/fake 证据证明相对主模型有增益。若扩展，也只从原已授权且已存在的任务/能力候选中推荐，真正 child 创建和 Goal 写入永远留在原工具链。

## 核对范围与验收建议

已读本仓库入口：`AGENTS.md`、`LLM_GUIDE.md`、`docs/ROADMAP.md`、`docs/WORKSTREAMS.md`、`docs/design/DECISION_MODEL_INTEGRATION.md`、`docs/tasks/DECISION_MODEL_GOAL.md`，以及上文列出的 runtime、Goal、task_progress、父级 planner、创建/绑定/幂等、decision 设置与服务源码；对照 `docs/modules/subagent/04-structure.md` 的子代理创建结构。定向运行：`python3 -m pytest -o addopts='' agent_py_agent/tests/test_conversation_goal_tools.py agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_task_progress_tool_dispatch_reconcile.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py -q --tb=short`，**79 passed**。这些测试只验证原合同，未验证尚不存在的 planning 接入。

本机成熟参考限于另一开源 agent 项目参考副本中的 `src/agents/subagent-spawn-child-plan.ts`、`subagent-spawn-launch-request.ts`、`subagent-launch-authorization.ts`：其 plan 解析、launch request 的 `idempotencyKey`、精确 model override 授权分层可作为“建议不越过启动授权”的对照；未审计该项目全部 sessions_spawn/生命周期，也不据此宣称行为等价。未核对 Codex/Hermes/Free-Code 全链或真实 Jev；当前阶段没有必要扩大参考检索。

建议下一步：由独立实现者先补仅“已有 open Todo 的优先 ID 建议”切片，增加 off/observe/apply、超时、`need_data`、revision 变更、工具不可用、取消、普通任务无账本、Goal 不新建、child 不暗建的 fake 决策与 replay 定向测试；通过后用少量真实 Jev 验证是否优于原规划。可与子代理模型选择、召回切片并行，但不得编辑彼此的运行入口或改写原权限/幂等合同。
