## 2026-05-06 code-size guard cleanup
- Split `subagents/models.py` into focused capability, record, runtime, and task model modules while preserving the public compatibility import surface.
- Split runner result structured-output processing and output payload assembly out of `result_processors.py`.
- Split work-order path/file/validation helpers out of `manager_base.py` into `manager_work_orders.py`.
- Explicit UTF-8 debrief writes keep Windows locale defaults from corrupting runner notes.
# Subagent：开发推进记录

## 已完成
- 2026-05-06 code-size cleanup: split runner result payload/status helpers, capability route record/report helpers, evidence acceptance finding builders, and indexing record helpers out of oversized facade files. `check_code_size.py --mode warn` now reports `total=0 hard=0`.
- 2026-05-06 Task Tree Control Plane v1 第一片已落地：`SubAgentTask` 增加 `StatusReport`、progress、current step、latest summary、blockers、artifact/evidence refs、evidence packets 和 findings 字段；runner 写回会生成 `reports/status_report.json`，看板会展示子任务状态汇总、证据包数、finding 数和阻塞数。
- 2026-05-06 Evidence Packet / Finding 最小合同已接入：runner structured output 可写回 `evidence_packets` / `findings`，`output.json` 会保留这些结构化事实，acceptance 会阻断缺 evidence chain 的完成态结果。
- 2026-05-06 Acceptance 强化 + Verifier 第一片已落地：`AcceptanceReviewRecord` 分层保存 worker 自述、证据事实、父级结论和 verifier checks；确定性 verifier 会只读 evidence packets / findings，阻断 unresolved evidence risk 或 finding 缺 evidence chain 的通过。
- 2026-05-06 Rescue / Escalation 第一片已落地：due-check 转 action plan 时会附带 `rescue_trigger`、`rescue_strategy`、`escalation_target` 和 `rescue_context_refs`；action apply 记录会保留这些元数据，先做到可审计建议，不自动越权重试或接管。
- 2026-05-07 Compact / Checkpoint 协议第一片已落地：subagent 保存时会生成 `reports/checkpoint.json`、`decision_ledger.json`、`progress.md`、`failing_tests.json` 和 `next_actions.json`，`checkpoint_ref` 指向 compact 可读恢复入口；实现只写恢复事实，不保存完整聊天历史，也不改 memory compact 主链路。
- 2026-05-07 子代理经验候选第一片已落地：每个工单目录会生成 `SKILL_SPARKS.md`，只保存 task-local skill 学习候选，不自动写主代理长期记忆，也不自动升级正式 skill。
- 2026-05-07 Runtime Memory Phase 0 已接入 subagent persistence：保存工单时同步创建 `tasks/<root_id>/` task workspace skeleton，并在 `agents/<run_id>/legacy_run_ref.json` 里指回旧工单目录；旧 `task.json`、`run.json`、Markdown 工单文件继续保持兼容。
- 2026-05-07 Runtime Memory Phase 1 已接入 subagent persistence：`tasks/<root_id>/agents/<run_id>/` 现在有最小 agent run workspace，包括 `agent.yaml`、run state、run timeline、checkpoint、summary、final report、findings、inbox/outbox/artifacts/compactions，旧工单目录继续读写。
- 2026-05-07 Runtime Memory Phase 2 已接入 subagent persistence：保存工单时会追加 `daily/YYYY-MM-DD/events.jsonl`，只记录状态、摘要和 refs，方便主代理按天找 task/run 线索。
- 2026-05-07 Runtime Memory Phase 3 已接入 subagent persistence：保存工单时会写 task/run 两份 `artifacts/manifest.jsonl`，把 artifact refs 变成 summary/hash/path/size/exists 记录，不复制 artifact 正文。
- 2026-05-07 Runtime Memory Phase 4 已接入 subagent persistence：保存工单时会在 run `compactions/` 下追加 checkpoint snapshot ledger，写 summary/metadata，并把最新 compact refs 回写到 run `checkpoint.json`；当前是 `checkpoint_only` 恢复链，不删除旧上下文。
- 2026-05-07 Runtime Memory Phase 5 已接入 subagent persistence：保存工单时会同步 task-local `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，支持 sibling 子代理共享结构化事实但不写主 memory。
- 2026-05-07 Runtime Memory Phase 6 已接入 subagent persistence：保存工单时会同步 run-local `memory_gate/candidates.jsonl`、`review_queue.jsonl`、`skill_spark_gate.json`，把 lessons / findings 排成可复核候选；默认 `promotion_status=not_promoted`，不会自动写长期 memory 或正式 skill。
- 2026-05-07 Runtime Memory Phase 6 review decision 已接入：新增 `subagents-memory-gate` 命令，支持列出候选并把 approve/reject/needs_evidence 写入 `memory_gate/decisions.jsonl`；decision 会跨后续保存保留，但不会自动提升。
- 2026-05-07 Runtime Memory Phase 6 后半段已接入：`subagents-memory-gate` 支持 retention dry-run/apply、approved memory 显式导出、approved skill draft 显式导出和 boundary verify；导出均由父级显式触发，默认仍不会提升。
- 2026-05-07 bundle 接口规范已开始接入 subagent：acceptance review 核心实现改为 `AcceptanceReviewRequest`，patch review/apply、action apply、lifecycle status、memory gate export 和 CLI 边界都开始使用 Request/Options bundle；manager 旧签名继续作为兼容 wrapper。
- 2026-05-07 patch fallback bundle 收口已落地：`manager_patch` 的单任务 review/apply 兼容入口和无 service fallback helper 都可接收 `PatchReviewTaskRequest` / `ApplyPatchTaskParams`，旧散参只保留为兼容路径。
- 2026-05-07 bundle 接口第二波已落地：subagent dispatch CLI、inspection/action CLI、local/task CLI、legacy gateway process service、capability route、channel probe、due-check、planner/dispatch core 都改为先归一到 Request/Options bundle，再进入业务层；公开 manager 方法继续兼容旧散参调用。
- 2026-05-07 bundle 接口第三波已落地：`run_subagent`、runner worker/gate、recovery snapshot、spawn、board service/manager 和相关 CLI/slash/orchestration 入口改为 `SubagentRunParams`、`SpawnSubagentsParams`、`RecoverySnapshotParams`、`SubAgentBoardOptions`；旧调用形状继续可用。
- 2026-05-07 subagents high-risk params 收尾：`manager_actions`、`manager_indexing`、`manager_learning`、indexing/action/board/patch fallback services 改为同文件或相邻 service 的 Params/Request bundle；`manager_runner_results.py` 同步修复 runner work log 中 `dry_run` 未定义的半成品 bug。strict code-size 报告中 `agent_py_agent/agent/subagents/**` 已无 high-risk 项。
- 2026-05-07 Runtime Memory P0 安全切片已接入 subagent persistence：artifact manifest 现在按 workspace 边界解析 refs，越界路径只登记 blocked；shared workspace 的 findings/evidence 按 id 合并，messages 追加，避免 sibling 子代理互相覆盖。
- 2026-05-08 Agent Runtime Control Plane v1 第一片已落地：`SubAgentPersistenceService.save()` 会同步 `agent_runs`、`agent_events`、`task_rollups` 到 LocalStore，上级代理可按 root task 查询任务树、blocked runs 和 rollup；旧工单目录和 runtime workspace 仍是事实源，LocalStore 只做查询投影。
- 2026-05-08 Runtime Query Scope 第一片已落地：LocalStore 控制面新增 `AgentRuntimeQueryContext` / `AgentRuntimeQueryResult` 和 `query_agent_runtime()`，支持主代理或中间子代理按 root tree、own subtree、blocked runs、takeover candidates 查询；当前只做语义归一和扩展位预留，不做复杂授权。
- 2026-05-08 Inheritance Manifest 第一片已落地：有 parent 的子代理会生成 `InheritanceManifest` 并写入 `reports/inheritance_manifest.json`，记录 inherited / overridden / dropped 能力、工具、验收和 context pack；它只做审计和接管线索，不会自动展开父级上下文。
- 2026-05-08 Shared Progress Panel 第一片已落地：LocalStore 控制面新增 `SharedProgressPanel` 和 `query_shared_progress_panel()`，把 runtime query、rollup、blocked runs 和 inheritance manifest refs 合成上级/接管代理可读状态包；它只返回投影和 refs，不读取 artifact 正文。
- 2026-05-08 Failure Handoff 第一片已落地：失败、错误、超时、阻塞或带 `failure_type` 的子代理保存时会生成 `FailureHandoff` 并写入 `reports/failure_handoff.json`，记录风险等级、警告、最近 checkpoint、artifact/evidence refs、避坑建议和推荐下一步；LocalStore 控制面 metadata 暴露 `failure_handoff_ref`，但不自动 rescue。
- 2026-05-08 Security Signal 预留口已落地：`SubAgentTask` 增加 `SecuritySignal` 列表和 `security_review_required`，用于记录安全劫持、安全欺骗、prompt injection、工具权限异常等可疑信号；LocalStore 控制面 metadata 暴露 signal count / types / review flag，当前只审计不拦截。
- 2026-05-08 Shared Progress / Failure Handoff CLI 展示已落地：`status --json`、人类 `status` 和 `subagents` 看板会展示共享进度摘要、blocked 数和 failure handoff refs 数量；展示层只读 LocalStore 控制面投影，不加载 failure handoff 正文或 artifact 正文。
- 2026-05-08 Tool Output fail-safe checkpoint 已落地：大工具输出进入 externalizer 写 artifact 前，会先写 recovery snapshot，记录工具名、hash、大小、run/task/request id 和下一步建议；snapshot 不保存完整工具输出正文。
- 2026-05-08 ToolContextReducer live prompt 保护已落地：大工具输出外置后，下一轮 prompt 只注入 preview、artifact path、hash、size 和 fail-safe checkpoint；完整正文只留在 artifact 文件里。
- 2026-05-08 Takeover / Rescue readiness refs 已接入：rescue action plan 的 `rescue_context_refs` 和 takeover apply 的 `evidence_paths` 会优先暴露 `takeover_readiness.json`，再按 packet 的 `recommended_read_order` 展开 failure handoff、checkpoint、status report、artifact manifest、evidence/artifact refs；仍保持 refs-only，不自动读取大 artifact 正文。
- 2026-05-08 Rescue Packet / Rescue Action Plan 第一片已落地：`ActionPlanItem` / `ActionApplyRecord` 新增 `rescue_packet`，记录 dedupe key、合并后的 issue kinds / repeat count、保守重试上限、上抛目标、人工确认建议和恢复入口 refs；它是可审计计划，不自动 retry/takeover，也不读取 artifact 正文。

- 2026-05-05 CI 稳定性修复：修正 `manager_runner_results.py` 在结构化解析失败时仍把 `SubAgentRunnerResult.ok` 写成旧值的问题；同时补齐 `test_local_store_gateway.py` 的临时目录隔离和 heartbeat 读取路径，避免 gateway 测试互相污染。
- workflow 配置和开关已落地：`auto`、`manual`、`off`。
- 内置 workflow 模板、模板加载、覆盖和校验已有基础实现。
- `QualityContract`、`ContextManifest`、`context_packs` 已进入 subagent 任务结构。
- workflow router、compiler、parent acceptance planner 已有 dry-run 规划链路。
- `my-agent subagents-workflow-plan "<goal>"` 可预览 worker 拆分和父级验收清单。
- 通用 workflow apply path 已接入真实任务创建：`create_run(... workflow_mode="plan|auto")` 会保存 `workflow_plan`，`subagents-dispatch --apply --workflow-mode auto` 会通过 `realize_workflow_plan()` 物化 worker 子工单，并写入父任务的 `workflow_child_run_ids`。
- runner 并发已有保守线程池实现：默认/`auto` 仍是一轮 1 个 runner，显式数字 `runner_concurrency > 1` 时才并发执行同一轮候选 runner。
- learning draft 已接入 runner lessons：`enable_self_learning=true` 且 runner 结构化输出成功时，会把 `lessons` 去重沉淀到 `data/learning_drafts/*.json`，再由 `my-agent learn list/accept/reject/stats` 管理。
- LOG 模块已经验证了一条专项 apply path：把受控 work-order plan 落成真实 `SubAgentTask`，但不自动执行。
- 第一版代码/文档/注释同步门已落地：`scripts/check_doc_sync.py` 会检查 covered module 的代码改动是否同步更新模块文档和同文件注释。
- parent/subagent runner 跨天恢复场景已落地：`scenario-test --case parent-subagent-cross-day-resume` 会创建真实任务、执行 runner 工具回合、模拟跨天线索，并验证恢复回到任务事实源。
- LLM Failure Introspection 已实现：在规则分类器（SubAgentFailureAnalyzer）之后增加 LLM 自省层，分析失败原因并给出调参建议。
- **max_tool_rounds 调参生效修复**：之前 LLM 自省建议调整 max_tool_rounds 时参数存到 task.attributes 但 runtime_mixin 不读取，导致调参不生效。现已修复：subagent_mixin 在 runner 执行前设置 `self._current_task_attributes`，runtime_mixin 优先读取任务级别 max_tool_rounds，向后兼容全局配置。
- 记忆推模式已实现：`memory_push.py` 提供 `push_relevant_memories()` 函数，在关键决策点（TIMEOUT/FAILURE/PLANNING/GENERAL）自动注入相关记忆。
  - `MemoryType` 枚举支持 LESSON_GENERAL/LESSON_TASK/LESSON_TEMP/CONTEXT/FACT
  - dispatch_mixin 失败后自动注入教训记忆
  - failure_analyzer 增加 `relevant_memories` 字段
- 内部方法公开别名：`manager_acceptance_findings.py`、`manager_indexing.py`、`manager_patch.py` 为测试需要，将部分 `_` 前缀内部方法添加了公开别名（如 `acceptance_findings`、`select_runs`、`resolve_patch_target`）。
- due-check / action-plan 的建议命令已改用 `my-agent ...` 控制台入口，避免 Windows 上 `python3` 可能指向 Microsoft Store shim，同时保持 macOS/Linux 已安装后的主入口一致。

## 解决的问题

- GitHub Actions 里的 subagent/gateway 相关回归不再因为测试共享仓库目录、读取错误队列路径或 runner 结果字段不同步而误报失败。
- workflow 不再只是“多开 worker”的口头约定，而是能把质量标准、上下文包、写入边界和父级验收显式写出来。
- 用户可以少说任务怎么拆，系统先用模板和 dry-run 预览补足常见派工结构。
- worker 自述完成不会直接变成最终完成，父级验收门被放进计划和任务结构里。
- 文档四件套给后续模块讨论、推进、初心、结构说明提供固定位置，减少散乱文档继续膨胀。
- 同步门把“改功能就更新文档和注释”从口头约定变成可执行检查，降低 worker 并行开发时漏补文档的概率。
- runner 写回后的恢复不再只停留在单元 fixture：现在有可观察 scenario 证明父级恢复入口会推荐 `STATUS/WORK_LOG/HANDOFF/TEST_CHECKLIST/output.json` 等任务事实源。
- task workspace adapter 让 root task 有了任务级聚合目录，解决“子代理只有 run 目录、没有任务空间”的第一步问题；但当前仍不把子代理上下文写入主代理长期 memory。
- agent run workspace adapter 让每个子代理在 task 下有了可接管、可恢复、可 compact 的工作位，解决“只有 legacy 指针，没有 run 空间”的第二步缺口；当前仍保持旧工单为兼容读写面。
- daily ledger adapter 让父级可以按自然日找到 task/run/event/artifact 引用，解决“恢复入口只能靠 raw archive/LocalStore/旧目录扫描”的缺口；当前仍不把子代理完整上下文写入主 memory。
- artifact manifest adapter 让父级看到 artifact 的路径、大小和 hash，而不是只看到散乱字符串；当前不会读取或复制大输出正文。
- compact chain adapter 让 run workspace 的 `compactions/` 从空目录变成 append-only 恢复链，解决 checkpoint snapshot、summary、metadata 和 artifact manifest 之间缺少可追踪关系的问题。
- shared workspace adapter 让 sibling 子代理可以读同一 task 下的 evidence packets、findings、status messages 和 blackboard rollup，解决 shared/ 只有空壳的问题，同时继续隔离主代理长期 memory。
- workspace 安全切片让 artifact 和 shared 两个协作面更接近正式 runtime：artifact manifest 不会越界读取本机文件，shared facts 也不会被最后一个保存的 run 覆盖。
- memory gate adapter 让子代理 lesson/finding 有了提升前的 review queue，解决 `SKILL_SPARKS.md` 只有本地草稿、缺少 evidence/scope/gate 状态的问题；当前只记录候选和缺口，不执行提升。
- memory gate review 写回让父级可以留下可审计 reviewer 结论，解决候选长期停在 pending、没有决策日志的问题；approve 只是后续显式导出的前置条件。
- memory gate 显式收口链让 approve 后有了可控下一步：retention 只清 active queue，memory export 写主 JSONL 前必须看到 `approve_memory`，skill export 只生成 draft，不安装正式 skill。
- bundle 接口规范让父级验收、memory gate 和后续更多 subagent 服务有统一扩展面；未来新增 verifier、policy、retention 字段时优先加到 request bundle，而不是继续扩函数参数。
- patch fallback bundle 收口解决了“主 service 已 bundle 化，但 manager fallback 仍只能散传 output/patches/apply/reviewer/note”的缺口；这让 service 未初始化、测试替身或接管路径也遵循同一参数形状。
- dispatch/inspection/action/task/local/gateway 入口的 argparse/daemon context 已从业务调用中剥离出来，解决 CLI args 对象继续向内层渗透的问题；兼容 wrapper 保证老测试、脚本和外部调用不需要同步大改。
- runner 创建、运行、恢复快照和 board 展示 now use shared bundle modules/facades, so新增字段可以落在独立 params/options 模块，而不是继续让 `subagent_mixin.py` 和 board manager 膨胀。
- dry-run 下一步建议不再依赖平台解释器命名差异；用户复制 `my-agent subagent <run_id>` 等命令在 Windows/macOS/Linux 上语义一致。

## 下一步

- 继续补 Task Tree Control Plane v1 的外层可见性：把 status report 的最近状态索引到更完整的父级查询路径，并让 CLI/status report 对 root task 的 children、blocked、awaiting acceptance 展示更直接。
- 继续补 Evidence Packet / Finding 的验收深度：从 evidence packets 自动生成更细验收项，并把 verifier 从确定性规则扩展到独立 worker / critic 角色。
- 继续补 Rescue / Escalation 的执行层：把当前 action plan 元数据升级为 rescue packet 文件、重复失败去重、重试次数上限和父级/能力层上抛记录。
- 继续补 Compact / Checkpoint 协议：把 recovery artifact 接入 `memory-resume` 的优先事实源，并补定期 checkpoint / parent rollup / children rollup 的触发策略。
- 继续补 Agent Runtime Control Plane v1 的外层入口：把 Shared Progress Panel / Failure Handoff 接入更完整的接管命令和 status report，并让 takeover/rescue 命令能按 refs 读取 artifact 摘要和 failure handoff。
- 下一步继续补 Compact / Resume 链路：把 fail-safe checkpoint 接入 `memory-resume` 的优先事实源，让恢复流程先读 checkpoint/snapshot metadata，再显式读取 artifact。
- 继续补 Security Gate 研究入口：先核验 通道运行时/长期助手 等公开安全问题的来源、复现场景和风险模式，再把确认后的模式变成 detector fixture、security report 和权限收窄策略；当前 `SecuritySignal` 只做预留审计口。
- 继续把 Runtime Memory Phase 6 的 `verifier_report.json` 接入父级 acceptance / dispatch 摘要；正式 skill 安装仍要另走人工确认流程，不能由子代理自动完成。
- 把 workflow apply 的可解释性补齐：dispatch 记录要更清楚展示自动选择理由、模板 id、worker 子工单和父级验收门，尤其是 `auto/manual/off` 改变行为时。
- 补 Quality Profile 和更多内置 workflow 模板；当前内置模板仍是小集合，不等于设计里的首批完整模板库。
- 增强验收层：从 `output.json.tests` / `artifacts` 自动生成更细的验收任务，并在 report 中继续明确区分 worker 自述、证据事实和父级结论。
- 补 patch 集成验收、跨层能力上抛、外部 agent session / ACP adapter 等后续闭环。
- 补 workflow apply / worker 物化 / runner 并发 / learning draft 的回归样本，覆盖真实 dispatch 路径而不只覆盖 planner preview。
- 给 memory、gateway、live-lab 等模块补四件套后，把它们加入 `scripts/check_doc_sync.py` 的 `MODULE_RULES`。

## 已跑测试

- 历史记录显示 subagent workflow 专项测试已覆盖配置、模板、质量契约、router、compiler、planner、CLI preview 等路径。
- 相关旧记录见 [docs/design/subagent-quality-contract.md](../../design/subagent-quality-contract.md) 和 [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)。
- 本轮 subagent 模块本身只新增文档索引；代码变更发生在 LOG dispatch 的专项 apply path。
- 父会话 focused 组合验收：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `26 passed`。
- 父会话全量回归：`python -m pytest` -> `236 passed`。
- 空白检查：`git diff --check` -> passed。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `3 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- parent/subagent runner 跨天恢复 focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `2 passed`。
- CLI reference focused 验收：`python3 -m pytest agent_py_agent/tests/test_cli_reference.py -q` -> `1 passed`。
- 本轮 focused 组合验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `8 passed`。
- 本轮同步门验收：`python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python3 -m pytest -q` -> `251 passed`。
- 本轮 CI 回归修复验收：`python3 -m pytest -q agent_py_agent/tests/test_local_store_gateway.py::test_gateway_processing_recovery_requeues_then_fails_after_attempt_limit agent_py_agent/tests/test_local_store_gateway.py::test_gateway_worker_refreshes_processing_lease_heartbeat_during_long_run agent_py_agent/tests/test_log_analysis_query.py::test_local_store_upserts_and_queries_security_fields agent_py_agent/tests/test_log_evidence.py::TestEvidenceStoreIntegration::test_evidence_store_init agent_py_agent/tests/test_manager_runner_results.py::test_record_runner_result_handles_parse_failure` -> `5 passed`。
- 本轮 subagent control-plane focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_parsing.py agent_py_agent/tests/test_result_processors_edges.py agent_py_agent/tests/test_result_processors_payloads.py agent_py_agent/tests/test_manager_runner_results.py agent_py_agent/tests/test_subagent_persistence_service.py agent_py_agent/tests/test_manager_acceptance_findings.py agent_py_agent/tests/test_acceptance_helpers_class.py -q` -> `93 passed`。
- 本轮 acceptance/verifier focused 验收：`python3 -m pytest agent_py_agent/tests/test_agent/test_subagent_acceptance.py agent_py_agent/tests/test_subagent_reports_acceptance.py agent_py_agent/tests/test_subagent_rendering_reviews.py agent_py_agent/tests/test_manager_acceptance.py agent_py_agent/tests/test_manager_acceptance_findings.py -q` -> `45 passed`。
- 本轮 rescue/escalation focused 验收：`python3 -m pytest agent_py_agent/tests/test_agent/test_subagent_action_and_route.py agent_py_agent/tests/test_manager_actions.py agent_py_agent/tests/test_subagent_rendering.py agent_py_agent/tests/test_subagent_reports_acceptance.py agent_py_agent/tests/test_agent/test_subagent_lifecycle.py agent_py_agent/tests/test_manager_board_class.py -q` -> `59 passed`。
- 本轮 compact/checkpoint focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `2 passed`。
- 本轮 runtime memory Phase 0 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `3 passed`。
- 本轮 runtime memory Phase 1 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `4 passed`。
- 本轮 runtime memory Phase 2 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `5 passed`。
- 本轮 runtime memory Phase 3 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `6 passed`。
- 本轮 runtime memory Phase 4 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `7 passed`。
- 本轮 runtime memory Phase 5 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `8 passed`。
- 本轮 runtime memory Phase 6 focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `9 passed`。
- 本轮 runtime memory P0 安全 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_subagent_persistence_service.py::test_subagent_persistence_writes_artifact_manifests agent_py_agent/tests/test_memory_workspace_safety.py` -> passed。
- 本轮 Agent Runtime Control Plane focused 验收：`python -m pytest -q agent_py_agent\tests\test_local_store_control_plane.py` -> `2 passed`。
- 本轮 Runtime Query Scope focused 验收：`python -m pytest -q agent_py_agent\tests\test_local_store_control_plane.py` -> `4 passed`。
- 本轮 Inheritance Manifest focused 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_inheritance_manifest.py agent_py_agent\tests\test_subagent_persistence_service.py agent_py_agent\tests\test_local_store_control_plane.py` -> `15 passed`。
- 本轮 Shared Progress Panel focused 验收：`python -m pytest -q agent_py_agent\tests\test_local_store_shared_progress_panel.py` -> `1 passed`。
- 本轮 Failure Handoff focused 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_failure_handoff.py` -> `1 passed`。
- 本轮 control-plane 组合验收：`python -m pytest -q agent_py_agent\tests\test_subagent_failure_handoff.py agent_py_agent\tests\test_local_store_shared_progress_panel.py agent_py_agent\tests\test_local_store_control_plane.py agent_py_agent\tests\test_subagent_inheritance_manifest.py agent_py_agent\tests\test_subagent_persistence_service.py` -> `17 passed`。
- 本轮 Security Signal 预留口 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_security_reserve.py` 先因 `SecuritySignal` 不存在失败，补实现后 -> `1 passed`。
- 本轮 Shared Progress / Failure Handoff CLI 验收：`python -m pytest -q agent_py_agent\tests\test_status_shared_progress.py agent_py_agent\tests\test_status_commands.py::TestCmdStatus agent_py_agent\tests\test_subagent_commands.py::TestCmdSubagents` -> `8 passed`。
- 本轮 Tool Output fail-safe checkpoint 验收：`python -m pytest -q agent_py_agent\tests\test_tool_output_externalizer.py` -> `3 passed`。
- 本轮 ToolContextReducer live prompt 保护验收：`python -m pytest -q agent_py_agent\tests\test_tool_output_externalizer.py agent_py_agent\tests\test_tooling_base.py::TestToolExecutionResult` -> `6 passed`。
- 本轮 Takeover Readiness Packet TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_takeover_readiness.py` 先因 `takeover_readiness` service 不存在失败，补实现后 -> `2 passed`。
- 本轮 Shared Progress takeover refs 验收：`python -m pytest -q agent_py_agent\tests\test_local_store_shared_progress_panel.py agent_py_agent\tests\test_status_shared_progress.py` -> `4 passed`。
- 本轮 Takeover / Rescue readiness refs TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_takeover_readiness.py agent_py_agent\tests\test_manager_actions.py` 先因 action plan/apply record 未接入 `takeover_readiness.json` 失败，补实现后 -> `13 passed`。
- 本轮 Rescue Packet / Rescue Action Plan TDD 验收：`python -m pytest -q tests\test_subagent_takeover_readiness.py tests\test_agent\test_subagent_action_and_route.py::test_subagent_action_plan_dry_run tests\test_subagent_rendering.py::TestActionPlanRendering::test_render_action_plan_markdown_basic -p no:cacheprovider` 先因 `rescue_packet` 缺失失败，补实现后 -> `6 passed`；组合回归 `python -m pytest -q tests\test_subagent_takeover_readiness.py tests\test_manager_actions.py tests\test_agent\test_subagent_action_and_route.py tests\test_subagent_rendering.py -p no:cacheprovider` -> `34 passed`。

## 未跑测试

- 当前文档同步轮没有重新跑真实 API 冒烟。
- workflow apply 已有实现，但仍需要继续补更贴近真实 dispatch 的端到端回归，尤其是 worker 子工单依赖、验收阻断和失败回放。
- 同步门目前只覆盖 `log-analysis` 和 `subagent` 两个模块；其它模块还需要先补四件套和规则映射。
- parent/subagent 跨天恢复已有确定性 backend 场景和真实 API 多轮恢复记录；后续交付级变更仍应按风险补跑真实 API 冒烟。

## 风险

- 旧文档里已有大量 subagent 设计细节，第一版索引还没有逐段拆入四件套。
- workflow 已经能从规划进入真实 worker 子工单创建，但产品风险还在：什么时候需要用户确认、怎么展示自动选择理由、如何避免高风险任务被过度自动化，还需要继续验证。
- 并行 worker 可能同时补文档，后续需要以模块四件套为主入口，避免再次分散。
## 2026-05-06 code-size cleanup
- Split subagent workflow routing, manager helpers, patch review, runner rendering, and service utilities into smaller focused helpers.
- Kept public report dataclasses, patch rendering compatibility, and re-export behavior intact.
- Verified with subagent-focused pytest, ruff, and the global code-size report.

## 2026-05-07 bundle interface completion
- Converted subagent manager/core action, dispatch, indexing, create-run, patch review/apply, runner next-action, state transition, run-subagent, recovery snapshot, and dispatch-loop compatibility paths to bundle-first or explicit keyword -> bundle adapters.
- Added an architecture guardrail that blocks new product-code function var-keyword service interfaces; only transparent retry decorator forwarding remains exempt.
- Focused verification covered policy checks, manager actions/dispatch/indexing, bundle interfaces, dispatch loop/watchdog, subagent runner, acceptance, patch review, and mixin recovery snapshot paths.

## 2026-05-07 hard/soft code-size cleanup
- Cleared current subagent patch/action soft findings without changing public manager facades.
- `SubAgentPatchMixin` remains a thin adapter over patch services; action application now keeps helper context bundled so follow-up fields do not lengthen service signatures.
- Split acceptance Markdown item rendering into shared helpers, keeping verifier checks, findings, and record summary output identical while reducing function-level near-soft risk.
- Continued high-risk cleanup by moving patch review record helpers, action record/log helpers, acceptance evidence helpers, and orchestration specs behind smaller focused modules while keeping manager/service facades compatible.
- Focused verification covered manager patch, manager actions, strict code-size, ruff, and architecture guardrails.
- 2026-05-07 workflow high-risk cleanup continued in router, planner, and parent acceptance helpers; route fields, compile inputs, and final-gate checklist expansion stay separated so worker self-report cannot become the acceptance source.
## 2026-05-07 LLM annotation coverage update
- Product-code modules, classes, functions, and methods in the active module now carry the required `LLM:` plus `函数用途:` / `类用途:` definition-level double-layer comments format.
- This is a documentation-only maintainability pass: behavior, file formats, workflow semantics, and public interfaces are intended to stay unchanged.
- Future module changes must keep these comments current when changing module/class/def behavior, side effects, bundles, or caller expectations.
