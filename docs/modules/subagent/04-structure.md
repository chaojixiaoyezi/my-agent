## 2026-05-06 structure update
- 中文说明：subagent 数据模型拆成多个职责文件，旧 `models.py` 继续做兼容门面；任务树、证据、finding、checkpoint、runtime memory workspace 和 skill sparks 都落到结构化字段/文件里，方便恢复和验收。
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
- Subagent coordination messages now have two explicit lanes: `subagent_message mode=direct` writes targeted inbox/outbox messages for a few specific descendants or same-parent peers, while `mode=broadcast scope=descendants` appends a scoped shared-board notice for many lower agents. Broadcast rows carry `recipient_scope` and `scope_root_run_id`; a child cannot broadcast commands into sibling subtrees, and peer discussion stays direct-only for future team workflows.
- Each persisted task now writes a Phase 6 run-local memory gate under `memory_gate/`, with candidate and review queue files that never auto-promote into main memory or formal skills.
- `subagents-memory-gate` can now write explicit review decisions to `memory_gate/decisions.jsonl`; these decisions are preserved across later saves and still do not auto-promote.
- `subagents-memory-gate` can now explicitly run retention, export approved memory candidates, export approved skill drafts, and verify the no-auto-promotion boundary; none of these paths installs a formal skill automatically.
- Each persisted task now also updates the LocalStore agent runtime control-plane projection: `agent_runs`, `agent_events`, and `task_rollups`. This projection is for upper-agent, takeover, and shared progress views only; task/run workspace files remain the source of truth.
- LocalStore control-plane queries now accept a runtime query context, so a main agent, middle subagent, or future takeover agent can ask for root tree, own subtree, blocked runs, or takeover candidates through the same bundle-shaped API. `blocked_runs` stays `BLOCKED`-only; `takeover_candidates` includes `BLOCKED`, `FAILED`, `ERROR`, and `TIMEOUT` so a timed-out grandchild is visible to recovery views.
- Subagent persistence preserves append-only hierarchy links during stale snapshot saves: when an older parent or child task object is saved after descendants were created, existing on-disk `child_ids` are merged into the write. This keeps multi-level trees from losing child/grandchild edges during parallel orchestration.
- Child tasks with a parent now carry an audit-only inheritance manifest, written to `reports/inheritance_manifest.json`, showing inherited / overridden / dropped capability and context fields without expanding parent context into the child.
- The first shared progress panel read model now composes runtime query results, task rollup, blocked visible runs, and inheritance manifest refs for upper-agent or takeover-agent views; it still points back to task/run workspace facts instead of loading artifact bodies.
- Failed or blocked tasks now carry a first failure handoff record, written to `reports/failure_handoff.json`, with warnings, last safe checkpoint refs, artifact/evidence refs, and next-run avoidance advice; it is recovery guidance, not an automatic rescue trigger.
- Tasks now also have audit-only security reserve fields: `SecuritySignal` entries and `security_review_required`. These fields are for future security hijack/deception defenses and do not enforce policy by themselves.
- CLI status surfaces now expose shared progress, failure handoff refs, takeover refs, parent acceptance dry-run summaries, and parent acceptance next-action summaries in `status --json`, human `status`, and the `subagents` board. These views show counts, decisions, actions, command summaries, `mutates_task_state`, and refs only; they do not execute commands or load failure handoff, test report, or artifact bodies.
- Status and startup recovery now use lightweight board options: they do not expand child status counts, read runner prompt/response logs, read cold artifact bodies, execute tests, or call models. The full board still preserves child status counts, but derives them from already loaded task records instead of reloading children one by one.
- Large runtime tool outputs now write a fail-safe recovery snapshot before externalization. The snapshot stores metadata such as tool, hash, size, run/task/request ids, and next action, while the full output body still belongs only to the externalized artifact.
- ToolContextReducer now protects the next live prompt: externalized large outputs are injected as refs and metadata only, while full bodies remain in artifacts.
- Takeover/rescue command paths now consume takeover readiness refs first: action plans and takeover apply records surface `takeover_readiness.json` before its recommended read order, without loading artifact bodies.
- Rescue packet metadata now travels with action plan/apply records: dedupe, repeat count, retry limit, escalation target, manual confirmation, and recovery entrypoints are visible as refs-only audit data.
- Parent Acceptance Auto Policy v1 dry-run 已有第一片实现：当前只生成策略审计，不执行 tests、不 apply acceptance、不自动触发 rescue。
- Typed Action Protocol 第一片已接入：`agent/action_protocol.py` 是兼容 facade，具体类型拆在 `action_protocol_core.py` / `action_protocol_tooling.py` / `action_protocol_subagents.py` / `action_protocol_compact.py`；旧文本协议只作为兼容输入，后续执行/恢复/验收优先读 envelope 字段。
- `tooling/registry_execution.py` 会把旧 `[TOOL_CALL]` 解析成 `ToolCallEnvelope` 后执行，并把 `call_id/result_envelope` 挂回 `ToolExecutionResult`；envelope 去重/结果关联在 `registry_envelopes.py`，最终工具执行在 `registry_invoke.py`，旧标记扫描在 `registry_markers.py`，避免一个 registry 文件重新变厚。
- `subagents/parsing.py` 保留旧 parser 和旧导入入口；`subagents/parsing_envelope.py` 负责把旧 `[SUBAGENT_RESULT]` 转为 `SubagentResultEnvelope`。真实工具列表由执行层传入，模型 `used_tools` 和 summary 不作为权威工具事实。
- `create_subagents` 与 runner 内 `schedule_child_subagents` 会在 JSON 响应里附带 `typed_envelope.kind=subagent_schedule`，多层派工通过 parent/root/created refs 串联；这只是 refs-first 调度事实，不代表 child 已完成。
- `create_subagents`、`schedule_child_subagents` 和 `dispatch_subagents` 都会返回 `current_turn_run_state`：它只读取当前轮明确记住的 run ids，给出可 dispatch、running、blocked、verified、unfinished 和 missing buckets，以及下一步建议工具调用。调度类输出被外置时，live prompt 摘要仍保留这张状态表。
- `dispatch_subagents` 现在也会附带 `typed_envelope.kind=subagent_dispatch`，把 `actionable_run_ids`、`recovery_run_ids`、dispatch 报告 refs 和状态摘要作为稳定控制字段；父级推进和恢复优先读这些字段，不从自然语言 `message` 里猜 run id。
- `dispatch_subagents` 的 `result_refs_by_run` 是父级最终汇总的 refs-first 交接入口：每个直接 child 一行，包含 run id、状态、主产物路径、`output.json` 路径和从 `output.json.artifacts[]` 提取的 `primary_artifact_summaries`。这吸收 Hermes “parent 拿 summary/refs、不吞 child 原文”和 OpenClaw “控制面先看 session/run 状态”的做法；父级要先读这些机器字段，只有需要核对细节时才按需读取 artifact 正文。
- `dispatch_subagents` / `subagent_board` 现在把 `completion_status`、`must_not_report_done`、`blocking_run_ids`、`deliverable_artifact_refs` 和 `deliverable_evidence_refs` 放到顶层；大输出被外置后，`tool_context_orchestration_summary.py` 仍保留这些机器字段，root 必须先处理阻塞 run，再基于 artifact/evidence refs 汇总，不能只因为某个报告文件存在就向用户报完成。若本轮还有阻塞，dispatch 不再把未验收产物放进 `deliverable_*`，只放进 `pending_artifact_refs` / `pending_evidence_refs`，避免 root 被“看起来已经有文件”诱导提前收尾。direct-write guard 也会检查当前轮 remembered run ids，只要还有 run 未 `DONE/VERIFIED` 且没有被 verified coverage record 覆盖，root 不能直接写 `final_report.md` 这类最终交付报告。
- runner artifact refs 现在会在 `result_structured.py` / `result_structured_evidence.py` / `result_artifact_evidence.py` / `result_artifact_integrity.py` 中归一化和校验：模型写短路径时，系统会优先在当前 task/output/report/allowed_write_roots 内解析成真实路径；父级 coordinator 猜错 child 产物目录时，会有界读取直接 child 的 `task.json`，按 child `artifact_refs` 和文件名把 ref 规范到真实产物路径。证据包里声明的本地产物若仍找不到，runner 会直接转 `BLOCKED / missing_artifact_refs`，不让父级把不存在的文件当作真实交付。这里仍然只检查路径和小型 task 元数据，不读取正文、不扫描整机。
- `create_subagents.allowed_tools` 现在是工具偏好提示，不是能力删除列表；模型少填 `write_file/append_file/replace_in_file` 时，系统会补齐基础读写工具包，防止 worker/researcher/coordinator 因提示词少写一个工具名而无法产出报告。
- Provider transient error 第一片接入 backend/gateway：EOF、remote disconnected、connection reset、broken pipe、proxy tunnel 503、bad gateway、gateway timeout 这类“模型调用前连接抖动”会有限重试；耗尽后标记 `transient_error`，子代理状态保留可恢复失败类型。模型超时仍走 timeout 语义，不被当成普通瞬断重试。
- `acceptance_helpers/evidence_acceptance_findings.py` 与 service 版 evidence findings 已去掉 summary 关键词推断；read/write_file 验收只看系统工具事实和证据字段，避免自然语言自证。
- Context Bundle v1 已接入执行上下文生成：`SubAgentTask` 会被压成实时工单包，写入旧 run 工单目录和 agent run workspace，runner prompt 只展示 gate 状态和 refs，不展开大型 artifact 正文。
- Context Bundle v1 的 `workspace_refs` 现在包含 agent run workspace 的 task/checkpoint/summary/final_report/findings/timeline/compactions 和 shared refs；这些 refs 是父级状态、接管和 compact 接续的共同事实入口。
- Context Gate v1 当前检查最小工单字段是否齐全；缺字段时要求 runner 返回 `BLOCKED` 和缺字段列表，后续可升级为调度前硬阻断。
- Context Bundle v1 现在带 `lineage`：记录 root、parent、depth、自己的 bundle ref 和直接父级 bundle ref；多层恢复时只沿 refs 读交接包，不把父级全文塞给子孙节点。
- `takeover_readiness.json`、`rescue_context_refs` 和 hierarchy recovery-tree 节点会暴露 context bundle refs；失败、阻塞、超时或父节点超时后，接管者可以先读当前/父级交接包，再读 checkpoint、status report、artifact manifest，仍不展开 artifact 正文、不自动执行接管。
# Subagent：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- subagent.py                         # 兼容入口
|-- subagents/                          # subagent 任务、manager、报告、runner、解析和渲染
|   |-- kernel.py                       # 子代理内核只读快照：run/session/tree/status/refs
|   |-- protocol.py                     # TaskAddress / TaskEnvelope / protocol validation
|   |-- protocol_preflight.py           # 工具和写入合同预检，不修改任务状态
|   |-- context_bundle.py               # 实时子代理工单包和 Context Gate
|   |-- context_bundle_semantic.py      # Context Gate 的文件合同语义校验
|   |-- root_task_policy.py             # self-authorized root 与主代理直接 child 的能力边界
|   |-- role_template_resolution.py      # 自然 role 名到模板 id 的运行时解析
|   |-- role_templates.py               # role template 加载、校验和查询
|   |-- role_template_catalog/builtin/  # 内置广义角色模板 JSON
|   `-- workflow_template_catalog/      # 后续 workflow 模板外置化预留目录
|-- subagent_workflows/                 # workflow 模型、模板加载、路由、编译和验收规划
|   |-- builtin/                        # 内置 workflow 模板
|   |-- models.py                       # workflow / 任务 / 质量契约相关模型
|   |-- store.py                        # 模板加载和覆盖
|   |-- router.py                       # 根据目标选择 workflow
|   |-- compiler.py                     # 把 workflow 编译成 worker 派工规格
|   |-- planner.py                      # dry-run 规划门面
|   `-- acceptance.py                   # 父级验收计划
`-- agent_core/                         # 主循环、dispatch、runner prompt、compact continuation 等接入点
```

## 核心文件

- `agent_py_agent/agent/subagent_workflows/router.py`：回答”这个任务适合哪种 workflow”。
- `agent_py_agent/agent/subagent_workflows/compiler.py`：把抽象模板变成具体 worker 任务说明。
- `agent_py_agent/agent/subagent_workflows/acceptance.py`：生成父会话要检查什么。
- `agent_py_agent/agent/subagents/`：保存真实 subagent 管理、运行、报告和验收相关代码。
- `agent_py_agent/agent/subagents/kernel.py`：提供 `SubagentKernelQuery`、`SubagentKernelSnapshot` 和 `SubAgentManager.kernel_snapshot()`。它只读现有 task/run 事实，统一返回 root tree、own subtree、状态桶、workspace refs、recovery refs 和 takeover candidates；不调度、不恢复、不执行测试、不读取 artifact 正文。
- `agent_py_agent/agent/subagents/protocol.py`：定义第一版父子代理交接协议。`TaskAddress` 固定 run/root/parent/depth/lineage/workspace；`TaskEnvelope` 固定目标、角色、工具合同、写入合同、验收合同和上下文 refs。
- `agent_py_agent/agent/subagents/protocol_preflight.py`：根据 `TaskEnvelope` 做开工前预检，返回 `ToolContractError` 等结构化 issue；只报告缺工具、缺产物写入根、缺 scoped exec 授权，不自动加限制或剥夺基础工具。
- `agent_py_agent/agent/subagents/models.py`：定义 `SubAgentTask`、`TaskStatus`、`DISPATCH_INELIGIBLE_STATUSES` 等核心数据结构。
- `agent_py_agent/agent/subagents/model_task.py`：承接 `SubAgentTask`、`EvidencePacket`、`Finding`、`StatusReport`、`SecuritySignal` 等任务树、证据和安全预留合同模型，`models.py` 继续作为兼容导出入口。
- `agent_py_agent/agent/subagents/model_task.py`：同时定义 `RuntimeIdentity`，记录 service owner、requester、effective principal、conversation、memory namespace 和 config overlay scope；这些字段是隔离和审计口子，不是授权、长期记忆或全局配置事实源。
- `agent_py_agent/agent/subagents/services/hierarchy_scheduler.py`：定义 `HierarchyScheduleRequest` / `HierarchyChildSpec`，通过显式 bundle dry-run 或创建 child/grandchild run；默认不写任务，`apply=True` 时复用 `create_run`。`max_depth` / `max_children` 只在模型或高级调用方显式传入时生效，默认 `0` 表示不限制，避免普通任务因为流程参数太小被误伤。层级 child 会继承 bounded parent goal/thought；如果模型给下一层的 goal 太短，或只写了 build 路径但丢了父级 required/forbidden 文件清单、4层/depth 命名合同、能力安全合同，scheduler 会把 `继承父级目标/边界` 追加进 child goal，并通过独立策略模块推断 leaf 写文件工具和最小验收兜底。层级命名合同使用精确前缀判断，例如父级写 `小小小傻妞-*` 时，子级只写 `depth=3` 或写错成相似中文不会被视为已继承，系统会补回原始合同。模型把带 orchestration tools 的下一层误标成 `worker` 时按 depth 推断 coordinator role；模型把真实角色写在 `agent_name` 但 `role=child` 时，会先从 identity 中恢复 researcher/tester/acceptor/bug_finder/writer/worker 等角色。父任务明确点名 tester/bug_finder/acceptor 时，scheduler 返回 `quality_advice`，由 LLM 按 refs 选择 QA scope、数量和顺序；系统只守红线，不固定补派完整 QA 流程。`apply=True` 创建前还会走调度幂等合同：同 parent/root/role/goal/write-root 的 direct child 已存在时返回 `reused_run_ids`，并只把 `PLANNING/PENDING` child 放进 `dispatch_run_ids`，避免 runner 重复创建或重复调度已完成后代。层级深度不是固定 4 层：root/parent 可以按任务复杂度直接派 worker，也可以派 coordinator/lead；只有具体 prompt 要求 4 层时才按该场景约束执行。
- `agent_py_agent/agent/subagents/services/hierarchy_child_context.py`：层级 child context 选择 helper；child spec 携带 repair/context refs 时优先写入 child task，否则沿用父级 context，避免 scheduler 主文件继续膨胀。
- `agent_py_agent/agent/subagents/services/hierarchy_schedule_idempotency.py`：runner 内层级调度幂等合同层；只扫描当前 parent 的 direct children，按 `parent/root/role/goal/extra_write_roots/status` 判断能否复用，避免跨分支误合并，也避免模型重复调用 `schedule_child_subagents` 时无限扩容。
- `agent_py_agent/agent/subagents/services/repair_contract_identity.py`：repair owner 身份键 helper；从 `context_packs[*].contract` 中提取 `subagent_repair_contract.v1` 的 `kind/failed_run_ids/target_artifact_refs/required_read_paths`，供顶层 create 和 runner schedule 幂等层共用。这样固定名字的 `小傻妞-验收修复` 不会把不同失败对象误合并，同一失败对象也不会被拆成多个“只修/只执行/只验证”的漂移子任务。
- `agent_py_agent/agent/subagents/manager_lifecycle.py`：负责 runner attempt 的开始、放弃和基础状态迁移。`prepare_runner_attempt()` 在切到 `RUNNING` 时同时写 `runner_active_attempt_id`、`runner_last_attempt_at`、`heartbeat_at` 和 `updated_at`；这样 board、recovery-tree 和真实 E2E 日志能区分“已经真实启动但还没返回结果”的 runner 与“从未启动/历史残留”的 run。最终 attempt 计数仍由 runner result 写回阶段统一递增，避免开始和结束重复计数。
- `agent_py_agent/agent/subagents/services/hierarchy_agent_names.py`：统一生成 `小傻妞-*` / `小小傻妞-*` / `小小小傻妞-*` 等层级展示名。生成下一层名字时会同时看 `parent.depth + 1` 和父节点当前可见中文前缀；如果 root 已经显示为 `小傻妞-*`，它创建的 child 会前进到 `小小傻妞-*`，避免真实 E2E 中重复同一层展示名。系统生成或半截名字会按 `小...傻妞-角色-编号` 补齐，例如 `小小傻妞-tester-2`；模型明确给出的语义后缀仍保留，避免把 `小小傻妞-product-worker` 误修成普通 worker。
- `agent_py_agent/agent/subagents/services/base.py`：`_extract_write_dirs()` 是从自然语言目标里提取本地写入根的统一入口；它会跳过 URL span，避免 `https://picsum.photos/...`、API 地址或图片地址被误当成本地授权目录。
- `agent_py_agent/agent/subagents/services/output_ref_rebinding.py`：run 创建后的自写产物路径绑定层。模型有时会把旧的 `data/subagents/subagent-.../file.md` 写进新 child goal；该模块只重写“当前任务要输出/生成/保存”的路径，把旧 run id 换成真实新 run id，并把审计记录写入 `task.attributes.output_ref_rebindings`。`读取/基于/参考` 这类输入路径不重写，避免破坏合法的上游依赖。
- `agent_py_agent/agent/subagents/services/hierarchy_role_identity.py`：从 scheduler 拆出的角色恢复 helper；当模型把结构化 `role` 写成 `child` / `general` 但 `agent_name` 或 goal 已经写出真实角色时，先归一化成内置角色再进入写入根和工具策略。
- `agent_py_agent/agent/subagents/services/hierarchy_scheduler_models.py`：层级调度请求、结果和内部构建 bundle 的模型文件；新增 `quality_advice`、`scheduling_warnings` 等字段时先放这里，避免 `hierarchy_scheduler.py` 主流程继续膨胀。
- `agent_py_agent/agent/subagents/services/hierarchy_scheduled_role.py`：下一层真实 role 推断 helper；把 child/general/worker 这类模型模糊角色修正成 child_coordinator、grandchild_coordinator 或 leaf_worker。
- `agent_py_agent/agent/subagents/services/qa_role_contract.py`：tester / bug_finder / acceptor 角色合同识别 helper；调度器和验收发现共用它来判断父任务是否要求真实 QA 后代，以及某个 child 的 persisted role/agent_name 是否真正覆盖了该角色。它只读取当前节点自己的 direct goal / acceptance 文本；遇到 `继承父级目标/边界`、`父级层级/协作约束` 等继承块会停止，避免每个中间 coordinator 都被父级 QA 总目标硬性绑定。tester / bug_finder / acceptor 自己是终端 reviewer，不会因为自己的 goal 写了角色名就被要求再创建同名 QA 子代理。
- `agent_py_agent/agent/subagents/services/hierarchy_qa_scheduler.py`：调度阶段的 QA advice helper；根据 `qa_role_contract.py`、已有 child 和实现进度计算缺失 QA 角色，返回 `quality_advice` 给 LLM 选择 scope、角色数量和顺序；系统不直接替 LLM 固定创建 QA。
- `agent_py_agent/agent/subagents/services/hierarchy_write_policy.py`：集中维护层级写入根策略，把“拥有产物根覆盖权限”和“写入路径边界”拆开；同时从父级 goal 中提取可委派产品路径，保证 coordinator/tester/reviewer/worker 都能在授权目录内读写必要产物、报告和修复文件。角色模板只影响职责重点，不再默认剥夺读写能力；真正的硬边界仍是 workspace/write roots、系统自毁红线和显式 `product_write_policy=delegate` 这类任务级严格覆盖。
- `agent_py_agent/agent/subagents/services/hierarchy_acceptance.py`：从 scheduler 拆出的 acceptance fallback，模型漏传 `acceptance_checks` 时只派生最小稳定验收项。
- `agent_py_agent/agent/subagents/services/hierarchy_tool_policy.py`：从 scheduler 拆出的工具策略，集中处理 coordinator/leaf 工具继承、写文件工具补齐和工具名别名修正；coordinator 即使显式传了 allowed_tools，也会合并内置 coordinator 工具包，避免漏掉继续派下一层所需的编排工具。leaf 写产物的默认工具包包含 `read_artifact`，这样真实运行里遇到外置工具输出时，叶子代理可以按 scoped artifact ref 分片读取，而不会被卡在只能 `read_file` 包装 JSON 的死路上。文件工具真实执行时还会由 registry 临时扩展父级授权的 `write_boundary.allowed_write_roots/product_write_roots/task_dir`，因此用户产物目录即使不在 my-agent 代码工作区内，也能被当前子代理读、列、搜、写；扩展只作用于当前工具调用，执行后恢复。
- `agent_py_agent/agent/subagents/services/hierarchy_scope_guards.py`：承接层级调度的空计划、显式深度/数量、禁止 sibling 领域、child 写入根漂移和 domain mismatch 检查；scheduler 只消费红线阻断原因，避免调度主文件继续膨胀。`forbidden_child_scope:<domain>` 只用于 `不得创建 arithmetic/text` 这类真实业务领域串线，`depth/layer/level/child/worker` 等层级或角色词会被过滤，避免“不要创建 depth>=4”误伤正常派工。`mixed_coordinator_leaf_children` 不再硬阻断，只返回 `scheduling_warnings`；父级 LLM 可以在同一次计划里混合派 coordinator、worker、tester 或 acceptor，只要任务说明和 ownership 讲清楚。4 层链路不再靠调度阶段硬性要求每层都是 coordinator；root/parent 可以按任务复杂度直接派 coordinator、worker 或 writer，最终是否满足“至少一条 4 层链路”交给验收和真实 `task.json` 事实判断。领域去重会过滤 `run/id/ref/refs/qa` 等结构词，避免自动 QA child 因共享 run refs 被误判同域重复。`duplicate_child_domain:<domain>` 和 `duplicate_leaf_target:<file>` 都不再作为调度硬阻断；重复 checkout/quality coordinator、多个 QA 角色看同一个 build 目录、修复 worker 重写同一文件时，scheduler 返回 `scheduling_warnings`，提醒父级用看板、消息或任务说明协调 ownership。真正红线仍是写入根漂移、真实领域越界、显式深度/数量上限和空产物 QA；同域 repair、共享资产补丁或后续协作由父级 LLM/模板/验收事实决定。`child_write_root_drift` 仍用于阻断模型把父级权威产物根 invent 成 sibling 目录后继续落盘。
- `agent_py_agent/agent/subagents/services/hierarchy_scope_guards.py` 还包含 QA 阶段门：有产物根的父任务，如果直接请求 tester/bug_finder/acceptor，但整个子树里还没有任何 worker/writer/leaf_worker 进入 `AWAITING_ACCEPTANCE`、`NEEDS_ACCEPTANCE` 或 `DONE/VERIFIED`，会返回 `qa_before_implementation_ready`。这保证 QA 不在空 build 目录上空转，也允许 root 通过 coordinator 链路完成 leaf 后再创建 QA。
- `agent_py_agent/agent/agent_core/hierarchy_tools.py` 会把 `quality_advice` 放进 `schedule_child_subagents` 的 JSON 返回；当模型还没传 `children` 时，工具也可以返回 `no_child_specs + quality_advice`，让 LLM 先看建议再决定下一次如何派工。`hierarchy_qa_scheduler.py` 的 QA advice 同样读取后代实现状态，不要求 ready implementation 必须是直接 child，避免 4 层 delegation 完成后 root 仍误以为要先实现。
- `agent_py_agent/agent/agent_core/subagent_finalize_helpers.py` 会在 runner 收尾时检查 coordinator 是否真的创建了 child。若模型没调用工具、没有 child refs，却把“下一步要 schedule_child_subagents”写成可验收完成，系统会改成 `BLOCKED / needs_child_creation`，让 LLM 继续派工而不是进入验收。
- `agent_py_agent/agent/subagents/services/hierarchy_leaf_targets.py`：已验证 leaf 目标文件审计 helper；从 direct child 元数据、`output.json` 结构化 refs 和 `[SUBAGENT_RESULT]` 里的 artifact/files_modified 引用提取文件名 token，不读取产物正文。`task_actual_target_tokens()` 会优先结构化 refs，再回退自然语言 goal；英文 `use/include/import/load/link to` 必须按词边界识别，避免 `/Users/...` 本地路径被误切。它只产生 `duplicate_leaf_target:<file>` warning，不替 coordinator 做项目管理式硬拦截。
- `agent_py_agent/agent/agent_core/orchestration_tools.py`：保留模型可调用 orchestration 工具的兼容入口和 create/board 薄封装；`DispatchSubagentsTool` 已拆到 `orchestration_dispatch_tool.py`，workflow mode 归一化拆到 `orchestration_workflow_mode.py`，看板 payload 整形拆到 `orchestration_board_payload.py`，避免单个工具入口继续膨胀。顶层 `create_subagents` 同时支持旧的 `goal + count` 和 Hermes 风格 `items/tasks` 批量入口；`items/tasks` 每项独立 goal/context，避免 `count` 复制同一个任务目标。批量入口会先拒绝 `items` 与 `tasks` 同时出现、`items/tasks` 又混用 `count>1`、以及 root 直接创建 `grandchild_*` / `小小傻妞-*`；这类下一层必须由当前小傻妞在 runner 内调用 `schedule_child_subagents` 创建，避免任务树事实和自然语言层级描述分叉。系统默认名会在创建前补成 `小傻妞-角色-编号`，例如 `小傻妞-worker-1` / `小傻妞-tester-2`，让看板、幂等和调度状态能稳定对齐。创建响应会返回 `next_action.dispatch_subagents` 和真实 `run_ids`，明确“创建任务记录”和“真实执行 runner”是两步。它继续暴露 `schedule_child_subagents`，只在当前 subagent runner 上下文中创建下一层 child；没有 active runner id 会拒绝。显式 root/coordinator seed 会忽略模型额外塞入的 shell/web 工具，只保留内置 coordinator 工具包，避免 root 因自然语言漂移拿到不该有的权限。`coordinator_seed_tools.py` 承接 root/coordinator seed 工具过滤，避免核心工具入口继续变胖。`orchestration_create_policy.py` 的 role 纠偏只看 goal/name/thought/plan 等任务语义，不再把 `allowed_tools` 当派工意图；工具授权只是能力事实，不能把普通 `items[]` worker 误升成 coordinator。`orchestration_dispatch_scope.py` 集中维护 runner 内部 `dispatch_subagents` 的默认 parent scope、self-exclude、workflow-off 和 defer-acceptance 规则，避免外层绕过主节点或递归跑自己；顶层正在推进 active root/coordinator 且 `apply=true` 时也会强制 workflow off，防止全局 `subagent_workflow_mode=auto` 先套 producer/critic/repair 并绕过 root 自己派工；runner-context 在 `apply=True` 且 `execute_acceptance_tests=True` 时会打开 `auto_apply_acceptance_followup`，让父 runner 在孩子测试通过后收口直接孩子，顶层 CLI/API 仍保持人工 apply。`dispatch_subagents` 支持 `run_ids` / `include_run_ids` 精确指定本轮要跑的 direct child，并按给定顺序过滤 runner 候选；显式 run_ids 是父级/模型的准确操作请求，不再被 coordinator/worker/tester 阶段排序静默缩窄，阶段闸门只用于隐式自动候选。`dispatch_runner_selection.py` 会先预检显式 id，缺失或越界时返回 `runner_selection/invalid_run_ids` 和可用 direct child ids，不启动错误 runner；dispatch payload 顶层会放 `runner_selection_recovery.valid_run_ids`，避免大 records 被外置后模型看不到纠偏指令。dispatch payload 还会检查当前轮 remembered run ids，只要还有未 `DONE/VERIFIED` 的 run，就写 `unfinished_run_ids`、`must_not_report_done=true` 和 `continue_dispatch_unfinished_run_ids`，避免 root 只看到一个已完成报告就收尾。当一个批次包含多个 pending runner 时，共享 `runner_instruction` 会被忽略并写入 `ignore_multi_runner_instruction` 记录，避免一个孩子的专属提示污染其他分支。`subagent_board` payload 会先给 `actionable_run_ids`，再给截断后的 items，并把 `status=ALL/*/ANY` 归一成不过滤。`orchestration_progress_payload.py` 会在 runner-context dispatch 响应里附带 direct child status counts、PLANNING/RUNNING ids、BLOCKED/FAILED/TIMEOUT recovery ids、`needs_more_dispatch`、`needs_recovery`、`unfinished_run_ids`、`next_action` 和带 `run_ids` 的建议工具调用；这些继续推进已有 direct child 的建议默认 `workflow_mode=off`，不能重新打开 generic workflow 自动扩容。若需要新建恢复 child，会给默认 worker 且可改 coordinator/lead 的 refs-only 建议，避免把所有恢复都固定成 coordinator。若父任务点名 tester/bug_finder/acceptor 且实现后代已 ready，dispatch payload 会返回 `quality_advice`、`next_action=create_quality_children_from_ready_refs` 和可复制的 QA child 建议，避免 root 反复读取产物正文来替代 QA 子代理。`create_subagents` 还会保留 `context_manifest`、`context_packs`、`required_read_paths`、`source_refs` 和 `context_pack_refs` 这类 refs-only 资料路径，写入对应 run 的 Context Manifest / Context Packs；这让 root 能把长正文交给小傻妞自己读，而不是派工前先把所有正文塞进 root 上下文。
- `agent_py_agent/agent/agent_core/orchestration_create_items.py`：顶层 `create_subagents` 的批量参数解析层；把 `items` / `tasks` 解析成独立子任务 bundle，顶层验收/工具/写入边界作为默认值继承，单项字段可以覆盖。顶层全局 `plan` 默认不继承给 item，避免主代理收尾计划误变成每个 child 的文件合同。这个文件只做参数解析，不创建 run，不调度 runner。
- `agent_py_agent/agent/agent_core/orchestration_lineage_names.py`：顶层 `create_subagents` 的小傻妞默认命名 helper；把系统生成名补成 `小傻妞-角色-编号`，并避免重复回放时把 `-1` 继续追加成 `-1-1`。
- `agent_py_agent/agent/agent_core/orchestration_create_idempotency.py`：顶层 `create_subagents` 的幂等层；同一 parent/root 下明确同名、同 role 且仍可复用的小傻妞已存在时，工具复用已有 run，不再创建重复 child。默认显示名和 `小傻妞-角色-编号` 这类系统名不能只按名字复用，而是按 parent/root/role/goal/owner/supervisor/final_owner/extra_write_roots 的精确合同复用，避免 root 复读时无限扩容，也避免两个不同默认 worker 被合并。`count>1` 的 sibling 会得到稳定序号名，让重复批量 create 复用 `-1/-2` 对应 run。create payload 会区分 `created_run_ids`、`reused_run_ids` 和 `dispatch_run_ids`：已 `DONE/VERIFIED` 或 `RUNNING/BLOCKED` 的复用 run 仍展示给 root，但不会被建议重复 dispatch；没有可调度 run 时，`next_action` 会转向 `subagent_board` 而不是空跑 dispatch。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_state_contract.py`：顶层 `dispatch_subagents` 的当前轮状态合同层；它只读取本轮 touched run ids，把 `PLANNING/RUNNING/BLOCKED/DONE` 压成 `current_turn_run_state`，并给出 dispatchable/running/blocked/verified/unfinished/missing run ids 和下一步建议。这样 root 不用展开 bulky records，也不会凭自然语言记忆重复调度已完成任务或跳过阻塞任务。
- `agent_py_agent/agent/agent_core/orchestration_item_dependencies.py`：`create_subagents(items=...)` 的批次依赖推断层；当后续 item 写“基于小傻妞-数据收集提供的结果”“整合数据收集和内容编写的结果”时，会追加 `required_read_paths` 或 run 级 `workflow_depends_on`，让显式 `dispatch_subagents(run_ids=[...], max_runners=N)` 也必须等上游完成。大白话说：模型可以自然地说“B 用 A 的结果”，系统会把这句话变成机器能执行的先后顺序。
- `agent_py_agent/agent/agent_core/dispatch_runner_candidates.py`：runner 候选策略层；显式 `run_ids`、输入文件等待、`workflow_depends_on` sibling 等待和 packet/checkpoint 恢复候选都在这里集中判断。显式调度只点名下游 run 时，依赖判断仍会用完整可见任务表查找已完成上游，避免“上游完成了但下游仍看不到依赖事实”。
- `agent_py_agent/agent/subagents/dependency_artifact_refs.py`：workflow sibling 依赖的 artifact ref 解析层；当下游 `required_read_paths` 只有 `data_collection.md` 这种短名，而上游已完成并登记 `/.../subagent-id/data_collection.md` 时，调度门会认为输入已满足，执行上下文会把真实 artifact 路径补进下游 `Context Manifest`。这对齐 Hermes/OpenClaw 的 refs-first 交接思想：父级/下游读机器 refs，不猜上游目录。
- `agent_py_agent/agent/agent_core/orchestration_create_context.py`：顶层 `create_subagents` 的 refs-first 上下文归一层；它把 `required_read_paths`、`source_refs`、`context_manifest`、`context_packs` 和 `context_pack_refs` 合并成子代理可见的 Context Manifest / Context Packs，不读取正文、不创建 run。
- `agent_py_agent/agent/agent_core/orchestration_predelegation_read_guard.py`：派工前正文读取提示门；当 root 还没创建当前轮小傻妞、且用户明确要求派工时，允许读 README/rubric/目标等 brief，但会把 `data/docs/materials` 正文读取挡回 `create_subagents + required_read_paths/context_manifest`，避免 root 在第一次派工前吞掉大量资料正文。
- `agent_py_agent/agent/agent_core/orchestration_quality_payload.py`：runner-context dispatch 的 QA 修复建议层；它只读取后代 QA 任务的 `output.json` 摘要、状态和 refs，不读业务产物正文。若 tester/bug_finder/acceptor 自己报告失败、缺陷、缺失、断裂或状态失败，payload 会返回 `direct_children.qa_repair_advice`、`needs_repair_wave=true` 和一个可编辑的 repair worker 建议工具调用，并把 `ready_for_parent_acceptance` 置回 false。它不自动创建修复节点，也不固定流程；父级 LLM 需要根据失败 QA refs 决定修复范围、修复角色和复测顺序。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_scope.py`：集中处理 `dispatch_subagents` 的 apply/execute/max runner/parent scope 默认值。显式给多个 `run_ids` 且真实执行 runner 时，若模型漏写 `max_runners`，默认推进全部显式目标；顶层没有 run_ids 时仍默认 1，runner-context 仍默认最多 6 个 direct child。
- `agent_py_agent/agent/agent_core/orchestration_artifact_integrity_signals.py` / `orchestration_artifact_integrity_repair.py`：产物结构失败的专用 repair lane。signal 层只读取 `output.json`、`run.json`、dispatch evidence paths 和 task 字段，提取 `artifact_integrity_failed` blocker、artifact refs 与 product write roots；repair 层把这些 refs 转成 `artifact_integrity_repair_advice`。顶层 root 会拿到 `create_subagents` 建议，runner-context 父节点会拿到 `schedule_child_subagents` 建议，避免父级因为 generic `classify_blocker` 自己下场修产物。
- `agent_py_agent/agent/agent_core/orchestration_root_contract.py`：显式 root/coordinator seed 的合同修复 helper；从原始用户 prompt 补回 required/forbidden 文件清单和精确层级命名合同，让 `orchestration_tools.py` 保持薄入口。
- `agent_py_agent/agent/agent_core/dispatch_loop.py` / `dispatch_no_progress.py`：父级 dispatch 闭环负责连续调用 `dispatch_subagents`，直到没有 pending runner、达到最大轮数，或触发 no-progress fuse。no-progress fuse 的签名和进展判断在独立 helper 中，只比较稳定字段，忽略 record id / 时间；如果连续两轮只是 `due_check`、`leadership_recovery_plan` inspect 或 `classify_blocker` 等 record-only action，且没有状态/验收/child 创建变化，就标记 `stopped_by_no_progress=True` 并停止，避免 root 完成后被历史 blocked 节点拖入无限记账循环。
- `agent_py_agent/agent/agent_core/runner_dispatch.py`：runner 候选选择按阶段执行。coordinator/lead 先拆任务，worker/writer/leaf_worker 先产出，tester/bug_finder 后检查，acceptor 最后验收；排序优先看 `role` / `agent_name` 身份，只有身份不明确时才读 goal，避免继承的父级 QA 合同把普通 coordinator 误判成 QA。
- `agent_py_agent/agent/agent_core/tool_agent_budget.py` / `tool_agent_budget_stage.py`：单个代理工具预算守卫。它按 `run_id` 记录滚动时间窗口内的工具调用次数，默认 10 分钟 50 次；没有 `run_id` 的主代理普通聊天不受这个守卫限制，不做整棵任务树或单次对话的全局限流。触发后返回工具失败结果，让模型自检、总结已有进展并向父级请求接管/提高预算，而不是直接终止 runner。
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`：主代理和 subagent 共用的工具循环；现在会在执行真实工具前调用单代理预算守卫、委托期读正文守卫和系统保留工具记录守卫。预算触发时只拦截当前 run 的本次工具，不消耗兄弟代理预算，也不改写任务状态。委托期读正文守卫会让已有 child 的父级在 acceptor 完成前保持 refs-only：可以读 task/status/acceptance/handoff/takeover 这类运行元数据，但不能主动 `read_file` 产物正文或 `read_artifact` 大正文，除非用户当前 prompt 明确授权“你自己/主代理亲自验收”。保留记录守卫会拒绝模型自己写的 `[tool-record]` / `[tool-output-record]`：真实工具调用仍执行，伪造回执不进下一轮 prompt；如果模型只伪造回执又没有真实 `[TOOL_CALL]`，系统会纠偏一次，再重复就确定性阻断。几个守卫和 `max_tool_rounds` 互补：前者防止单个代理复读工具、父级偷读正文和模型伪造工具结果，后者在配置为正数时限制单次响应链的工具轮数；`max_tool_rounds=0` 表示不限制轮数。文本协议还有一条硬边界：同一轮模型回复里第一个完整工具调用闭合后，后续正文不会再作为工具调用、工具结果或事实进入控制流。
- `agent_py_agent/agent/agent_core/tool_reserved_record_guard.py` / `tool_loop_response_decision.py`：工具循环里的伪造回执防线。前者识别并清理模型写出的系统保留标记，后者把“继续纠偏 / 执行真实工具 / 直接收口 / 确定性阻断”的决策从主循环拆出来，避免 `_tool_loop_service.py` 再次接近 code-size 风险。
- `agent_py_agent/agent/agent_core/tool_stream_boundary.py` / `tool_model_generation.py`：流式工具调用边界防线。`tool_model_generation.py` 包装 backend streaming callback；`tool_stream_boundary.py` 发现第一个完整 `[TOOL_CALL]...[/TOOL_CALL]` 或 `[SUBAGENT_CALL]...[/SUBAGENT_CALL]` 后，停止转发后续 chunk，并把返回的 `ModelResponse` 裁到同一边界。这样模型不能在同一轮里先要求工具、再自导自演 tool-output-record 或追加第二批工具调用。没有闭合标记时保留原有 parse recovery 行为，避免真实模型漏写结束标记但 JSON 完整时被误杀。
- 工具回执的长期架构原则：模型文本只能“申请工具调用”，不能“证明工具已执行”。这个原则参考了 Hermes 的 `assistant(tool_calls) -> tool(tool_call_id)` 消息链、OpenClaw 对泄漏 tool tags / downgraded tool-result 文本的剥离，以及两者对大工具输出外置化的处理。当前 my-agent 仍保留文本协议 `[TOOL_CALL]`，但真实状态只信工具循环写入的 records、task refs、filesystem refs 和外置 artifact refs；模型自己写出的 tool-looking 回执全部按不可信文本处理。
- `agent_py_agent/agent/agent_core/orchestration_body_read_guard.py`：委托期 refs-only 策略入口。当前 runner 已派出 child 且没有完成的 acceptor 后代时，阻断父级读取业务正文，返回 `delegating_body_read_blocked=true` 和下一步建议：看 `subagent_board` / dispatch 摘要、补 tester/bug_finder/acceptor、或基于失败 refs 创建 scoped repair worker。调度类小 artifact（`dispatch_subagents-*`、`subagent_board-*`、due-check/action-plan refs）允许读取，因为它们是父级协调所需的状态摘要，不是产品正文；显式用户授权会临时放行产品正文；普通“验收标准”不会放行。
- `agent_py_agent/agent/agent_core/subagent_message_tool.py`：模型可调用的层级通信工具。`direct + descendants` 只能发给当前 sender 的子孙，用于少数下属的不同纠偏；`broadcast + descendants` 写入 shared messages/blackboard，并带作用域字段供下级按祖先过滤；`direct + peers` 只允许同父级 sibling 点对点讨论，为后续 team 功能预留，不能越权给 cousin 分支或祖先发命令。
- `agent_py_agent/agent/agent_core/capability_request_tool.py` / `agent_py_agent/agent/subagents/root_task_policy.py`：模型可调用的正式能力申请工具。普通 runner 缺工具、skill、MCP、网络或 shell 命令时调用它写 OPEN `CapabilityRequest`；工具只允许为当前 run 申请，不授权、不执行，也不能替 sibling 或别的分支写入。只有显式 self-authorized root/coordinator/lead seed 没有上级，当前不走 `capability_request`；主代理直接创建的一层 worker/researcher/writer 虽然 `parent_id` 为空，但真实上级是主代理，仍可写 capability request。root 自毁/卸载/系统红线属于后续 root policy 设计，不放进 child capability_request 流。父级随后通过 `route_capability_request` 生成 grant/gap 并决定是否重跑。
- `agent_py_agent/agent/capability/runtime_config_patch.py` / `runtime_config_reload.py` / `agent_py_agent/agent/agent_core/capability_config_patch_tool.py`：能力配置自修复通道。agent 不能直接编辑 `capability_config.yaml`，必须提交 `CapabilityConfigPatchRequest`；服务层用文件内容 hash 做并发校验，只自动写 allowlist 安全字段，危险字段返回 `manual_approval_required` 建议。成功补丁会写 JSONL 审计和通知记录，并刷新 future dispatch/watch 的 config snapshot；已经运行的子代理上下文不被中途改写。
- `schedule_child_subagents` 的模型工具入口单次最多接受 2 个 child。原因不是底层 manager 不能批量，而是真实模型在三条以上长 goal 时容易输出半截 JSON，导致没有任何 child 落盘。需要大规模扇出时，runner 应连续多次调用，每次 1-2 个 child，再用 `dispatch_subagents` 跑真实返回的 `created_run_ids`。
- `agent_py_agent/agent/subagents/required_file_terms.py`：层级 handoff、context bundle 和静态站父级验收共用的文件契约提取器。它把正向交付文件写入 `required_files`，把 `禁止改成 product.html`、`不要创建 legacy.html`、`禁止文件名：product.html/old-product.html`、`禁止文件名（product.html/old-product.html）`、`## 禁止文件`、`禁止内部文件（output.json）`、`禁止创建文件（forbidden_files）：\nproduct.html...`、`不写output.json/RUNNER_RESULT.md`、`无 forbidden_files(product.html)`、`无内部文件污染(output.json)`、`用户原始禁止文件/反例名（禁止创建...）：product.html` 这类反例写入 `forbidden_files`；若负向标题后面接 bullet 文件列表，或下一行是纯文件列表，也会把该标题状态带到对应行。它还会把 `禁止 style.css/app.js 放进 css/ 或 js/ 子目录` 识别成位置约束，不会把这两个必需资源误标成 forbidden。`task.json 里的状态`、`execution_context.json refs`、`必须按真实 task.json 阻塞汇报` 这类内部状态引用不会被当成用户交付文件；即使前文有 `禁止创建 depth>=4` 这样的词，紧邻 `里的/refs/阻塞/汇报/状态` 的内部文件名仍按状态引用处理。`读取/参考/输入资料/source/input/reference` 语境里的文件名也不会进入 required，例如 `vietnam.md 是输入资料` 或 `读取 data/.../vietnam.md`；只有 `交付/输出/创建/写入/包含` 等明确交付动作才会让文件名进入 required。文件名提取使用 ASCII 边界，因此中文可以紧贴 `output.json` 或 `RUNNER_RESULT.md等`，两者仍能被识别。required/forbidden 分开传给下层，避免把反例或输入资料误传成任务合同。
- `agent_py_agent/agent/path_recovery_hints.py`：统一提供 URL span、路径范围重叠判断和工作区路径拼写修复建议；派工守卫和文件系统工具共用，避免一个入口能恢复、另一个入口又泛化报错。
- `agent_py_agent/agent/agent_core/orchestration_write_guard.py`：派工前只做明显越界写入预检，真实写边界仍由工具执行层负责；URL 内部的 `s://` / `//host/path` 片段不会被当成本地写目标；当外部绝对路径疑似只是工作区前缀拼错时，会返回 `suspected_path_typo=true` 和 `suggested_target`，要求模型按建议路径重试调度，而不是申请扩大权限。
- `agent_py_agent/agent/agent_core/tool_round_execution.py` / `tool_loop_completion.py`：承接一轮工具调用执行、live context 回写、子代理 `output.json` 收口和顶层 dispatch 完成收口。当前 runner 成功写入自己的 `output.json` 后，会从该 JSON 合成 `SUBAGENT_RESULT` 并停止工具循环，避免完成态子代理继续请求模型；顶层 `dispatch_subagents` 后若 scoped tasks 全部 `DONE/VERIFIED` 且没有缺失的显式 tester/acceptor 要求，会直接用本地 task 状态、artifact refs 和 output refs 生成确定性收口，不再额外发起自由模型轮。收口检测同时支持 `path` 和 `filesystem.path` 两种 write_file 参数形态，适配真实模型常用的 bundle 写法；同一轮如果先执行了 `create_subagents` / `schedule_child_subagents`，后续依赖真实 run id 的编排工具会被延后到下一轮，防止模型用脑补 id 直接 dispatch。
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`：`max_tool_rounds` 为正数且工具轮数到达上限后，会先给模型一次收口机会；如果模型仍输出工具调用，系统改写为确定性停止说明，避免上层把 `[TOOL_CALL]` 当最终回答继续传播。`max_tool_rounds=0` 表示不限制工具轮数，不会触发这条收束逻辑；单代理工具预算仍可单独防复读。该逻辑不放开工具执行，只保护收束边界。
- `agent_py_agent/agent/agent_core/tool_call_context_reducer.py` / `agent_py_agent/agent/agent_core/tool_context_reducer.py` / `agent_py_agent/agent/agent_core/tool_context_orchestration_summary.py`：大段 assistant tool-call payload 不再原样进入下一轮 live prompt；例如 `write_file(content=<large html>)` 会压成工具名、路径、字段大小、sha256 和短预览，完整正文留在目标文件或调试 detail ref。普通外置工具输出仍保留 artifact/path/hash/size 恢复锚点；调度类外置输出（`dispatch_subagents` / `schedule_child_subagents` / 读回来的调度 artifact）会改成 compact orchestration summary，只保留 `next_action`、run ids、状态计数、QA/parent-acceptance/artifact-integrity repair advice 和 suggested tool call，不默认给 `read_artifact_hint` 诱导父级展开正文。`_tool_loop_service.py` 的 live context marker 使用 `[tool-record ...]` / `[tool-output-record ...]` 这类中性标签，避免模型把历史记录复制成新的 `[TOOL_CALL]`。
- `agent_py_agent/agent/backends/errors.py` / `agent_py_agent/agent/backends/gateway_helpers.py` / `agent_py_agent/cli/local_commands.py` / `agent_py_agent/agent/agent_core/runner_dispatch.py`：模型接口网络超时不再只是普通 `RuntimeError`；HTTP/streaming 层会抛 `ProviderTimeoutError`，子代理 runner 记录 `failure_type=provider_timeout`，顶层 `my-agent run` 会输出可读的 `provider_timeout` 恢复提示并用非 0 退出码收口。`provider_timeout` 现在进入有上限的 runner retry 策略，避免无人值守时只看到进程沉默等待或反复 classify_blocker。
- `agent_py_agent/agent/tooling/_filesystem_read.py` / `agent_py_agent/agent/tooling/filesystem_read_file.py` / `agent_py_agent/agent/tooling/filesystem_artifact_guard.py` / `agent_py_agent/agent/tooling/artifact.py` / `agent_py_agent/agent/tooling/artifact_read_budget.py` / `agent_py_agent/agent/memory_archive/artifact_reader.py` / `artifact_read_modes.py`：普通 `read_file` 只读工作区文本文件；`filesystem_read_file.py` 承接读取、行号分页、结构化摘要和截断提示，避免工具声明文件继续变厚；读/列/search 同时接受顶层 `path/query/range` 和 `filesystem.path/query/range` bundle 参数，避免模型按 bundle 规范调用时退回全工作区；遇到疑似工作区路径拼写错误时会给 `suggested_target`；不允许直接读取 `memory_archive/artifacts/tool_outputs/*.json` 外置工具输出包装；如果模型把外置 artifact 包装路径前缀抄错，纠错提示也会要求改用 `read_artifact`，而不是继续 `read_file suggested_target`；需要正文时必须走 `read_artifact`，通过已登记 `artifact_ref` / `scoped_call_id` / `call_id`、当前 run/task/request scope 和 `mode=slice/head/tail/search` 窄读，避免短 call id 跨 run 读到旧 artifact；单 run artifact 正文读取预算按 `run_id` 限制读取字符，防止下级反复展开大 artifact。
- `agent_py_agent/agent/tooling/registry_execution.py` / `agent_py_agent/agent/tooling/registry_payload_normalize.py` / `agent_py_agent/agent/tooling/parse_error_hint.py`：工具调用解析失败时返回固定重试格式提示，要求下一轮使用 `[TOOL_CALL]` + 单个 JSON 对象 + `[/TOOL_CALL]`，不回显原始坏工具正文；`registry_payload_normalize.py` 专门负责 JSON payload 校验、工具名别名和路径参数别名归一，保持执行/授权主流程薄；如果坏块像 `write_file` / `append_file` 的长 `content`，提示会转向短骨架 + `append_file` 分块，而不是让模型重复输出完整正文。
- `agent_py_agent/agent/tooling/content_transport_policy.py`：统一管理长内容传输规则。`write_file.content` / `append_file.content` 不能把大文件正文一次塞进工具 JSON；超过 inline 上限会失败并提示使用短骨架、分块追加、`replace_in_file`/patch 小 diff，或在已有父级 `controlled_exec` grant 时用受控脚本生成文件并只回传 refs。这个策略同时接入公开 `filesystem_write.py` 和内部 `_filesystem_write.py`，避免不同入口行为分叉。
- `agent_py_agent/agent/tooling/content_recovery_mode.py`：当上一轮因为长 `write_file`/`append_file` 正文导致 parse error 或 inline 上限拒绝时，`_tool_loop_service.py` 会给下一轮 live prompt 追加 `long_content_recovery_mode`。它要求模型只输出 1 个写入工具调用、先写短骨架、再把每块 `content` 降到恢复上限内，避免模型复制同一段大 JSON 反复失败。这个做法吸收 Hermes/OpenClaw/Codex 一类“输出外置、摘要回传、小 diff 优先”的方向，但实现上只落成一个独立策略模块，后续扩展流式写入或 artifact-backed 写入时不用改散落提示词。
- `spawn-subagents --role coordinator --agent-name <name>` / 顶层 `create_subagents(role=coordinator)` 是真实层级 E2E 的 seed 入口：外层只创建一个 root/coordinator，自动授予 `schedule_child_subagents` / `dispatch_subagents` / `subagent_board`、基础读写、web 取证和报告写入工具；shell/exec 仍走后续受控网关，不靠角色模板暗开。顶层 `create_subagents` 会从当前原始用户 prompt 里补回 required/forbidden 文件合同和 4层/depth/命名约束，避免主代理摘要 root goal 时把机器合同缩水。`spawn_role_seed.py` 会把 goal 里的产物路径提取为 root/coordinator 的覆盖写入根，便于验收、接管和救援；但最终业务代码/页面/文档产物仍应派给 worker/writer，角色职责和父级验收负责约束“有权限但不乱写”。后续子、孙、孙孙任务必须由上一层 runner 通过 orchestration tools 创建，外层不得直接替下层创建。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_payload.py`：承接 `dispatch_subagents` 工具输出里的单条 record payload，保持返回给 runner 的 acceptance/test/follow-up refs 精简且可测试；runner 真实创建了下级时，会透传 `runner_created_children`、child run ids 和 roles，避免父级把 dispatch 记录数误读成真实孩子数；run id 选择失败时还会把 `valid_run_ids` 放到顶层 recovery payload。
- `agent_py_agent/agent/subagents/services/hierarchy_recovery.py`：定义 `HierarchyRecoveryRequest` / `HierarchyRecoveryResult`，从 root run 只读扫描 child_ids 子树，返回需要恢复的后代、context bundle、父级 context bundle、takeover readiness、failure handoff 和 checkpoint refs；可按 capability timeout 阈值把 stale `RUNNING` 后代纳入恢复候选，不展开 artifact 正文、不自动接管。
- `agent_py_agent/agent/subagents/manager_hierarchy.py`：给 `SubAgentManager` 暴露 `schedule_child_runs(params=...)` 薄 facade，让层级创建入口保持单一且可测试。
- `agent_py_agent/agent/subagents/role_templates.py`：加载内置和用户外置 JSON 角色模板；模板必须是广义角色、带中文说明、可处理多个目标，坏模板只记录 issue，不影响内置模板。`role_template_index_text()` 给主代理常驻 prompt 提供模板索引、适用/不适用场景、能力标签和模板位置，但不展开默认工具和完整系统提示；`role_template_detail_text()` 给普通 runner 只展开当前角色详情，给 coordinator runner 在派工时展开角色全集详情，避免不派工时加载全部派工细节。`role_template_id_for_role()` 是运行时兜底解析层：LLM 写出 `child_coordinator`、`qa_tester`、`slide_ppt_polisher_lead` 这类自然角色名时，会从当前模板目录按 token 匹配到 `coordinator/tester/ppt_polisher`，找不到时由角色契约回退到 `worker` 模板，避免空工具子代理。
- `agent_py_agent/agent/subagents/role_template_catalog/builtin/*.json`：内置角色模板，包括协调、执行、找茬、测试、验收、研究、写作。模板定义默认工具、是否可写、是否可验收、输出契约和中文 prompt。
- `docs/modules/subagent/08-role-selection-strategy.md`：角色选择策略文档，明确 root/coordinator/lead 什么时候用 coordinator、worker、writer、researcher、tester、bug_finder、acceptor，以及常驻短索引和按需模板详情的边界。
- 角色选择策略必须和模板分开维护：root/coordinator/lead 常驻只加载“角色索引 + 何时使用”的短规则，真正派工时才展开模板详情。推荐默认语义是：`coordinator/lead` 拆分、广播、纠偏、接管和 refs 汇总；`worker/writer` 负责真实产物；`researcher` 查资料/事实源；`tester` 做验证计划或执行可用测试；`bug_finder` 找错、找风险、做反例；`acceptor` 做最终验收建议但不自验收。一个 tester/bug_finder/acceptor 可以检查多个 worker 的产物，不要求一一对应。
- `coordinator` 角色模板把最终产物写入定义为“有权限覆盖但默认委派”的职责边界：coordinator 可以继承产物目录权限用于检查、接管和救援；runner write boundary 会把这些目录标成 `product_write_roots`，并对 coordinator/tester/reviewer 等上层角色使用 `product_write_policy=delegate`，所以它们只能写 task-local 报告和证据，真实业务产物必须转派 worker/writer/leaf_worker。
- `agent_py_agent/agent/subagents/role_contracts.py`：集中定义 `reporter` / `checker` 兼容角色契约，并把 `bug_finder/tester/acceptor/coordinator/worker/researcher/writer` 接到 role template；检查型角色可以写 task-local 检查报告和证据摘要，但不能自验收、不能替 worker/writer 写最终业务产物，最终仍由父级 gate 裁决。运行时角色名可保留层级/专业辨识度，例如 `child_coordinator`、`leaf_worker`、`frontend_footer_builder`，但工具、验收和质量合同必须套到匹配模板；未知自由角色至少回退 `worker` 模板，不能生成 `allowed_tools=[]` 的空能力代理。
- `agent_py_agent/agent/agent_core/runner_dispatch.py`：runner 候选会先过滤可执行状态，再按角色阶段排序：coordinator/规划类优先，worker/产出类先于 tester/bug_finder/critic，acceptor 最后；同一轮 dispatch 只放行当前最低阶段候选，producer/coordinator 未完成前 QA/test/review/acceptance 会等下一轮，排序发生在 `max_runners` 截断前，避免真实业务链路出现“先验收再干活”。runner dispatch record 会保存内部创建的 child ids/roles/status counts；runner 超时但已创建 child 时标记 `runner_partial_success` 和未完成 child ids。
- `agent_py_agent/agent/agent_core/runner_dispatch.py` 还会在角色阶段排序前检查 workflow refs 依赖：带 `workflow_depends_on` 的 phase child 只有在同一 `workflow_parent_run_id` 下的 upstream phase 已经 `AWAITING_ACCEPTANCE` / `DONE` 或 `NEEDS_ACCEPTANCE` / `VERIFIED` 后才会进入候选。这样 auto workflow 的 `repair` 不会抢在 `critic` 前面执行。
- `agent_py_agent/agent/agent_core/orchestration_tool_specs.py`：`create_subagents` / `schedule_child_subagents` 的工具说明只展示角色模板索引，不展开 `prompt_zh` 和默认工具细节；真正需要派工时由 runner prompt 加载详情。
- `agent_py_agent/agent/agent_core/runner_gate.py`：统一计算 runner 外层超时；默认用户配置 `runner_timeout_seconds: "off"` 表示不限制，`auto` 才进入动态 timeout，数字字符串表示固定秒数。`runner_timeout_by_role` 可按 `root` / `coordinator` / `worker` / `takeover` 等角色覆盖全局值，适合 root 长跑、普通 worker 短保护、接管 run 更长预算的真实 E2E；`leaf_worker` / `repair_worker` 等内部角色会自动匹配用户写的 `worker`。
- `agent_py_agent/agent/subagents/services/takeover_run.py`：创建接管 run 并继承 source refs；`subagent_takeover_chain_max_depth` 是隐藏兼容熔断项，默认 `0` 表示不限制，只有显式正数才限制同一失败任务连续 takeover 深度。默认路径优先把任务接住，防无限扩容交给 no-progress fuse 和父级验收事实。
- `agent_py_agent/agent/tooling/json_repair.py`：工具调用 JSON 的窄口修复层；目前只接受“第一个 JSON 对象后面额外多出右花括号”的真实模型漂移，不会吞掉第二个 JSON 对象或任意坏格式。
- `agent_py_agent/agent/subagents/automation_gate.py`：定义半自动/自动执行门；只允许 `query_recovery_tree` / `inspect_refs` 这类 refs-only 动作自动放行，跑工具或改 task 状态必须人工确认或继续阻断。
- `agent_py_agent/agent/subagents/execution_records.py`：定义 `TestExecutionRecord`，保存真实验收执行证据字段、序列化、stdout/stderr 截断和 `passed` 派生结果。
- `agent_py_agent/agent/subagents/execution_executor.py`：定义最小 `TestExecutor`，支持 command / file_check / content_check / static_site_check，当前不写任务状态、不生成 `test_execution.json`；command 可带 workspace 内 `working_dir` / `cwd`，执行记录会保存真实工作目录。`content_check` 支持包含匹配，也支持 `content_equals` / `expected_content` + `match_mode=exact` 的精确内容检查；`static_site_check` 检查静态站点必需文件、本地链接/资源、`${...}` 占位符和明显无动作控件，不执行 JS、不访问网络。
- `agent_py_agent/agent/subagents/static_site_validator.py` / `static_site_dom_checks.py` / `static_site_path_checks.py`：承接 `static_site_check` 的 HTML 扫描、链接解析和控件检查；DOM/id/control 检查与路径/ref 检查拆成小模块，所有路径都限制在 workspace 内，只返回 refs-only 问题摘要。
- `agent_py_agent/agent/subagents/execution_executor_helpers.py`：承接 `TestExecutor` 的命令解析、跨平台 python argv、记录构造和 UTC 时间 helper，让 executor 主文件保持薄执行器职责。
- `agent_py_agent/agent/subagents/execution_test_items.py`：在父级验收执行前预处理 tests；当 runner 只给出相对测试命令但 artifacts 都指向同一产物目录时，安全补 `working_dir`，并能在 workspace 内按唯一路径后缀恢复嵌套相对 artifact；常见 `cd <workspace内目录> && python3 -m pytest ...` 会被归一成受限 `working_dir` 加纯命令，即使 runner 同时给了 `working_dir` 也会拆掉安全开头 `cd`，但不放开 shell；模型把内置 `static_site_check` 写成 `command` 时会转回原生 `validation_method`，不会被 shell allowlist 当陌生命令拦住；若 tests 为空但 artifacts 里有 workspace 内 `test_*.py`，会生成保守 pytest 兜底，避免假绿和无谓的“0 tests ran”；若 artifacts 显示静态 HTML（单页或多页）且 runner 未显式声明可执行的 `static_site_check`，会自动补一个静态站点机器验收项；同时会从 task goal/thought/description/acceptance_checks 抽取 `index.html`、`style.css`、`app.js` 等任务承诺的静态必需文件并合入机器验收；当该机器验收已存在时，缺 `command`、`file_path`、`content_pattern` 或 `site_root` 的模型空壳测试会被过滤，避免格式错误的自报清单压过确定性检查。
- `agent_py_agent/agent/subagents/execution_static_site_items.py`：根据 output artifacts 推断 `static_site_check` 测试项，生成 workspace 相对 `site_root` 和 `required_files`，并合并父级传入的 task-level required files；推断阶段只读路径引用，不读取 HTML 正文。
- `agent_py_agent/agent/subagents/execution_content_checks.py`：从 tests 预处理拆出的内容验收窄口，只把明确期望内容的 `cat <workspace文件>` 转成受控 `content_check`。
- `agent_py_agent/agent/subagents/execution_report.py`：写入和读取 `test_execution.json`，并生成 `test_execution.md` 展示报告；JSON 是事实源，Markdown 不参与机器判断。
- `agent_py_agent/agent/subagents/services/acceptance_machine_evidence.py`：只读 `reports/test_execution.json`，当父级真实测试全部通过且 runner 没有 evidence packet 时，把测试报告作为机器证据链；已有坏 evidence packet 不会被测试报告覆盖。
- `agent_py_agent/agent/subagents/parent_acceptance_controller.py` / `agent_py_agent/agent/subagents/parent_acceptance_preflight.py`：生成父代理验收 dry-run 决策，返回 `execute_tests` / `review_patches` / `inspect_only` / `request_human` / `rescue` 等下一步；它只读 refs 和机器事实源，不执行命令、不写 task；预检前会复用 `prepare_test_items()` 归一化安全 cwd 包装，并把任务文本里的静态 required files 传入自动 `static_site_check`，避免真实 runner 常见的 `cd <workspace内目录> && pytest` 被误判成人审，也避免 split branch 局部 artifact 通过但顶层页面缺失；显式写入时生成 `parent_acceptance_decision.json`。
- `agent_py_agent/agent/subagents/static_required_files.py`：从 task goal / thought / description / acceptance_checks 提取 `index.html`、`style.css`、`app.js` 等静态 Web 必需文件名；只处理任务文本，不读取产物正文。
- `agent_py_agent/agent/subagents/execution_test_items.py`：父级验收 tests 的预处理层。除安全 cwd 推断外，还会把真实模型常写的 `cat <workspace内文件>` 内容验收归一成受控 `content_check`，只有能从字段或窄文本中拿到明确期望内容时才转换；系统仍不把 `cat` 加进命令 allowlist。静态 Web 自动测试只在 artifacts 足够明确时补 `static_site_check`，已有同类测试不会重复追加。
- `agent_py_agent/agent/subagents/parent_acceptance_apply.py`：保存父级验收显式 apply 的结果模型、拦截/应用结果构造和 `parent_acceptance_apply.json` 落盘逻辑。
- `agent_py_agent/agent/subagents/parent_acceptance_next_action.py`：把父级验收 plan/apply 审计映射成下一步动作建议，例如 `run_tests`、`review_patches`、`request_human_confirmation`、`plan_rescue` 或 `apply_acceptance`；它只返回建议和 refs，不执行动作。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_policy.py`：把 next-action 映射成自动策略 dry-run 判断并写入 `parent_acceptance_auto_policy.json`；当前生成 allow/blocked、would_execute、manual-only 半自动计划、preflight 检查和 executed=false。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_execution.py`：定义父级验收自动执行 Request/Result bundle，并生成 `parent_acceptance_auto_execution.json` 审计；默认只记录计划、policy ref、recommended command、blockers 和 hard guard，显式确认时只允许执行 tests。手动确认执行同样先走 `prepare_test_items()`，因此安全 cwd 包装会被拆成 `working_dir + 纯命令`，但 shell 字符和越界目录仍被阻断。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_followup.py`：显式父级测试后的 follow-up 审计包，写 `parent_acceptance_auto_followup.json`，归类人工 apply、patch review、人工 rescue、人工确认或继续测试；只保存 refs、失败摘要和建议命令，不改 task 状态。
- `agent_py_agent/agent/subagents/parent_acceptance_followup_control.py`：读取测试后的 `parent_acceptance_auto_followup.json`，提供 `--followup` 预览和 `--apply-followup` 受控入口；坏 JSON 会返回 blocked 而不是崩 CLI，测试通过时复用 `inspect_only` apply 桥，测试失败或当前任务已失败/阻塞时复用 `takeover_or_reassign` action gate，不直接改写接管文件。
- `agent_py_agent/agent/subagents/parent_acceptance_followup_consistency.py`：承接 follow-up preview/apply 前的一致性校验，检查 run_id、当前 task 状态、测试报告引用、失败数和新鲜度；旧 apply follow-up 在任务已失败/阻塞后会转成受控 rescue/takeover 建议。
- `agent_py_agent/agent/subagents/parent_acceptance_rescue_followup.py`：承接没有测试 follow-up 的失败/阻塞任务 rescue 预览和 payload 构造，避免 manager 桥接文件膨胀。
- `agent_py_agent/agent/agent_core/dispatch_service.py`：承接 due-check、action apply、capability route、patch review 等 dispatch 记录构建；acceptance 记录已拆到专门模块，避免主调度服务继续膨胀。
- `agent_py_agent/agent/agent_core/dispatch_acceptance_records.py`：在 acceptance 调度记录上附加 parent acceptance auto-policy、auto-execution 和 follow-up 摘要；默认 dry-run，只在 `execute_acceptance_tests` 显式打开时调用第一层 run_tests 执行路径。顶层 dispatch 测试通过后仍只刷新报告和 follow-up；runner-context dispatch 若带 `auto_apply_acceptance_followup`，会在 follow-up 判定安全可验收时复用受控 apply，把直接孩子收口成 `DONE/VERIFIED`。
- `agent_py_agent/agent/agent_core/dispatch_acceptance_refresh.py`：当显式 parent tests 本轮写出新 `test_execution.json` 后，重新计算 acceptance dry-run 结果，并让 dispatch 展示、单 run 审计和 aggregate acceptance report 都对齐测试后结论；它不 apply acceptance、不触发 rescue、不修改 task 状态。
- `agent_py_agent/agent/subagents/report_dispatch_models.py` / `services/dispatch_params.py`：`DispatchRecord` 和 `DispatchRecordParams` 持有 `parent_acceptance_policy_*` 与 `parent_acceptance_auto_execution_*` 机器摘要字段，包括可选 test report ref/total/failed，供 JSON report、Markdown 和 watch 读取。
- `agent_py_agent/agent/subagents/rendering_dispatch.py`：`SUBAGENT_DISPATCH.md` 展示 policy 摘要和 ref，仍不展开 audit 文件正文。
- `agent_py_agent/agent/subagents/manager_parent_acceptance.py`：承接 manager 的父级验收 plan/write/apply/next-action/auto-policy/follow-up 桥接流程，并在 follow-up apply 前校验 run_id、当前 test report 引用、失败数和文件新鲜度；已失败/阻塞且没有测试报告的任务会得到合成 rescue follow-up，不会被误导去跑 tests。
- `agent_py_agent/agent/subagents/manager_acceptance_parent_facade.py`：承接父级验收公开方法薄 facade，让 `manager_acceptance.py` 继续只负责普通 acceptance review 编排。
- `agent_py_agent/agent/subagents/services/workflow.py`：workflow auto-mode 的 phase child 现在复用层级命名 helper，而不是复制 parent agent_name；例如 `小傻妞-*` 的 workflow 子节点会落成 `小小傻妞-*`。workflow child 仍只是普通 subagent run，状态、依赖和验收继续走 task/workspace 事实源。
- `agent_py_agent/agent/agent_core/dispatch_limiter.py`：runner 启动前的统一限流 helper，接收 `RunnerJobLimitRequest`，先保持 `runner_start_rate` 兼容语义，再提供可选 `runner_role_limits` 角色预算口子。
- `agent_py_agent/agent/subagents/acceptance_test_execution.py`：把显式开启的真实测试执行接入 acceptance findings，生成 `test_execution_recorded` 和 `test_execution_passed`，默认不运行；执行前会复用 tests 预处理，仍只允许 workspace 内目录。`auto_acceptance` / `child_acceptance` 不相信模型自称完成，而是检查 direct child 是否真实存在并全部 `DONE/VERIFIED`；coordinator/root 有 child 但 tests 为空时，会合成一个 `child_acceptance`。
- `agent_py_agent/agent/subagents/services/acceptance_findings.py`：普通 acceptance 的 finding 汇总层；当已有 `reports/test_execution.json` 时，tests_passed 以机器执行报告为准，而不是只相信 `output.json.tests[*].ok`。artifact 检查会把 task `allowed_write_roots` 纳入安全 roots，并对 allowed root 内的绝对路径 typo 做保守后缀恢复，避免真实模型把 case id 写错一位导致误判失败。required product 和 patch 协议检查已拆到独立 findings 模块，避免普通验收汇总层继续变胖。
- `agent_py_agent/agent/subagents/services/acceptance_product_findings.py`：业务产物验收 finding；核对 required files 是否真的出现在 product write roots 下，防止内部 run workspace 的同名文件冒充用户产物。
- `agent_py_agent/agent/subagents/services/acceptance_patch_findings.py`：patch 协议验收 finding；检查 planned/blocked、非法 status 和 applied patch review 状态。
- `agent_py_agent/agent/subagents/services/acceptance_descendant_health.py`：父级验收的后代健康门。它只沿真实 `task.child_ids` 读取有界 `task.json` 状态，不扫产物正文；只要任一后代不是 `DONE/VERIFIED` 或明确收口的恢复状态，父级就不能被验收为完成。
- `agent_py_agent/agent/subagents/services/board.py`：构建看板摘要和 child status counts。`SubAgentBoardOptions(include_child_status_counts=False)` 用于 status / startup recovery 等轻量路径，避免默认查询二次加载 child task；完整看板通过已加载 task 索引汇总 child 状态，不读取 runner prompt/response 或 artifact 正文。模型工具层的 `subagent_board` 会把可操作 run ids 和 `completion_status` 放在长 item 列表前，item 只暴露 refs-only 的 `target_tokens` 并截断 goal，减少真实 runner 在 board 大输出里丢失关键 id 或把旧失败误判成永久阻塞。
- `agent_py_agent/agent/subagents/services/board.py`：`due_check(..., root_id=...)` 和 `plan_actions(..., root_id=...)` 可把巡检/动作计划限制到一棵任务树，适合真实 E2E 多 root 共用 workspace 时减少噪音；默认仍处理全部 run。
- `agent_py_agent/agent/subagents/services/board_due_models.py`：承接 due-check 共享 DTO 和 issue helper，让巡检谓词文件保持可维护。
- `agent_py_agent/agent/subagents/services/board_due_checks.py`：heartbeat/run-timeout 只针对真实运行中或待接管的执行任务；带 child runs 且没有 active attempt 的 `PLANNING` coordinator 不按普通 runner 卡死处理，而是报告 `coordinator_heartbeat_stale`，由父代理决定是否恢复/转移领导权。父节点已经 `TIMEOUT` 但仍有 direct child 处于未验收、未完成或缺失状态时，会额外报告 `parent_timeout_with_unfinished_children`，用于提示恢复残留子树。
- `agent_py_agent/agent/subagents/services/action_handlers.py`：`recover_coordinator_leadership` 是受控 apply 动作；必须显式传已有 leader run id，才会把旧 coordinator 标记为 `TAKEN_OVER`，把直接子任务重挂到新 leader，并同步 `parent_id`、`depth`、`supervisor`、`final_owner`。`recover_child_after_parent_timeout` 当前是 record-only 恢复提示动作，用于把父超时后的残留孩子暴露给上级，不自动接管或改写任务树。
- `agent_py_agent/agent/subagents/services/leadership_recovery.py`：批量领导权恢复计划器；复用 due-check 的 `coordinator_heartbeat_stale` 事实源，把多个失联 coordinator 的直接孩子按候选 leader 容量拆批，当前只生成 refs-only dry-run 报告，不修改任务树。
- `agent_py_agent/agent/subagents/services/hierarchy_recovery.py`：`subagents-recovery-tree` 的 refs-only 恢复树构建器；父节点 `TIMEOUT` 后，未完成 child 会以 `parent_timeout_unfinished_child:<parent>` 进入恢复候选，所以 `--hide-healthy` 仍能展示需要继续派发或接管的孩子。
- `agent_py_agent/agent/subagents/services/leadership_recovery_apply.py`：受控分批 apply 服务；只移动显式 child 子集，校验 root、leader 健康、直接 child 关系和可选容量，移动后递归刷新后代 depth，并在旧 coordinator 清空后标记 `TAKEN_OVER`。
- `agent_py_agent/cli/_acceptance_plan.py`：提供 `subagents-acceptance-plan` CLI 入口；默认只展示父级验收 dry-run 决策和 refs，`--write` 只写决策审计文件，`--apply` 只允许 `inspect_only`，`--followup` 只预览，`--apply-followup` 才进入受控 apply/rescue。
- `agent_py_agent/cli/_acceptance_plan_renderers.py`：承接 acceptance-plan 的 JSON 转换和人类输出渲染；只打印 refs、状态和推荐命令，不展开 audit 文件正文。
- `agent_py_agent/cli/_hierarchy.py`：实现 `subagents-hierarchy` 命令；`--child ROLE:AGENT_NAME:GOAL` 可重复，默认 dry-run，`--apply` 才创建下一层 run，输出只包含 refs 和摘要。
- `agent_py_agent/cli/_hierarchy.py`：同时实现 `subagents-recovery-tree` 命令；按 root run 输出多层恢复包，`--hide-healthy` 可减少上下文体积，`--capability-config` 提供 stale RUNNING 判定阈值，仍只展示 refs 和摘要。
- `agent_py_agent/cli/_leadership.py`：实现 `subagents-leadership-recovery-plan` 和 `subagents-leadership-recovery-apply`；前者只写计划，后者默认 dry-run，只有 `--apply` 才按指定 `--child-run-id` 重挂子树。
- `agent_py_agent/cli/_review.py`：提供 `subagents-tests` 和验收相关兼容导出；tests 默认只读取已有 `test_execution.json`，显式 `--re-run` 才重新执行 `output.json.tests`。
- `agent_py_agent/agent/settings/config.py` / `agent_py_agent/config/agent_config.yaml`：默认只暴露 8 个用户可见子代理配置：`enable_subagents`、`subagent_mode`、`max_subagents`、`subagent_workspace`、`subagent_role_template_dirs`、`subagent_debug_trace_level`、`acceptance_execute_tests` 和 `acceptance_test_timeout_seconds`。`subagent_mode="trusted_local_hardening"` 表示本地默认少卡流程、多保证干活；角色模板和调度策略继续自动判断工具、QA 拓扑和上下文，不再把 `subagent_allowed_tools`、`subagent_context_budget`、runner 超时、动态超时等微参数暴露给普通用户。旧字段在代码层继续兼容读取，主要服务测试、迁移和高级调试。
- `agent_py_agent/config/capability_config.yaml`：默认只保留能力路由开关和任务内授权过期两项。子代理不会因为配置文件没列工具而变成“没手没脚”；工具和 skill 选择由任务包、模板、能力路由和 LLM 共同判断，细碎限制项只作为代码兼容默认值，不放进普通用户配置。
- Auto Policy v1 当前只实现 dry-run 审计，尚未接主配置；后续若做成用户可见主配置，配置默认值和中文说明必须同步写入 `agent_py_agent/config/agent_config.yaml` 与 `AgentConfig`。如果只是 subagent/capability 路由内部的授权、次数或 allowlist 细则，应进入 `agent_py_agent/config/capability_config.yaml` 与 `capability_config.py`，不要扩张主配置。
- `agent_py_agent/agent/subagents/manager_indexing.py`：实现 `_select_runs()` 等索引和过滤逻辑，同时提供公开别名 `select_runs()`、`index_task()` 等。
- `agent_py_agent/agent/subagents/manager_acceptance_findings.py`：实现验收发现逻辑，公开别名 `acceptance_findings()`。
- `agent_py_agent/agent/subagents/acceptance_review_service.py`：对单个任务做父级验收，生成分层 acceptance record，并执行只读 verifier checks。
- `agent_py_agent/agent/subagents/acceptance_helpers.py`：验收发现子模块的兼容导出入口，真实实现已拆到 `acceptance_helpers/` 目录。
- `agent_py_agent/agent/subagents/acceptance_helpers/evidence_acceptance_findings.py`：承接证据存在性和 read/write_file 工具证据 findings 构建逻辑，保持 `evidence.py` 作为薄兼容入口。
- `agent_py_agent/agent/subagents/manager_patch.py`：实现补丁操作，公开别名 `resolve_patch_target()`。
- `agent_py_agent/agent/subagents/services/patch_review_helper.py` / `patch_apply_helper.py`：保留 service 未初始化时的兼容 fallback；当前也支持 `PatchReviewTaskRequest` / `ApplyPatchTaskParams` bundle，避免 fallback 路径继续依赖散参。
- `agent_py_agent/agent/subagents/manager_runner_results.py`：负责 runner 结果写回、状态降级和 `output.json` / `runner_result.json` 持久化；结构化解析失败时会把最终结果统一降级成 `BLOCKED` / `ok=False`。解析层允许两类窄恢复：缺结束标记但 JSON object 完整；或成功态 JSON 尾部截断但前置 `evidence_packets` 已完整且带 `artifact_refs` / `evidence_refs`。没有可追溯 refs 的截断成功态仍会阻断。
- `agent_py_agent/agent/subagents/manager_runner_result_payload.py`：承接 runner result payload/status 构建 dataclass 和纯 helper，让 manager facade 保持短小。
- `agent_py_agent/agent/subagents/services/persistence.py`：负责 task/run/status report 落盘和旧任务兼容归一化，生成 `reports/status_report.json`。
- `agent_py_agent/agent/subagents/services/control_plane_projection.py`：把一次 subagent 保存同步成 LocalStore 控制面投影，写入 agent run、保存事件和 root task rollup。
- `agent_py_agent/agent/subagents/services/inheritance_manifest.py`：创建子代理时比较 parent/child 字段，生成 inherited / overridden / dropped 继承清单；只记录审计事实，不改变 child 运行字段。
- `agent_py_agent/agent/subagents/services/persistence_inheritance.py`：归一化并写入 `reports/inheritance_manifest.json`，让 persistence 主流程保持薄编排。
- `agent_py_agent/agent/subagents/services/failure_handoff.py`：根据失败/阻塞状态和 `failure_type` 生成失败交接记录，给后续接管代理留下警告、避坑建议和推荐下一步。
- `agent_py_agent/agent/subagents/services/persistence_failure_handoff.py`：归一化并写入 `reports/failure_handoff.json`，让 persistence 主流程只负责编排。
- `agent_py_agent/agent/subagents/services/persistence_recovery_outputs.py`：集中写 checkpoint artifacts、takeover readiness 和 task-local continue packet，避免 persistence 主保存流程重新靠近 code-size 风险。
- `agent_py_agent/agent/subagents/services/compact_continue_packet.py`：子代理保存闭环的写入层；每次保存会在 run workspace `compactions/session/` 下写 `latest_continue_packet.json` 并追加去重后的 `session_compact_ledger.jsonl`，父级后续重新 dispatch 同一 run 时通过 runner prompt 自动读取。
- `agent_py_agent/agent/subagents/services/session_progress.py`：子代理 runner 的 task-local 工具进度层；成功写文件后在 `agents/<run_id>/progress/` 写 `latest_tool_progress.json` 和 `tool_progress.jsonl`，让 continue packet 携带已写路径、标题、HTML 完整性小字段和下一步。HTML 未闭合时提示继续分块；HTML 完整时提示写 `output.json` / `SUBAGENT_RESULT` 收口；有 `placeholder_hash_link` 等明显问题时提示先修复再交父级验收。
- `agent_py_agent/agent/subagents/services/subagent_session_compact.py`：子代理本地 session compact package 写入层；当 runner 结果带 compact 信号时，只在当前 run 的 `compactions/session/` 下写 `latest_metadata.json`、`latest_summary.md`、package refs 和 ledger，不写主代理 `memory_archive/compact_applies`。
- `agent_py_agent/agent/subagents/services/takeover_readiness.py`：生成 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md` 接管前必读包；只整理 context bundle refs、checkpoint refs、manifest 元数据和读取顺序，不读取 artifact 正文；并提供 refs-only 的推荐读取顺序解析给 rescue/action apply 使用。
- `agent_py_agent/agent/subagents/services/persistence_security.py`：归一化 `SecuritySignal` 预留字段，让安全信号解析不挤进 persistence 主流程；当前不执行安全策略。
- `agent_py_agent/agent/subagents/services/persistence_identity.py`：归一化 `RuntimeIdentity` 预留字段，让员工/会话/配置 scope 解析不挤进 persistence 主流程；当前只保留审计元数据。
- `agent_py_agent/agent/subagents/services/checkpoint_artifacts.py`：从 task facts 和 `output.json` 构建 compact 可读恢复包，包含 checkpoint、decision ledger、progress、failing tests 和 next actions。
- `agent_py_agent/agent/subagents/services/task_workspace_adapter.py`：把 runtime memory task workspace 路径同步回 `SubAgentTask`，避免 persistence 保存函数继续膨胀。
- `agent_py_agent/agent/memory_archive/task_workspace.py`：subagent 保存路径调用的 runtime memory adapter；创建 `tasks/<root_id>/` task workspace skeleton 和 `agents/<run_id>/legacy_run_ref.json`，但不移动旧工单目录。
- `agent_py_agent/agent/memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 agent run workspace skeleton；旧工单目录仍是兼容读写面，run workspace 先承接恢复、接管、finding 和 compact 链的后续入口。
- `agent_py_agent/agent/memory_archive/compact_subagent_owner.py`：给 `memory-resume --from-compact` 提供子代理 owner 只读引用解析；`subagent_run` / `subagent_session` 会指向 task-local run workspace、legacy adapter refs 和 session compact hook 路径；当 `latest_continue_packet.json` 已存在时，父级能看到 `continue_packet_ready=true`，但不写主 memory、不自动执行工具。
- `agent_py_agent/agent/agent_core/subagent_compact_continuation.py`：给 runner prompt 生成 `Task-Local Compact Continuation` 小节；只从当前子代理 run workspace 读取 bounded checkpoint/summary/task/findings/latest continue packet 摘要，不读取主代理长期记忆，不自动执行工具。
- `agent_py_agent/agent/memory_archive/daily_ledger.py`：追加 `daily/YYYY-MM-DD/events.jsonl`，只写 task/run 状态摘要、duration、artifact/evidence refs 和 workspace 路径引用。
- `agent_py_agent/agent/memory_archive/artifact_registry.py`：把 `artifact_refs` 写成 task/run `artifacts/manifest.jsonl`，记录摘要、hash、路径、size、exists 和 `resolution_status`；只读取 legacy task dir、task workspace、agent run workspace 和该 run 的 `allowed_write_roots` 内的文件，越界路径只登记 blocked，不复制正文。
- `agent_py_agent/agent/memory_archive/artifact_reader.py`：显式读取 tool-output artifact 时仍以 `tool_outputs/index.jsonl` 为唯一事实源；如果模型把路径前缀抄错，但 artifact 文件名在 index 中唯一，会修复到登记记录，再做目录边界和 sha256 校验。
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
- `agent_py_agent/agent/agent_core/tool_context_reducer.py` / `tool_context_orchestration_summary.py`：控制工具结果进入下一轮 live prompt 的形态；大输出只注入 artifact 摘要和 checkpoint refs，调度类大输出额外保留 next_action/run refs/suggested tool call，不鼓励父级读回完整正文。
- `agent_py_agent/agent/agent_core/orchestration_body_read_guard.py`：父级/上层代理已有 child 时，默认保持 refs-only，不读业务产物正文；但 task-local 运行元数据（如当前 runner 的 `output.json`、`runner_result.json`、`task.json`）允许读取，用于收口、接管和恢复。用户明确要求“你自己/主代理亲自验收”或 acceptor 已完成后，才放开业务正文读取。
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
- `agent_py_agent/agent/subagents/services/rescue_policy.py`：根据 due-check issue 给 action plan 添加 rescue/escalation 元数据，保持建议可审计但不自动越权执行；当 task 目录存在 `reports/takeover_readiness.json` 时，会优先把该包和推荐读取 refs 放入 `rescue_context_refs`，并生成 refs-only 的 `rescue_packet`。父超时残留子任务会把 `unfinished_child:<child_id>:<status>` 作为结构化 ref 写入 action plan，避免后续自动化解析自然语言。
- `agent_py_agent/agent/subagents/rendering_rescue.py`：渲染 rescue packet 的 retry / manual confirmation / recovery refs 摘要，避免主 `rendering.py` 继续接近 code-size 风险线。
- `agent_py_agent/agent/subagents/result_structured.py` / `result_structured_evidence.py` / `coverage_records.py`：解析 runner structured output 中的 capability requests、tools、artifacts、tests、blockers 和 `coverage_records`，并写回任务事实；evidence、evidence_packets 和 findings 的解析写入由 `result_structured_evidence.py` 承接。`coverage_records` 是 `covered_run_id -> covered_by_run_id` 的机器关系，父级不能只靠 summary 说“另一个 run 已覆盖”；final closeout 和 descendant health 只有在 covering run 已 `DONE/VERIFIED` 时才认这条关系。`result_artifact_evidence.py` 承接 artifact refs 合并和 refs-only evidence packet 合成；`result_artifact_roots.py` 统一 normalize 与 integrity 的有界产物查找根，包含 run-local roots 和从 `data/subagents/<run>` 推导出的任务工作区根；若真实模型只给 artifacts 而漏写 `evidence_packets`，会从 artifact refs 合成证据包，避免正确产物因为缺证据包被父级误拒。
- `agent_py_agent/agent/subagents/capability_route_service.py`：承接 capability route record 构建、gap 包装、summary 和报告落盘。
- `agent_py_agent/agent/subagents/services/indexing_records.py`：承接 LocalStore dataclass record 索引 helper，让 indexing service 只保留编排入口。
- `agent_py_agent/agent/subagents/policies.py` / `policy_checks.py`：负责 due-check 风险规则和下一步建议命令；用户可见命令统一使用 `my-agent` 控制台入口。父超时子任务残留会建议 `recover_child_after_parent_timeout`，命令入口指向 `subagents-recovery-tree <root> --hide-healthy`，保持 refs-only。
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
9. Acceptance Real Execution 当前提供 `TestExecutionRecord`、最小 `TestExecutor`、tests 工作目录预处理、report 存储、显式 acceptance 接入、机器证据链兜底、`subagents-tests` CLI、`subagents-acceptance-plan` CLI 和配置默认值；只有 `AcceptanceReviewOptions(execute_tests=True)`、`subagents-tests --re-run`、`subagents-acceptance --execute-tests` 或配置 `acceptance_execute_tests: true` 时才运行 tests 并生成报告/阻断 findings，默认旧验收路径和 acceptance-plan 都不执行命令；已有通过的 `test_execution.json` 会作为后续 acceptance/apply 的测试事实源，并可在没有 worker evidence packet 时作为机器证据链。
9. Parent Acceptance Controller 的 `--apply` 当前只是安全桥接：只有 `inspect_only` 才能进入普通 acceptance apply；`execute_tests`、`review_patches`、`request_human`、`rescue` 会被写入 `parent_acceptance_apply.json` 并保持任务状态不变，留给上级/自动调度器下一步显式处理。空测试报告不会自动等同失败：如果没有可执行 tests，但 task/output 已有 refs-only artifact/evidence 证据链，会进入 `inspect_only` 让父级继续验收。
9. Parent Acceptance Controller 的 `--next-action` 是自动调度前的建议层：它读取当前 plan 和已有 apply 审计 refs，返回下一步建议命令或人工/救援意图；测试通过但 patch 未审核时会先建议 `subagents-patches --review-apply --run-id <run_id>`，不会直接跳到 acceptance apply。
9. Parent Acceptance Auto Policy v1 dry-run 当前消费 next-action、决策/apply refs 和保守 allowlist。第一片只判断“策略是否允许、如果允许会执行什么、为什么仍不执行”，写 `parent_acceptance_auto_policy.json`；半自动计划只暴露 `execution_mode=manual_only`、`automatic_execution_allowed=false`、`recommended_command` 和 preflight 检查，不运行 tests、不 apply、不 rescue，也不改 task/run 状态。
9. Parent Acceptance Auto Execution facade v1 当前消费 auto-policy 输出并写 `parent_acceptance_auto_execution.json`；默认 dry-run 固定 `execution_allowed=false`、`guard_status=blocked`、`executed=false`、`mutates_task_state=false`。只有显式 `ParentAcceptanceAutoExecutionOptions(execute_tests=True)` / `--execute-auto-tests` 才允许执行 run_tests 并写 `test_execution.json`，执行前会归一化安全 cwd 包装，仍不 apply、不 rescue、不修改 task 状态。
9. Parent Acceptance Auto Follow-up 当前只在显式测试执行后写 `parent_acceptance_auto_followup.json`：测试通过且 patch 已满足 gate 时给 `ready_for_manual_apply`，测试通过但 patch 未审核时给 `needs_patch_review`，测试失败时给 `needs_manual_rescue` 和失败测试摘要，危险或缺事实时保持人工确认/继续测试。follow-up 自身不读取 artifact 正文、不自动 apply、不自动 rescue。
9. Parent Acceptance Follow-up Control 当前把 follow-up 变成半自动受控入口：`--followup` 只展示下一步和 refs；`--apply-followup` 必须由人或上级调度显式调用。测试通过时它只走已有 `inspect_only` 父级验收 apply；测试失败或当前任务已失败/阻塞时必须带 `--take-over-by`，并复用 action handler 的 `takeover_or_reassign` 门禁和审计。失败验收会记录 `acceptance_rejected`，不会被标成 ok。若 `subagents-tests --re-run` 已经写出当前 `test_execution.json` 但 follow-up 文件缺失，manager 会从现有测试报告补一个 refs-only follow-up；若 runner 已经失败/阻塞且没有测试报告，manager 会生成只读 rescue follow-up，不重新执行 tests。
9. `subagents-dispatch` 现在会在 acceptance 记录上附带 parent auto-policy 的 machine summary、auto-execution 摘要和 follow-up ref；默认不会因为 `would_execute=true` 自动执行任何动作。只有 `DispatchParams/WatchParams(execute_acceptance_tests=True)` 或 CLI `--execute-acceptance-tests` 会受控执行 run_tests；本轮 tests 写入后会刷新 acceptance dry-run 展示，避免报告继续显示测试前旧结论。顶层 dispatch/watch 仍不 apply、不 rescue、不修改 task 状态；runner-context dispatch 在 `apply=True`、`execute_acceptance_tests=True` 且 `auto_apply_acceptance_followup=True` 时，可把已经测试通过且 follow-up 安全的 direct child 收口为 `DONE/VERIFIED`。dispatch/watch 遇到 stale coordinator 或失败父节点时只额外写 `leadership_recovery_plan` refs-only 记录，不自动调用 leadership apply。
10. Compact resume 的 continue packet 可以携带 subagent owner refs 和 acceptance/test 线索，但它只证明“恢复上下文可继续”，不证明“子代理业务验收通过”。父级验收和 auto-policy 仍是 tests/apply/rescue 的唯一判断层，compact auto 不得绕过。
10. compact/resume 与父级验收的联调链路是：continue packet ready -> parent acceptance 发现缺 `test_execution.json` 并阻断为 `execute_tests` -> 显式测试执行写报告 -> parent acceptance 变为 `inspect_only` -> 显式 apply 才能写 `DONE/VERIFIED`。任一步都不允许 compact auto 直接跑 tests、apply 或导出子代理 memory。
10. due-check 把 blocked、timeout、stale heartbeat、capability request/gap 等问题转成 action plan，并附带 rescue/escalation 元数据。
10. persistence 同步 `tasks/<root_id>/state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/`、`artifacts/` 和 `agents/<run_id>/legacy_run_ref.json`，为后续正式 agent run workspace 做兼容桥。
11. persistence 同步 `tasks/<root_id>/agents/<run_id>/agent.yaml`、run `state.json`、run `timeline.jsonl`、`task.md`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl` 和 inbox/outbox/artifacts/compactions 目录，先形成 agent run workspace skeleton。
12. persistence 追加 `daily/YYYY-MM-DD/events.jsonl`，让主代理先按天查 task/run/event/artifact refs，再回到 task/run 文件核实。
13. persistence 写 `artifacts/manifest.jsonl`，让父级先看摘要、hash、path、exists 和 resolution status；只有 workspace 边界内的 artifact 才会被读取正文或计算 hash。
14. persistence 追加 `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并把 run `checkpoint.json` 指向最新 compact refs；当前不会删除 timeline、artifact 或旧 work-order 文件。
15. persistence 同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享结构化任务事实；messages 追加，findings/evidence 按 id 合并，避免互相覆盖，但不写入主 memory。主动通信时，`subagent_message` 会把少量不同消息写进目标 run 的 `inbox/` 和 sender `outbox/`，把大量统一通知写进 shared messages/blackboard；广播必须带 `scope_root_run_id`，下级只应响应自己祖先作用域内的通知。
16. persistence 同步 `memory_gate/candidates.jsonl`、`review_queue.jsonl` 和 `skill_spark_gate.json`，把 lesson/finding 作为候选排队；`subagents-memory-gate` 可把 reviewer decision 追加到 `decisions.jsonl`。
17. create_run 如果看到 `parent_id`，会生成 task-local inheritance manifest，记录 child 当前字段相对 parent 的 inherited / overridden / dropped 项；manifest 是审计事实，不会把 parent 全量上下文灌入 child prompt。
18. persistence 对失败、错误、超时、阻塞或带 `failure_type` 的 task 生成 `reports/failure_handoff.json`；它记录 warning、risk level、last safe checkpoint、artifact/evidence refs、avoid-next-time 和 recommended next action，但不触发自动 rescue。
19. persistence 生成 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md`，把 failure handoff、checkpoint、status report、artifact manifest、evidence refs 和 artifact refs 排成 recommended read order；它是恢复索引，不复制或读取大 artifact 正文。
20. persistence 把当前 task 投影到 LocalStore 的 `agent_runs` / `agent_events` / `task_rollups`；上级代理或接管代理可以先用 `AgentRuntimeQueryContext` 查 root tree、own subtree、blocked runs 或 takeover candidates，再回到 task/run workspace 核实事实。`takeover_candidates` 是恢复候选视图，包含阻塞、失败、错误和超时；`blocked_runs` 保持只看阻塞。
20. persistence 保存旧 task 快照时会把磁盘已有 `child_ids` 合并回来，避免父/子任务在并行派生后被旧对象保存覆盖层级链接。
20. task 如果携带 `security_signals` 或 `security_review_required`，persistence 会随 `task.json` 保存这些 audit-only 字段；它们只记录可疑信号和证据引用，不自动阻断 runner、改变工具授权或修改 memory。
21. 控制面投影的 metadata 会暴露 `inheritance_manifest_ref`、`failure_handoff_ref`、`takeover_readiness_ref`、`security_signal_count`、`security_signal_types` 和 `security_review_required` 这类恢复/安全线索；这些 refs 指向任务目录里的事实文件，不替代 artifact、checkpoint 或 verified finding。
22. 如果 task 携带 `RuntimeIdentity`，控制面 metadata 会额外暴露 `runtime_identity`、`memory_scope` 和 `config_scope`。默认 `conversation_memory_policy=not_enabled`、`writes_global_config=false`；员工/外部会话只能形成可审计 run/conversation 作用域元数据，不能自动生成员工长期记忆或污染全局配置。
23. `query_shared_progress_panel()` 在 runtime query 之上返回面板状态包，包含 rollup、可见 runs、blocked runs、inheritance refs、failure handoff refs 和 takeover readiness refs；它只做投影汇总，不读取 artifact 正文或替代 verified facts。
24. `status --json`、人类 `status` 和 `subagents` 看板通过 `cli/shared_progress.py` 展示共享进度摘要；展示层按 root task 查询控制面，不扫描旧工单目录正文。接管视图可以显示 principal、conversation、memory namespace 和 config scope 摘要；`Acceptance Plan` 只调用父级 dry-run planner 生成决策摘要，不执行 tests、不写 task；`Acceptance Next Action` 只调用父级 next-action planner 生成 action、reason、command、refs 和 `mutates_task_state` 摘要，不执行命令、不写 task。需要审计落盘时必须显式调用 `subagents-acceptance-plan --write`；需要尝试写回状态时必须显式调用 `subagents-acceptance-plan --apply`，且当前只放行 `inspect_only`；需要单独查看下一步建议时可调用 `--next-action`。
24. runtime tool loop 对大工具输出先调用 `tool_output_failsafe.py` 写 recovery snapshot，再调用 `tool_output_externalizer.py` 写 artifact；snapshot 里只有摘要 metadata，完整输出不会进入 snapshot。
25. `tool_context_reducer.py` 根据 archive record 决定下一轮 prompt 内容：小输出保留原工具结果，大输出只保留 preview、artifact path、hash、size 和 fail-safe checkpoint path；调度类输出走 `tool_context_orchestration_summary.py`，保留可执行摘要而不是让父级读 artifact 正文。
26. action plan 的 `rescue_context_refs` 和 takeover apply 的 `evidence_paths` 会把 `takeover_readiness.json` 放在首位，再按 packet 的 recommended read order 展开 failure handoff、checkpoint、artifact manifest 等 refs；这些路径是恢复索引，不代表自动读取正文或自动接管。
27. action plan / apply record 的 `rescue_packet` 会记录 `dedupe_key`、`issue_kinds`、`repeat_count`、`retry_policy.max_attempts`、`escalation.target`、`manual_confirmation.required` 和 `recovery_entrypoints`；`auto_retry=false`、`auto_execute=false`、`reads_artifact_bodies=false` 是当前安全边界。
28. 只有显式 review/gate 通过且再触发 `--export-memory` 或 `--export-skill` 后，候选才允许进入长期 memory 或 skill draft 流程；approve 不会自动导出。
29. retention 只从 active review queue 移除 rejected / already exported 候选，候选、decision、export 和 verifier 文件仍留在 run workspace 里供接管和审计。
28. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源和 checkpoint artifacts，再由父级决定是否验收；这只是恢复入口推荐，不代表子代理写入主代理长期 memory。
29. workflow preview 仍可通过 CLI dry-run 展示；真实路径已接入 `create_run(... workflow_mode="plan|auto")` 和 `subagents-dispatch --apply --workflow-mode auto`，可把父任务上的 `workflow_plan` 物化为 worker 子工单。LOG 专项 apply path 和 runner 恢复 scenario 继续作为真实任务记录的先行验证样本。
30. 主节点单入口层级执行的外层控制器只启动 root。root/child/grandchild 通过 runner-context `schedule_child_subagents` 和 `dispatch_subagents` 向下反馈；外层不得直接启动或修复下层节点。创建类工具保持 one-shot 防重复，dispatch/board 属于 progress-loop 工具，可在同一父节点工具循环中重复调用。
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
- `review_patches`：只提示显式 patch review 命令；它会写 patch review 审计和 `review_status`，因此不进入当前自动 allowlist。
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
  "next_action": "run_tests|review_patches|request_human_confirmation|plan_rescue|apply_acceptance|none",
  "allowed_by_policy": false,
  "dry_run": true,
  "auto_execute": false,
  "would_execute": false,
  "execution_mode": "manual_only",
  "automatic_execution_allowed": false,
  "recommended_command": "subagents-tests <run_id> --re-run",
  "preflight_status": "manual_ready|blocked",
  "ready_for_manual_execution": true,
  "ready_for_automatic_execution": false,
  "preflight_checks": {},
  "preflight_blockers": ["automatic_execution_disabled"],
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

审计记录里的 `executed` 第一版必须恒为 false；`would_execute` 只表达策略判断，不代表动作已经发生。Markdown 或 CLI 视图只能展示摘要，机器判断必须读取 JSON。半自动字段只表达“下一条人工/受控调度参考命令”：`execution_mode` 第一版恒为 `manual_only`，`automatic_execution_allowed` 第一版恒为 false，`recommended_command` 不得被当前 policy 构建函数直接执行。preflight 可以显示 manual-ready，但 `ready_for_automatic_execution` 第一版仍必须为 false。

### Dispatch / Watch 接入

- `subagents-dispatch` 的 acceptance record 会带 `parent_acceptance_policy_ref`、`parent_acceptance_policy_decision`、`parent_acceptance_policy_action`、`parent_acceptance_policy_would_execute`、`parent_acceptance_policy_executed`、`parent_acceptance_policy_execution_mode`、`parent_acceptance_policy_automatic_execution_allowed`、`parent_acceptance_policy_recommended_command`、`parent_acceptance_policy_preflight_status`、`parent_acceptance_policy_ready_for_automatic_execution` 和 `parent_acceptance_policy_preflight_blockers`。
- `SUBAGENT_DISPATCH.md` 展示同一组摘要，方便人类快速判断下一步；完整事实源仍是 run-local `reports/parent_acceptance_auto_policy.json`。Markdown 展示 recommended command 不代表 dispatch 会执行。
- `subagents-dispatch` 的 acceptance record 也会带 `parent_acceptance_auto_execution_ref`、status、allowed、executed、guard status、blockers，以及显式执行时的 test ref/total/failed；完整事实源是 run-local `reports/parent_acceptance_auto_execution.json` 和 `reports/test_execution.json`。
- 显式测试执行会在同一轮 dispatch 内刷新 acceptance dry-run record，让 `SUBAGENT_DISPATCH.md`、`subagent_dispatch_report.json`、单 run `acceptance_review.json` 和 aggregate `subagent_acceptance_report.json` 展示测试后的 accept/reject 判断；刷新不等同于 `--apply-followup`。
- 显式测试执行后还会写 `parent_acceptance_auto_followup.json`，dispatch/watch 摘要只展示 follow-up status/action/command/reason/ref。测试通过且 patch 已审核时 command 会指向 `subagents-acceptance-plan <run_id> --apply-followup`；测试通过但 patch 未审核时会指向 `subagents-patches --review-apply --run-id <run_id>`；失败测试场景会额外提示 `--take-over-by <agent>`。它用于告诉上级“下一步人工 apply、patch review 还是人工 rescue”，不代表调度器已经做了下一步。
- `subagents-dispatch --watch` 不单独执行 auto-policy。watch record 的 evidence 只指向 `subagent_dispatch_report.json` 和 `SUBAGENT_DISPATCH.md`，由调用方沿 ref 读取具体 run 的 policy audit；若 watch params 显式打开 `execute_acceptance_tests`，本轮 dispatch report 会记录 tests_executed，但 task 仍等待后续显式 apply。
- 默认接入仍是 refs-only dry-run：`would_execute=true` 只说明未来可考虑执行，`executed=false` 仍是硬边界；显式测试执行只证明 tests report 已刷新，不代表验收已经 apply。

### 安全预留

后续接入真实执行前，Auto Policy 必须先接入这些闸门：
- `requires_human`：人工确认优先级高于 allowlist。
- rescue / takeover：只能从 failure handoff、takeover readiness 和 rescue packet 读取 refs，不能自动读取大 artifact 正文。
- 安全信号：`SecuritySignal` 和 `security_review_required` 先作为硬阻断输入，再考虑细分 severity。
- 租户/员工 conversation 配置隔离：员工会话里的策略测试只能落在 conversation/run scope；提升到 project、tenant 或 global 必须有 admin approval、diff、audit event 和 rollback ref。

## 2026-05-06 structure update
- 中文说明：workflow 路由、manager 内部、patch 归一化和 service action 的结构已拆开；兼容模块仍保留旧导出名，避免调用方被迫一次性迁移。
- Workflow routing and subagent manager internals now separate decision fields, rendering sections, patch normalization, and service actions.
- Compatibility modules still re-export the existing public model and rendering names for callers.

## 2026-05-07 bundle structure update
- 中文说明：manager/core/runner/recovery 路径现在先收敛到明确的 Params/Options bundle，再进入服务逻辑。旧字段只作为兼容 adapter，不能再作为新功能扩展点。
- `manager_base.py`, `manager_dispatch.py`, `manager_actions.py`, `manager_indexing.py`, and patch facades now list old compatibility fields explicitly and immediately construct `CreateRunParams`, dispatch params, `ActionApplyOptions`, `LocalRecordParams`, or patch request bundles.
- `policy_checks.py` / `policies.py` use `RunnerNextActionParams`; `state_machine.py` uses `StateTransitionParams`; runner output payload construction passes those bundles instead of loose fields.
- `agent_core` dispatch/watch/run-subagent/recovery paths now normalize into `DispatchParams`, `WatchParams`, `DispatchLoopParams`, `SubagentRunParams`, and `RecoverySnapshotParams` before entering service logic.
- The remaining subagent params cleanup now routes action post-recording, report/dataclass indexing, learning candidate updates, due-check issue creation, patch review fallback, and patch spec validation through `RecordAfterTaskActionParams`, `IndexReportParams`, `DataclassRecordIndexParams`, `UpdateLearningCandidateParams`, `DueIssueSpec`, `PatchReviewTaskRequest`, and `PatchSpecFields`.

## 2026-05-07 hard/soft structure update
- 中文说明：patch/action/acceptance 的内部细节继续从主 mixin/facade 拆出去，公开 manager 方法仍走原服务门面。workflow 的 route、compile、parent acceptance 也保持分层，worker 自报不能直接成为验收来源。
- Patch manager parsing details stay outside the main mixin path, preserving `SubAgentPatchMixin` as a facade over patch review/apply services.
- Action application split unsupported-action record creation and handler context construction into helpers while keeping `ActionApplyOptions` as the service bundle.
- `subagents/rendering.py` keeps acceptance record section and failed-summary formatting behind small shared helpers so verifier checks and findings use one rendering path.
- Patch/action/acceptance service internals now keep record construction and evidence item builders in focused helper modules; public manager methods continue to delegate through the existing service facade.
- The subagent slice contributes no strict code-size hard or soft findings after this cleanup; near-soft warnings remain visible for future pre-feature refactors.
- `subagent_workflows` keeps routing, dispatch-plan compilation, and parent acceptance planning as separate helper surfaces; compatibility callers can still ask for one planning result, but new fields enter through route/compile/acceptance bundles.

## 2026-05-07 annotation structure update
- 中文说明：结构文档把代码里的双层注释纳入架构同步要求。新增文件、服务、bundle 或 facade 方法时，要同时更新本结构页和 in-code 注释。
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

## 2026-05-08 parent acceptance CI fix structure update
- 新增或修改的 subagent/CLI 文件必须继续使用半角 `模块用途:`、`函数用途:`、`类用途:` 注释标签，避免 annotation coverage 在 CI 中漏检失败。
- `TestExecutor` 仍只执行 allowlist 内命令且不使用 shell；当环境没有 `python` 可执行文件时，会把首个 `python` 参数替换为当前解释器路径，保证本地和 CI 的真实验收 smoke test 行为一致。

## 2026-05-09 debug trace and tool alias structure update
- `agent_py_agent/agent/subagents/debug_trace.py` 是正式调试追踪写入层；`subagent_debug_trace_level=0` 完全静默，开启后只写内部 `debug_traces/subagent_trace.jsonl`，事件必须 refs-only、bounded，不复制 prompt/response/artifact/tool output 正文。
- `subagent_debug_trace_level=4` 会在 JSONL 里追加短 prompt/response/tool 预览和当前 task goal，便于 `tail -f` 实时观察；`level=5` 会把完整 prompt、response、tool payload 和 tool output 写到内部 `debug_traces/details/<run_id>/` 并在 JSONL 里留下 ref。该等级只用于真实 E2E 排障，不污染用户产物目录，默认 0 不写。
- `SubAgentManagerInitParams.debug_trace_level` 从 `AgentConfig.subagent_debug_trace_level` 传入 manager；创建任务和 runner 收束分别写 `task_created`、`runner_result_recorded`，但只在 trace level 允许时写入。
- `agent_py_agent/agent/subagents/debug_trace_reports.py` 承接报告类 trace：level 3 会记录 due-check、action-plan、hierarchy recovery、dispatch 和 dispatch-watch 的 counts、summary、candidate/action/issue ids；它不读取报告 refs 指向的正文，也不调用模型或命令。
- `agent_py_agent/agent/agent_core/runner_stage_trace.py` 把 core tool loop 里的模型/工具阶段接入 subagent debug trace；level 3 会记录 `runner_model_request_started`、`runner_model_response_received`、`runner_model_request_failed`、`runner_tool_call_started`、`runner_tool_call_finished`。这些事件只写 prompt/response 长度、backend、工具名、payload keys、ok 和输出长度，不写 prompt、response 或工具输出正文。
- Trace 第二片把 `hierarchy_schedule_result`、`parent_acceptance_decision` 和 `parent_acceptance_next_action` 纳入 level 2。它们只记录 parent/run/root/depth/status、decision/action、human gate、命令短预览、refs 和计数，不执行命令、不读取正文。
- `SubAgentHierarchyScheduler` 会在持久化 child/leaf run 前把模型常见工具别名归一成真实工具名，例如 `write -> write_file`、`read -> read_file`、`list -> list_files`；leaf 写文件任务仍会补齐安全文件工具包，coordinator 会保留报告写入工具用于计划/证据/协调记录，最终业务产物仍由 worker/writer 和写入边界控制。
- `parent_acceptance_controller` 会把空 command 测试项识别为不可执行占位检查，写入 refs 摘要 `ignored_empty_command_tests=N`，但不会因此触发人工确认或测试执行。真正有命令的测试仍走 allowlist / shell 字符安全预检。
- 这条结构边界服务真实 E2E：测试期可以开 1-5 级观察多层代理行为，正常用户运行保持 0；后续新增 trace 事件必须同步测试和 findings 台账。

## 2026-05-11 controlled tools structure update
- `model_capabilities.py` 继续是能力申请、授权和缺口的唯一数据合同；新增 shell/MCP/tool/skill/path/network/output budget 字段必须先进入这些 dataclass，再由 service bundle 承接。
- `services/lifecycle.py` 的 `RecordCapabilityRequestParams`、`RecordCapabilityGrantParams`、`RecordCapabilityGapParams` 是产品接口入口；后续新增能力字段不能通过散乱 `**kwargs` 进入 manager。
- `result_structured.py` 负责把 runner 结构化输出里的 capability request 扩展字段规范成 dataclass；它只保留 bounded JSON-like scope/budget，不执行任何工具。
- `services/persistence.py` 读取嵌套 capability 记录时会过滤未知字段，给 reserved/schema v2 留升级空间；过滤只发生在读取边界，不会吞掉当前模型已声明字段。
- `services/lifecycle_capability_records.py` 承接 capability request/grant/gap 构造逻辑，让 `SubAgentLifecycleService` 保持读写编排职责，不随字段扩展继续变大。
- 第一阶段只补“表达和持久化能力需求”的结构层，尚未放开 shell 执行；真实执行必须继续走后续 shell gateway、trash、输出预算和审计层。
- `capability_scope.py` 是父级路由的 scope 投影层：从 request 提取 request snapshot、legacy constraints、command allowlist、gap attempted tools 和 escalation chain。`rm/rmdir/unlink` 这类删除请求不得进入 shell command allowlist，只能作为 request scope 审计事实交给 task trash 替代层处理。
- `capability_request_identity.py` 是能力申请去重层：用 capability/tool/skill/MCP/命令/path/network/output budget 生成 scope 签名，避免 `capability_request` 工具调用和结构化结果重复写入同一个 request；命令签名会抽取 base command，和 shell gateway 的检查口径保持一致。
- `manager_capabilities.py` 只负责把路由命中转成 `RecordCapabilityGrantParams`，不直接解释 shell 命令；`capability_route_service.py` 只负责 gap/dry-run/apply report 的 refs-only 记录。
- `CapabilityRouteRecord.request_scope` / `grant_scope` 只用于审计和展示，不触发执行；后续 shell gateway 必须重新检查 grant、cwd、路径、网络和输出预算。
- `shell_gateway.py` 是受控 shell 的策略入口。它提供 `ShellGatewayRequest`、`ShellGatewayDecision` 和 `plan_shell_command()`，执行层也必须复用这个策略层，不得绕过危险命令、cwd、网络和输出预算检查。`dry_run` 字段会跟随请求：计划阶段为 true，真实执行阶段为 false，避免模型把已执行命令误读成 dry-run。shell 操作符检查使用 `shlex` punctuation token，只阻断字符串命令里未引用的 `;|&<>` 等 shell 分隔符；argv list 命令不会按 shell 字符串扫描，因此 `["python3","-c","print(1); print('x' * 2000)"]` 这类 Python 语句分隔符不会被误挡，因为执行层使用 argv / `shell=False`。
- shell gateway 的 dry-run `allowed=True` 只代表“如果进入执行层，可以尝试执行”；它不表示已执行，也不允许子代理获得裸 shell。
- `controlled_exec_gateway.py` 是父级 grant 到 shell gateway 的桥接层。子代理 exec 请求必须先拿 `CapabilityGrant`，再由 `plan_controlled_exec()` 把 grant 中的 `command_allowlist`、`path_scope`、`network_scope` 和 `output_budget` 编译成 `ShellGatewayRequest`；模型不能通过工具参数自填 allowlist 来给自己授权。`controlled_exec_grant_refs()` 会接受 `shell` grant，也会接受显式包含 `controlled_exec` 且有 command/path scope 的 `tool` grant，用来兼容真实模型把 capability_type 写成 tool 的情况。删除类命令会返回 `use_task_trash`，不进入 shell 执行；grant refs 还会暴露 `delete_policy.mode=task_trash`，明确 rm/rmdir/unlink 不需要进入 shell allowlist，完成条件是 moved=true 和 trash_manifest_ref。
- 删除类命令提前绕过 shell 执行，但仍按命令 `cwd` 解析相对目标；source 只要位于父级 grant 的 `path_scope` 内，就会被移动到当前 run 的 task-local trash 并写 manifest。
- `hierarchy_scope_guards.py` 的四层合同 guard 只在父级明确写出 `4 层` / `4层` / `四层` / `孙孙` / `depth=3` / `great-grandchild` / `root ->` 等词时启用；启用后 depth<2 的节点不能直接创建 leaf/worker，必须继续创建 coordinator。写根漂移 guard 会忽略父级内部 task/run workspace 引用，避免把 `task_dir` 这类上下文路径误判成用户产物根漂移。
- `tooling/controlled_exec.py` 是 registry-aware 工具包装层。它只从 `write_boundary.controlled_exec_grants` 读取父级 grant；`apply=false` 返回 dry-run plan，`apply=true` 才调用 bounded shell execution 或 task trash，且模型常见的 `apply/execute/run/full` 字符串也会被识别为执行意图。删除类 dry-run 的 `allowed=false/action=use_task_trash` 是有效计划，工具结果会保持 ok，避免模型误以为还要申请裸 `rm/mv`；但最终验收仍要求真实执行 refs，dry-run/plan 不能当完成。
- 如果 runner 里出现多个等价 `controlled_exec` grant，工具会按执行边界去重后视为一个 grant；如果确实存在不同边界的多个 grant，仍要求模型显式传 `grant_id`，避免选错权限范围。
- `tooling/registry_tool_dispatch.py` 是 registry 已完成解析/授权/写边界后的最终执行分发层；普通工具走 `tool.execute()`，`controlled_exec` 这类需要 registry 注入上下文的工具在这里接入，避免 `registry_execution.py` 继续变厚。
- `manager_runner_context.py` / `context_bundle.py` 会把 shell grant 投影成 `controlled_exec_grants`，并同步放进 `write_boundary`；runner prompt 只看到 refs/scope，不读取大 artifact 正文。
- `hierarchy_context.py` 会把父级目标里的 required 文件、forbidden 文件、4层/depth/命名合同以及能力/工具/安全合同作为硬继承块传给下一层。能力合同当前覆盖 `controlled_exec`、`capability_request`、`requested_tools`、`requested_commands`、`path_scope`、`output_budget`、`task_trash`、`stdout_ref`、`audit_ref`、`trash_manifest_ref`、`grant`。这些字段不能在 root -> 子 -> 孙 -> 孙孙转述中缩水。
- `agent_core/runner_prompts.py` 的 capability request 模板必须覆盖 tool/skill/MCP/shell/path/network/output budget 字段；子代理缺能力时上抛结构化请求，不应该靠自然语言猜授权。
- `shell_gateway_execution.py` 提供第一版执行入口 `execute_shell_command()`；它先调用 `plan_shell_command()`，再用 `subprocess.Popen(..., shell=False)` 执行 argv，并用 `_read_limited()` 持续 drain stdout/stderr。
- `ShellGatewayExecutionResult` 只保存预览、字节数、截断标记和文件 refs；完整输出不会自动塞进模型上下文，调用方要显式读 refs。
- `task_trash.py` 是删除类动作的受控替代层：`ensure_task_trash()` 管目录，`move_to_task_trash()` 只做 workspace/task-local move 和 manifest 记录；shell gateway 仍阻断 `rm`。
- `fallback_report.py` 是父级保存兜底结果的最小写入层；它只写 task-local reports，不替代 runner 正常 artifact 写入，也不把报告内容提升进长期 memory。
- `tool_call_context_reducer.py` 同时保护两条 live prompt 入口：assistant 回复里的大工具调用会摘要，`_record_tool_call` 里的工具 payload 也会摘要；小 dict payload 也渲染为摘要行，避免模型把历史 dict 复制成新工具调用。完整正文只能留在目标 artifact、debug detail 或显式读取的外部文件里。
- `hierarchy_write_policy.py` 的 `requested_child_write_roots()` 会合并父级继承根、显式 `extra_write_roots` 和 child spec goal 中的产物路径；上层/协调/检查类节点也继承覆盖下级的写入根，便于验收、接管和救援。是否应该亲自写最终产物由角色职责、提示词和父级验收约束，不再用“没有写权限”来表达。
- `hierarchy_scope_guards.py` 的同父级重复领域去重只使用稳定业务域词；会过滤 generated run-id 片段、数字编号和 `grand/one/two` 泛词，避免把恢复树里的编号 checker siblings 当成重复业务 coordinator。goal 兜底提取领域词前会剥离绝对路径、文件名和 inherited parent blocks，并过滤 `users/claude/code/shop/tests/css/js` 等路径或脚手架词，避免多个不同 child 因共享 `/Users/.../my-claude-code/...` 被误判同域。
- `hierarchy_scope_guards.py` 的 child write-root drift guard 会在父级已有权威产物根时阻断 sibling 目录漂移；失败返回 `child_write_root_drift`，让上级重写 child goal 后再创建。
- `dispatch_runner_selection.py` 在 runner 候选执行前预检显式 `include_run_ids`。缺失或越界 id 会生成 `runner_selection/invalid_run_ids`，把 `valid_scope_run_ids` 和保守 `possible_corrections` 返回给父节点；调度器不自动改写 id，也不启动该轮 runner。
- `subagents/parsing.py` 的 runner/planner 结果块解析现在支持“缺结束标记但 JSON object 完整”的窄恢复；`parsing_partial.py` 只在成功态且已有可追溯 `evidence_packets` 时恢复截断尾部，避免把无证据长文本误当完成。
- `agent_core/orchestration_tools.py` 的 `create_subagents` 会在显式 root/coordinator 交付文件/网站但缺少产物写入根时拒绝创建，要求模型把 `extra_write_roots` 放在工具 JSON 顶层重试；这样 root 不会把内部 agent-run workspace 下的 `build` 误当成用户 deliverables 目录。
- `agent_core/tool_loop_completion.py` / `subagent_dispatch_closeout.py` 是工具轮后的确定性收口层：子代理 runner 仍优先通过自身 `output.json` 收口，顶层主代理在刚执行过 `dispatch_subagents` 且所有 run 已严格 `DONE/VERIFIED`，或旧失败/待验收 run 的目标文件已被后续 `DONE/VERIFIED` sibling 覆盖时，本地生成 refs-first 最终回答。它和 `subagent_board.completion_status` 共用目标 token 语义，避免看板与最终账本互相打架。
- `agent_core/orchestration_progress_payload.py` 给 runner-context `dispatch_subagents` 返回直接 child 进度摘要：有 PLANNING/RUNNING 时提示继续 dispatch，有 BLOCKED/FAILED/TIMEOUT 或最新 acceptance review 为 REJECT 时提示恢复；当所有直接 child 都已 `AWAITING_ACCEPTANCE` 或完成且没有 rejected acceptance 时，返回 `summarize_direct_children_refs`，要求上层只汇总 refs，不要反复读取子产物正文。
- `agent_core/dispatch_capability_followup.py` 是同轮能力申请闭环：runner 写出 OPEN `capability_request` 后，dispatch 先 route/grant，再只重跑一次相关 child。授权后续跑会补一段 refs-first 提示，要求读取 blockers、next_actions、artifact refs 和已授权工具继续同一个 run，不从头重做任务。
- `subagents/services/acceptance_evidence_findings.py` 的父级验收证据判断优先读取当前 runner attempt 的证据和证据包；历史失败证据仍保留在 `task.evidence` 里做审计，但不会在后续成功重试时继续把任务判成失败。
- `tooling/artifact_integrity.py` 的 `ArtifactIntegrityIssue` 携带 `count/examples`，HTML 假链接和缺失 hash target 会给出 bounded 示例；`subagents/services/session_progress.py` 会把这些示例写入 `latest_tool_progress.json.artifact_integrity.issues` 和 `next_action`，让 runner 修具体链接而不是猜抽象 code。
- `subagents/services/session_progress.py` 对大量同类 `placeholder_hash_link` 会追加批量修复策略，要求 runner 搜索全部 `href="#"` 并重写整段或整文件，避免真实模型一轮只替换一个链接拖慢收口。
- `subagents/services/session_progress.py` 会把 runner 内部 `task.output_json` 写入识别为 closeout，而不是新的业务产物进度；它保留上一条产品 `latest_written_path`、`artifact_integrity` 和 `next_action`，并追加 `closeout_written_path`，避免交作业文件盖掉未修完页面的问题。
- `agent_core/subagent_finalize_artifact_integrity.py` 在 success-like closeout 前会从 `latest_tool_progress.json` 恢复缺失的 product artifact ref，并把 HTML 的 `placeholder_hash_link` 和 `missing_hash_target` 提升为 closeout blocker；这些问题在写作进度里是 warning，但在“我要交给父级验收”时会导致真实按钮/链接失效，必须先进入修复链。
- `subagents/runner_result_rendering.py` 在 `RUNNER_RESULT.md` 里把 `artifact_integrity_failed` 渲染成父级可读的 `Parent Next Action`：父级/root 不直接写业务产物，而是创建 repair worker 读取 `output_json`、artifact refs 和失败码后继续修复，再重新 dispatch/验收。`runner_rendering.py` 只保留通用报告入口和 re-export，避免继续变胖。
- `agent_core/orchestration_repair_contract.py` 是 repair/execute/verify 的共享合同层。parent-acceptance repair 和 artifact-integrity repair 都通过它给 suggested tool call 附加 `repair_contract`、`required_read_paths`、`context_manifest` 和 `context_packs`，要求同一个 repair run 完成读取失败 refs、修复、必要执行、产物验证和 refs 汇报；这条边界学习 Codex 的 schema 化工具返回、OpenClaw 的 completion handoff 状态、Hermes 的 refs-only delegate handoff，避免 repair 链继续拆成“只修”“只执行”的漂移 child。
- `subagents/runner_rendering.py` 的 Context Packs 会 bounded 渲染 repair contract 的 schema/kind、same-run required actions 和 target artifact refs。完整合同仍在 execution context JSON，Markdown 只显示短字段，避免 runner 看不到关键产物路径或把大 JSON 塞满 prompt。
- `subagents/services/recovery_strategy.py` 是 packet-first 恢复决策入口：它只读取 `latest_continue_packet.json` 的小 JSON 和本地 checkpoint/summary refs，输出 `rerun_original_from_continue_packet`、`rerun_original_from_checkpoint`、`create_takeover_run_from_*`、`recover_coordinator_leadership` 或 `stop_no_progress_and_escalate`。它不读取 artifact 正文、不自动执行工具、不写主 memory。
- `subagents/services/task_attribute_reader.py` 是恢复/接管逻辑读取 task 字段的兼容层；缺字段、旧测试替身或 MagicMock 占位值会被当作空值，不会误判成真实 takeover、child_ids 或 compaction refs。
- `subagents/services/takeover_run.py` 是 dead runner 的幂等接管创建层：同一个 source run 只能有一个 takeover run；新 run 通过 `attributes.takeover_source_refs` 和 `allowed_write_roots` 带上旧 task_dir、agent_run_artifacts、task_workspace_artifacts、checkpoint、summary、latest_continue_packet 等 refs，旧 run 通过 `record_takeover()` 进入 `TAKEN_OVER`。
- `agent_core/orchestration_progress_payload.py` 会把 recovery strategy 放进 runner-context `direct_children.recovery_strategies`；单个 run 且适合续跑原 run 时，`suggested_tool_call` 才会带 `runner_instruction`，多个失败 run 只返回 run_ids 和 action counts，防止一条恢复提示误传给多个不同 child。
- `agent_core/orchestration_progress_payload.py` 的直接 child 下一步优先级是 recovery > parent-acceptance repair > QA repair > continue dispatch > QA wave > closeout。当同一个 child 同时有 `latest_continue_packet` 和 QA repair signal 时，payload 会设置 `repair_wave_deferred_by_recovery=true`，要求父级先续跑原 run。若父级验收或真实 tests 写出 `REJECT`，payload 会附带 `parent_acceptance_repair_advice`、`parent_acceptance_repair_run_ids` 和可复制的 repair child 建议，避免 root 从自然语言失败摘要里猜修复目标。
- `agent_core/dispatch_runner_batches.py` 对显式 packet/checkpoint 恢复有窄放行：只有 `include_run_ids` 和 `runner_instruction` 明确指向 `latest_continue_packet` / `checkpoint` 时，`BLOCKED/FAILED` 原 run 才能重新进入 runner 候选；`DONE`、`TAKEN_OVER`、已验证或通道断开的 run 不能被误重跑。
- `agent_core/tool_context_orchestration_summary.py` 在 dispatch 大输出被外置后会保留 `recovery_action_counts` 和 `recovery_strategy_preview`，让 root 不需要读取完整 artifact 就能看到 packet-first 恢复动作。
- `subagents/services/hierarchy_recovery.py` 的恢复树节点现在暴露 `recovery_action`、`continue_packet_ref`、`continue_packet_status` 和 `no_progress_fuse`，使 root/中间父级/接管者能在四层链路里看到中间 coordinator 是否应该 leader recovery，而不是重新从业务目标猜。
- `agent_core/orchestration_dispatch_scope.py` 给 runner-context dispatch 补默认恢复 owner：模型未传 `take_over_by` 时使用当前父级 run id；显式 leader id 仍优先。这个默认只解决“父级接管自己的孩子”场景，不给顶层普通聊天静默授权。
- `agent_core/dispatch_no_progress.py` 会在 dry-run 接管/领导权恢复预览里返回 `suggested_tool_call`，要求下一轮用 `apply=true`、`execute_runners=false`、`max_runners=0` 写回恢复状态；普通 classify-only 仍建议停止并汇报 blockers。
- `subagents/models.py` 的 `SubAgentDueCheckOptions` / `SubAgentPlanActionsOptions` 和 `services/action_options.py` 的 `ActionApplyOptions` 都带 `exclude_run_ids`。runner-context dispatch 会排除当前父级 run，避免 due-check 把正在执行的 root/coordinator 当作 stale child 接管。
- `agent_core/orchestration_body_read_guard.py` 的 refs-only 保护区明确区分“业务正文”和“恢复控制面”。委托期父级仍不能读 deliverables 正文，但可以读 task-local `latest_continue_packet.json`、`checkpoint.json`、`summary.md`、compact ledger、runner/result/status 等小 refs。
- `agent_core/orchestration_direct_write_guard.py` 不只看用户是否显式说“不要自己写”。当前 root 轮次一旦已经 create/dispatch 子代理，root/父级默认不能直接写业务产物；用户明确要求亲自修复/自己写时才放行。被派出的 active runner 仍保留基础写入能力。
- `manager_work_orders.py` 的初始 `output.json`、`status_report.json`、`dependencies.json` 必须由 dict template 写入，再交给 `_write_json_if_missing` 序列化一次；禁止重新引入“JSON 字符串包 JSON 对象”的双层编码。
- `subagents/utils.py::_read_json_object` 是子代理 JSON 事实源的容错读取入口，兼容历史双层编码但只返回 dict；新读取点应优先复用它，不要在业务层重复 `json.loads(...).get(...)`。
- Runner contract 和文件工具说明明确 `write_file` / `append_file` 会在授权 `allowed_write_roots` 内自动创建父目录；叶子节点不应因为目标产品目录尚不存在就上抛 shell/mkdir capability request。长 CSS/JS/HTML 或大段代码必须走 bounded content transport：`write_file` 写短骨架，`append_file` 分块追加，或在已有授权时走 `controlled_exec` 生成文件并返回 artifact/audit refs。
- `tooling/filesystem_write.py` 的 `replace_in_file` 支持 `count=0` 表示替换全部匹配；工具示例必须展示这个批量形态。`subagents/services/session_progress_integrity.py` 在大量同类 `href="#"` 残留时会把 `count=0` 写进 next_action，避免 runner 一轮只改一个重复链接。
- `hierarchy_scope_guards.py` 的四层链路 guard 只阻止真正 leaf/worker 跳层；如果 child spec 明确是 coordinator/lead/tester/reviewer/checker，即使 `agent_name` 含 `writer` 这类业务词，也不能被当成 leaf。
- `required_file_terms.py` 必须把“禁止改名（如 x.html）/例如/比如/禁止文件名：x.html”里的文件名作为 forbidden examples；这些名字不得进入 inherited required files，否则下级会同时收到“必须创建”和“禁止创建”的冲突合同。
- `hierarchy_leaf_targets.py` 的 leaf 产物去重只应该认定输出目标，不应该把“引入/引用/链接/导入/加载/use/include/import/link to”后面的共享资源当作当前 leaf 的 ownership claim；真实产物仍以显式目标路径和 `output.json.artifacts` 为准。
- `agent_core/_tool_loop_service.py` 的 one-shot 编排去重只在工具结果真正推进时登记；`schedule_child_subagents` 这类工具可能 `ok=True` 但 JSON 输出 `blocked=true`，这类语义阻断必须保留父级修正后重试的空间。
- `tooling/write_boundary.py` 把 task-local `output_json` 和 product_write_roots 分开处理：子代理可以写自己的 `execution_context.output_json` 收口，但不能在用户 deliverables/product roots 里创建内部 `output.json`，避免系统交接文件污染业务产物目录。
- `agent_core/runner_prompts.py` 必须持续提醒 runner/coordinator：`output.json` 是内部结果文件名，给 child goal 时不能要求写到产品目录；需要结构化汇报时引用 `execution_context.output_json`。
- `agent_core/runner_identity_prompt.py` 提供 `subagent_runner_system_prompt(context)`；runner 模型轮会通过 `PromptBuilder.build(system_prompt_override=...)` 注入子代理专属身份，而不是继承 root / 主代理的全局 `system_prompt`。这条边界只改本轮 prompt，不修改共享 config，避免测试 root prompt 或用户主代理身份污染子代理。
- `tooling/registry_execution.py` 对标准 `[TOOL_CALL]` 做窄恢复：如果模型漏写 `[/TOOL_CALL]` 但中间 JSON object 已完整，就直接恢复并执行；如果 JSON 真的截断，仍返回 parse error 和分块写入提示。
- `capability_status.py` 是 pending capability 状态的共享判断层；`policies.py` / `policy_checks.py` 把 pending capability/tool/skill/shell/MCP 状态统一映射为 `BLOCKED`。
- `parsing_capability_requests.py` 负责 capability-request 兜底：模型写出 `PENDING_CAPABILITY_REQUEST` / `NEEDS_TOOL` / `NEEDS_SHELL` 等状态但漏填 `capability_requests` 时，会只从结构化 `pending_steps`、summary 和 blocked_reason 里恢复一个保守请求，交给父级路由；它不会直接授权或执行。若文本里推不出具体工具或命令，不生成空泛 generic request，避免污染 runner context 和 grant 列表。
- `services/acceptance_findings.py` 的 `no_pending_structured_status` 会阻止 pending capability 输出进入父级验收，避免“还在申请工具”的 run 被误判完成。
- `services/acceptance_findings.py` 的 `required_child_spawned` 会在任务目标明确要求创建下级/depth child 时核对真实 `task.child_ids`，防止 coordinator 在没有真实 child run 的情况下只靠结果块伪造完成；leaf/self 任务里“真实创建 leaf_worker”这类自述不会误触发下级创建合同。
- `services/acceptance_descendant_health.py` 的 `descendant_health` 会在父级已有真实 `child_ids` 时扫描所有后代状态；未完成、未验收、失败、阻塞、缺失的后代都会阻断父级 `DONE/VERIFIED`，防止 root 在 QA 子代理仍失败或未跑完时假绿。
- `services/acceptance_controlled_exec_findings.py` 的 `controlled_exec_contract_satisfied` 会在目标声明 `controlled_exec` 时要求真实工具记录和 `stdout_ref` / `audit_ref` / `trash_manifest_ref`；它会从 `output.json`、task_dir 和 `allowed_write_roots` 下固定小型 refs/summary 文件中找证据，单文件 64KB 上限。若 refs 文件只写了明确的 `/memory_archive/artifacts/tool_outputs/controlled_exec-*.json` 小 artifact 引用，验收会精确读取这些受控工具 artifact 来补齐 stdout/audit refs；不会扫整个 artifact 目录，也不会读取大日志。

## 2026-05-14 parent planner control-plane structure update
- `agent_core/planner_templates.py` 定义 parent planner 专用 system prompt；planner 是控制面结构化调用，不继承 root/worker/subagent runner 身份。
- `agent_core/_subagent_planner_mixin.py` 调用 parent planner 时使用 `context_scope="control_plane"`、`resume_context=False` 和 `allowed_tools=[]`；State Snapshot 是唯一事实来源，planner 不再进入工具循环。
- `prompting_parts/builder.py` 和 `agent_core/runtime_loop_support.py` 把 `control_plane` 纳入隔离上下文；这类调用不会注入 owner memory、home files、配置 prompt files、memory routing 或 auto-resume。
- `subagents/parsing.py` 对 parent planner 结果做 schema-bound marker 恢复：误用 `[SUBAGENT_RESULT]` 但 payload 明确是 `decision/should_dispatch/actions` 的 parent planner JSON 时可以恢复；普通 runner `status/used_tools` 结果仍会被拒绝。
- 后续新增控制面 LLM 调用应优先复用这条结构：预先构造 bounded State Snapshot，tool-less 一次性返回结构化结果；需要文件核实时另建 worker/tool 阶段，不能让控制面自己长时间翻文件。

## 2026-05-15 subagent hardening structure update
- `config/agent_config.yaml` 只暴露少量子代理用户项；`AgentConfig` 仍保留旧字段以兼容已有调用和测试，但新功能不要再把调度微参数直接塞进默认用户配置。
- `settings/services/_normalize_runtime_fields.py` 继续归一旧字段，同时新增 `subagent_mode` 三档：`trusted_local_hardening`、`balanced`、`strict`。当前默认是本地硬化模式，目标是少卡流程、多保证子代理能完成工作。
- `subagents/role_contracts.py` 的角色契约改为“职责叠加”：显式工具、模板工具和 `ROLE_BASE_TOOLS` 合并去重。reporter/checker/tester/acceptor/researcher 等角色也有基础读写、web 取证和报告能力，最终能否验收仍由父级质量门决定。
- `subagents/manager_runner_context.py` 只会在 self-authorized root/coordinator/lead execution context 里移除 `capability_request`，因为这类 seed 没有上级授权者；主代理直接创建的一层 worker/researcher/writer 仍保留能力申请通道。
- `subagents/context_bundle.py` 新增 `task_packet` 字段，schema 为 `subagent_task_packet.v1`。它是 runner 和 takeover 优先读取的结构化工单，包含 role、goal、plan、acceptance、file_contract、write_contract、tool_contract、workspace_refs 和 reserved。
- `subagents/context_bundle.py` 也会写入 `task_envelope` 与 `tool_preflight`：前者是更完整的 TaskAddress/TaskEnvelope 协议包，后者是开工前工具/产物写入/controlled exec 授权预检。runner prompt 只显示短 issue codes，不展开大 JSON；preflight 只报告，不自动关工具或改状态。
- `subagents/context_bundle_semantic.py` 承接 Context Gate 的轻量语义校验：从 goal/plan/acceptance 重新提取明确文件名，确认 `output_contract.required_files` 与 `task_packet.file_contract.required_files` 没有缩水；它不读取 artifact 正文。
- `context_gate_prompt_lines()` 会明确告诉 runner 优先按 `context_bundle.task_packet` 执行，避免从自然语言摘要里重新猜路径、工具名或文件合同。
- `tooling/registry_execution.py` 现在对标准 JSON 工具块也做窄别名归一：工具名如 `write/read/list/search/append/replace` 会转成正式工具名；文件路径参数如 `file_path/filename/target_path` 会转成 `path`。同一参数别名冲突会返回 parse error，不会静默猜测。
- `docs/modules/subagent/09-hardening-migration.md` 是这轮迁移的总说明，包含参考项目经验、24 步迁移计划和 15 组测试矩阵。
- `agent_core/subagent_dispatch_closeout.py` 的最终收口把 `DONE/VERIFIED`、`TAKEN_OVER -> verified replacement`、以及“旧 run 目标文件已被后续 verified sibling 覆盖”都视为 resolved；旧 run 必须有精确 `takeover_by` 且接管者已验证，或有明确结构化 target token 被覆盖，才不会继续阻塞整棵树。
- `subagents/result_structured.py` 在结构化结果入口规范模型证据：`kind=content_check`、带 `content_pattern`、summary 明显表达“无/没有/不包含/absent/no”时，`ok=false` 代表坏模式未命中，会转成验收通过语义；普通失败仍保持失败。
- `subagents/execution_executor.py` / `execution_executor_helpers.py` 的 `content_check` 支持 `match_mode=not_contains`、`expect_absent`、`negate` 和 `should_not_contain`，用于父级生成的机器测试，不再靠自然语言猜正反。
- `subagents/static_site_validator.py` / `execution_static_site_items.py` 默认把自动推断的 HTML 验收提升为完整骨架检查，并输出 `html_structure_hits`、`repair_hints`；安全可选 DOM 绑定不再触发硬失败。
- `agent_core/orchestration_dispatch_scope.py` 允许真实执行 dispatch 时把模型常用的 `limit` / `runner_limit` 作为 `max_runners` 别名；dry-run/report-only 语义保持原样。
- `agent_core/runner_prompts.py` 的 real-runner prompt 只内联瘦身执行摘要；完整 execution context、context bundle、TaskEnvelope 和 tool preflight 通过 refs 读取，避免真实模型启动时被大 JSON 拖到超时。
- `agent_core/runner_prompt_context_summary.py` 会把 `context_manifest.required_read_paths` 拆成 `input_contract.resolved_read_paths` / `unresolved_read_paths`；runner prompt 明确要求先读真实存在的上游 artifact refs，某个自然语言别名不存在时不能立刻 BLOCKED。
- `agent_core/runner_prompt_contract_lines.py` 渲染 input/output 合同提示：下游必须优先读 resolved upstream paths，用户业务产物必须写到 `output_contract.required_file_refs`，agent-run 内部报告不能冒充交付物。
- `prompting_parts/builder.py` 在 `# Related Memory` 里把旧记忆标成“历史参考，不是当前任务指令”；当前 `# User Task`、当前工作区文件和最新工具结果拥有更高优先级。这个边界学习 OpenClaw/Hermes 的上下文分层，防止 daily memory 或旧子代理摘要把新任务改写成旧任务。
- `agent_core/orchestration_item_dependencies.py` 在 `create_subagents(items=...)` 阶段把自然语言流水线转成机器依赖：下游输入路径匹配上游输出路径、或 `dependencies/depends_on/workflow_depends_on` 显式标签匹配上游 agent 名/产物名时，会写入真实 `workflow_depends_on`。这使 runner selection 后续能通过上游 artifact refs 接续，不需要 root 自己翻文件接管。
- `subagents/context_bundle_contracts.py` 会从文件级 product write roots 推导 `required_files`，例如 `/.../furniture-home/index.html` 同时产生 `index.html` 和 `furniture-home/index.html`；内部 task/run workspace 文件不会进入用户产物合同。`output_contract` 必须区分用户产物 refs（`required_file_refs` / product `final_report_ref`）和内部 agent-run 交接 refs（`agent_run_final_report_ref`），内部报告不能冒充用户业务交付物。
- `agent_core/subagent_dispatch_closeout.py` 只把显式 tester/acceptor 角色话术视为必须创建质量子代理；普通“安排和验收 / 汇报验收结果”可由父级验收满足，避免自然语言被死流程带偏。
- `agent_core/orchestration_dispatch_scope.py` 对模型工具调用固定执行 live runner 的父级验收测试；CLI 直达 `DispatchParams` 仍可人工关闭测试。这个边界保证 LLM 不会误把 `execute_acceptance_tests=false` 当成跳过质量门。
