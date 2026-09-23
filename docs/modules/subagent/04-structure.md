# Subagent Structure

第 7 步父终态通知已在本地集成，将实际逻辑归入 `RunnerCompletionNotifier`：只持任务关联、WakeStore、
读取父任务和保存错误四项依赖，完成／受控取消共用原投递路径；结果服务和外部停止端负责装配。
保留 exact attempt、直属父级、文件模式及内部监督者信号语义；阶段提醒和能力申请入口不扩改。
已与恢复扫描接口片组合，原 sweep 和恢复测试均绑定同一通知器；旧接口调用删除；组合包 a067baddd 已双机部署，实际验收进行中，详见 STATUS。

第 7 步结果提交依赖已在本地候选收窄：初次提交只接原 RuntimeDB、canonical task、结构化结果、
保存回调和绑定本轮身份的交付回调；WAL 原语只接 save，运行结算与诊断只接原 RuntimeDB。
结果服务装配 trace→父通知，仍在 WAL→运行账之后执行；不新增状态副本或兼容转发。
恢复扫描已集成为显式 repo/load/save/list/notify，原 sweep 绑定窄父通知器；恢复模块不再持有完整 manager。
本片不新增扫描器或业务重跑入口，不声称第 7 步完成；新版部署和原生验收分列 STATUS。

`shell_gateway_execution.py` 只从 `common/cancellation.py` 查询本次执行取消；与本模块 `cancellation.py` 的持久子树权限和资源冻结职责分开。

## 第 7 步结果链职责（本地小片）

- `manager_runner_result_payload.py` 把宿主轮结束事实送入 `runner_result_state.py`，该模块仍负责 typed 状态、失败和当前尝试字段。
- `runner_display_projection.py` 是纯展示函数，只从已裁决的 `status/failure_type` 得出中文活动标签；原状态写回入口在相同时机设置 TUI 的 `current_step/current_tool`，不改变运行事实。
- `runner_completion_payload.py` 是完成内容的只读投影，根父通知、直属父等待和递归父级快照共用；`runner_completion_wake.py` 只拥有原状态更新、投递、去重和错误记录，不再导出完成内容构造函数。
- 完成通知向 `store.wakes.append_observation` 显式传 `retain_handled=True`，由 `conversation/store_wake_publication.py` 在原锁内冻结完整负载、补齐配对并裁决旧消费事实；前置查询不承担去重权威。原 WAL 重试通路不增加新服务，通用 Goal 保持 handled 后新代。
- `runner_result_admission.py` 显式接收原 `RuntimeRepository | None` 与 canonical task，在结果落盘前裁决 exact run／attempt 的接管、废弃、换代及终态冲突；拒绝诊断写同一运行账，不接收完整 manager，原结果服务不再保留旧私有准入函数。
- `services/runner_result_service.py` 负责准入后的结果文件和任务投影；`services/runner_result_commit.py` 按原先后编排 WAL、RuntimeDB 结算与直属父级通知；`services/runtime_closeout.py` 仍是 WAL 原语、恢复扫描与已交付标记的权威实现。
- 终态冲突由同一服务拒绝后，诊断必须写到 manager 持有的 RuntimeDB；只补原 `closeout_blocked` 事件，不让诊断成功与否改变原拒绝结果。
- 结果链拆分不改 canonical run／attempt、锁或 WAL 格式；本地通知修复只将原 dedupe 记录显式升级为可恢复的 v2，旧 v1 在写入口迁移。详细边界见[发布恢复合同](../../design/closeout_state_machine.md#唤醒配对发布的半写恢复第-7-步本地实现)及[并行执行设计](../../design/SUBAGENT_PARALLEL_EXECUTION.md#第-7-步结果链迁移边界进行中)。

## 原创建锁与执行轮短事务

- `coordination.py` 复用 `.create-subagents.guard` 的原线程锁和文件锁，仅为同进程/线程/canonical 路径处理嵌套持有；不另存任务状态。
- `services/base.py::create_run` 在实际登记处持锁；上层模型创建批次使用同一边界，不能绕到第二创建入口。
- `services/lifecycle_runner_attempts.py` 在锁内完成原 DB 激活和投影发布；放弃旧轮通过最新 canonical mutation 精确清指针。
- `orchestration/tools/capability.py` 排队前锁内复读控制终态；`dispatch/capability_auto_sweep.py` 核对原 attempt/session 后才重排失联者。
- `conversation/agent_control.py` 按单 run admission→creation 排队；`_AgentGuidanceDelivery` 仅交接响应和启动需求，启动/探测不持 creation 锁。
- `runner_start.py` 只接纳原 pending 并核对身份；原 lifecycle 服务拥有 launch 条件 mutation 的真实实现，进程内与 CLI 直接调用，不保留自由函数转发或新增边界例外。
- `file_runner_start.py` 只处理显式无数据库模式：预留与消费写原 canonical 记录；启动身份和生命周期同次 mutation，旧整任务保存只可回收同一身份。
- `DispatchParams.expected_attempt_ids` 是宿主字段，经隐藏成对 argv 运输；普通模型 request_params 不读取它。顺序和并行 worker 使用同一映射。
- `RuntimeRepository.create_attempt(expected_pending_attempt_id=...)` 在同一写事务只激活原 pending，失效不创建后继；session 发布与心跳沿同一 attempt。
- `subagents/cancellation.py` 在原 creation 内关闭原树权限、暂停子 Goal、冻结后台/PTY；`cleanup_subagent_stops` 不接收 agent，只消费固定批次。
- `conversation/task_resources.py` 合并主/子清单；主 Goal→task→短读 Gateway T，释放 T 后 creation→子 Goal，进程等待在所有控制锁外。
- `runner_control.py` 读取原取消状态；原 session 心跳在独立 worker 内发送 exact attempt 中断，注册后模型前再核对。
- `cancellation_hosts.py` 只读观察冻结的原宿主退出，不把 launch/PID 当整个 OS 树独占证明。
- 创建回执读取最新 canonical，不要求控制域原地改写旧对象；旧 overlay 只在本轮激活后窄写。
- 插话回执重放与文件模式一次性接纳均已补入源码；完整开发验证与发布仍待收口，不能替代新版真实 TUI。

## runner 当前错误投影

- `manager_runner_result_payload.py::apply_status_and_build_payload` 将显式 `turn_end_reason` 传入既有
  状态上下文；后续为展示推导的原因不能用来证明本次没有失败。
- `runner_result_state.py` 复用 `turn_end.py::subagent_outcome_for_turn_end`：只有已知原因对应的状态和
  `ok` 同时匹配、且没有显式失败时，才清除当前 `failure_type` 与 `runner_last_error`。
  正常让出仍为 `PENDING/ok=False`，不改成完成；后续正常完成也不遗留上一轮错误。
- 缺失或未知原因、状态不符及显式失败保留原有保守诊断；不改历史结果、attempt 身份、权限、收口和通知顺序。
- 定向入口 `test_subagent_runner_result_state.py` 覆盖正式管理器落盘、结果归档、正常让出与完成、
  模型失败、能力阻塞、取消和冲突组合；真实逐项唤醒及停止证据见
  [并行执行验收](../../design/SUBAGENT_PARALLEL_EXECUTION.md)。

## 派工事实与角色提示

- `agent_tree/model_view.py` 从同一已授权快照投影模型状态；`list_agents` 不再原样输出界面恢复细节。
- `orchestration/child_result_index.py` 的 `read_order` 不含恢复摘要；实际产物优先，缺产物时可读已存在的终态报告。
- 大结果继续由统一工具归档保存；实时摘要明确省略范围并保留逻辑读取入口，不切坏身份或路径。
- `orchestration/dispatch/state_contract.py` 仅汇总 run 状态、缺失与恢复事实，不为父级推导等待或完成动作。
- `orchestration/coordinator_policy.py` 是根、递归协调者和创建入口共用的软分工说明，不构成权限或完成门。
- `runner/prompts.py` 按创建时快照装入当前角色行为；协调层只另取角色索引，不装载所有角色正文。
- 协调者可在用户授权内承担独立实施和整合；结果存在性与引用以工具和 canonical 交接为准，不要求重复写入。
- 定向入口：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`、`test_tool_context_reducer.py`；真实分工结果不能由这些测试代替。

## 执行器退出与积压恢复

- `services/runtime_closeout.py` 的无操作结果不接受任意扩展参数；读取权威行失败在原收口入口显式返回
  `write_error`、精确 run/attempt 与重试事实，持久恢复及通知仍只有原有一条路径。

- `runtime_db/executor_liveness.py` 在真实执行边界记录 exact attempt 元数据；本进程注册只在执行期间存在，
  不以 Gateway PID 或旧心跳代替真实执行器。退出事实不产生模型调用。
- `services/executor_recovery.py` 复用正式 runner_result/WAL/父级通知；没有结论的退出显示失败，
  存在未决工具则保留 UNKNOWN 与执行锁，显示阻塞，不能凭全部工具成功反推整个任务成功。
- `runtime_db/repository.py::pending_events_page` 按未消费事实分页轮转；游标与消费回执分开，
  原事件不删除，处理失败可在下一圈重试，旧记录不受最近 50/100 条窗口限制。
- 定向回归：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`；真实 TUI 验收另记，
  不以无模型回归代替真实模型通过。

### R291 追加：未知/脏 run 状态不再静默丢弃 runner 结论（僵尸根因之一）

**现场（104 轮真机注入）**：执行中把 run 状态改成未知值后，`status_conflict` 被写下、runner 停止，
但**没有 runner_result、没有待重试事实、没有父级唤醒**，task 停在 RUNNING；把记录改回合法 `created`
后仍挂住。根因之一是 `_managed_runtime_result_conflict` 把"未知/脏 run 状态"当成权威冲突直接丢弃结论。

**语义修正**：未知/脏 run 状态**不是**权威终态事实，不得据此丢弃 runner 的最终结论。现在结论照常落账
（runner_result + task 投影），run 收口由 closeout 记为 `unknown_status` 待重试：不猜成功、不覆盖未知行、
不改写权威状态，也不通知父级；记录被修复后再补收口与通知。配合 `runtime_closeout_pending` 投影，
owner 仍会被发现层当硬事实驱动，恢复链每周期重试（诊断事件有界：首个周期与每 10 次各一条）。

**仍未覆盖（未验）**：runner 在**没有任何结论**的情况下消失（例如进程在切片中途死亡且从未登记
runner session）时，`_dead_running_reclaim_facts` 因 `if not session: return None` 直接放弃，
open attempt 会一直挂着。它的判据应当建立在"执行器确证退出/失去宿主所有权"的结构化事实上
（execution lock 的 holder pid+start_token 存活判定、无 in-flight 工具操作、runner session 的
ended/进程事实），而不是心跳年龄或"没有输出"；健康慢首包/持续慢流/合法工具进行中必须继续存活。
这一缺口的修复入口见本页“执行器退出与积压恢复”；旧版记录仅用于解释问题来源，当前边界以该节为准。

### R291 追加：收口事实写不进去时不得继续不可恢复的后续动作

**缺口（监督者复现）**：`record_pending_closeout` 返回 False 时旧实现只把 `wal_active` 置 False，
随后**仍** settle、**仍** deliver；一旦 deliver 失败，父级永远收不到通知，且没有持久化重试也没有诊断
——注释写的"先成功持久化再收口"并未落实。`clear_closeout` 的返回值同样被忽略，清账失败会报 advanced 成功。

**修法（都在既有结果/恢复主链内，不另造队列）**：

1. `ensure_closeout_fact()` 返回三态 `persisted / unpersisted / not_required`：canonical task WAL
   写失败先重试一次（吸收瞬时抖动）。
2. 仍失败 → `record_unpersisted_closeout()` 在 **runtime 事件账本**（另一个存储域）落
   `closeout_unpersisted` 事件，带精确身份（run/attempt/agent_run/target_status/turn_end/结果与
   output 引用）；两处都写不进去则响亮记录并**停止**本片收口与通知（不再继续不可恢复的动作）。
3. `restore_unpersisted_closeouts()` 在 task 存储恢复可写后把事件还原成正式 WAL，再走既有推进；
   事件账本 append-only，用同身份的 `closeout_restored` 标记做消费判定，避免每周期重复还原。
4. 交付顺序改为「先持久化 delivered，再清账」；`clear_closeout` 失败不再报 advanced 成功
   （新增 `cleanup_failed` / `delivery_mark_unpersisted` 两个诚实回报状态），已交付的唤醒不会因为
   清账失败被重发。

守卫用例：`test_closeout_fact_save_failure_stops_and_stays_recoverable`（只在 canonical task
已带 WAL 属性的那次 `manager.save` 上注入失败，确保前一步结果保存真的成功；断言不 settle/不通知、
事件账本留下可达事实、恢复后收口与通知恰好一次、重复恢复幂等）、
`test_closeout_cleanup_failure_is_not_reported_as_success`（清账失败不得报成功、通知不重）。

### R291 追加：被拒收口事实只诊断一次后清账

恢复链原先对被拒事实（`stale_attempt` / 冲突终态）只写诊断、不清账：由于重试永远不会成功，
每个 reconcile 周期都会重复写 `closeout_blocked`，且该 owner 会因这条永久事实被反复当成硬事实扫描
（无界重试 + 事件洪泛）。现在被拒分支写一次诊断后 `clear_closeout()` 清账；权威 run/task 事实照旧成立，
换代后的新 attempt 走它自己的正常收口与通知。守卫用例
`test_rejected_closeout_fact_is_cleared_once_not_retried` 固定"只诊断一次、不无界重试、不洪泛"。

### R291 追加：owner 投影新增 runtime_closeout_pending

`build_owner_agent_projection` 增加结构化布尔量 `runtime_closeout_pending`（来自 canonical task
`attributes.runtime_closeout_pending`）。它不是新的权威，只是让 owner 级发现层能看见"这个终态 run
还有一笔收口/通知欠账"，从而让 Gateway reconcile 车道继续驱动恢复；权威与恢复语义仍在
`services/runtime_closeout.py`。

## 2026-09-14 R291 收口可恢复：待重试事实 + 一致终态重入（修 e00866d2 剩余两缺口）

**缺口 1（收口写库失败被吞）**：`settle_runtime_run_for_result` 把写库异常 `catch Exception → None`，
`record_runner_result` 丢弃返回值并无条件 `notify_parent`；`settled=False` 同样无人处理。结果：
task/wake 已是终态而 runtime 仍 `created`，且**既没有记录也没有持久化待重试事实**，临时写库失败会
永久留成矛盾；监督者指出这不是 fail-soft 记录。

**缺口 2（re-entrant 被误判为冲突）**：`settle_agent_run` 只改「未 ended」的 attempt，所以真实事故
形态是 `run=failed + attempt=done`。该形态下同一 `is_current=True` 的 FAILED 重放被
`_managed_runtime_result_conflict` 判成 `conflicting with runtime terminal fact run=failed attempt=done
incoming=failed`——只有 `failed+failed` 才放行。若在「run 收口后、父 wake 前」中断，重放无法补通知。
旧单测只看 `replay_same.status==FAILED` + 事件数不变，被拒的 quick-result 也能通过，不是真幂等恢复。

**语义（对照既有结构化 run/attempt + 持久化结果 + wake 去重主链）**：

1. **收口结果结构化**（新模块 `agent/subagents/services/runtime_closeout.py`）：`settle_runtime_run_for_result`
   返回 `state ∈ {settled, already_consistent, not_applicable, write_error, unknown_status, missing_run,
   conflict, stale_attempt}`；写库异常不再返回 `None`，而是 `write_error` + 有界错误文本。
2. **事实先行、未提交不通知**：终态结论先在子代理 canonical task 的 `attributes.runtime_closeout_pending`
   落版本化 WAL（`subagent-runtime-closeout.v1`，存 run/attempt/agent_run/目标终态/turn_end/结果与
   output 引用/交付状态/重试次数），再动权威账本；`retryable` 时保留事实并**不唤醒父级**，
   `rejected`（冲突/换代）只留 `closeout_blocked` 诊断且绝不覆盖既有终态。
3. **恢复只补记账/通知**：既有关键sweep（`supervise_stalled_orphans`）确定性调用
   `recover_pending_closeouts()`：补 settle 权威 run → 补父级 wake → 清事实；不重跑 runner、
   不建新任务、不读自然语言、不依赖文本触发。单条推进失败不拖垮整轮。
4. **一致终态可重入**：run 已是终态时只放行 `incoming_terminal == run_status`（同一 exact current
   attempt 的一致终态，含 `run=failed + attempt=done + incoming=failed`），冲突终态与 stale/换代仍拒。
5. **通知不丢不重**：wake 去重键按 exact attempt 定身份
   （`subagent-finished:{task}:{status}:{attempt}`，attempt 未知时退回既有形态），并新增
   `ConversationStore.wake_delivery_receipt()` 回执查询；同一 attempt 已 pending/handled 时回报
   `already_delivered` 不重发，换代后的新 attempt 仍正常通知。

**验收**：`test_closeout_interrupted_before_notify_recovers_once`（收口后通知前中断 → 恢复恰补一次）、
`test_closeout_write_failure_leaves_retryable_fact_then_recovers`（写库一次失败 → 留事实、不通知、
恢复后补收口+补通知）、`test_repeated_closeout_recovery_is_idempotent`（重复恢复幂等）、
`test_consistent_terminal_result_is_reentrant_but_conflict_is_rejected`（重入放行/冲突仍拒）；
BLOCKED 用例改为排除**全部**终态（`AGENT_RUN_TERMINAL_STATUSES`）并断言 attempt 未被顺带结清。

## 2026-09-13 R287 runner 最终结论必须收口 runtime 权威 run

**问题（真实 TUI 注入后独立 SQL 发现）**：宿主机对"可续跑族"（如 `MODEL_STREAM_INCOMPLETE`）只调
`settle_agent_attempt` 结清 attempt、把 `agent_runs.status` 留在 `created` 等 resume；`record_runner_result`
通过 guard 后只写 task/runner_result 并通知父级，**没有收口 run**。结果同一事实上出现两种矛盾结论：
`canonical_state.status=FAILED` / `runner_result=FAILED` / 父 wake 已消费，而 runtime.db 仍是
`run_status=created`、只有 `agent_attempt.completed`、没有 `agent_run.completed`。

**语义（对照既有状态机）**：runner 的最终 typed 结论就是该 exact current attempt 的终态事实，因此
`settle_runtime_run_for_result()` 在 `_post_result_side_effects` 之后、**notify_parent 之前**按结论收口 run：
`FAILED→failed`、`CANCELLED→cancelled`、`DONE→done`；**PENDING/BLOCKED/RUNNING 等非终态不收口**
（可恢复等待/Compact/健康慢流不得被误结束）。收口只针对 `agent_run_for_run_id` 指向的同一条 run，并带
`attempt_id`（stale/换代保护不变）；`settle_agent_run` 本身是单事务 CAS 且以第一次为准 → 重放幂等。
收口失败（stale attempt / 写库异常）fail-soft，不改变已落账的 runner 结论，也不阻塞父级唤醒。
**顺序**：任务与 runner_result 落账 → run 收口 → 父级唤醒，避免父级在矛盾窗里被唤醒。

**验收**：账本级集成测试断言最终 `agent_runs.status=failed` + `agent_run.completed` 存在 + 重放不重复收口；
新增非终态用例断言 `BLOCKED` 不给 run 写终态。

## 2026-09-13 R285.1 精确 attempt 的终态收口（僵尸 RUNNING 根因）

**冲突如何形成**：模型流异常（`stream_eof` → `MODEL_STREAM_INCOMPLETE`）后，
`_settle_main_agent_run_status` 按"可续跑族"只结清 attempt（`agent_attempts.status=done`），把
`agent_runs.status` 留在 `created` 等 resume；随后 runner 对该 attempt 回写 FAILED，
`_managed_runtime_result_conflict` 的组合判定只接受 `run∈{"","created"} ∧ attempt=running`、
`done/done`、`failed|cancelled 对齐`，**以及 turn_end 映射为 PENDING/BLOCKED 的情形**——
`MODEL_STREAM_INCOMPLETE` 映射 FAILED，于是被判冲突并**静默丢弃**（旧实现的 `_make_quick_result`
不写文件/不写 record/不发事件/不发 wake）→ 任务永久 RUNNING，父代理永远收不到完成信号。

**正确语义（已改）**：`run=created + attempt=done` 就是"宿主已按可续跑族结清 attempt、run 留给
resume"的签名；该 attempt 不会再产出，runner 对**同一个 attempt** 的终态回写是唯一事实来源。
因此 `_runner_result_matches_settled_attempt` 在 `is_current` 为真的前提下，接受
`FAILED/CANCELLED`（turn_end 与 status 必须一致）并按其收口 run；PENDING/BLOCKED 原规则不变。
**过期保护未放宽**：stale/superseded/abandoned attempt 仍在更早的闸口被拒，测试同时锁两侧。
另：冲突被拒时一律写 `closeout_blocked`(`reason=runner_result_conflict`) 结构化诊断，不再静默。

## 2026-09-13 R284 后台上下文续接 / 主代理等待 / 终态静默 / Goal 唯一性

1. **后台工作片与前台共用同一份 canonical 历史投影**：`conversation/runtime.py::_background_conversation_history_seed`
   复用 Gateway 的 `_gateway_conversation_refs`（未压缩尾部行 + provider 消息投影，含工具块），
   `_run_params(..., history_seed=)` 写入 `RunParams.conversation_history_seed`，`context_markdown` 在
   有 seed 时**不再重复注入** `## Recent Messages` 摘要副本；窄范围审计事件（finding/capacity）保持
   只有事件事实、不带 owner 历史。压缩仍由前台 Compact 与本片 `context_overflow→compact` 路径负责，
   seed 构造**只读**、不触发压缩。
2. **主代理等待不再替换模型正文**：`queue_interim_reply_for_open_subagents` 恒返回 False，
   `_tool_loop_service` 不再因活跃子代理改写用户可见回复并继续空转；活跃子代理只作为状态/展示事实，
   后续工作由明确的子代理生命周期事件唤醒（对齐 会话运行时 `wait_agent` 由模型显式调用）。
3. **终态冲突不再静默**：runner 结果被运行时终态闸拒绝时，写
   `closeout_blocked` + `reason=runner_result_conflict` 结构化事件（含 run/attempt/incoming/task 状态）。
4. **同一 task 只允许一个未完成目标**：`store.create_goal` 与 `create_goal` 工具都拒绝在同 task 上
   隐式再建（名字不同也拒绝）；不按 updated_at 猜、不自动 supersede，收口只由显式 `update_goal`。
5. **流末事实落账**：模型调用账本记录 `stop_reason`/`runtime_reason`/`turn_end_reason`/`truncated`，
   空正文/断流类事故可直接从调用账本读出。

## 2026-09-13 R283 JSONL 记录边界统一为物理 LF（真实事故修复）

**事故**：子代理 transcript 的 JSON 字符串里含 U+0085(NEL)，`path.read_text().splitlines()` 在 NEL
处把一条完整记录切成两条 → 2026-09-13 三个 child（`subagent-1789309101-d6832a68/-72be5b55/-ec2d50a9`）
的 `runner_result`/`final_report` 报 `conversation transcript is unreadable`，`append_message_once`
因 `recent_messages_report` 的 load_errors 抛 `DataCorruptionError`，整个 child 判 FAILED。
同一批文件按物理 LF 读：175/162/154 条记录、0 错误；按 `splitlines()` 读：190/163/158 行、19/2/5 个 JSON 错误。

**修复边界**：唯一实现 `common/json_io.py::jsonl_lines()`——只按物理 LF 切记录（末尾容忍一个 `\r`），
保留字符串内的 NEL/U+2028/U+2029/VT/FF/FS 等字符；**不清洗字符、不吞坏行**。真正的半行、截断、
非法 JSON、非对象行仍然逐条产生结构化 `load_errors`（守卫测试两侧都锁）。所有 JSONL 读取点统一改用它，
包括会话账本全量/尾部倒读、协作账本、审计账本读取与重写、memory_archive 各分片、gateway history/late/
http 事件、takeover readiness、CLI resume、identity/daily memory。会按行重写文件的路径（审计清理、
task workspace 摘要同步）同样改用它，避免"读时切开、写回落成 LF"的静默改写。

## R232 回收结果字段名

`delete_policy.completion_requires` 是模型侧自查清单，字段名必须与 `controlled_exec` 的
`payload.trash` 结构一致（`trash.moved` / `trash.manifest_ref`）；它仍是软提示，
不参与授权、执行或完成判定，机器判定只看结构化 payload 与 grant/路径门。

## R228 角色与授权分离

`role_template_catalog/builtin/bug_finder.json` 只负责职责软提示；`model_guidance` 对主/子共同说明检查范围、
委派不扩权与证据边界。工具 Schema/结构化能力仍由原角色快照提供，修复没有新增角色专项机器判断。

## R227 默认终端工具面

`role_templates.SHELL_SESSION_TOOLS` 是普通命令、后台续接、PTY 的统一默认组合；公开派工的
`orchestration/tool_grants.py` 和可写角色默认消费它。PTY 不成为 run_command 显式授权的隐式依赖，
因此精确只读/Audit 授权保持原边界，最终执行仍受 owner 与父代理范围约束。

## R223 失败与共享文件契约

`orchestration_tools.CreateSubagentsTool` 的异常分支保留客观系统故障，不伪造 schema 错误；
`direct_parent_lifecycle` 和 `capability_auto_sweep` 的检查失败保留 unavailable 事实。
底层已有任务 ID/operation 幂等不变，结果只提示先核查可能已创建的对象，不自动选择重派。
文件读写一致性通过 `common/file_version.py` 的观察版本及明确前置条件表达，不从 parent/child 角色、
交付路径或任务名字推导写权限。兄弟代理共享家目录的协作和跨 owner 硬边界不变。

## R219 上下文快照与 Compact 同源

`call_runtime` 在真实 preflight 写入 exact `agent_thread_id` 的 `ConversationThread.model_context_usage`。
`context_usage` 只接受数字协议并验证 Compact generation；旧 stage_trace/run 属性的写读入口删除。
名册和子页同次读取数字及代次，缺值用 `context_known=false` 显示破折号，不能把未知说成零 token。
原 runner 阶段追踪继续刷新心跳，不改生命周期；显示字段从模型 bundle 排除，计费和校准继续用原账本。

## 独立模型选择入口

`create_subagents.model` / `items[].model` → `create_policy.create_task_attributes` →
`settings/model_profiles.inherit_model_profile`：省略继承父级来源，显式值经 `resolve_child_model_profile` 在当前
owner 私有配置中解析名称/ID，绑定原 host_model_profile.v1。root/批次/递归均先构造完整规格再写任务，
失败返回 TOOL_INVALID_ARGUMENTS/not_started。runner 继续通过 inherited_model_config 复用原恢复路径。
模型只传公开引用，不传 key/base；名称重名需 ID。`parameters.subagent_intent_identity` 仅在显式选择时
包含 model，无 model 的历史键不变；没有另建模型队列或 Gateway。

## 子代理结束原因同源

`agent_thread.append_subagent_thread_result` 用 `result_turn_end_reason` 保存 canonical final；
`run_flow._run_subagent_model_turn` 提交后把同一原因传给 `BackgroundTranscriptSink.finish`。
sink 只发布独立 system_message 技术提示，不修改生命周期；历史投影也复用 `turn_end_notice`。

## 同步命令与交互输入

`tooling/shell.py` 的普通 run_command、`shell_gateway_execution._run_subprocess_with_budget` 的
controlled_exec 和 `attempt/sandbox.py::run` 都是非交互批处理，spawn 显式使用 DEVNULL；不能继承
Gateway/TUI 的 fd 0。显式命令管道/重定向仍由子 shell 建立；PTY 继续绑定独立 slave fd，MCP 使用协议管道。
此修复不改变命令授权、输出预算、取消和进程终止证明，也不解析确认文案或自动输入 yes。

## 正常接续与失败恢复

`runner/worker.py::_resume_direct_parent_after_session` 先按 exact child 释放直属等待，再交给
`capability_auto_sweep.auto_start_orphan_run`。该入口不再限制累计工作片次数，也不为 Audit 另开豁免，
而是共用 `runner/dispatch.py::_is_dispatch_runner_candidate` 的等待、能力、终态和失败预算。
RuntimeDB UNKNOWN 仍在创建 attempt 前被阻断；不会把已失败状态重写成可续跑状态。

## 普通代理的家目录范围

R184：main/child/grandchild 的普通文件工作区是同一个 owner home，不再按业务 tasks 目录收窄。
`run_workspace` 保留 parent/run/恢复记录；`conversation_execution_cwd` 独立表示文件起点，创建 child 时按
owner 校验。普通目录名不触发路径别名、task 选择或权限变化；exact-source Audit 继续使用明确窄范围。


本文只描述当前子代理主链路。

`SubAgentManager` 不再接收 `closeout_for_all_task_nodes`，`runner_result_service.py` 也不生成
`task_node_closeout` 副本。canonical task/result 是唯一结果事实源；父代理通过结构化 status、blockers、
findings、artifact refs 和 result payload 阅读子代理工作，再由模型向用户汇总。

## Capability request 的授权目标

- `problem`、`expected_output` 与 `needed_capability` 是人/模型可读说明，不是授权对象。request 必须在
  `requested_tools/requested_mcp_tools/requested_skills/requested_commands` 或
  `path_scope/cwd_scope/network_scope` 中声明至少一项结构化目标，才能进入 canonical request 账本。
- scope 只限定边界，不能凭空变成工具。routine auto-grant 仅对精确工具、命令或 typed shell 生成有效 grant；
  path-only、Skill、MCP、network 和非 routine 工具由直属父级按其真实 authority 裁决。旧空 request 也不能
  被补成 `tools=[]` 的 GRANTED。

## AgentRun guidance successor

- 用户从 TUI/Web/IM 对一个可见 child 插话时，控制面先做 exact owner/conversation/root/run 授权，再以
  `(owner_id, run_id)` 的短 admission 锁保护“读 current attempt → 预留 successor → 写 durable guidance →
  接纳启动”。锁不覆盖 provider 调用或 runner 生命周期，不同 owner/run 互不阻塞。
- RuntimeDB 的 `queue_pending_attempt` 只排队不执行：一个尚未启动的 current pending attempt 可幂等复用；
  running、unknown、非终态异常、遗留 execution lock 均 fail closed；合法 terminal predecessor 才能在同一
  `BEGIN IMMEDIATE` 事务内递增 generation、CAS current pointer 并写 queued events。`create_attempt` 仍是
  唯一取得执行锁和激活 pending 的入口。
- guidance receipt 记录 exact `expected_turn_id`。启动前失败允许同 stable id 重放；只有同 AgentRun 的旧
  attempt 已终态，才能把尚未 provider-submitted 的 receipt 重绑到新 pending attempt。submitted/consumed/
  rejected receipt 不再启动下一轮。模型消费后由 typed reply obligation 约束当前 agent 回应用户，不从消息
  文案猜“是否需要回复”。

## Exact stop admission 与 terminal fencing

- `enqueue_agent_stop` 在调用线程内完成 exact owner/conversation/parent/run 鉴权和终态短路，然后按稳定键
  去重并启动唯一 daemon cancel；它返回 typed accepted，不直接写 CANCELLED。真正终态仍由现有 canonical
  cancel service、attempt interrupt、manager 和 persistence 收口。
- 用户从 TUI/Web 触发的 stop 在整棵目标分支收口后必须额外交接一次 typed `CANCELLED`：直属 root child
  复用 canonical conversation completion wake，nested child 只释放并恢复 exact direct parent。模型本轮
  调用 `cancel_subagents` 已同步取得工具结果，root `/stop` 也有自己的整树停止边界，二者不重复发布该 wake。
- direct-parent reconcile 不把所有 CANCELLED 当失败。它读取目标 canonical
  `attributes.cancel_subagents.source`：`user_agent_control` 是父轮外发生的新事实，即使同批 sibling 仍运行也
  立即恢复 parent；`cancel_subagents` 是父模型本轮已处理的动作，仍按普通终态合批。周期恢复复用这份耐久
  来源，不能依赖 TUI 进程内参数或取消 reason 文本。
- `cancel_subagents` 优先解析 exact child，再使用 durable conversation task identity；短生命周期 request id
  不能覆盖父级身份。signal 只针对该 attempt，siblings 不进入停止集合。
- persistence 默认 `allow_terminal_reactivation=False`；保存 runner snapshot 前如发现 canonical store 已有
  更新终态，就恢复该终态而不是覆盖。普通 heartbeat 同样在 terminal 时退出。只有显式结构化 user-stop
  recovery 才可传 true，不能从自然语言“继续”或旧状态标签猜复活。
- `lifecycle_runner_attempts.reconcile_dead_runner_attempt` 是 runtime.db 与 task 文件投影之间唯一的重启恢复桥。
  exact `agent_run.completed` 带 `runtime_status` 表示普通模型轮已经自然收口：它必须复用
  `runner_result_service` 补写 task/result、capability/source-worker 覆盖与 parent wake，不能 abandon 或重派。
  `reclaim_orphaned_attempt` 生成、没有 `runtime_status` 的终态只证明死 attempt 已安全封存，才允许
  `RUNNING -> PENDING`。因此终态来源而非状态字符串本身决定恢复动作。

## 2026-08-25 main/child 跨回合工具终态折叠

- 每条 agent ConversationThread 继续独立；父子不复制 transcript。某个 main/child 回合结束后，宿主仅从
  该回合 canonical archive 生成一次 `conversation_terminal_tool_fold.v2`，写进同一 assistant metadata。
- fold 是模型续接投影，不是第二账本、ToolCall/ToolResult 伪造物或 Compact。公开正文、TUI transcript 与
  channel delivery 保持原样；完整工具输出、操作事实和产物继续分别由 owner archive、operation ledger 和
  artifact refs 掌权。
- V2 一次固定生成 hot-tail、cold-fold 和 deadline；缓存热期附加热尾，过期后附加短折叠，旧 V1 恒按
  cold-fold 兼容。真正 Compact 才把
  fold 纳入摘要并推进 generation；`compact_source_tool_pairs` 只累计运行中 native IR 真压掉的完整工具对，
  当前尾部 fold 的回合/调用数单独投影，禁止双计数。
- `ModelCallLedger` 仍提供 exact request/run 的累计调用快照；每次运行收口以物理调用累计数作为 cursor，
  `store.model_usage` 的 `ModelUsageStore` 在同一 JSONL 追加锁内减去该 scope 已落盘增量，再写一条带原快照 digest 的
  delta。这个账只管用量，不推进 Compact、任务终态或 child attempt；重放同 cursor 幂等，异值复用 fail closed。

## 2026-08-25 长期进度账本与当前回合展示计划

- Todo 与覆盖目标的部分更新区分“未提供状态”和“新建默认状态”。更新备注/优先级不会把原状态重置
  为 pending；即便传更正标记，也只有明确传入的状态才可参与更正。新项仍默认 pending，参数身份门不变。
- Todo 项的原生 Schema 同样声明 ID 必填，避免生成合同说可省略、运行时却不断拒绝；新项标题和状态取值
  仍由 handler 返回具体错误，已有 ID 的部分更新无需强填标题或状态。
- task-path `task_progress.v1` 继续保存跨阶段完整历史，是唯一耐久进度事实；TUI/Web 当前清单只是投影，不能
  删除旧项或重新定义完成状态。
- 宿主在普通 conversation request 开始时以 exact request id 建立 `display_plan`，`task_progress` 与
  `create_subagents` 只把本次结构化更新涉及的 item ids 加入同一代。lifecycle wake/child continuation 继承
  同一 request id，新普通用户回合才换代。
- activity poll、tool result、child detail 和 final notice 都携带 `generation_id + revision`。客户端先清新代，
  再拒绝其它代或更低 revision 的迟到快照；没有 display plan 的旧 ledger 仍展示全部项，便于滚动升级。
- display identity 只治理展示新鲜度，不参与派工、完成、验收或恢复。Compact 次数仍独立读取 conversation/
  child canonical thread generation，不能由 Todo 代次或可见历史推算。

## 2026-08-25 lifecycle mailbox 接收者边界

- `root_task_id/parent_run_id` 是血缘事实，不是广播订阅。根 child 的 completion/capability wake 只属于 exact
  conversation parent；`task_local` child、孙代理、控制面和辅助轮不能枚举或确认这份队列。
- `runtime/guidance.py::_pending_task_events` 在读取 durable wake 前先校验当前 scope 是主代理
  `default/conversation`。后台主代理仍以 default scope 消费合批 snapshot；被本轮 snapshot 选中的 id 继续由
  scheduler 精确 ack，未选 id 保持 pending。
- 子代理的用户插话使用 `target_type=agent_run + exact run_id`，不依赖 root lifecycle mailbox。实现不得为了
  修父级漏信而关闭、广播或改写 direct child guidance。

## 2026-08-25 后台命令工具依赖闭包

- `run_command` 的 schema 自带 `run_in_background=true`，因此 `process_session` 不是模型另行申请的可选便利，
  而是同一能力的续接面。`role_templates.active_model_subagent_tools` 是该依赖的 canonical closure：任何模型
  快照只要含 `run_command`，就按原顺序在其后补一个 `process_session`。
- 新建 role/coding preset、动态 capability grant、磁盘恢复的旧 `allowed_tools` 和后台 main 配置都经过该
  closure；`runner_context_service._runner_allowed_tools` 是 child 真正执行前的最后补齐点，不能只在创建回执
  或 prompt 里声称存在。
- owner/task 的结构化 `disabled_tools` 在补齐后做最终收窄，仍可明确禁用 `process_session`；session id 只做
  定位，实际读日志、等待和停止还要通过 executor 注入的 owner/TUI scope 精确匹配。这不新增自然语言授权，
  也不扩大 workspace 或 shell access mode。

## 2026-08-25 root completion mailbox 合批边界

- 每个直属 child 的 `subagent-completion.v1` 都是独立耐久交付义务；canonical 树全终态只证明可以整合，
  不能证明 root 模型已经读取每份结果，也不是处理某一份已完成结果的前置条件。
- 成功通知只保留 `background_completion_coalesce_seconds` 的短窗口；以已持久信封的创建时间
  为准，慢兄弟不会无限延后。原批次筛选、逐 ID 确认与跨页恢复保留。
- scheduler 可把同一 exact root 的多个成功信封合成有界模型轮。选中的 batch 由 background context projector
  缩短 `completion_message`，同时保留 child id、status、`final_report_ref`；预算外 sibling 仍在 durable queue。
- 后台轮开始时记录队列采样快照：快照内但未入 active batch 的信封不能走 mid-turn 瘦事件通道。root 任务
  关闭和用户最终回复都要等这些延期信封被后续批次完整读取；每批提交后只 ack 本批精确 id。
- 该结构适配 会话运行时 `forward_child_completion_to_parent -> parent mailbox`：调用次数可以合并，完成消息不能
  因 token 预算或普通 pending-list 展示上限被提前确认。

## 自然结果交接与唯一文件起点

- `runner_result_service.py` 在自然与结构化 final 的共同出口调用 `result_registered_artifacts.py`，
  只消费 canonical agent run 下的工具产物 registry。ready 记录按 artifact id 取最新、按 exact run 筛选；
  工具归档、其它 run、删除/missing 不进入产物列表。文件存在不等于项目质量验收通过。
- `context_bundle_refs.execution_cwd` 是声明文件与工具的共同起点，允许根不表示多个目标目录。
  绝对声明不重写，相对声明不移入内部 output；旧 delivery map、收口复制和声明写授权已删除。
- 创建回执明确 `task_root_role=internal_run_state`，另给 `execution_cwd`；模型投影不再列内部
  task_work_dir/task_output_dir 作为可选交付位置。恢复记录与用户业务文件分开。

## 2026-08-25 create_subagents 单派/批量目标合同

- 单派以顶层非空 `goal` 为 child 的完整目标；批量以非空 `items` 为权威，每个 item 的非空 `goal` 分别
  约束对应 child，顶层 `goal` 只是可选的人类可读批次说明。
- native schema 不把任一互斥形态静态写成全局 required；root 与 descendant 在统一 handler 中校验至少
  命中一种。空调用、空批次且无单目标、item 缺目标仍在创建任何 run 前原子失败。
- 该结构对照 会话运行时 v1 `spawn_agent`/`parse_collab_input` 的“schema 可选、运行时判形态”，避免为了批量
  派工复制一份没有身份、权限、调度或交付用途的总目标。

## 2026-08-25 create_subagents 并行边界

- `agent_core/orchestration/tool_spec_data.py::_CREATE_DEPENDENCY_ORDER_RULE` 是 items 立即并发与依赖任务分批
  创建的唯一模型文案事实；参数详情和 native 工具总说明复用它，避免两处语义漂移。
- `_CREATE_DISJOINT_WRITE_SCOPE_RULE` 表达 会话运行时 的模型软纪律：并行编码 item 要有同一个目标目录和
  互不重叠的文件/模块写入范围，宽职责或同文件修改必须分批。总说明、items 参数和 item goal 描述复用
  同一常量；`output_files` 仍是可选提示，不变成完整写集、权限、锁或运行时验收。
- `tool_specs.py::build_create_subagents_model_spec` 把软合同放进 provider 可见 schema/description；
  `planned_delegation.py` 只核对调用者主动填写的 output 是否仍在父级 workspace；同批相同或祖先路径
  允许共享 task root，不形成目录锁。活动直属 child 对 exact covers 的占用由 planned dispatch v3 整批
  `not_started`。执行器不解析自然语言顺序、不把 role 当依赖事实、不猜未声明写入，也不增加完成裁决。

## 2026-08-25 当前任务 Todo 的显式读别名

- 会话运行时 `update_plan` 由当前 session/turn 定域，不接受模型提供另一个计划身份。本项目保留读取其它历史
  run 的扩展，但 `task_progress(read, run_id=<当前 typed task_id>)` 必须与省略 `run_id` 等价。
- `TaskProgressTool` 只在显式 id 精确等于当前 `durable_task_id` 时复用 `progress_ledger_id`；其它 id
  原样读取，因此不会把 child/历史账错误吸进当前计划，也不会从 `gwreq-` 等字符串前缀猜身份。

## 2026-08-25 完成续片的操作事实与交接叶子

- root lifecycle wake 是原 active turn 的后续工作片；child canonical attributes 与 completion wake 携带
  exact `conversation_request_id`，carried tool index 按该 turn id 恢复原派工的 typed
  `tool_execution/tool_operation`。operation verification 仍要求 mutating call 同时满足 `ok=true` 与
  `operation.status=succeeded`，不允许因索引缺字段降级后靠正文或前端补完成。durable task id 只定位
  workspace；新索引显式保存 turn id，旧索引仅以同值 `request_id` 精确兼容。
- `work/agents/<run>/final_report.md` 是宿主从 canonical child result 生成的有界交接叶子，只含
  task/run/status 与最终回复。完成信封可以把精确 ref 给直接父级，`read_file` 可读该叶子；若请求保留
  exact run id 但 task 目录过期，只按同 owner agent projection 解析 canonical ref 并重过读权限；同目录内部状态、
  list/shell 仍受 `WRONG_STATUS_SURFACE` 保护。报告是证据投影，不是第二份 lifecycle authority。

## 2026-08-25 普通前台续轮的完成输入

- `subagents/runner_completion_wake.py` 继续是 `subagent-completion.v1` 的唯一生产者；完成状态仍由 canonical
  child task/attempt 决定，信封正文没有状态权威。
- `gateway_parts/request_context.py::_gateway_subagent_completion_context` 只做 ConversationStore 的有界
  读取投影。普通追加轮会为同一 workspace 产生新 task id；它先按 same-thread、非 detached task link 的
  exact canonical task path 找出 workspace lineage，再要求 observation root 属于 lineage 且 parent 等于
  该 root，因此只返回直属 child；同 child 多次终态取最新一条。
- 模型视图 `conversation-subagent-completions.v1` 最多展开 12 项并保留 total/omitted_count。每项只含
  task/status/turn-end、完成回复、报告与产物 refs；内部 runner/output JSON 不投影。普通聊天可见，named
  Audit prepare 不可见。这是 会话运行时 inter-agent completion message 的本项目适配，不新增第二套消息账本。

## 2026-08-24 活动投影的索引选择与 canonical 精确读取

- LocalStore `legacy_agent_runs` 只负责按 `root_task_id/parent_run_id/depth` 找出候选 run id；它是
  可重建 read projection，不拥有 lifecycle、结果、Compact、权限或完成事实。
- `SubAgentPersistenceService.list_runs_by_ids_report` 是候选 id 到 canonical `task.json` 的唯一批量精确
  读取入口。它逐个校验 opaque id、保留缺失/损坏记录的结构化错误，并返回与 `list_runs_report` 相同的
  独立 `SubAgentTask` 副本。调用方仍须按 canonical `root_id/parent_id/depth` 复核关系。
- 进程内 parsed-state cache 由 persistence service 唯一持有，以 `st_mtime_ns` 失效并由 `RLock` 保护；
  这只减少 ThreadingHTTPServer 并发展示读取的重复解析，不改变跨进程写入、文件权威或 save 的投影同步。
- fake/旧适配器没有 LocalStore 或 exact-id reader 时，`conversation_agent_activity` 可退回既有全量读取；
  该分支只为接口兼容，产品 Gateway 主链必须走索引选择，不能再让每个 TUI 快照复制全部历史 run。

## 2026-08-24 Coordinator policy 与后台展示事件边界

- `agent_core/orchestration/coordinator_policy.py` 是主代理工具发现、任意层 coordinator runner 和创建回执
  共用的派工后职责说明。R230 `coordinator_execution_scope.v2` 保留用户授权内不冲突的本地工作；
  派工不产生永久禁写，用户明确的主代理实现限制仍保留。后台唤醒共用同一份软指导；
  它是模型执行指导，不是 write boundary、工具授权、任务完成或质量验收事实。
- `conversation/agent_activity.py` 继续只拥有固定 main/child/Todo scalar projection；
  `conversation/background_transcript.py` 独立保存每 thread 最多 1024 条易失公开事件。两者都不是会话
  transcript 或生命周期权威，Gateway 重启可以丢中间展示，最终回复仍由 `background_notice.v2` 持久交付。
- 后台事件 block id 位于 `bg-main:<thread>:<turn>` 命名空间，内容仅含显式 thinking、真实工具边界前的
  过程段、`_structured_tool_progress` 已脱敏的工具/display、数值 Compact 和重试计数。薄客户端以独立
  `event_after/event_cursor` 拉取，并直接发布到既有 TUI session sequencer；不创建第二个 reducer、Working
  行或前端完成状态。
- child 使用相同的 `BackgroundTranscriptSink` 事件映射，但把 writer 换成
  `agent_transcript_events/<run_id>.jsonl`，命名空间为 `bg-agent:<run>:<attempt>`。工具轮先识别 sink 的
  `write_progress` typed 方法，只有没有该方法时才退回普通 callable；因此对象本身不可调用也不会再丢失
  工具卡和工具前 commentary。详情的首条 user block 单独来自 canonical `SubAgentTask.goal`，名册短标题
  `description` 只留在固定 child 行。
- 运行 child 的用户输入复用 ConversationStore 唯一 guidance receipt。Gateway HTTP 成功只表示
  `queued/pending`；`BackgroundTranscriptSink.complete_active_turn_input` 只在 provider 成功消费后把
  exact `client_message_ids` 写入上述 child 事件流。导航页的 `TuiRuntime` 据此将本地 pending
  提升为 user history；这条展示回执不代替 receipt 状态，也不能改 child 生命周期。

## 2026-08-24 Child attempt registration and activation

- `SubAgentBaseService._write_authority_records` 创建 child 时落一条 `pending` generation 1 AgentAttempt。
  这一步只表达“委托已经存在”，metadata 不含 runner PID，也不取得 execution lock。
- `lifecycle_runner_attempts._runtime_attempt_identity` 是 child runner 的唯一激活入口。它要求 repository
  原子复用 current pending attempt，写入当前 runner PID/start token、建立 exact generation 的执行锁，
  再发 typed started events；文件投影中的 `runner_active_attempt_id` 复用同一个 attempt id。
- current attempt 已是 running 时重复调用该入口会 fail-closed，不生成下一代。后续 generation 只表示
  previous attempt 已结构化结束后的真实 retry/resume；展示层因此隐藏首轮、只在真实重试后显示“尝试 N”。
- 该状态边界参考 会话运行时 child 的 `PendingInit -> TurnStarted`，但执行权、owner 隔离和审计仍以本项目
  runtime.db 的 AgentRun/current pointer/resource lock 为权威。

## 2026-08-24 Unknown orphan recovery preflight

- `RuntimeRepository.agent_run_recovery_block_for_run_id` 是子代理自动恢复的权威只读预检；它与
  `create_attempt` 共用 run/current-attempt 的结构化 unknown 判据。前者避免明知不合法的调度，后者仍是
  事务内最终执行权闸。
- `supervise_stalled_orphans` 只对文件投影和 conversation lifecycle 都可派、且 runtime.db 没有 recovery
  block 的 run 调用 durable auto-start。unknown run/attempt 返回 `authority_recovery_blocked`，不会创建
  runner、占用并发或把未启动的进程计作 `orphans_revived`。
- 该预检不从 `CHANNEL_ERROR`、重试次数、模型正文或错误文案猜恢复安全性，也不自动把 unknown 改成
  failed/abandoned。唯一放行仍是操作者核对副作用后调用 `recover_attempt_unknown`。
- 对照 会话运行时 的 `AgentStatus`：`Errored/NotFound` 在 multi-agent wait/tool 状态中直接投影失败，恢复是
  显式 `resume_agent` 行为；本项目保留持久 orphan 巡查，但不再周期性盲拉已知 unknown 执行。

## 2026-08-23 Runner result commit fence

- `runtime_db.repository.runner_result_commit_authority` 是 MANAGED runner 结果回写的 typed 查询口；
  它同时返回 current attempt id/generation、run status 和 attempt status。文件投影中的 status 和模型正文
  不能代替这个提交权。
- `runner_result_service` 先校验 canonical `runner_active_attempt_id`，再校验 runtime.db。active run 只允许
  exact running attempt；宿主正常 `done` 表示执行片段已干净返回，仍可投影 DONE/BLOCKED/FAILED 等
  task 结果；宿主 `failed/cancelled` 只接受相同结果。迟到、重放、旧 generation 或与取消事实冲突的
  回复只保留原始 runner archive，不覆盖 canonical task。
- `ManagedOperationStore` 的只读权限门和 mutating claim 共用同一条规则：AgentRun 必须是
  `created` 族且 exact AgentAttempt 必须是 `running`。终态记录保留 current pointer 仅供审计，
  不代表还能调工具。
- execution lock 的 lease 只是复核时钟；接管要求同时超过 grace 并由 PID/start token 证明
  holder 已死。无法证明死亡时 fail-closed 保留原执行权。
- 可续跑的 runtime status 不再让旧 AgentAttempt 永久 `running`：它用 `agent_attempt.completed`
  关闭 exact attempt、撤销工具权限并保持 AgentRun `created`；下次 typed wake 必须显式创建新 attempt。
- 已批准 capability 的当轮 BLOCKED 是可继续 attempt：`runner_result_state` 用 grant 时间和
  request status 投影为 `PENDING`，当前 runner session 退出后复用 exact-run durable auto-start。真正仍 OPEN
  的 request 才保持 `BLOCKED/等待父级授权`。

## 2026-08-23 普通计划停止核对入口【状态：2026-08-28 已退役】

- 历史 `agent_core/tool_loop/plan_closeout.py` 会在模型已给出 plain final 后再次读取 Todo，并强制同轮返工或
  把整个 turn 改成 blocked。它把模型工作记忆升级成了第二套完成判官，与当前开发铁律和 会话运行时 自然 turn
  边界冲突，现已连同配置项和专用测试物理删除。
- `task_progress` 仍是 canonical 计划事实，继续供模型、Compact、resume 和 TUI 展示使用；它不再覆盖模型
  最终回复，也不自动创建额外 model call。显式 `/goal`、直属 child 等待和 UNKNOWN 副作用安全门不受影响。

## 2026-08-22 lifecycle wake 的 root active-turn 续接

- child completion/block/capability wake 是 exact root task 的下一工作片。`ThreadTaskLink.goal/task_path` 是
  原始目标与任务目录权威；goal 保持 `User Task/root_user_prompt`，wake prompt 只进入 runtime injection。
- `work/blobs/tool_outputs/index.jsonl` 是该 root 已执行工具的耐久索引。续接只接收 exact
  `run_id == task_id == root task id` 的 `tool_call/tool_output` 行，按 scoped call id 去重，恢复参数、终态、
  artifact ref、one-shot 派工键和工具轮基线。child 的 task index、sibling/旧 root 和自然语言正文不参加。
- `background_max_tool_rounds` 仍表示每个新工作片可新增的轮数，因此 absolute limit 在 carried baseline 上
  平移；这不是放大无限预算。typed Audit provider-quota wake 是 detached 用户通知，继续使用自己的 prompt，
  不冒充 active-turn continuation。

## 2026-08-22 child runner 续跑所有权

- canonical child task 与其独立 `agent_thread_id` 只由 child runner 续跑。task-local finalize 可以保存
  transcript、Compact 和 runner 结果，但不得建立 root `ordinary_task_resume`；root BackgroundMain
  收到 task id 能被 SubAgentManager 精确加载的历史 policy/wake 时，必须在模型调用前退役或无模型确认。
- 一个后台 batch host 可以同时承载多个 runner，进程 PID 不是任一 child 的执行所有权。每个 runner
  结果一旦把 exact run 投影回 `PENDING`，`runner_result_state.py` 只回收该 task 的
  `background_start`；不终止共享进程、不碰兄弟记录。session lease 退出后由 canonical orphan auto-start
  立即续派，周期监督只承担崩溃兜底。
- 对于因 `max-tokens/interrupted/blocked` 结束的物理片段，runtime.db 先关闭 exact
  current attempt，然后 runner result 再按同一 `turn_end_reason` 回写 `PENDING/BLOCKED`。
  `runner_result_service.py` 的 MANAGED 提交栅栏只放行这个严格匹配；错代、缺 reason、伪 `DONE`
  或与 run 级终态冲突的迟到结果继续拒绝。新一代 attempt 只能在该投影清空 active id 后重建。
- 上述判断只读取 exact task id、canonical manager 与 typed context scope，不读 run id 前缀、模型正文、
  Todo 或职责描述；因此它是身份/调度收口，不是任务质量验收或自然语言状态机。

## 2026-08-22 TUI/Web 共用活动投影

- `conversation_agent_activity.v5` 是 owner/thread 认证后的只读展示 schema：main 只含 task id、phase、
  activity、时间和数字 context usage；直属 child 只含 run lineage、名称/角色、typed status、职责短标题、
  attempt、当前上下文 token、Compact 和时间；child 另携带派工时显式声明的有界 `progress_item_ids`，仅用于
  Todo 展示关联。当前 active task 的 Todo 只含 canonical `id/title/status`。提示词、回复、工具
  活动/输出、路径、权限和 secret 均不进入该 schema。
- 当前上下文 token 来自统一 provider preflight 的 `model_visible_context_usage.v1.current_tokens`，在每次
  真实 child 模型调用前写入 exact agent thread 的有界数字快照；不是累计账单 token。Compact 次数只读该 child
  `agent_thread_id` 的 `ConversationThread.compact_generation`；transcript 旧段和运行中 native IR 都先提交
  同一 checkpoint/CAS 后才推进该数。控制面、TUI 和后续 Web 不得各算一套，缺失/损坏事实按空展示处理
  且不改变 run 状态。
- `SubAgentTask.description` 是创建时保存的非权威职责短标题。单 child 可从 `create_subagents` 顶层写入；
  批量 `items[]` 必须逐项写入，顶层 description 只代表整批派工，不能扇出成所有 child 的相同文案。
  递归层级使用同一字段；省略时展示层只可退回该 child 的有界 goal，但任何运行、权限、恢复和结束逻辑
  都不得读取 description 或 goal 做机器判断。
- 系统生成的 sibling display name 在同一 exact parent 的全部历史中连续编号；根级单个补派、根级批量
  和递归 scheduler 的 apply/dry-run/blocked 预览共用同一历史起点，后批不能重新出现
  `worker-1/worker-2`。显式自定义名保持原样；该编号只用于稳定展示与未来精确控制，不替代 canonical
  run id。
- main `Working`、fixed Todo 和 fixed child panel 都是同一 reducer snapshot 的派生 view。
  main 位于正文末尾，Todo 位于输入框上，child panel 位于输入框下。Todo 的 exact
  `run_id/progress_item_ids` 标记、main/child 严格单行截断、首次 attempt 隐藏和重试文案都属于展示规则，
  不能成为完成、恢复或验收信号。
- Todo 默认只显示四条状态窗口：最近完成、当前运行和下一待办优先，运行项使用共享动画帧；`Ctrl+T`
  仅展开/收起完整投影，不改变顺序或状态。main 的 compact 数字来自 thread `compact_generation`；旁边
  “压缩点 90%”是自动触发阈值，不是次数或操作进度。常驻 Context 的协议构成详情留给 `/context`。
- exact `item.id == direct child.run_id` 的自动 seed Todo 在展示上限计算前过滤，live panel 与最终 notice
  共用同一规则；canonical ledger 不删除，普通 Todo 不因前面的隐藏 seed 占满投影上限而消失。
- 会话运行时 式协调边界只进入模型执行策略：一旦 main 把实际工作交给 child，main 只协调、整合已有产物、
  测试和汇报；剩余实现继续 guidance 或 replacement child。它不新增机器验收，也不把描述文字解析为权限。
- `runner_completion_wake.py` 是 child→直接父级完成交接的唯一 root 会话出口：事件包含 typed status 与
  `subagent-completion.v1` 结果信封，`completion_message` 有界、`final_report_ref` 指向系统完整报告，
  declared/artifact refs 继续来自 canonical 合同/结果。`conversation/runtime.py` 只把同 root 的普通 DONE
  信封按预算合批，`context_budget.py` 保证 active wake 在压力下不丢 metadata 和批成员；这些投影不拥有
  验收、权限或根任务完成权。
- `services/persistence/service.py::list_runs_for_root_report` 是 runtime 按 root 读取 child tree 的查询入口。
  managed 模式先用 LocalStore `legacy_agent_runs.root_task_id` 索引选 run ids，再调用 exact-id reader 重载
  canonical task 并复核 `id/root_id`；SQLite 行只负责定位。索引损坏、canonical 缺失或 lineage 不一致均
  形成 load error，后台完成合批保守等待，不从 title/goal/status 文案补猜。没有 LocalStore 的显式
  local-unmanaged 测试/嵌入模式才允许全量 canonical fallback。

## 2026-08-26 子代理具体工具审批

- `conversation/background_transcript.py::BackgroundTranscriptSink` 通过
  `conversation/agent_tool_approval.py::SubagentToolApprovalSinkMixin` 获得 child `request_permission`；
  它只发布并等待共享 `ToolApprovalRequest/Decision`，不拥有 ActionPolicy 或 handler 执行权。
- pending record 的身份来自 canonical child task/root 与 request 完整 binding。显示名、goal、工具说明、
  TUI 行号和 feedback 都不能决定授权；child 终态行不再投影。
- owner TUI/Web consumer lease 只解决“是否有人接收”。批准仍由 agent control 服务的 owner/root/current
  attempt 门和 record transition lock 裁决；无 consumer 时 child 返回 unavailable，而不是永久 RUNNING 或
  自动放行。
- approved 后由公共 `_resolve_tool_approval` 对原 ToolCall 追加 approved binding 并原地重执行；
  denied/cancelled 进入既有 rejected-action 账。该链不改变 child DONE/BLOCKED、capability lifecycle、
  completion wake、Todo 或 Compact。

## Memory Candidate 接口

- `SubAgentManagerInitParams.candidate_service` 只接收当前 owner 已创建的 CandidateService，不在
  `subagents/` 内创建候选仓库。
- `services/runner_result_service.py` 在 canonical result 已成功解析和保存后调用
  `services/memory_candidates.py`；一次批次把去重后的 lesson 与结构化 finding 转成统一
  `CandidateObservation`。
- task/run/artifact/evidence 只以 typed refs 传递；子代理自然语言摘要不决定 scope、审核状态或晋升。
- dry-run、坏 result 和未找到正式 CandidateService 时不写候选；正式结果事实仍由 canonical task/result
  持有，Candidate 只是待审核的跨任务复用提议。
- scope 统一：`memory_candidates.py::_task_scope` 是唯一构造点（`scope_type=project`、
  `scope_key=f"project:{safe_id}"`）；新持久化一律 project 单一权威，`task:<id>` 仅旧账本召回
  read alias，由 curator 侧 `canonical_scope_key` 归一，不允许以 `task:<id>` 新持久化；goal 只作
  `applies_when` 人类说明。

## 2026-07-30 启动上下文必需字段

- `goal`、`output_contract`、`permissions`、`constraints` 和 `workspace_refs` 是子代理启动所需的
  执行与权限事实，缺失时继续 fail-closed。
- 历史 task 账本里的 `acceptance_checks` 只为兼容旧数据保留；当前 `TaskEnvelope`、启动前检查和
  context bundle 不再把它暴露给模型，也不据此阻止 child 启动或结束。质量要求继续放在自然语言目标中，
  由执行模型和父代理结合真实工具结果判断。
- 这个边界适用于所有子代理，不根据 `/audit`、任务正文或角色名称分支，也不放宽工具权限。

## 2026-08-22 显式产物与完成交接

- 模型或用户显式给出的输出引用保持原值，继续参加 shared-output 冲突检查。
- 未显式声明输出的 child 不生成业务文件合同；typed status、最终回复、系统
  `agent_run_final_report_ref` 与真实 artifact refs 通过直属 lifecycle wake 交给父级，和 会话运行时 child
  final message watcher 同义。该系统 ref 只存在于宿主生成的完成信封，不进入正在执行 child 的
  `output_contract`、task packet 或 workspace refs；模型不需要也不能为了“交差”另写一份 Markdown。
- 旧 durable task 可能仍带 `system_default_output_ref=true` 和
  `work/child_outputs/<run_id>/<slot>-<slug>.md`。唯一 rebind 读取入口继续保留，避免恢复时路径相撞；但
  context bundle、完成信封和父级 `expected_outputs` 全部隐藏该内部 ref。真正的用户文件、artifact 和
  显式共享引用不享受隐藏或冲突例外。
- 2026-08-25 起，根 wake 与递归父级恢复上下文共用 `subagent-completion.v1` 交接内容；
  `direct-children-context.v2` 直接给出有界最终回复和精确交付 refs。内部
  `runner_result.json`、`output.json`、`runner_response.md` 仍可供宿主审计/恢复，但不再进入父模型的正常
  prompt。该设计对应 会话运行时 把 child `last_agent_message` 随 terminal status 交给父级，而不是让父级猜目录。
  旧 context bundle 若仍保存宿主收口键，runner 的有界摘要也必须过滤；真正显式声明的业务文件无论名称
  是否为 `final_report.md`，仍以 `required_file_refs` 为权威正常交付。
- `SubAgentExecutionContext.write_boundary` 的完整对象继续供 Tool Gateway 执行权限门使用，落盘完整上下文也
  继续供宿主审计；模型 prompt 只投影 cwd、读写根、grant 和 policy，不再拿完整 execution-context 文件当
  渐进披露入口。恢复 bundle 只暴露 checkpoint/summary/task 等续跑线索，不能暴露本 run 的最终响应或结果。

## 2026-07-29 后台 claim 与当前执行轮

- `ConversationStore.claim_background_run` 仍是同一 thread 的独占执行租约；scheduler 只有取得租约后
  才构造 `source=background_main_agent` 的运行参数。
- 该参数对精确 task 携带 per-turn `conversation_task_turn_active=true`，表示“当前轮是租约持有者”，
  使工具网关不会把它当成外部第二执行器。该标志不进入 owner 长期状态，也不由模型提供。
- 其他前台、后台或不同 task 的轮次没有这个当前轮事实，继续经过
  `conversation_workspace_execution_blocker`。因此续派 child 的可用性修复没有放宽并发写边界。

## 2026-07-28 工具能力继承边界

- 根 main 的 child 不能用 `agent.tools` 当前进程注册表或模型正文反推父级授权上限。创建 child 的
  `create_subagents.execute_scoped` 从当轮 Tool Gateway `ToolRuntimeSnapshot` 冻结
  `direct_parent_tool_authority.v1`，经 ContextVar 只进入 canonical task attributes；输入中同名字段会被
  删除并由宿主事实覆盖。嵌套 parent 从 exact runner execution context 读取有效工具，因而自然包含已获批
  ordinary/MCP grant 与 disabled policy。
- `capability_scope.direct_parent_tool_authority` 是父级 wake 与 grant 硬门的共同来源。请求工具不在快照、
  parent id 不匹配或快照不可用时 fail closed；不对名称做前缀、描述或 capability 文本模糊匹配。
  grant 只表示 child 可在下一执行片看见该工具；真正危险 ToolCall 仍走独立 approval ledger。
- `requested_mcp_tools` 允许模型填写 tool 的公开短名，但宿主只在直属父快照内存在唯一
  `mcp__server__<完全相同末段>` 时规范成完整 registry 名；两个 server 重名或没有命中时保留原值并由权限
  硬门拒绝。语义 CapabilityRouter 遇到父级可完整授予的 exact 请求只投影
  `PARENT_RESOLUTION_REQUIRED`，不得在父 wake 前把 OPEN 抢先关闭为 GAP。
- 同一规则也处理模型误放在 `requested_tools` 的 MCP 短名：父级普通 exact 工具名优先；只有不存在普通 exact
  且恰好命中一个 MCP 末段时才移入 `requested_mcp_tools`。字段归类来自 registry 事实，不解析 capability
  文案或任务正文。
- `CapabilityGrant.tools` 与 `mcp_tools` 为审计保留类型差异，但 runner immutable snapshot 和 task durable
  `allowed_tools` 合并二者。这样同一 AgentRun 授权后能继续，且孙代理只能在直属父当前有效集合内再申请。

- `services/hierarchy/scheduler.py` 把已经解析的 child role 与父 task 的当前 `allowed_tools` 一并交给
  `services/hierarchy/tool_policy.py`。
- `tool_policy.scheduled_child_tools` 先整理显式请求与角色候选，再统一与父工具集合求交集。
  worker/researcher/tester/writer/bug-finder 等 leaf 随后移除 create/guidance/cancel/resolve 四个直属下级
  控制入口；只有结构化模板 `can_spawn_children=true` 的 coordinator 才能保留父级已有的对应能力。
  coordinator 不做额外扩权，所有后代因此只能沿树继续减法。
- 角色默认、显式 grant、历史快照与递归继承在进入模型工具面前共用
  `active_model_subagent_tools`；它删除已退休的查树/手动调度工具并保持顺序去重。显式
  `allowed_tools=[]` 保持为空，只有缺省值 `None` 才从角色模板派生候选工具。
- 文件变更工具不再由各层分别手写：`tooling/write_boundary.py` 的
  `WRITE_TOOL_ORDER=(write_file, edit_file, apply_patch)` 与对应不可变成员集是直属 coding child、递归 leaf、
  角色模板、capability grant、写围栏和进度投影的唯一来源。继承仍先与父级当前工具集合求交，因此父级
  显式缺少 `edit_file` 时 child 不得补回。`apply_patch` 未命中继续严格失败，只额外返回有界 expected lines；
  一处或少数片段由模型改用空白容错 `edit_file`，宿主不自动改写文件。
- task-local child 虽保存父 conversation/task id 作为结构化 lineage，但工具准入读取其既有
  `context_scope=task_local`，不进入主 conversation execution lane 的防双执行判断。
- 这条链复用现有 role/template/scheduler，没有新增研究型、编码型、测试型等底层 Agent 分类，
  也没有保留旧的“默认工具表自动补权”兼容分支。

## 2026-08-21 递归关系与工作目录

- 每一层都把直接父级视为自己的用户边：父级只创建、插入消息、打断和处理权限申请；child 创建孙代理时
  使用完全相同的四项边界。模型没有 inspect、wait、dispatch、push 或 acceptance 工具，内部 dispatcher
  只负责自动启动、故障恢复和生命周期通知。
- `cancel_subagents` 通过直属授权门后立即生效；“该 run 仍可自动重试”只是调度事实，不能驳回父级的
  显式打断。同批 child 不再收到 `sibling_roster`，自己的 goal、父级指令和显式 refs 已足够；旧任务里的
  roster 在 prompt 渲染时过滤，避免恢复后重新注入。
- `write_boundary.execution_cwd` 是子代理相对路径的唯一结构化起点，继承父级项目 cwd；`task_dir`、
  `task_workspace_dir`、内部 `work/output` 只负责状态、归档和显式命名空间。写入许可仍由
  `allowed_write_roots/forbidden_write_roots` 独立裁决，不允许用权限根反推 cwd。
- 对根 main 创建的普通 child，`conversation_execution_cwd` 与
  `conversation_runtime_workspace_roots` 必须先从当前 active turn 的 host-validated attributes 复制；它们
  同时覆盖 shared manager 的 daemon/repository fallback。`output_files` 只声明交付目标与验证线索，不能成为
  child 获得当前项目工作区的必要条件。孙代理继续从直接父 task 的结构化产品根逐层继承，不解析 goal。

## 2026-08-22 Dispatcher 互斥权威

- `agent_core/orchestration/dispatch/lock.py` 使用操作系统 advisory lock 持有唯一 watch 所有权；锁文件里的
  JSON 只用于诊断，不参与“谁正在运行”的机器判定。持有进程退出时由内核释放，文件可以稳定留在原 inode。
- 空文件、损坏 JSON 和旧 pid 都不能阻塞下一任 Gateway；反过来，`--force-lock` 也不能绕过内核去抢占
  仍然存活的持有者。这样恢复并发仍由单 Gateway 控制，不从展示文案或遗留文件内容猜状态。

## 2026-07-27 共享工具历史窗口

- 子代理没有独立的 live tool-context compactor。每次 provider 调用都与主代理共用
  `agent_core._tool_loop_service` 中的工具历史窗口入口；原生协议的完整请求计量和整对回收也在这里完成，
  不是 `subagents/` 下的专属 service。
- `agent_core.tool_ir_compact` 只负责对 provider-neutral IR 做 ToolCall/ToolResult 整对删除；
  当前模型窗口、90% 阈值和 recent-tail 都读取公共 `RuntimeCompactPolicy`。子代理仍以自己的
  run workspace 保存事实，但不拥有另一套上下文算法。
- `live_context_compaction` 已从 runner context、result payload、finalization 和父代理结果投影删除。
  通用 Compact 仍负责运行上下文压缩；结构化 task state、原始运行证据和持久 conversation
  transcript 各自维持原有职责，不新增兼容分支。

## 2026-08-22 Compact 当前边界与统一目标

- 当前阈值、token estimator 和 native ToolCall/ToolResult 成对裁剪由所有代理共用
  `agent_core._tool_loop_service`；持久状态也已统一到每个 agent 自己的 ConversationThread。main、child、
  grandchild 分别保存 summary/cursor/generation/checkpoint/CAS，不共享正文。
- `conversation/agent_thread.py` 是 delegated adapter：创建/恢复时按稳定 `agent_thread_id` 幂等物化线程；
  精确 ID 落盘和身份/谱系冲突检查由 `conversation/agent_thread_store.py` 单独负责，避免通道会话 store 吸收 agent runtime 策略；
  每个 attempt 按 `conversation_request_id` 写入 user/assistant，轮前与 provider overflow 后调用
  `conversation/compact.py`。task-local 仍控制 Memory、工具权限和工作区，不再拥有第二条持久 Compact 链。
- delegated runtime 同时携带两类不可互换的 thread 身份：继承的 `conversation_thread_id` 与
  `conversation_task_id` 属于父会话的 workspace/task lifecycle；`agent_thread_id` 属于当前 child/grandchild
  的模型 transcript 与 Compact。工具晋升和工作区重定位继续读取前者，模型历史只读取后者；任何 adapter
  都不得用 child `agent_thread_id` 覆盖父 `conversation_thread_id`。这对应 会话运行时 的独立
  `child_thread_id` 与单独 `parent_thread_id`，而不是兼容分支。
- `conversation_transcript_authoritative=true` 是通用 structured owner 标记；finalization、tool window 和工具轮
  只据此把 durable Compact 交给 ConversationStore，不再要求 `context_scope=conversation`，所以 delegated
  task-local 不会生成旧 `compact_applies/continue`。没有 generation 或 typed tool/guidance 进展时，溢出
  重试明确失败，避免无效重放。
- `conversation/live_tool_compact.py` 把允许持久化的 child native IR 真裁剪适配到同一 owner/thread
  checkpoint/CAS：摘要合并上一代 thread summary，checkpoint 用 `source_kind=live_tool_ir` 保存精确移除与
  保留的 ToolCall ID，cursor 不前移，`compact_source_tool_pairs` 累加。rich sink 事件展示提交后的 canonical
  generation；它仍不写 exact run attribute。`control_plane_projection.runtime_compact_count`、TUI 与 SQLite
  只精确加载 `task.agent_thread_id` 的 `compact_generation/checkpoint_id`，旧 ledger/attribute 即使存在也不回读。
- provider 实报 overflow 的 native PTL 也调用上述完整预算事务，不再按 20% 在账外删工具对。摘要、checkpoint
  或 CAS 失败时 `_tool_loop_service` 恢复压缩前 IR/tool-context 并走同一 thread failure circuit；只有
  presentation/no-save 回合保留不计数的临时窗口事件。
- child runner 对 provider/preflight `context_overflow` 的 Compact 重试属于同一 active attempt：通用
  `agent.run()` 在 `context_scope=task_local` 且 `conversation_transcript_authoritative=true` 时不提前 settle，
  外层 runner 用同一 exact attempt 继续，最终返回后再由 child lifecycle 唯一收口。其它非终态仍关闭旧
  attempt、下轮显式创建新 attempt；工具权限门不接受已经终态的身份，也不会在 Compact 后静默重开。
- `.10` fresh MiniMax-M2.7 真机已验证该结构：工具运行时 child 的 generation 1 checkpoint 与 Compact 后
  `run_command status=ok` 都携带 exact `attempt-1787877717-487f5d43`，最终才进入 DONE；同 task 权限错误为 0。
- 历史上已经删除的子代理专属 service、查树推动、session continue-packet 和多重索引不得因迁移复活；
  新 thread 只替换 Compact/会话持久层，不改变 `run_id/parent_run_id/root_run_id` 生命周期权威。
- 当前 task 的结构化 goal 和 next actions 是恢复后的最高任务权威；旧摘要、归档包装和读取游标只能
  补充事实。只有任务显式声明 `full_source_read` 时，未完成读取游标才可成为下一动作。
- 子代理仍隔离在自己的 run home；它不拥有第二份长期 Memory，也不能读取父代理或其他 owner 的
  私有人格、Memory、Skill 或任务文件。
- 父级共享给 child 的 read/search 预览只从创建发生时当前 tool loop archive 生成；不保留
  agent-level 跨轮缓存。上一任务读过的内容不能自动出现在下一任务 child 上下文，跨轮信息只走正式
  transcript、Compact 或 typed refs。

## 核心链路

前台模型快照不延迟 `orchestration`：根主代理与 coordinator 的 `create_subagents`、`send_guidance`、
`cancel_subagents`、`resolve_capability_requests` 首轮直接可见；普通 leaf 的角色快照根本不含这四项。
这只改变 Schema 披露，真正可调用集仍由同一 `ToolRuntimeSnapshot.allowed_tools` 和 availability 决定，
不扩大权限。
旧 `raise_event` 不在模型快照；进展、阻塞、权限申请和结束由宿主 lifecycle service 写入同一
observation/wake 事实源。runner 的离散模型/工具阶段只向 canonical state 投影有界活动摘要，不保存正文。

```text
SimpleAgent orchestration tool
  -> SubAgentManager
  -> services/base.py creates SubAgentTask
  -> services/persistence writes canonical state
  -> runner worker runs model/tool loop
  -> services/runner_result_service.py records result/artifacts/status
  -> nested parent stores an exact direct-child wait marker and yields its slice
  -> child event resumes only the direct parent (root child wakes its conversation)
  -> resumed parent receives bounded direct_children facts/refs and continues naturally
```

## 入口

- `agent/subagents/manager.py`：子代理 root manager，负责初始化、基础生命周期、工单路径和服务组合。
- `agent/subagents/kernel.py`：从 canonical state、projection 和 task workspace 生成稳定树快照。
- `agent/subagents/parsing/`、`agent/subagents/rendering.py`、`agent/subagents/role_templates.py`：
  放当前子代理协议解析、展示渲染和模板策略；不再保留单独一跳 facade。
- `agent/agent_core/subagent_mixin.py`：子代理生命周期入口，包含 run/finalize、结构化修复、
  recovery snapshot 和 parent planner 记录；旧私有 repair/planner mixin 不再作为跳转层存在。
- `agent/agent_core/subagent/params.py`：子代理生命周期和 parent planner 参数类的权威位置。
- `agent/subagents/service_window.py`：持续型委派语义的生命周期事实源；它按结构化
  `attributes.long_running + service_window_seconds + created_at` 计算剩余值守窗口，供父侧 wake 和
  恢复展示使用，不作为任务质量验收或模型结束的第二道硬门。
- `agent/subagents/direct_parent_lifecycle.py`：递归父子等待、同批成功合并、失败立即释放和
  `direct_children` 上下文的唯一领域规则。它只读精确 id/status/refs，不解释 goal 或汇报正文。
- `agent/subagents/runner_completion_wake.py`：直接根 child 通过
  `ConversationStore.append_observation_with_wake` 发布会话通知；嵌套 child 不越级发根 wake，由 runner lease
  退出后的直属父级恢复链处理。
- 父级后台整合轮只在模型调用前冻结一次 child phase；prompt、最终化和公开投递共用该快照。同 root
  的 DONE 通知只有在该时刻已经创建才可同批确认，之后到达的通知保持 pending 并另开一轮。新鲜轮
  看到全部 child 终态后直接交付模型自然回复，不等根 task link 先完成第二次机器验收。
- 父级自身的权威 run/current attempt 若为 `unknown`，child lifecycle 通知继续留在 durable queue，但宿主
  不反复挂载父代理，也不创建替代父代理。人工核对并显式恢复后，原通知再唤醒同一父级；这与根代理、
  子代理管理孙代理完全同构。模型工具面没有“催促/推动”工具，只有事件通知、单 child 插话和取消。
- `agent/agent_core/orchestration/`：模型可见的统一 `create_subagents`、`send_guidance`、
  `cancel_subagents` 与直属 capability 处理入口。创建即由宿主自动启动；
  `dispatch/scheduler` 只保留为内部执行引擎，不再注册成模型工具。
- `agent/agent_core/orchestration/capacity.py`：根与任意递归层级共用的创建容量入口；在 owner-local 创建事务
  内按当前 conversation tree、全 owner 非终态 run、task/per-call 配置计算槽位，读取失败或整批超限均在
  materialize 前 fail closed。它不控制 runner 并发，也不读取模型文字。
- `agent/agent_core/agent_tree/status.py`：`/status`、TUI、恢复和诊断共用的内部树投影；旧
  `orchestration/tools/status.py` 与 `InspectAgentTreeTool` 已删除，不能从内部 projection 反向恢复模型工具。
- `agent/conversation/agent_activity.py`：从 active conversation task link 与 canonical child run 生成
  owner/thread-scoped 的有界直属活动行；只供 TUI 和后续 Web/IM 展示，不是另一份树、状态或生命周期账本。
- `cli/gateway_loops.py::_GatewayOrphanReconciler`：不执行模型的独立周期控制器；从 owner
  投影发现未完成 run，再调用 orchestration 层现有的结构化孤儿监督。它与后台主代理的 LLM
  scheduler 分线程运行，但不建立第二套恢复状态机。
- `agent/agent_core/runner/`：子代理 worker、prompt、session heartbeat、timeout policy。
- `agent/agent_core/runner/stage_trace.py`：从模型请求与工具调用的 typed 边界刷新 heartbeat，并写入有界
  `runtime_activity/current_step/current_tool`；该投影只供状态面观察，不包含 prompt、response 或工具输出。
- `agent/subagents/runner_session_liveness.py` 只判会话新鲜度；`runner_session.in_process=False` 不证明 OS 树独占。
  `cancellation_hosts.py` 只对精确 attempt 协作中断，`runner_control.py` 让异进程心跳读取原取消事实；不能按宿主 PID 清掉新接续任务。
- `cli/subagents.py`：子代理 CLI 命令和注册入口，包含基础、监控、层级和 leadership recovery 命令；不再通过单独 registration / hierarchy 注册文件跳转。

## 用户观察与直控边界

- 当前已落地的只有只读面：TUI 固定显示主任务的直属 child；默认不把全树压平，后续详情面才递归展开。
- 用户控制面与模型控制面分权。设计为 root owner 可通过未来 Gateway/domain protocol 操作自己树中任意后代；
  代理模型仍只能对直属 child 调用 create/guidance/cancel/capability，防止越级代管和身份伪造。
- 待实现的共享 typed actions 是 `list/read`、`message`、可恢复 `interrupt`、`resume/start`、
  终态 `cancel/close` 和 capability 裁决。每次写操作都要有幂等 operation id、exact target、expected
  state/version 与三态回执；TUI、Web、IM 都只调用该服务，不直改 canonical 文件/数据库。
- 当前 `cancel_subagents` 会让 run 进入终态，不等价于 会话运行时 `interrupt_agent` 的“只停当前 turn、保留线程可继续”。
  未来要先在底层拆出可恢复 interrupt/resume，再接 TUI 按键和 Web 操作。

## 状态和路径

- 权威状态：当前 task workspace 的 `work/agents/<run_id>/canonical_state.json`。
- runner-session heartbeat 是 canonical state 内的窄 lease 事实，不是任务内容转换。
  `persistence.save_runner_session` 在 run-local guard 内只改 session/heartbeat 与轻量
  locator mtime；`persistence.save` 的完整 workspace/compact/projection 同步也持同一
  guard。禁止把周期 heartbeat 再接回完整保存链。
- 模型可见的子代理工作根：当前 task workspace 的 `work/agents/<run_id>/`。旧
  `.my_agent/subagents/<run_id>` 只做 locator / owner projection / 查找索引，不是
  `task_dir`、write root 或 artifact root。
- 子代理的 runner identity、父 conversation task lineage 与当前 cwd 是三项独立结构化事实。
  `conversation_task_id` 只表示结果和生命周期归入哪个父任务；child 由精确 owner task 绝对写路径
  重绑定 `run_workspace` 时，只改变当前 runner cwd，不得 reopen、supersede 或 select 全局会话任务。
  后续 task promotion 只验证父 link 仍有效，不得再以父 task path 覆盖 child cwd。该边界对照 会话运行时 的
  `parent_thread_id + config.cwd` 和 通道运行时 的 `parentSessionKey + childSessionKey` 分离关系实现。
- 运行回合结束先写公共 `TurnEndReason`：`completed`、`aborted`、`blocked`、`error`、
  `max_tokens`、`interrupted`。主代理、子代理、Gateway 与 TUI 共用同一映射；模型口头说“完成”或旧状态
  文本都不能改写该结构化原因。
- 子代理持久状态仍使用当前 `TaskStatus` 枚举。`failure_type` 也一样：runner/action 原始结果可以留作
  审计文本，但写入 `task.failure_type`、重试和恢复前必须是当前已知枚举；未知值不能靠小写化或旧标签
  兼容变成机器状态。
- `VerificationStatus` 只用于读取历史账本，不再进入当前模型上下文、父级摘要、启动前检查或普通完成判定；
  旧 `subagents/state_machine.py` 私有转换表已删除，避免 `WAIT_CHILD` 等历史状态绕过当前协议。
- 恢复候选、agent tree bucket、due-check、leadership recovery 和 runner 结果
  payload 不再各自维护失败/完成状态集合；这些机器判断从 `subagents.models`
  读取当前协议集合，未知旧标签只保留为审计文本。
- 恢复模式和 capability 等待状态也只认当前结构化枚举。未知 `rerun_*` / `takeover_*`
  前缀、`NEEDS_TOOL` 这类旧别名、工具错误正文，都不能触发自动重跑、接管、授权或生命周期变更。
- 子代理 runner 默认复用主代理当前 `AgentConfig`，包括 `model_context_window_tokens`、
  `memory_compact_auto_trigger_percent`、`runner_timeout_seconds`、runner 并发和工具预算。
  只有任务自己携带结构化 `config_overlay_ref` 时才形成 run/task layer 覆盖；不要为
  子代理 compact 或常规真实测试另建第二套参数。
- 路径 allow/forbidden 冲突由 `tooling/write_boundary.py` 按最具体命中条目裁决；更窄 allow 可穿过祖先
  forbidden，同层或更窄 forbidden 仍拒绝。这个规则只读结构化路径，不从 goal 或文件正文推导权限。
- 自适应重试拆分父任务时不再写历史自定义状态 `SPLIT`；父任务进入当前协议
  `TAKEN_OVER`，拆分关系只记录在结构化 `attributes.split_into` 和 `child_ids`。
- capability request 的打开/终态判断集中在 `model_capabilities.py`。`OPEN` 代表待处理，
  `GRANTED` 代表已授权且可避免重复申请，`GAP` 和 `CLOSED` 是当前终态；旧
  `RESOLVED`、`APPROVED`、`REJECTED` 不再被 kernel、protocol、runner、board 或
  runner context 静默当成当前终态。
- OPEN capability request 优先于 provider 的普通 completed 收尾并把 child 投影为 BLOCKED。直属父级
  grant/deny 后，裁决入口把同一 run 重排为 PENDING、恢复 conversation task link，再由 lifecycle event
  触发内部 dispatcher；禁止另建 replacement 或要求模型调用推进工具。
- capability route 自动匹配只读结构化能力字段：`needed_capability`、requested tool/skill/mcp/
  command、constraints 和 scope。任务目标、问题描述、期望输出、证据摘要这类自然语言
  只用于人类审计和模型理解，不能参与自动 grant query。
- 子代理过程文件：`work/agents/<run_id>/...`。
- 用户最终交付：主代理汇总后写当前 task `output/`，或用户显式指定的输出目录。
- 当前 run 没有用户显式指定输出目录时，`output_files` / `output_refs` / `artifact_refs`
  里的普通相对路径按当前可信 cwd/workspace 解析；只有显式 `output/...`、`work/...` 分别归一到当前
  task 内部 output/work。绝对路径保持原目标身份，再由写边界允许或拒绝，不能静默搬运。
- 普通 child 逐层继承直接父级的结构化产品写区，后代不得扩大上界；`output_files` 负责交付身份、读取顺序
  和冲突锁，不再是父级 workspace 内写入的唯一授权来源。命名 Audit/exact-scope worker 不走该继承。
- owner projection：`owner_home/agents/<run_id>/state.json` 保存可重建索引和当前状态投影，用于
  tree、恢复、跨 session 查找以及 Gateway 冷启动后的 owner 发现；canonical state 仍在
  task workspace，投影不能取代它成为状态权威。
- 输出路径合同只接受真实结构化路径。`[name]/file.md` 或 `【name】/file.md`
  这类括号占位符路径段会被过滤出 required refs、declared refs、write roots 和 artifact
  roots；普通自然语言说明可以留给模型阅读，但不能成为机器写入授权。
- `agent_name` 是展示名，不是层级或角色事实。默认展示名使用 `agent-d<depth>-<role>-<index>`；
  深度、权限、模板和状态仍只读结构化字段，不能从显示名、中文叫法或英文别名里反推。
- `role` 选择角色模板时只认明确模板 id；不做“字符串里包含 tester/worker 就套模板”的宽匹配，
  也不再把旧层级别名静默映射成当前模板。
- role snapshot 除能力标量外还冻结 `name_zh/prompt_zh`。普通 leaf runner 只把当前角色提示作为软行为条款
  注入；coordinator 继续使用完整角色索引。旧 snapshot 缺提示时只按 exact role id 回落内置模板，不能从
  goal 或展示名猜角色。worker 的共享目录/局部编辑纪律不产生目录锁、写权限或完成判定；小范围修改优先
  `edit_file`、关联多文件修改使用 `apply_patch` 只是模型选择提示，真实准入仍由结构化工具快照与写边界决定。
- 层级继承状态写在 `attributes.inherited_parent_context`；`goal` 只承载给模型阅读的任务说明和
  父级边界摘要，不承担机器状态判断。

## Services

| Service | 负责什么 |
|---|---|
| `base.py` | create_run/split、owner 继承、runtime config scope |
| `persistence/` | canonical state 读写、projection、global index、LocalStore 投影 |
| `dispatch/` | 创建后的宿主自动启动、runner 选择、watch 和父级运行报告；不提供模型手动派工工具 |
| `runner_context_service.py` | 执行上下文、写入边界、任务配置、runtime guidance、runner allowed tools |
| `runner_result_service.py` | runner 输出解析、状态和 artifact refs 写回 |
| `board/` | board、due-check、action-plan（直接导入 `board.service` 等实现模块） |
| `actions/` | action-plan 应用、取消、接管动作记录 |
| `hierarchy/` | 多层级 child 自动启动、recovery packet、leadership recovery（直接导入 `hierarchy.service` 等实现模块，包 `__init__` 不再转发） |
| `patch_apply/` | patch review/apply/report/rollback |
| `capability_service.py` | capability request/grant/gap 路由 |
| `memory_gate/` | 子代理 task-local 候选经验，不自动写长期记忆 |

可复用执行方法来自同一 `SkillsService` snapshot；当前任务计划来自 `task_progress`；子代理执行只走上面的原生编排工具，不存在第二个 workflow service。

## Recovery And Result Signals

内部恢复器只根据 `BLOCKED`、`FAILED`、`TIMEOUT`、`CHANNEL_ERROR` 等结构化状态、`turn_end` 和 refs 行动。
自动启动、runner summary、parent-timeout recovery、通用 Compact work-state 和 board risk 共用同一组
状态 helper，不在各自模块维护额外的状态别名表。

普通 runner 不再要求模型生成 `SUBAGENT_RESULT`、`VERIFIED` 或另一份机器验收结论。宿主保存模型的自然
最终答复、真实工具结果、产物 refs 和六类 `turn_end`；父代理据此继续、补充提示、取消或自然汇总。
`ERROR`、`FAILED` 这类写在 summary/旧 payload 里的普通词仍不能触发恢复动作。

## Guidance

底层 conversation guidance 账本继续承载用户/CLI 对 agent run、thread、task 或 case 的输入。模型可见的
`send_guidance` 则只接受一个直接 child 的 run id 和 message；父代理不能广播、越层管理孙代理或用消息
改变状态。child 下一轮执行上下文会读取点名给自己的 guidance。停止、取消、接管和授权继续走对应
结构化控制入口，不能靠解析自然语言 guidance 改写任务合同。

## Cancel And Takeover

当前代理可以用 `cancel_subagents` 按精确 `run_id/run_ids` 取消自己的直属下级。模型 Schema 不提供
root/status/整树筛选，也不能越过 child 代管孙代理。取消在原创建事务中关闭可取消的执行权和投影，
固定后台/PTY 资源；DONE/FAILED/UNKNOWN 保留真实历史与锁，不从宿主 PID 推断可杀整棵进程树。宿主恢复与运维仍保留内部批量 primitive；模型
工具只处理 canonical loader 能读取且直接父子授权通过的 run。父级说明取消/接管原因后，可以继续汇总，
或用 `create_subagents` 创建替代执行者。

合法取消一个节点时，资源范围包含原节点及原后代，已终态节点也可能仍有后台程序。参考 Codex
`close_agent/shutdown_agent_tree` 的固定集合思路：先进入 owner-local 创建事务，按 canonical parent_id
固定原分支，逐项关闭执行权并冻结资源；清理 worker 不再选择后来恢复的新孩子。模型仍只能点名直属 child，
用户控制面仍需证明 owner/conversation 归属；不扩大到祖先或兄弟。根 `/stop` 与 direct/local 共用同一准备入口，
主/子资源在释放控制锁前一并固定，准备部分失败仍交出其它已提交清单。
控制受理不代表资源已退出；后台、PTY 请求和宿主观察分别报告，跨进程 runner 由原心跳转交精确中断。

takeover replacement 的来源权威入口是 `context_bundle.takeover`：创建时由
`services/takeover/refs.py::source_handoff` 生成有界结构化快照，包含 source run id、状态、
未完成步骤、摘要、阻塞项和 refs。模型不应使用普通文件工具读取 canonical/checkpoint 等
受管状态面；refs 是按需证据指针，不是启动前置条件。旧 attributes 中只有 refs 而没有
handoff 的 run 保持可读，但新创建/重新合并的 takeover 必须补齐 handoff。

## Create-Time Boundaries

`create_subagents` 是所有层级唯一的创建入口，负责结构化目标、路径、写入安全和 lineage，并在创建后由
宿主自动启动 child。业务质量要求可以随自然语言目标传递，但不能变成启动或结束硬门；父代理读取真实
结果和 refs 后，自然决定汇总、补充 guidance、取消或再创建一个明确分工的 child。
当前代理已有 canonical `task_progress` 计划时，创建入口先运行同一份 `planned_delegation` 预检：每个
item 的 `covers` 是可选 exact-id 映射；一旦提供，必须引用仍 open 的 id，且不能被同批多个 child 占用。
未绑定 child 按真实 run id 形成独立进度行，不关闭现有 Todo。`output_files` 同样只是可选交付目标与冲突
线索，不是权限、完整写集或创建前置条件；一旦提供仍不能越出直接父级 workspace。未知、已关闭或重复的
显式 covers，以及显式 output 越界的批次，在 create/save/publish 前整体返回 `not_started`。
预检不读 goal 或 Todo 标题，不判断交付质量、代码量和完成状态；没有 canonical 计划的普通轻量派工沿原
入口运行。read-only、tester、coordinator 等不直接拥有产品写集合的角色继续使用各自既有合同。
即使 goal 正文包含与 Todo id 完全相同的目录名或模块名，也不会自动生成 `covers`；历史
`covers_auto_bound` 旁路已删除，调用参数中的显式 `covers` 是唯一绑定来源。
已关闭项需要返工时，先用 `task_progress` 对同一 id 传 `status=in_progress, correction=true` 显式重开，
再决定是否绑定；也可省略 covers，但不能拿无关 open id 顶替。
用户明确的保存路径应通过顶层或逐 item 的 `output_files` 记录交付和验证线索；普通 child 的权限上界来自父级
workspace，goal 或 output_files 都不能扩大到该上界之外。
root 对用户完整目标负责；delegated runner 只把直接父级当前 `goal` 当作本轮完整工作边界，不能因根目标
更大而实现未交给自己的兄弟计划项。这个边界是 会话运行时 式模型执行纪律，不由宿主解析 goal 或扫描文件硬判。
对 local/unmanaged owner，若该 inherited workspace 同时是结构化 allowed root，创建时会移除
与它完全同路径的默认 home deny，避免子代理被父级已授权的 cwd 反向拦住。这不改变
通用同层 deny 胜出规则：`.ssh`、`Desktop`、`Downloads` 等更窄 deny 继续生效，远程 owner
的宿主 home 围栏也不移除。调和只读 `owner + workspace_root(s) + allowed_write_roots`，不解析 goal。

## Collaboration Capabilities

子代理模板或创建属性可以显式声明 `capabilities`、`collaboration_capabilities` 或
`provided_capabilities`。协作路由按这些字段和真实工具名匹配，不从工具名子串、角色说明
或 summary 自动推断 `query`、`write`、`delegate` 等抽象能力。

## Artifact Rule

子代理可以写自己的过程产物和协作文件，但最终用户交付由主代理汇总。子代理 `final_report.md` 这类内部文件只作为证据/引用，不会自动变成用户最终交付。

子代理自然完成与结构化完成都读取本 run 工具 registry 的真实文件引用；模型结果可补充
`artifacts`、顶层 `artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
`deliverables`、`files_modified`、顶层 `file_path/path` 这类历史别名不会被恢复成
artifact refs；补丁工具通过独立结构化 `artifact_refs` 交接已提交文件，不解析显示名称。
创建任务时给子代理的 `output_files` 是目标路径合同，不是结果回报别名。

声明产物只表示预期交付，不授权 runtime 自动创建内容。runner result 交接正式工具账本中的文件，
读写权限仍由工具在执行时核验；缺失声明保持缺失，交给父代理根据
结构化缺口继续工作或报告。禁止从另一个文本产物按后缀或同名搜索复制，禁止把 summary/
findings 渲染成占位文件。收口不复制或搬运业务文件；声明、文件工具与结果引用共用可信 cwd。

`create_subagents` 的模型入口只有单 `goal` 和明确 `items` 两种形态，不克隆同一份任务。
多个 child 必须在 `items` 里声明不同工作；需要给出明确交付路径时，每个 item 可显式声明自己的
`output_files`。共享目录及目录与子路径重叠本身不构成互斥，模型应合理划分实际文件修改；没有声明时
不能假定模型已经预报完整写集，也不自动创建目录锁。顶层交付目标归父任务，不会暗中复制到所有 child。

## 2026-06-10 Facade 清理

- `services/__init__.py` 不再 re-export 各 service 类；`SubAgentManager` 与所有调用方直接导入实现模块。
- 单模块包 `services/capabilities|runner_context|runner_result/` 打平为 `capability_service.py` / `runner_context_service.py` / `runner_result_service.py`。
- `services/board/__init__.py`、`services/hierarchy/__init__.py`、`parsing/__init__.py` 尾部 re-export 删除；envelope/hierarchy 调用方直连实现模块，消除模块级循环导入。

## 2026-06-10 合约身份去重合并

- `idempotency_contract_identity.py` 与 `repair_contract_identity.py` 合并为
  `contract_identity.py`：两类合约共用同一套 pack 遍历与字符串归一化 helper
  （`_iter_packs` / `_string_tuple` / `_normalized_path`），原来逐字重复三份；
  合并后各自身份计算用 `_idempotency_identity` / `_repair_identity` 区分，
  共享 helper 留一份。调用方（create_context / create_constraints /
  hierarchy/schedule_idempotency）改导入合并后模块，行为不变。

## 输出路径投影与权限

- `services/output_alignment.py`：路径解析不改 task。`anchored_output_refs(task)`
  产出 OutputAnchoring（anchored_refs / warnings）；
  `anchor_refs_for_execution(task, refs)` 按可信 cwd 解析目标 refs，不重定位或扩权。
  消费方：`context_bundle_contracts.task_packet/output_contract`（执行合同）、
  `services/runner_context_service`（执行上下文）。结果文件来自
  `result_registered_artifacts.collect_registered_artifacts` 的 exact run 账本投影，不经完成时搬运。
- `agent_core/orchestration/tools/capability.py`：`resolve_capability_requests` 工具
  实现（grant/deny + 安全围栏 + wake）；注册链 core.py → orchestration_tools.py。
- `services/lifecycle.py`：capability request/grant/gap 与 evidence 生命周期写入；grant 保存后只对精确
  `capability_request` 阻塞 child 做 conversation link 的 `blocked -> active` CAS，使同 run 可以续跑，
  但 cancelled/terminal link 不会被复活。
- `services/runner_context_service.py` 的 `task_product_write_roots` 投影继承的业务写区，
  过滤内部 agent/report 区；空范围不由内部 output/work 兜底，也不由输出声明产生授权。

## TUI/Web Agent View And Control

`conversation_agent_view.v1` 是单个 exact run 的共用详情投影：返回 goal/activity、canonical status、
Context/Compact、Todo、直属 child、公开过程事件 cursor 与终态 final。TUI 进入 child 后只换当前 view store，
继续使用与 root 相同的 typed event reducer 和 renderer；未来 Web 也应调用同一个 Gateway service，不直接读
runner 私有目录或拼接日志。

普通与详细 transcript control 都按 exact `TuiStateStore` 保存进程内 viewport：follow-tail、cursor、最近
行数和 unseen 基线各页隔离。第一次进入的新 agent 页默认跟随尾部；返回已经看过的页恢复原锚点，不强制
End。selection 与最近渲染行不跨 store 保留，避免复制到另一代理正文。该 viewport map 不是 session/history
事实源，重启或 resume 后仍由 canonical conversation/agent view 重建正文。

查看和修改使用不同授权强度。历史 view 仍须证明 `owner + conversation root + ancestry`，但允许当前 attempt
已结束；guidance/stop 除此之外还须通过 active binding 和 mutation gate。普通 guidance 只追加到 exact run
的 canonical guidance 账本，`operation_id` 保证重试幂等；stop 调用现有 `cancel_subagent_task`，不另写前端
状态。终态 child 不接受 guidance，也不会被普通输入恢复；显式 resume 若未来落地，必须是独立 typed 操作。

`agent_transcript.py` 保存 child 已公开的 display 事件，路径固定在 owner conversation store 下并按 run id
隔离。单调整数 cursor、原子追加、条数/字节裁剪用于跨进程增量展示；该流允许丢失或裁剪，不能替代 run、
attempt、Compact generation、权限和交付事实。`Esc` 只发停止当前代理，`Ctrl+G` 只弹出前端 view stack，
这两个动作不能在任何层共享副作用。

## Canonical task workspace and assistant parts

父会话首个 `promotes_task` 动作后，`run_workspace.task_root` 是整棵代理树唯一默认 cwd。创建策略、写根预检、
runner task attrs、相对 `output_files` 和孙代理继承都先读该 root；只有尚未晋升时才读 Gateway 校验过的
thread/client cwd。child 不能从 goal、绝对路径或 manager daemon root 扩大/更换这个范围。

递归创建的具体收口点是
`agent_core/orchestration/create_policy.py::_inherit_current_conversation_workspace_attrs`：它不仅复制
`conversation_execution_cwd/runtime_workspace_roots`，还必须复制父级完整 `run_workspace` 并重新派生
`work_dir/output_dir`。manager runtime 只保存调度器自身状态，永远不能成为普通孙代理的新 task root。

每个 child provider turn 可产生 `commentary:1..N` 与一个 `final` assistant part。part id 由宿主按真实工具
边界生成并参与幂等键；commentary 保留完整用户可见正文，final 承载 terminal tool fold 与运行终态 metadata。
历史投影保留全部 part，但轮次预览只把 final 当终答，不能把过程消息误当成第二个任务或完成事实。
