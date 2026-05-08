## 2026-05-06 structure update
- `subagents/models.py` is now a compatibility facade over `model_capabilities.py`, `model_records.py`, `model_runtime.py`, and `model_task.py`.
- `subagents/result_processors.py` delegates structured output handling to `result_structured.py` and output payload assembly to `result_payloads.py`.
- `subagents/manager_base.py` delegates work-order filesystem concerns to `manager_work_orders.py`.
- `SubAgentTask` now carries task-tree control-plane fields: `StatusReport`, progress/current step/latest summary, blockers, artifact/evidence refs, evidence packets, findings, and checkpoint refs.
- Each persisted task can write `reports/status_report.json`; report board items include child status counts, evidence/finding counts, progress, latest summary, and blocker count.
- Acceptance review records now expose layered fields: worker claims, evidence facts, parent conclusions, and verifier checks.
- Action plan/apply records now expose rescue/escalation metadata: trigger, strategy, escalation target, and context refs.
- Each persisted task now writes compact/checkpoint recovery artifacts under `reports/`: `checkpoint.json`, `decision_ledger.json`, `progress.md`, `failing_tests.json`, and `next_actions.json`.
- Each persisted task now creates `SKILL_SPARKS.md` as a task-local skill learning candidate file; it is not main memory and is not auto-promoted.
- Each persisted task now mirrors a Phase 0 runtime memory task workspace under `tasks/<root_id>/`, with `state.json`, `timeline.jsonl`, shared/artifact folders, and a legacy run adapter pointing back to the old work-order directory.
- Each persisted task now mirrors a Phase 1 runtime memory agent run workspace under `tasks/<root_id>/agents/<run_id>/`, with agent identity, run state, task brief, timeline, checkpoint, summary, final report, findings, inbox/outbox, artifacts, and compactions skeletons.
- Each persisted task now appends a Phase 2 daily event ledger row under `daily/YYYY-MM-DD/events.jsonl`, with compact status/summary/refs instead of full subagent context.
- Each persisted task now writes Phase 3 task/run artifact manifests under `artifacts/manifest.jsonl`, with summary/hash/path metadata and workspace-boundary resolution status instead of artifact bodies.
- Each persisted task now writes a Phase 4 run-local compact checkpoint chain under `compactions/`, with append-only ledger rows plus latest summary/metadata refs.
- Each persisted task now writes a Phase 5 task-local shared workspace under `shared/`, with blackboard rollup, appended status messages, id-merged findings, and id-merged evidence packet files for sibling collaboration.
- Each persisted task now writes a Phase 6 run-local memory gate under `memory_gate/`, with candidate and review queue files that never auto-promote into main memory or formal skills.
- `subagents-memory-gate` can now write explicit review decisions to `memory_gate/decisions.jsonl`; these decisions are preserved across later saves and still do not auto-promote.
- `subagents-memory-gate` can now explicitly run retention, export approved memory candidates, export approved skill drafts, and verify the no-auto-promotion boundary; none of these paths installs a formal skill automatically.
- Each persisted task now also updates the LocalStore agent runtime control-plane projection: `agent_runs`, `agent_events`, and `task_rollups`. This projection is for upper-agent, takeover, and shared progress views only; task/run workspace files remain the source of truth.
- LocalStore control-plane queries now accept a runtime query context, so a main agent, middle subagent, or future takeover agent can ask for root tree, own subtree, blocked runs, or takeover candidates through the same bundle-shaped API.
- Child tasks with a parent now carry an audit-only inheritance manifest, written to `reports/inheritance_manifest.json`, showing inherited / overridden / dropped capability and context fields without expanding parent context into the child.
- The first shared progress panel read model now composes runtime query results, task rollup, blocked visible runs, and inheritance manifest refs for upper-agent or takeover-agent views; it still points back to task/run workspace facts instead of loading artifact bodies.
- Failed or blocked tasks now carry a first failure handoff record, written to `reports/failure_handoff.json`, with warnings, last safe checkpoint refs, artifact/evidence refs, and next-run avoidance advice; it is recovery guidance, not an automatic rescue trigger.
- Tasks now also have audit-only security reserve fields: `SecuritySignal` entries and `security_review_required`. These fields are for future security hijack/deception defenses and do not enforce policy by themselves.
- CLI status surfaces now expose shared progress, failure handoff refs, takeover refs, parent acceptance dry-run summaries, and parent acceptance next-action summaries in `status --json`, human `status`, and the `subagents` board. These views show counts, decisions, actions, command summaries, `mutates_task_state`, and refs only; they do not execute commands or load failure handoff, test report, or artifact bodies.
- Large runtime tool outputs now write a fail-safe recovery snapshot before externalization. The snapshot stores metadata such as tool, hash, size, run/task/request ids, and next action, while the full output body still belongs only to the externalized artifact.
- ToolContextReducer now protects the next live prompt: externalized large outputs are injected as refs and metadata only, while full bodies remain in artifacts.
- Takeover/rescue command paths now consume takeover readiness refs first: action plans and takeover apply records surface `takeover_readiness.json` before its recommended read order, without loading artifact bodies.
- Rescue packet metadata now travels with action plan/apply records: dedupe, repeat count, retry limit, escalation target, manual confirmation, and recovery entrypoints are visible as refs-only audit data.
- Parent Acceptance Auto Policy v1 dry-run 已有第一片实现：当前只生成策略审计，不执行 tests、不 apply acceptance、不自动触发 rescue。
# Subagent：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- subagent.py                         # 兼容入口
|-- subagents/                          # subagent 任务、manager、报告、runner、解析和渲染
|-- subagent_workflows/                 # workflow 模型、模板加载、路由、编译和验收规划
|   |-- builtin/                        # 内置 workflow 模板
|   |-- models.py                       # workflow / 任务 / 质量契约相关模型
|   |-- store.py                        # 模板加载和覆盖
|   |-- router.py                       # 根据目标选择 workflow
|   |-- compiler.py                     # 把 workflow 编译成 worker 派工规格
|   |-- planner.py                      # dry-run 规划门面
|   `-- acceptance.py                   # 父级验收计划
`-- agent_core/                         # 主循环、dispatch、runner prompt 等接入点
```

## 核心文件

- `agent_py_agent/agent/subagent_workflows/router.py`：回答”这个任务适合哪种 workflow”。
- `agent_py_agent/agent/subagent_workflows/compiler.py`：把抽象模板变成具体 worker 任务说明。
- `agent_py_agent/agent/subagent_workflows/acceptance.py`：生成父会话要检查什么。
- `agent_py_agent/agent/subagents/`：保存真实 subagent 管理、运行、报告和验收相关代码。
- `agent_py_agent/agent/subagents/models.py`：定义 `SubAgentTask`、`TaskStatus`、`DISPATCH_INELIGIBLE_STATUSES` 等核心数据结构。
- `agent_py_agent/agent/subagents/model_task.py`：承接 `SubAgentTask`、`EvidencePacket`、`Finding`、`StatusReport`、`SecuritySignal` 等任务树、证据和安全预留合同模型，`models.py` 继续作为兼容导出入口。
- `agent_py_agent/agent/subagents/model_task.py`：同时定义 `RuntimeIdentity`，记录 service owner、requester、effective principal、conversation、memory namespace 和 config overlay scope；这些字段是隔离和审计口子，不是授权、长期记忆或全局配置事实源。
- `agent_py_agent/agent/subagents/execution_records.py`：定义 `TestExecutionRecord`，保存真实验收执行证据字段、序列化、stdout/stderr 截断和 `passed` 派生结果。
- `agent_py_agent/agent/subagents/execution_executor.py`：定义最小 `TestExecutor`，支持 command / file_check / content_check，当前不写任务状态、不生成 `test_execution.json`。
- `agent_py_agent/agent/subagents/execution_report.py`：写入和读取 `test_execution.json`，并生成 `test_execution.md` 展示报告；JSON 是事实源，Markdown 不参与机器判断。
- `agent_py_agent/agent/subagents/parent_acceptance_controller.py`：生成父代理验收 dry-run 决策，返回 `execute_tests` / `inspect_only` / `request_human` / `rescue` 等下一步；它只读 refs 和机器事实源，不执行命令、不写 task；显式写入时生成 `parent_acceptance_decision.json`。
- `agent_py_agent/agent/subagents/parent_acceptance_apply.py`：保存父级验收显式 apply 的结果模型、拦截/应用结果构造和 `parent_acceptance_apply.json` 落盘逻辑。
- `agent_py_agent/agent/subagents/parent_acceptance_next_action.py`：把父级验收 plan/apply 审计映射成下一步动作建议，例如 `run_tests`、`request_human_confirmation`、`plan_rescue` 或 `apply_acceptance`；它只返回建议和 refs，不执行动作。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_policy.py`：把 next-action 映射成自动策略 dry-run 判断并写入 `parent_acceptance_auto_policy.json`；当前只生成 allow/blocked、would_execute 和 executed=false。
- `agent_py_agent/agent/subagents/manager_parent_acceptance.py`：承接 manager 的父级验收 plan/write/apply/next-action/auto-policy 桥接流程，让 `manager_acceptance.py` 类体只保留薄转发方法。
- `agent_py_agent/agent/subagents/acceptance_test_execution.py`：把显式开启的真实测试执行接入 acceptance findings，生成 `test_execution_recorded` 和 `test_execution_passed`，默认不运行。
- `agent_py_agent/cli/_acceptance_plan.py`：提供 `subagents-acceptance-plan` CLI 渲染；默认只展示父级验收 dry-run 决策和 refs，`--write` 只写决策审计文件，`--apply` 只允许 `inspect_only` 进入既有 acceptance apply，其它决策只写拦截审计，`--next-action` 只打印上级动作建议，`--auto-policy` 只打印并写入自动策略 dry-run 审计。
- `agent_py_agent/cli/_review.py`：提供 `subagents-tests` 和验收相关兼容导出；tests 默认只读取已有 `test_execution.json`，显式 `--re-run` 才重新执行 `output.json.tests`。
- `agent_py_agent/agent/settings/config.py` / `agent_py_agent/config/agent_config.yaml`：提供 `acceptance_execute_tests` 和 `acceptance_test_timeout_seconds`，默认保持老验收路径不自动跑命令。
- Auto Policy v1 当前只实现 dry-run 审计，尚未接主配置；后续若做成用户可见主配置，配置默认值和中文说明必须同步写入 `agent_py_agent/config/agent_config.yaml` 与 `AgentConfig`。如果只是 subagent/capability 路由内部的授权、次数或 allowlist 细则，应进入 `agent_py_agent/config/capability_config.yaml` 与 `capability_config.py`，不要扩张主配置。
- `agent_py_agent/agent/subagents/manager_indexing.py`：实现 `_select_runs()` 等索引和过滤逻辑，同时提供公开别名 `select_runs()`、`index_task()` 等。
- `agent_py_agent/agent/subagents/manager_acceptance_findings.py`：实现验收发现逻辑，公开别名 `acceptance_findings()`。
- `agent_py_agent/agent/subagents/acceptance_review_service.py`：对单个任务做父级验收，生成分层 acceptance record，并执行只读 verifier checks。
- `agent_py_agent/agent/subagents/acceptance_helpers.py`：验收发现子模块的兼容导出入口，真实实现已拆到 `acceptance_helpers/` 目录。
- `agent_py_agent/agent/subagents/acceptance_helpers/evidence_acceptance_findings.py`：承接证据存在性和 read/write_file 工具证据 findings 构建逻辑，保持 `evidence.py` 作为薄兼容入口。
- `agent_py_agent/agent/subagents/manager_patch.py`：实现补丁操作，公开别名 `resolve_patch_target()`。
- `agent_py_agent/agent/subagents/services/patch_review_helper.py` / `patch_apply_helper.py`：保留 service 未初始化时的兼容 fallback；当前也支持 `PatchReviewTaskRequest` / `ApplyPatchTaskParams` bundle，避免 fallback 路径继续依赖散参。
- `agent_py_agent/agent/subagents/manager_runner_results.py`：负责 runner 结果写回、状态降级和 `output.json` / `runner_result.json` 持久化；结构化解析失败时会把最终结果统一降级成 `BLOCKED` / `ok=False`。
- `agent_py_agent/agent/subagents/manager_runner_result_payload.py`：承接 runner result payload/status 构建 dataclass 和纯 helper，让 manager facade 保持短小。
- `agent_py_agent/agent/subagents/services/persistence.py`：负责 task/run/status report 落盘和旧任务兼容归一化，生成 `reports/status_report.json`。
- `agent_py_agent/agent/subagents/services/control_plane_projection.py`：把一次 subagent 保存同步成 LocalStore 控制面投影，写入 agent run、保存事件和 root task rollup。
- `agent_py_agent/agent/subagents/services/inheritance_manifest.py`：创建子代理时比较 parent/child 字段，生成 inherited / overridden / dropped 继承清单；只记录审计事实，不改变 child 运行字段。
- `agent_py_agent/agent/subagents/services/persistence_inheritance.py`：归一化并写入 `reports/inheritance_manifest.json`，让 persistence 主流程保持薄编排。
- `agent_py_agent/agent/subagents/services/failure_handoff.py`：根据失败/阻塞状态和 `failure_type` 生成失败交接记录，给后续接管代理留下警告、避坑建议和推荐下一步。
- `agent_py_agent/agent/subagents/services/persistence_failure_handoff.py`：归一化并写入 `reports/failure_handoff.json`，让 persistence 主流程只负责编排。
- `agent_py_agent/agent/subagents/services/persistence_recovery_outputs.py`：集中写 checkpoint artifacts 和 takeover readiness 文件，避免 persistence 主保存流程重新靠近 code-size 风险。
- `agent_py_agent/agent/subagents/services/takeover_readiness.py`：生成 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md` 接管前必读包；只整理 refs、manifest 元数据和读取顺序，不读取 artifact 正文；并提供 refs-only 的推荐读取顺序解析给 rescue/action apply 使用。
- `agent_py_agent/agent/subagents/services/persistence_security.py`：归一化 `SecuritySignal` 预留字段，让安全信号解析不挤进 persistence 主流程；当前不执行安全策略。
- `agent_py_agent/agent/subagents/services/persistence_identity.py`：归一化 `RuntimeIdentity` 预留字段，让员工/会话/配置 scope 解析不挤进 persistence 主流程；当前只保留审计元数据。
- `agent_py_agent/agent/subagents/services/checkpoint_artifacts.py`：从 task facts 和 `output.json` 构建 compact 可读恢复包，包含 checkpoint、decision ledger、progress、failing tests 和 next actions。
- `agent_py_agent/agent/subagents/services/task_workspace_adapter.py`：把 runtime memory task workspace 路径同步回 `SubAgentTask`，避免 persistence 保存函数继续膨胀。
- `agent_py_agent/agent/memory_archive/task_workspace.py`：subagent 保存路径调用的 runtime memory adapter；创建 `tasks/<root_id>/` task workspace skeleton 和 `agents/<run_id>/legacy_run_ref.json`，但不移动旧工单目录。
- `agent_py_agent/agent/memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 agent run workspace skeleton；旧工单目录仍是兼容读写面，run workspace 先承接恢复、接管、finding 和 compact 链的后续入口。
- `agent_py_agent/agent/memory_archive/compact_subagent_owner.py`：给 `memory-resume --from-compact` 提供子代理 owner 只读引用解析；`subagent_run` / `subagent_session` 会指向 task-local run workspace 和 legacy adapter refs，但不写主 memory、不改 runner。
- `agent_py_agent/agent/memory_archive/daily_ledger.py`：追加 `daily/YYYY-MM-DD/events.jsonl`，只写 task/run 状态摘要、duration、artifact/evidence refs 和 workspace 路径引用。
- `agent_py_agent/agent/memory_archive/artifact_registry.py`：把 `artifact_refs` 写成 task/run `artifacts/manifest.jsonl`，记录摘要、hash、路径、size、exists 和 `resolution_status`；只读取 legacy task dir、task workspace、agent run workspace 内的文件，越界路径只登记 blocked，不复制正文。
- `agent_py_agent/agent/memory_archive/compact_chain.py`：把 run `compactions/` 升级成 checkpoint-first compact chain，追加 ledger、写 summary/metadata，并把最新 refs 回写到 run checkpoint。
- `agent_py_agent/agent/memory_archive/shared_workspace.py`：同步 task-local `shared/` 协作面，把 evidence packets、findings、status messages 和 blackboard rollup 写成 sibling 可读事实；messages 追加，findings/evidence 按 id 合并，不写主 memory。
- `agent_py_agent/agent/memory_archive/memory_gate.py`：同步 run-local `memory_gate/`，把 lessons / findings 写成带证据、适用范围、缺口和 review 要求的候选；它只排队，不负责正式提升。
- `agent_py_agent/agent/memory_archive/memory_gate_retention.py`：生成/应用保守 retention，只从 active review queue 清出 closed 候选，保留候选、decision 和 export 审计。
- `agent_py_agent/agent/memory_archive/memory_gate_export.py`：把 `approve_memory` 候选显式写入主 JSONL memory，或把 `approve_skill` 候选显式写成 skill draft。
- `agent_py_agent/agent/memory_archive/memory_gate_verifier.py`：检查 no-auto-promotion 边界，写 `verifier_report.json`。
- `agent_py_agent/agent/local_storage/control_plane.py`：提供 `upsert_agent_run()`、`record_agent_event()`、`rebuild_task_rollup()`、`list_agent_tree()` 和 `query_agent_runtime()` 等上级代理/接管代理查询 API。
- `agent_py_agent/agent/local_storage/control_plane_panel.py`：基于 runtime query 生成 `SharedProgressPanel`，汇总 rollup、可见 runs、blocked runs 和 inheritance manifest refs。
- `agent_py_agent/cli/shared_progress.py`：给 `status` 和 `subagents` CLI 生成共享进度摘要，只展示 blocked/failure refs 数量，不读取正文。
- `agent_py_agent/agent/agent_core/tool_output_failsafe.py`：在大工具输出写 artifact 前写 recovery snapshot，避免外置失败时没有恢复锚点。
- `agent_py_agent/agent/agent_core/tool_context_reducer.py`：控制工具结果进入下一轮 live prompt 的形态；大输出只注入 artifact 摘要和 checkpoint refs。
- `agent_py_agent/agent/local_storage/control_plane_models.py`：定义 `AgentRunRecord`、`AgentEventRecord`、`TaskRollupRecord`、`AgentRuntimeQueryContext` 等控制面记录结构，并预留 `metadata` / `reserved` 扩展字段。
- `agent_py_agent/agent/local_storage/control_plane_codec.py`：集中维护控制面 SQLite SQL、写入参数和 row codec，让公开 mixin 不承载大块 SQL 细节。
- `agent_py_agent/agent/subagents/manager_memory_gate.py`：给 manager 增加候选列表、review decision、retention、export 和 verifier 方法；默认路径仍不导出 memory/skill。
- `agent_py_agent/agent/subagents/acceptance_review_service.py`：父级验收核心实现接收 `AcceptanceReviewRequest` bundle，manager 旧参数入口只做兼容转接。
- `agent_py_agent/agent/subagents/manager_acceptance.py`：提供父级验收 dry-run 计划入口，并让默认验收路径保持旧 `apply/reviewer/note` 关键字调用；只有真实测试执行等新 options 字段启用时才传完整 bundle，兼容旧测试替身和外部调用。
- `agent_py_agent/agent/subagents/models.py`：继续作为兼容导出入口，并新增 `SubAgentCapabilityRouteOptions`、`SubAgentChannelProbeOptions`、`SubAgentDueCheckOptions`，让 manager/core/CLI 共用同一批业务 options bundle。
- `agent_py_agent/cli/models.py`：承接 CLI 专属 options bundle；命令函数只在边界读取 argparse，再把参数转成小 dataclass 传给业务层或 manager bundle。
- `agent_py_agent/agent/agent_core/dispatch_params.py`：承接 dispatch/watch 的 `DispatchParams` / `WatchParams`，让父代理 dispatch 主链路不再依赖裸 kwargs 扩展。
- `agent_py_agent/agent/agent_core/subagent_params.py`：承接 runner/spawn 相关轻量 bundle，包括 `SubagentRunParams`、`SpawnSubagentsParams` 和 runner flow 内部的 probe/failure/finalize 参数。
- `agent_py_agent/agent/agent_core/subagent_run_flow.py`：承接单个 subagent runner 从 prompt、probe、模型调用到结果写回的流程，避免 `subagent_mixin.py` 因 bundle 兼容继续变胖。
- `agent_py_agent/cli/_gateway_process_service.py`：legacy gateway process service 通过 `GatewayRunContext` / `GatewayRunOptions` 重建 worker 和 heartbeat，不再把 argparse 对象传入后台线程。
- `agent_py_agent/cli/_memory_gate.py`：实现 `subagents-memory-gate` CLI，显式列出候选、写回 approve/reject/needs_evidence、导出 approved 候选、生成 skill draft 和跑 verify。
- `SKILL_SPARKS.md`：子代理目录里的经验火花候选，只记录可复用步骤、触发条件、证据引用、限制和反例；后续提升为 skill 必须经过单独 gate。
- `agent_py_agent/agent/subagents/services/board.py`：把任务树节点转成 report board item，并汇总 child status、progress、summary、evidence/finding/blocker 计数。
- `agent_py_agent/agent/subagents/services/rescue_policy.py`：根据 due-check issue 给 action plan 添加 rescue/escalation 元数据，保持建议可审计但不自动越权执行；当 task 目录存在 `reports/takeover_readiness.json` 时，会优先把该包和推荐读取 refs 放入 `rescue_context_refs`，并生成 refs-only 的 `rescue_packet`。
- `agent_py_agent/agent/subagents/rendering_rescue.py`：渲染 rescue packet 的 retry / manual confirmation / recovery refs 摘要，避免主 `rendering.py` 继续接近 code-size 风险线。
- `agent_py_agent/agent/subagents/result_structured.py`：解析 runner structured output 中的 evidence packets、findings、artifacts、tests、blockers，并写回任务事实。
- `agent_py_agent/agent/subagents/capability_route_service.py`：承接 capability route record 构建、gap 包装、summary 和报告落盘。
- `agent_py_agent/agent/subagents/services/indexing_records.py`：承接 LocalStore dataclass record 索引 helper，让 indexing service 只保留编排入口。
- `agent_py_agent/agent/subagents/policies.py` / `policy_checks.py`：负责 due-check 风险规则和下一步建议命令；用户可见命令统一使用 `my-agent` 控制台入口。
- `agent_py_agent/agent/memory_push.py`：实现记忆推模式，在决策点自动注入相关记忆。
- `agent_py_agent/cli/subagents.py`：用户从 CLI 预览或操作 subagent 的入口。

## 数据流

1. 用户输入目标，例如“帮我做一个高质量文档交付”。
2. router 根据目标和配置选择 workflow。
3. store 加载内置或用户覆盖的模板。
4. compiler 生成 worker 派工规格，包含写入范围、证据要求和不能自验收的规则。
5. acceptance planner 生成父级验收清单。
6. runner 根据 execution context 调模型和工具，把 `RUNNER_RESULT.md`、`reports/runner_result.json`、`reports/status_report.json`、`reports/checkpoint.json`、`reports/progress.md`、`SKILL_SPARKS.md`、`output.json` 写回旧 run 工单目录。
7. structured output 中的 `evidence_packets` / `findings` 会进入任务事实源；acceptance 会检查完成态结果是否有 evidence chain。
8. acceptance report 分层记录 worker 自述、证据事实、父级结论；verifier checks 只读 evidence packets / findings 并能阻断未解决风险。
9. Acceptance Real Execution 当前提供 `TestExecutionRecord`、最小 `TestExecutor`、report 存储、显式 acceptance 接入、`subagents-tests` CLI、`subagents-acceptance-plan` CLI 和配置默认值；只有 `AcceptanceReviewOptions(execute_tests=True)`、`subagents-tests --re-run`、`subagents-acceptance --execute-tests` 或配置 `acceptance_execute_tests: true` 时才运行 tests 并生成报告/阻断 findings，默认旧验收路径和 acceptance-plan 都不执行命令。
9. Parent Acceptance Controller 的 `--apply` 当前只是第一片安全桥接：`inspect_only` 才能进入普通 acceptance apply；`execute_tests`、`request_human`、`rescue` 会被写入 `parent_acceptance_apply.json` 并保持任务状态不变，留给上级/自动调度器下一步显式处理。
9. Parent Acceptance Controller 的 `--next-action` 是自动调度前的建议层：它读取当前 plan 和已有 apply 审计 refs，返回下一步建议命令或人工/救援意图，但不会执行建议，也不会把建议当 verified fact。
9. Parent Acceptance Auto Policy v1 dry-run 当前消费 next-action、决策/apply refs 和保守 allowlist。第一片只判断“策略是否允许、如果允许会执行什么、为什么仍不执行”，写 `parent_acceptance_auto_policy.json`；不运行 tests、不 apply、不 rescue，也不改 task/run 状态。
10. due-check 把 blocked、timeout、stale heartbeat、capability request/gap 等问题转成 action plan，并附带 rescue/escalation 元数据。
10. persistence 同步 `tasks/<root_id>/state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/`、`artifacts/` 和 `agents/<run_id>/legacy_run_ref.json`，为后续正式 agent run workspace 做兼容桥。
11. persistence 同步 `tasks/<root_id>/agents/<run_id>/agent.yaml`、run `state.json`、run `timeline.jsonl`、`task.md`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl` 和 inbox/outbox/artifacts/compactions 目录，先形成 agent run workspace skeleton。
12. persistence 追加 `daily/YYYY-MM-DD/events.jsonl`，让主代理先按天查 task/run/event/artifact refs，再回到 task/run 文件核实。
13. persistence 写 `artifacts/manifest.jsonl`，让父级先看摘要、hash、path、exists 和 resolution status；只有 workspace 边界内的 artifact 才会被读取正文或计算 hash。
14. persistence 追加 `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并把 run `checkpoint.json` 指向最新 compact refs；当前不会删除 timeline、artifact 或旧 work-order 文件。
15. persistence 同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享结构化任务事实；messages 追加，findings/evidence 按 id 合并，避免互相覆盖，但不写入主 memory。
16. persistence 同步 `memory_gate/candidates.jsonl`、`review_queue.jsonl` 和 `skill_spark_gate.json`，把 lesson/finding 作为候选排队；`subagents-memory-gate` 可把 reviewer decision 追加到 `decisions.jsonl`。
17. create_run 如果看到 `parent_id`，会生成 task-local inheritance manifest，记录 child 当前字段相对 parent 的 inherited / overridden / dropped 项；manifest 是审计事实，不会把 parent 全量上下文灌入 child prompt。
18. persistence 对失败、错误、超时、阻塞或带 `failure_type` 的 task 生成 `reports/failure_handoff.json`；它记录 warning、risk level、last safe checkpoint、artifact/evidence refs、avoid-next-time 和 recommended next action，但不触发自动 rescue。
19. persistence 生成 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md`，把 failure handoff、checkpoint、status report、artifact manifest、evidence refs 和 artifact refs 排成 recommended read order；它是恢复索引，不复制或读取大 artifact 正文。
20. persistence 把当前 task 投影到 LocalStore 的 `agent_runs` / `agent_events` / `task_rollups`；上级代理或接管代理可以先用 `AgentRuntimeQueryContext` 查 root tree、own subtree、blocked runs 或 takeover candidates，再回到 task/run workspace 核实事实。
20. task 如果携带 `security_signals` 或 `security_review_required`，persistence 会随 `task.json` 保存这些 audit-only 字段；它们只记录可疑信号和证据引用，不自动阻断 runner、改变工具授权或修改 memory。
21. 控制面投影的 metadata 会暴露 `inheritance_manifest_ref`、`failure_handoff_ref`、`takeover_readiness_ref`、`security_signal_count`、`security_signal_types` 和 `security_review_required` 这类恢复/安全线索；这些 refs 指向任务目录里的事实文件，不替代 artifact、checkpoint 或 verified finding。
22. 如果 task 携带 `RuntimeIdentity`，控制面 metadata 会额外暴露 `runtime_identity`、`memory_scope` 和 `config_scope`。默认 `conversation_memory_policy=not_enabled`、`writes_global_config=false`；员工/外部会话只能形成可审计 run/conversation 作用域元数据，不能自动生成员工长期记忆或污染全局配置。
23. `query_shared_progress_panel()` 在 runtime query 之上返回面板状态包，包含 rollup、可见 runs、blocked runs、inheritance refs、failure handoff refs 和 takeover readiness refs；它只做投影汇总，不读取 artifact 正文或替代 verified facts。
24. `status --json`、人类 `status` 和 `subagents` 看板通过 `cli/shared_progress.py` 展示共享进度摘要；展示层按 root task 查询控制面，不扫描旧工单目录正文。接管视图可以显示 principal、conversation、memory namespace 和 config scope 摘要；`Acceptance Plan` 只调用父级 dry-run planner 生成决策摘要，不执行 tests、不写 task；`Acceptance Next Action` 只调用父级 next-action planner 生成 action、reason、command、refs 和 `mutates_task_state` 摘要，不执行命令、不写 task。需要审计落盘时必须显式调用 `subagents-acceptance-plan --write`；需要尝试写回状态时必须显式调用 `subagents-acceptance-plan --apply`，且当前只放行 `inspect_only`；需要单独查看下一步建议时可调用 `--next-action`。
24. runtime tool loop 对大工具输出先调用 `tool_output_failsafe.py` 写 recovery snapshot，再调用 `tool_output_externalizer.py` 写 artifact；snapshot 里只有摘要 metadata，完整输出不会进入 snapshot。
25. `tool_context_reducer.py` 根据 archive record 决定下一轮 prompt 内容：小输出保留原工具结果，大输出只保留 preview、artifact path、hash、size 和 fail-safe checkpoint path。
26. action plan 的 `rescue_context_refs` 和 takeover apply 的 `evidence_paths` 会把 `takeover_readiness.json` 放在首位，再按 packet 的 recommended read order 展开 failure handoff、checkpoint、artifact manifest 等 refs；这些路径是恢复索引，不代表自动读取正文或自动接管。
27. action plan / apply record 的 `rescue_packet` 会记录 `dedupe_key`、`issue_kinds`、`repeat_count`、`retry_policy.max_attempts`、`escalation.target`、`manual_confirmation.required` 和 `recovery_entrypoints`；`auto_retry=false`、`auto_execute=false`、`reads_artifact_bodies=false` 是当前安全边界。
28. 只有显式 review/gate 通过且再触发 `--export-memory` 或 `--export-skill` 后，候选才允许进入长期 memory 或 skill draft 流程；approve 不会自动导出。
29. retention 只从 active review queue 移除 rejected / already exported 候选，候选、decision、export 和 verifier 文件仍留在 run workspace 里供接管和审计。
28. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源和 checkpoint artifacts，再由父级决定是否验收；这只是恢复入口推荐，不代表子代理写入主代理长期 memory。
29. workflow preview 仍可通过 CLI dry-run 展示；真实路径已接入 `create_run(... workflow_mode="plan|auto")` 和 `subagents-dispatch --apply --workflow-mode auto`，可把父任务上的 `workflow_plan` 物化为 worker 子工单。LOG 专项 apply path 和 runner 恢复 scenario 继续作为真实任务记录的先行验证样本。
30. CLI / core / manager 的新接口规则是先构造 Request/Options bundle，再进入业务服务；旧散参入口只做兼容 adapter，不作为新增字段的扩展位置。
31. subagent runner、spawn、recovery snapshot 和 board 的新字段优先加到 `subagent_params.py` 或 `SubAgentBoardOptions`，mixin 只做兼容 facade 和少量编排。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/subagents.py`，理解用户命令怎么进入程序。
2. 再看 `planner.py`，理解一个“规划结果”包含哪些部分。
3. 再看 `router.py`，学习如何把自然语言目标映射到模板。
4. 再看 `compiler.py`，学习模板怎样变成具体工作单。
5. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_parent_subagent_cross_day_resume_uses_runner_task_facts`，理解 runner 写回后如何跨天恢复到任务事实源。
6. 再看 `agent_py_agent/tests/test_subagent_persistence_service.py` 和 `agent_py_agent/tests/test_result_processors_edges.py`，理解 status report、evidence packets 和 findings 如何写回。
7. 最后看 `agent_py_agent/tests/test_subagent_workflow_*.py`，理解怎么证明路由、模板和编译没有坏。

## 当前第一版索引 / 待补齐

本页先解释主结构和学习路径。更细的状态机、workflow 子工单依赖、status report 索引范围和验收阻断细节，后续仍需要继续补齐。

## Parent Acceptance Auto Policy v1

### 设计目标

Auto Policy v1 解决的问题是：父级验收已经能给出 next-action，但上级代理还缺少一份可审计、可配置、默认安全的“是否允许自动推进”判断。当前第一片已实现 dry-run 和审计，不把建议变成真实动作。

第一版明确不做：
- 不执行 `subagents-tests --re-run` 或任何测试命令。
- 不调用 acceptance apply。
- 不触发 rescue / takeover / retry。
- 不读取 artifact 正文。
- 不提升 conversation/run overlay 到全局配置。

### 配置字段草案

用户可见主配置候选：
- `acceptance_auto_policy_enabled: false`：总开关，默认关闭。
- `acceptance_auto_policy_mode: dry_run`：第一版只允许 `dry_run`；后续如引入 `enforce` / `execute`，必须另走设计评审。
- `acceptance_auto_policy_auto_execute: false`：是否允许策略直接执行动作；第一版固定 false。
- `acceptance_auto_policy_action_allowlist: ["run_tests"]`：策略允许考虑的动作；第一版默认只包含 `run_tests`，但仍因为 `auto_execute=false` 不会真正运行。
- `acceptance_auto_policy_max_actions_per_run: 1`：单个 run 最多建议/尝试的自动动作次数，`0` 按能力路由配置约定表示不限制，但不建议第一版使用。
- `acceptance_auto_policy_write_audit: true`：是否写审计记录；第一版建议默认写。

能力路由 / subagent 专属配置候选：
- action allowlist 的分层覆盖、capability grant 条件、同一 root run 的节流、上抛目标、rescue 入口、测试命令授权细则，优先放在 `capability_config.yaml` / `capability_config.py`。
- 这些字段属于能力授权和路由边界，不应混进主配置；主配置只表达用户对父级验收自动化的全局偏好。

### 动作边界

- `run_tests`：只允许形成 would-run 审计，记录测试 refs、命令摘要和安全预检结果；第一版不执行。
- `request_human_confirmation`：只生成需要人工确认的原因、问题和 refs，不自动发起外部通知。
- `plan_rescue`：只引用 `failure_handoff_ref`、`takeover_readiness_ref`、`rescue_packet` 和 recommended read order，不启动救援 agent。
- `apply_acceptance`：只说明需要显式 apply gate；第一版不会自动调用 apply。

自动执行必须被阻断的情况：
- `requires_human=true`。
- 存在 `security_review_required=true` 或高严重度 `SecuritySignal`。
- next-action 不在 allowlist。
- 缺少 parent decision / apply / evidence refs。
- effective config 来自 conversation/run overlay 且试图影响 project / tenant / global scope。
- action 需要读取 artifact 正文、写文件、发网络请求、启动进程或改变长期状态。

### 审计 JSON 草案

事实源路径：`reports/parent_acceptance_auto_policy.json`。当前实现使用 `schema: parent_acceptance_auto_policy.v1`、`dry_run=true`、`policy` 和 `reserved`；下面保留后续配置化扩展草案：

```json
{
  "schema_version": 1,
  "record_type": "parent_acceptance_auto_policy",
  "created_at": "ISO-8601",
  "run_id": "string",
  "root_run_id": "string",
  "parent_run_id": "string|null",
  "policy": {
    "enabled": false,
    "mode": "dry_run",
    "auto_execute": false,
    "action_allowlist": ["run_tests"],
    "max_actions_per_run": 1,
    "config_source": "default|agent_config|capability_config|conversation_overlay|run_override"
  },
  "decision_refs": {
    "parent_acceptance_decision_ref": "reports/parent_acceptance_decision.json",
    "parent_acceptance_apply_ref": "reports/parent_acceptance_apply.json|null",
    "next_action_ref": "inline|path|null",
    "test_execution_ref": "reports/test_execution.json|null",
    "takeover_readiness_ref": "reports/takeover_readiness.json|null"
  },
  "next_action": "run_tests|request_human_confirmation|plan_rescue|apply_acceptance|none",
  "allowed_by_policy": false,
  "dry_run": true,
  "auto_execute": false,
  "would_execute": false,
  "executed": false,
  "blocked_reason": "dry_run_only",
  "requires_human": false,
  "safety_signals": [],
  "rescue_refs": [],
  "runtime_identity": {
    "service_owner_id": "string|null",
    "effective_principal_id": "string|null",
    "conversation_id": "string|null",
    "memory_namespace": "string|null"
  },
  "config_scope": {
    "scope": "global|tenant|project|principal|conversation|run|default",
    "writes_global_config": false,
    "promotion_policy": "explicit_review"
  },
  "reserved": {}
}
```

审计记录里的 `executed` 第一版必须恒为 false；`would_execute` 只表达策略判断，不代表动作已经发生。Markdown 或 CLI 视图只能展示摘要，机器判断必须读取 JSON。

### 安全预留

后续接入真实执行前，Auto Policy 必须先接入这些闸门：
- `requires_human`：人工确认优先级高于 allowlist。
- rescue / takeover：只能从 failure handoff、takeover readiness 和 rescue packet 读取 refs，不能自动读取大 artifact 正文。
- 安全信号：`SecuritySignal` 和 `security_review_required` 先作为硬阻断输入，再考虑细分 severity。
- 租户/员工 conversation 配置隔离：员工会话里的策略测试只能落在 conversation/run scope；提升到 project、tenant 或 global 必须有 admin approval、diff、audit event 和 rollback ref。

## 2026-05-06 structure update
- Workflow routing and subagent manager internals now separate decision fields, rendering sections, patch normalization, and service actions.
- Compatibility modules still re-export the existing public model and rendering names for callers.

## 2026-05-07 bundle structure update
- `manager_base.py`, `manager_dispatch.py`, `manager_actions.py`, `manager_indexing.py`, and patch facades now list old compatibility fields explicitly and immediately construct `CreateRunParams`, dispatch params, `ActionApplyOptions`, `LocalRecordParams`, or patch request bundles.
- `policy_checks.py` / `policies.py` use `RunnerNextActionParams`; `state_machine.py` uses `StateTransitionParams`; runner output payload construction passes those bundles instead of loose fields.
- `agent_core` dispatch/watch/run-subagent/recovery paths now normalize into `DispatchParams`, `WatchParams`, `DispatchLoopParams`, `SubagentRunParams`, and `RecoverySnapshotParams` before entering service logic.
- The remaining subagent params cleanup now routes action post-recording, report/dataclass indexing, learning candidate updates, due-check issue creation, patch review fallback, and patch spec validation through `RecordAfterTaskActionParams`, `IndexReportParams`, `DataclassRecordIndexParams`, `UpdateLearningCandidateParams`, `DueIssueSpec`, `PatchReviewTaskRequest`, and `PatchSpecFields`.

## 2026-05-07 hard/soft structure update
- Patch manager parsing details stay outside the main mixin path, preserving `SubAgentPatchMixin` as a facade over patch review/apply services.
- Action application split unsupported-action record creation and handler context construction into helpers while keeping `ActionApplyOptions` as the service bundle.
- `subagents/rendering.py` keeps acceptance record section and failed-summary formatting behind small shared helpers so verifier checks and findings use one rendering path.
- Patch/action/acceptance service internals now keep record construction and evidence item builders in focused helper modules; public manager methods continue to delegate through the existing service facade.
- The subagent slice contributes no strict code-size hard or soft findings after this cleanup; near-soft warnings remain visible for future pre-feature refactors.
- `subagent_workflows` keeps routing, dispatch-plan compilation, and parent acceptance planning as separate helper surfaces; compatibility callers can still ask for one planning result, but new fields enter through route/compile/acceptance bundles.

## 2026-05-07 annotation structure update
- Module structure docs now treat the definition-level double-layer comments as part of the code architecture: `LLM:` records model-facing contract/caller/side-effect notes, and `函数用途:` / `类用途:` records beginner-readable purpose and edit guidance.
- New files, services, bundles, or facade methods must update both this structure page and the in-code comments at the same time.
- The global file tree in `CODEBASE_TREE.md` now includes a current architecture map for CLI, agent core, gateway, memory, log-analysis, subagent, tooling, and settings boundaries.
## 2026-05-08 status/board takeover view structure update
- `cli/shared_progress.py` 现在在 shared progress panel payload 里生成 `takeover_entries`，每条记录只包含 run 摘要、handoff/readiness refs 和 recommended read order。
- `cli/local_status_view.py` 和 `cli/_board.py` 共用同一个 takeover view 渲染函数，保证 `status` 和 `subagents` 看板的接管入口一致。
- 接管视图只解析 `takeover_readiness.json` 这个索引文件，不读取 artifact refs 指向的大正文；事实核实仍回到 task/run workspace、failure handoff、checkpoint 和 artifact manifest。

## 2026-05-08 status/board acceptance next-action structure update
- `cli/shared_progress.py` 现在在 shared progress panel payload 里生成 `acceptance_next_action_entries`，每条记录只包含 run_id、action、reason、command、refs 和 `mutates_task_state`。
- `cli/acceptance_progress.py` 承接 Acceptance Plan / Acceptance Next Action 的 refs-only payload 和人类输出渲染；`cli/shared_progress.py` 只负责控制面面板组装和 takeover view。
- `cli/local_status_view.py` 和 `cli/_board.py` 共用同一个 Acceptance Next Action 渲染函数，保证 `status` 和 `subagents` 看板展示一致。
- 下一动作视图只调用 manager 的 `plan_parent_acceptance_next_action()` 生成建议，不读取 artifact 正文、不执行建议命令、不写任务状态。
