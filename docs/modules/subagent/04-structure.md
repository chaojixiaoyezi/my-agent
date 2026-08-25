# Subagent Structure

本文只描述当前子代理主链路。

`SubAgentManager` 不再接收 `closeout_for_all_task_nodes`，`runner_result_service.py` 也不生成
`task_node_closeout` 副本。canonical task/result 是唯一结果事实源；父代理通过结构化 status、blockers、
findings、artifact refs 和 result payload 阅读子代理工作，再由模型向用户汇总。

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
- `gateway_parts/request_execution.py::_gateway_subagent_completion_context` 只做 ConversationStore 的有界
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
  共用的派工后职责说明。`coordinator_execution_scope.v1` 只区分允许的协调/整合/测试/汇报与禁止重复实现；
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

## 2026-08-23 普通计划停止核对入口

- `agent_core/tool_loop/plan_closeout.py` 是普通 root/child 自然 final 前的唯一清单一致性入口；它使用
  `progress_ledger_id + runtime_owner_root` 读取与 `task_progress` 工具相同的 canonical 账本。
- 该入口位于活动 child、命名 Audit、真实工具失败/未知副作用等专用收口之后，只返回 `ignore / continue /
  block`。`continue` 仍在原工具循环和原 active turn；`block` 写 typed turn-end，不创建 progress policy、
  新 task 或 runner。
- closeout 状态只存在当前 `live_archive_state`，配置次数有界；不存在这份状态时 fail-open，避免旧调用方
  形成无限提醒。原生协议通过 runtime-guidance user message 把核对包送给 provider。
- 该入口已随 `e94f8ec` 发布并部署；focused 已证明核对包真实进入下一次 provider messages。fresh r19
  在 final 前自行关闭全部 Todo，因此只证明正常关闭路径无回归，不算 open-Todo 分支的直接真机覆盖。
- 2026-08-24 r9 首次直接命中真机缺口：后台 main 的 `save=False` 被入口误当成辅助 no-save 轮，导致
  task-path ledger 仍 open 也跳过核对。`save` 现只保留 archive 含义；Gateway/TUI 与后台 main 无论是否存档
  都走同一入口，辅助轮仍由 `context_scope/work_kind` 等结构字段排除。

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
  真实 child 模型调用前写入 exact run 的有界数字快照；不是累计账单 token。Compact 次数只读该 child
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
  final message watcher 同义。模型不需要为了“交差”另写一份 Markdown。
- 旧 durable task 可能仍带 `system_default_output_ref=true` 和
  `work/child_outputs/<run_id>/<slot>-<slug>.md`。唯一 rebind 读取入口继续保留，避免恢复时路径相撞；但
  context bundle、完成信封和父级 `expected_outputs` 全部隐藏该内部 ref。真正的用户文件、artifact 和
  显式共享引用不享受隐藏或冲突例外。
- 2026-08-25 起，根 wake 与递归父级恢复上下文共用 `subagent-completion.v1` 交接内容；
  `direct-children-context.v2` 直接给出有界最终回复和精确交付 refs。内部
  `runner_result.json`、`output.json`、`runner_response.md` 仍可供宿主审计/恢复，但不再进入父模型的正常
  prompt。该设计对应 会话运行时 把 child `last_agent_message` 随 terminal status 交给父级，而不是让父级猜目录。

## 2026-07-29 后台 claim 与当前执行轮

- `ConversationStore.claim_background_run` 仍是同一 thread 的独占执行租约；scheduler 只有取得租约后
  才构造 `source=background_main_agent` 的运行参数。
- 该参数对精确 task 携带 per-turn `conversation_task_turn_active=true`，表示“当前轮是租约持有者”，
  使工具网关不会把它当成外部第二执行器。该标志不进入 owner 长期状态，也不由模型提供。
- 其他前台、后台或不同 task 的轮次没有这个当前轮事实，继续经过
  `conversation_workspace_execution_blocker`。因此续派 child 的可用性修复没有放宽并发写边界。

## 2026-07-28 工具能力继承边界

- `services/hierarchy/scheduler.py` 把已经解析的 child role 与父 task 的当前 `allowed_tools` 一并交给
  `services/hierarchy/tool_policy.py`。
- `tool_policy.scheduled_child_tools` 先整理显式请求与角色候选，再统一与父工具集合求交集。
  worker/researcher/tester/writer/bug-finder 等 leaf 随后移除 create/guidance/cancel/resolve 四个直属下级
  控制入口；只有结构化模板 `can_spawn_children=true` 的 coordinator 才能保留父级已有的对应能力。
  coordinator 不做额外扩权，所有后代因此只能沿树继续减法。
- 角色默认、显式 grant、历史快照与递归继承在进入模型工具面前共用
  `active_model_subagent_tools`；它删除已退休的查树/手动调度工具并保持顺序去重。显式
  `allowed_tools=[]` 保持为空，只有缺省值 `None` 才从角色模板派生候选工具。
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
- `agent/subagents/runner_session_liveness.py` 与
  `agent/agent_core/orchestration/tools/cancel.py`：`runner_session.in_process` 区分 Gateway 内线程与
  独立子进程；前者只能协作中断，后者才可发送操作系统信号，禁止把宿主 PID 当 child PID。
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
root/status/整树筛选，也不能越过 child 代管孙代理。取消会写 CANCELLED/ABANDONED、废弃 active attempt、
尽量 interrupt/terminate 已知 pid/session，并写审计记录。宿主恢复与运维仍保留内部批量 primitive；模型
工具只处理 canonical loader 能读取且直接父子授权通过的 run。父级说明取消/接管原因后，可以继续汇总，
或用 `create_subagents` 创建替代执行者。

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

子代理结果回报里的产物入口只认当前结构化字段：`artifacts`、顶层
`artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
`deliverables`、`files_modified`、顶层 `file_path/path` 这类历史别名不会被恢复成
artifact refs。创建任务时给子代理的 `output_files` 是目标路径合同，不是结果回报别名。

声明产物只表示预期交付，不授权 runtime 自动创建内容。runner result 只登记确实存在、
可解析到当前任务工作区且通过 registry 校验的文件；缺失声明保持缺失，交给父代理根据
结构化缺口继续工作或报告。禁止从另一个文本产物按后缀或同名搜索复制，禁止把 summary/
findings 渲染成占位文件。唯一允许的收尾复制是 `output_delivery_map` 明确记录的真实
`source -> target`，并且 source 必须存在、target 必须仍在声明写围栏内。

`create_subagents` 的模型入口只有单 `goal` 和明确 `items` 两种形态，不克隆同一份任务。
多个 child 必须在 `items` 里声明不同工作；需要给出明确交付路径时，每个 item 可显式声明自己的
`output_files`。提供的相同目录以及目录与其子路径属于同一个冲突范围，不能分给并行 child；没有声明时
不能假定模型已经预报完整写集。顶层交付目标归父任务，不会暗中复制到所有 child。

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

## 2026-06-11 产物落点投影层

- 新增 `services/output_alignment.py`：纯投影、不改 task。`anchored_output_refs(task)`
  产出 OutputAnchoring（anchored_refs / delivery_map / warnings）；
  `anchor_refs_for_execution(task, refs)` 给合同投影层批量翻译目标 refs。
  消费方：`context_bundle_contracts.task_packet/output_contract`（执行合同）、
  `result_artifact_evidence.deliver_anchored_outputs_to_declared`（收尾搬运）、
  `delivery_closeout/subagent_aggregation`（声明对账复用 looks_like_output_path）。
- `agent_core/orchestration/tools/capability.py`：`resolve_capability_requests` 工具
  实现（grant/deny + 安全围栏 + wake）；注册链 core.py → orchestration_tools.py。
- `services/lifecycle.py`：capability request/grant/gap 与 evidence 生命周期写入；grant 保存后只对精确
  `capability_request` 阻塞 child 做 conversation link 的 `blocked -> active` CAS，使同 run 可以续跑，
  但 cancelled/terminal link 不会被复活。
- `services/runner_context_service.py`:`task_product_write_roots` 子代理产物写区
  (过滤自己 agent 目录与 report 区);为空时 `_task_workspace_fallback_roots`
  回退任务工作区 output/work,保证子代理总能写产物(batch3 C3/G4 修复)。

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
