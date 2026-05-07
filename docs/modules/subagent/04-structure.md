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
- Each persisted task now writes Phase 3 task/run artifact manifests under `artifacts/manifest.jsonl`, with summary/hash/path metadata instead of artifact bodies.
- Each persisted task now writes a Phase 4 run-local compact checkpoint chain under `compactions/`, with append-only ledger rows plus latest summary/metadata refs.
- Each persisted task now writes a Phase 5 task-local shared workspace under `shared/`, with blackboard rollup, status messages, findings, and evidence packet files for sibling collaboration.
- Each persisted task now writes a Phase 6 run-local memory gate under `memory_gate/`, with candidate and review queue files that never auto-promote into main memory or formal skills.
- `subagents-memory-gate` can now write explicit review decisions to `memory_gate/decisions.jsonl`; these decisions are preserved across later saves and still do not auto-promote.
- `subagents-memory-gate` can now explicitly run retention, export approved memory candidates, export approved skill drafts, and verify the no-auto-promotion boundary; none of these paths installs a formal skill automatically.
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
- `agent_py_agent/agent/subagents/model_task.py`：承接 `SubAgentTask`、`EvidencePacket`、`Finding`、`StatusReport` 等任务树和证据合同模型，`models.py` 继续作为兼容导出入口。
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
- `agent_py_agent/agent/subagents/services/checkpoint_artifacts.py`：从 task facts 和 `output.json` 构建 compact 可读恢复包，包含 checkpoint、decision ledger、progress、failing tests 和 next actions。
- `agent_py_agent/agent/subagents/services/task_workspace_adapter.py`：把 runtime memory task workspace 路径同步回 `SubAgentTask`，避免 persistence 保存函数继续膨胀。
- `agent_py_agent/agent/memory_archive/task_workspace.py`：subagent 保存路径调用的 runtime memory adapter；创建 `tasks/<root_id>/` task workspace skeleton 和 `agents/<run_id>/legacy_run_ref.json`，但不移动旧工单目录。
- `agent_py_agent/agent/memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 agent run workspace skeleton；旧工单目录仍是兼容读写面，run workspace 先承接恢复、接管、finding 和 compact 链的后续入口。
- `agent_py_agent/agent/memory_archive/daily_ledger.py`：追加 `daily/YYYY-MM-DD/events.jsonl`，只写 task/run 状态摘要、duration、artifact/evidence refs 和 workspace 路径引用。
- `agent_py_agent/agent/memory_archive/artifact_registry.py`：把 `artifact_refs` 写成 task/run `artifacts/manifest.jsonl`，记录摘要、hash、路径、size 和 exists 状态，不复制正文。
- `agent_py_agent/agent/memory_archive/compact_chain.py`：把 run `compactions/` 升级成 checkpoint-first compact chain，追加 ledger、写 summary/metadata，并把最新 refs 回写到 run checkpoint。
- `agent_py_agent/agent/memory_archive/shared_workspace.py`：同步 task-local `shared/` 协作面，把 evidence packets、findings、status messages 和 blackboard rollup 写成 sibling 可读事实，不写主 memory。
- `agent_py_agent/agent/memory_archive/memory_gate.py`：同步 run-local `memory_gate/`，把 lessons / findings 写成带证据、适用范围、缺口和 review 要求的候选；它只排队，不负责正式提升。
- `agent_py_agent/agent/memory_archive/memory_gate_retention.py`：生成/应用保守 retention，只从 active review queue 清出 closed 候选，保留候选、decision 和 export 审计。
- `agent_py_agent/agent/memory_archive/memory_gate_export.py`：把 `approve_memory` 候选显式写入主 JSONL memory，或把 `approve_skill` 候选显式写成 skill draft。
- `agent_py_agent/agent/memory_archive/memory_gate_verifier.py`：检查 no-auto-promotion 边界，写 `verifier_report.json`。
- `agent_py_agent/agent/subagents/manager_memory_gate.py`：给 manager 增加候选列表、review decision、retention、export 和 verifier 方法；默认路径仍不导出 memory/skill。
- `agent_py_agent/agent/subagents/acceptance_review_service.py`：父级验收核心实现接收 `AcceptanceReviewRequest` bundle，manager 旧参数入口只做兼容转接。
- `agent_py_agent/agent/subagents/models.py`：继续作为兼容导出入口，并新增 `SubAgentCapabilityRouteOptions`、`SubAgentChannelProbeOptions`、`SubAgentDueCheckOptions`，让 manager/core/CLI 共用同一批业务 options bundle。
- `agent_py_agent/cli/models.py`：承接 CLI 专属 options bundle；命令函数只在边界读取 argparse，再把参数转成小 dataclass 传给业务层或 manager bundle。
- `agent_py_agent/agent/agent_core/dispatch_params.py`：承接 dispatch/watch 的 `DispatchParams` / `WatchParams`，让父代理 dispatch 主链路不再依赖裸 kwargs 扩展。
- `agent_py_agent/agent/agent_core/subagent_params.py`：承接 runner/spawn 相关轻量 bundle，包括 `SubagentRunParams`、`SpawnSubagentsParams` 和 runner flow 内部的 probe/failure/finalize 参数。
- `agent_py_agent/agent/agent_core/subagent_run_flow.py`：承接单个 subagent runner 从 prompt、probe、模型调用到结果写回的流程，避免 `subagent_mixin.py` 因 bundle 兼容继续变胖。
- `agent_py_agent/cli/_gateway_process_service.py`：legacy gateway process service 通过 `GatewayRunContext` / `GatewayRunOptions` 重建 worker 和 heartbeat，不再把 argparse 对象传入后台线程。
- `agent_py_agent/cli/_memory_gate.py`：实现 `subagents-memory-gate` CLI，显式列出候选、写回 approve/reject/needs_evidence、导出 approved 候选、生成 skill draft 和跑 verify。
- `SKILL_SPARKS.md`：子代理目录里的经验火花候选，只记录可复用步骤、触发条件、证据引用、限制和反例；后续提升为 skill 必须经过单独 gate。
- `agent_py_agent/agent/subagents/services/board.py`：把任务树节点转成 report board item，并汇总 child status、progress、summary、evidence/finding/blocker 计数。
- `agent_py_agent/agent/subagents/services/rescue_policy.py`：根据 due-check issue 给 action plan 添加 rescue/escalation 元数据，保持建议可审计但不自动越权执行。
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
9. due-check 把 blocked、timeout、stale heartbeat、capability request/gap 等问题转成 action plan，并附带 rescue/escalation 元数据。
10. persistence 同步 `tasks/<root_id>/state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/`、`artifacts/` 和 `agents/<run_id>/legacy_run_ref.json`，为后续正式 agent run workspace 做兼容桥。
11. persistence 同步 `tasks/<root_id>/agents/<run_id>/agent.yaml`、run `state.json`、run `timeline.jsonl`、`task.md`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl` 和 inbox/outbox/artifacts/compactions 目录，先形成 agent run workspace skeleton。
12. persistence 追加 `daily/YYYY-MM-DD/events.jsonl`，让主代理先按天查 task/run/event/artifact refs，再回到 task/run 文件核实。
13. persistence 写 `artifacts/manifest.jsonl`，让父级先看摘要、hash、path 和 exists，再按需读取 artifact 文件正文。
14. persistence 追加 `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并把 run `checkpoint.json` 指向最新 compact refs；当前不会删除 timeline、artifact 或旧 work-order 文件。
15. persistence 同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享结构化任务事实，但不写入主 memory。
16. persistence 同步 `memory_gate/candidates.jsonl`、`review_queue.jsonl` 和 `skill_spark_gate.json`，把 lesson/finding 作为候选排队；`subagents-memory-gate` 可把 reviewer decision 追加到 `decisions.jsonl`。
17. 只有显式 review/gate 通过且再触发 `--export-memory` 或 `--export-skill` 后，候选才允许进入长期 memory 或 skill draft 流程；approve 不会自动导出。
18. retention 只从 active review queue 移除 rejected / already exported 候选，候选、decision、export 和 verifier 文件仍留在 run workspace 里供接管和审计。
19. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源和 checkpoint artifacts，再由父级决定是否验收；这只是恢复入口推荐，不代表子代理写入主代理长期 memory。
20. workflow preview 仍可通过 CLI dry-run 展示；真实路径已接入 `create_run(... workflow_mode="plan|auto")` 和 `subagents-dispatch --apply --workflow-mode auto`，可把父任务上的 `workflow_plan` 物化为 worker 子工单。LOG 专项 apply path 和 runner 恢复 scenario 继续作为真实任务记录的先行验证样本。
21. CLI / core / manager 的新接口规则是先构造 Request/Options bundle，再进入业务服务；旧散参入口只做兼容 adapter，不作为新增字段的扩展位置。
22. subagent runner、spawn、recovery snapshot 和 board 的新字段优先加到 `subagent_params.py` 或 `SubAgentBoardOptions`，mixin 只做兼容 facade 和少量编排。

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
## 2026-05-06 structure update
- Workflow routing and subagent manager internals now separate decision fields, rendering sections, patch normalization, and service actions.
- Compatibility modules still re-export the existing public model and rendering names for callers.

## 2026-05-07 bundle structure update
- `manager_base.py`, `manager_dispatch.py`, `manager_actions.py`, `manager_indexing.py`, and patch facades now list old compatibility fields explicitly and immediately construct `CreateRunParams`, dispatch params, `ActionApplyOptions`, `LocalRecordParams`, or patch request bundles.
- `policy_checks.py` / `policies.py` use `RunnerNextActionParams`; `state_machine.py` uses `StateTransitionParams`; runner output payload construction passes those bundles instead of loose fields.
- `agent_core` dispatch/watch/run-subagent/recovery paths now normalize into `DispatchParams`, `WatchParams`, `DispatchLoopParams`, `SubagentRunParams`, and `RecoverySnapshotParams` before entering service logic.

## 2026-05-07 hard/soft structure update
- Patch manager parsing details stay outside the main mixin path, preserving `SubAgentPatchMixin` as a facade over patch review/apply services.
- Action application split unsupported-action record creation and handler context construction into helpers while keeping `ActionApplyOptions` as the service bundle.
- The subagent slice contributes no strict code-size hard or soft findings after this cleanup; near-soft warnings remain visible for future pre-feature refactors.
