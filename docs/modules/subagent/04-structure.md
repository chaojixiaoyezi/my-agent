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
- Context Bundle v1 已接入执行上下文生成：`SubAgentTask` 会被压成实时工单包，写入旧 run 工单目录和 agent run workspace，runner prompt 只展示 gate 状态和 refs，不展开大型 artifact 正文。
- Context Gate v1 当前检查最小工单字段是否齐全；缺字段时要求 runner 返回 `BLOCKED` 和缺字段列表，后续可升级为调度前硬阻断。
- Context Bundle v1 现在带 `lineage`：记录 root、parent、depth、自己的 bundle ref 和直接父级 bundle ref；多层恢复时只沿 refs 读交接包，不把父级全文塞给子孙节点。
- `takeover_readiness.json`、`rescue_context_refs` 和 hierarchy recovery-tree 节点会暴露 context bundle refs；失败、阻塞、超时或父节点超时后，接管者可以先读当前/父级交接包，再读 checkpoint、status report、artifact manifest，仍不展开 artifact 正文、不自动执行接管。
# Subagent：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- subagent.py                         # 兼容入口
|-- subagents/                          # subagent 任务、manager、报告、runner、解析和渲染
|   |-- context_bundle.py               # 实时子代理工单包和 Context Gate
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
- `agent_py_agent/agent/subagents/services/hierarchy_scheduler.py`：定义 `HierarchyScheduleRequest` / `HierarchyChildSpec`，通过显式 bundle dry-run 或创建 child/grandchild run；默认不写任务，`apply=True` 时复用 `create_run`，并限制 `max_depth` / `max_children`。层级 child 会继承 bounded parent goal/thought；如果模型给下一层的 goal 太短且丢了父级写入根，scheduler 会把 `继承父级目标/边界` 追加进 child goal，并通过独立策略模块推断 leaf 写文件工具和最小验收兜底。模型把带 orchestration tools 的下一层误标成 `worker` 时按 depth 推断 coordinator role；模型把真实角色写在 `agent_name` 但 `role=child` 时，会先从 identity 中恢复 researcher/tester/acceptor/bug_finder/writer/worker 等角色。
- `agent_py_agent/agent/subagents/services/base.py`：`_extract_write_dirs()` 是从自然语言目标里提取本地写入根的统一入口；它会跳过 URL span，避免 `https://picsum.photos/...`、API 地址或图片地址被误当成本地授权目录。
- `agent_py_agent/agent/subagents/services/hierarchy_role_identity.py`：从 scheduler 拆出的角色恢复 helper；当模型把结构化 `role` 写成 `child` / `general` 但 `agent_name` 或 goal 已经写出真实角色时，先归一化成内置角色再进入写入根和工具策略。
- `agent_py_agent/agent/subagents/services/hierarchy_write_policy.py`：集中维护层级写入根策略，把“能写本地报告”和“能写最终业务产物”拆开；同时从父级 goal 中提取可委派产品路径，保证 coordinator 不拿写权限也能把路径交给下一层 leaf。
- `agent_py_agent/agent/subagents/services/hierarchy_acceptance.py`：从 scheduler 拆出的 acceptance fallback，模型漏传 `acceptance_checks` 时只派生最小稳定验收项。
- `agent_py_agent/agent/subagents/services/hierarchy_tool_policy.py`：从 scheduler 拆出的工具策略，集中处理 coordinator/leaf 工具继承、写文件工具补齐和工具名别名修正；coordinator 即使显式传了 allowed_tools，也会合并内置 coordinator 工具包，避免漏掉继续派下一层所需的编排工具。
- `agent_py_agent/agent/subagents/services/hierarchy_scope_guards.py`：承接层级调度的空计划、深度、数量、禁止 sibling 领域、同批混建 coordinator/leaf、child 写入根漂移和 domain mismatch 检查；scheduler 只消费阻断原因，避免调度主文件继续膨胀。`mixed_coordinator_leaf_children` 用于保护主 -> 子 -> 孙 -> 孙孙链路，要求每一层只创建自己的直接下一层。`child_write_root_drift` 用于阻断模型把父级权威产物根 invent 成 sibling 目录后继续落盘。`root_leaf_bypass_existing_coordinators` 用于 root 已有 coordinator children 后阻断直接 leaf/worker 创建，要求先 dispatch 或修复直接 coordinator children。`duplicate_leaf_target:<file>` 只在同父级 DONE/VERIFIED leaf 的具体 artifact 文件名重合时阻断，避免重复写同一页面，同时允许不同页面 sibling 并行。
- `agent_py_agent/agent/subagents/services/hierarchy_leaf_targets.py`：已验证 leaf 目标文件去重 helper；从 direct child 元数据和 `output.json.artifacts` 路径引用提取文件名 token，不读取产物正文。
- `agent_py_agent/agent/agent_core/orchestration_tools.py`：保留模型可调用 orchestration 工具的兼容入口和 create/board 薄封装；`DispatchSubagentsTool` 已拆到 `orchestration_dispatch_tool.py`，workflow mode 归一化拆到 `orchestration_workflow_mode.py`，看板 payload 整形拆到 `orchestration_board_payload.py`，避免单个工具入口继续膨胀。它继续暴露 `schedule_child_subagents`，只在当前 subagent runner 上下文中创建下一层 child；没有 active runner id 会拒绝。显式 root/coordinator seed 会忽略模型额外塞入的 shell/web 工具，只保留内置 coordinator 工具包，避免 root 因自然语言漂移拿到不该有的权限。`coordinator_seed_tools.py` 承接 root/coordinator seed 工具过滤，避免核心工具入口继续变胖。`orchestration_dispatch_scope.py` 集中维护 runner 内部 `dispatch_subagents` 的默认 parent scope、self-exclude、workflow-off 和 defer-acceptance 规则，避免外层绕过主节点或递归跑自己；runner-context 在 `apply=True` 且 `execute_acceptance_tests=True` 时会打开 `auto_apply_acceptance_followup`，让父 runner 在孩子测试通过后收口直接孩子，顶层 CLI/API 仍保持人工 apply。`dispatch_subagents` 支持 `run_ids` / `include_run_ids` 精确指定本轮要跑的 direct child，并按给定顺序过滤 runner 候选；`dispatch_runner_selection.py` 会先预检显式 id，缺失或越界时返回 `runner_selection/invalid_run_ids` 和可用 direct child ids，不启动错误 runner；dispatch payload 顶层会放 `runner_selection_recovery.valid_run_ids`，避免大 records 被外置后模型看不到纠偏指令。当一个批次包含多个 pending runner 时，共享 `runner_instruction` 会被忽略并写入 `ignore_multi_runner_instruction` 记录，避免一个孩子的专属提示污染其他分支。`subagent_board` payload 会先给 `actionable_run_ids`，再给截断后的 items，并把 `status=ALL/*/ANY` 归一成不过滤。`orchestration_progress_payload.py` 会在 runner-context dispatch 响应里附带 direct child status counts、PLANNING/RUNNING ids、BLOCKED/FAILED/TIMEOUT recovery ids、`needs_more_dispatch`、`needs_recovery`、`unfinished_run_ids`、`next_action` 和带 `run_ids` 的建议工具调用，帮助父节点继续推进限速未跑完的孩子或进入恢复路径。
- `agent_py_agent/agent/agent_core/tool_round_execution.py`：承接一轮工具调用执行、live context 回写和子代理 `output.json` 收口检测。当前 runner 成功写入自己的 `output.json` 后，会从该 JSON 合成 `SUBAGENT_RESULT` 并停止工具循环，避免完成态子代理继续请求模型。
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`：工具轮数到达 `max_tool_rounds` 后会先给模型一次收口机会；如果模型仍输出工具调用，系统改写为确定性停止说明，避免上层把 `[TOOL_CALL]` 当最终回答继续传播。该逻辑不放开工具执行，只保护收束边界。
- `agent_py_agent/agent/agent_core/tool_call_context_reducer.py`：大段 assistant tool-call payload 不再原样进入下一轮 live prompt；例如 `write_file(content=<large html>)` 会压成工具名、路径、字段大小、sha256 和短预览，完整正文留在目标文件或 debug detail ref。`_tool_loop_service.py` 的 live context marker 使用 `[tool-record ...]` / `[tool-output-record ...]` 这类中性标签，避免模型把历史记录复制成新的 `[TOOL_CALL]`。
- `spawn-subagents --role coordinator --agent-name <name>` 是真实层级 E2E 的 seed 入口：外层只创建一个 root/coordinator，自动授予 `schedule_child_subagents` / `dispatch_subagents` / `subagent_board`、读取工具和 task-local 报告写入工具；如果模型显式传入 `run_command` / `fetch_url` 等额外工具，会被收敛回 coordinator 工具包。`spawn_role_seed.py` 会把 goal 里的产物路径保留下来作为派工上下文，但显式 root/coordinator 不再从 goal 自动提取最终产物写入根；coordinator 可以写自己的计划、分工、证据和协调报告，但最终业务代码/页面/文档产物仍应派给 worker/writer，并受 task-local 写入边界和父级验收控制。后续子、孙、孙孙任务必须由上一层 runner 通过 orchestration tools 创建，外层不得直接替下层创建。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_payload.py`：承接 `dispatch_subagents` 工具输出里的单条 record payload，保持返回给 runner 的 acceptance/test/follow-up refs 精简且可测试；runner 真实创建了下级时，会透传 `runner_created_children`、child run ids 和 roles，避免父级把 dispatch 记录数误读成真实孩子数；run id 选择失败时还会把 `valid_run_ids` 放到顶层 recovery payload。
- `agent_py_agent/agent/subagents/services/hierarchy_recovery.py`：定义 `HierarchyRecoveryRequest` / `HierarchyRecoveryResult`，从 root run 只读扫描 child_ids 子树，返回需要恢复的后代、context bundle、父级 context bundle、takeover readiness、failure handoff 和 checkpoint refs；可按 capability timeout 阈值把 stale `RUNNING` 后代纳入恢复候选，不展开 artifact 正文、不自动接管。
- `agent_py_agent/agent/subagents/manager_hierarchy.py`：给 `SubAgentManager` 暴露 `schedule_child_runs(params=...)` 薄 facade，让层级创建入口保持单一且可测试。
- `agent_py_agent/agent/subagents/role_templates.py`：加载内置和用户外置 JSON 角色模板；模板必须是广义角色、带中文说明、可处理多个目标，坏模板只记录 issue，不影响内置模板。`role_template_index_text()` 给主代理常驻 prompt 只提供模板索引和位置；`role_template_detail_text()` 给普通 runner 只展开当前角色详情，给 coordinator runner 在派工时展开角色全集详情，避免不派工时加载全部派工细节。
- `agent_py_agent/agent/subagents/role_template_catalog/builtin/*.json`：内置角色模板，包括协调、执行、找茬、测试、验收、研究、写作。模板定义默认工具、是否可写、是否可验收、输出契约和中文 prompt。
- `coordinator` 角色模板把最终产物写入拒绝定义为正常边界：coordinator 不给自己申请产物目录写权限，也不让父代理代写，必须转派 worker/writer/leaf_worker。
- `agent_py_agent/agent/subagents/role_contracts.py`：集中定义 `reporter` / `checker` 兼容角色契约，并把 `bug_finder/tester/acceptor/coordinator/worker/researcher/writer` 接到 role template；检查型角色可以写 task-local 检查报告和证据摘要，但不能自验收、不能替 worker/writer 写最终业务产物，最终仍由父级 gate 裁决。
- `agent_py_agent/agent/agent_core/runner_dispatch.py`：runner 候选会先过滤可执行状态，再按角色阶段排序：coordinator/规划类优先，worker/产出类先于 tester/bug_finder/critic，acceptor 最后；同一轮 dispatch 只放行当前最低阶段候选，producer/coordinator 未完成前 QA/test/review/acceptance 会等下一轮，排序发生在 `max_runners` 截断前，避免真实业务链路出现“先验收再干活”。runner dispatch record 会保存内部创建的 child ids/roles/status counts；runner 超时但已创建 child 时标记 `runner_partial_success` 和未完成 child ids。
- `agent_py_agent/agent/agent_core/orchestration_tool_specs.py`：`create_subagents` / `schedule_child_subagents` 的工具说明只展示角色模板索引，不展开 `prompt_zh` 和默认工具细节；真正需要派工时由 runner prompt 加载详情。
- `agent_py_agent/agent/agent_core/runner_gate.py`：统一计算 runner 外层超时；默认用户配置 `runner_timeout_seconds: "off"` 表示不限制，`auto` 才进入动态 timeout，数字字符串表示固定秒数。
- `agent_py_agent/agent/tooling/json_repair.py`：工具调用 JSON 的窄口修复层；目前只接受“第一个 JSON 对象后面额外多出右花括号”的真实模型漂移，不会吞掉第二个 JSON 对象或任意坏格式。
- `agent_py_agent/agent/subagents/automation_gate.py`：定义半自动/自动执行门；只允许 `query_recovery_tree` / `inspect_refs` 这类 refs-only 动作自动放行，跑工具或改 task 状态必须人工确认或继续阻断。
- `agent_py_agent/agent/subagents/execution_records.py`：定义 `TestExecutionRecord`，保存真实验收执行证据字段、序列化、stdout/stderr 截断和 `passed` 派生结果。
- `agent_py_agent/agent/subagents/execution_executor.py`：定义最小 `TestExecutor`，支持 command / file_check / content_check / static_site_check，当前不写任务状态、不生成 `test_execution.json`；command 可带 workspace 内 `working_dir` / `cwd`，执行记录会保存真实工作目录。`content_check` 支持包含匹配，也支持 `content_equals` / `expected_content` + `match_mode=exact` 的精确内容检查；`static_site_check` 检查静态站点必需文件、本地链接/资源、`${...}` 占位符和明显无动作控件，不执行 JS、不访问网络。
- `agent_py_agent/agent/subagents/static_site_validator.py`：承接 `static_site_check` 的 HTML 扫描、链接解析和控件检查；所有路径都限制在 workspace 内，只返回 refs-only 问题摘要。
- `agent_py_agent/agent/subagents/execution_executor_helpers.py`：承接 `TestExecutor` 的命令解析、跨平台 python argv、记录构造和 UTC 时间 helper，让 executor 主文件保持薄执行器职责。
- `agent_py_agent/agent/subagents/execution_test_items.py`：在父级验收执行前预处理 tests；当 runner 只给出相对测试命令但 artifacts 都指向同一产物目录时，安全补 `working_dir`，并能在 workspace 内按唯一路径后缀恢复嵌套相对 artifact；常见 `cd <workspace内目录> && python3 -m pytest ...` 会被归一成受限 `working_dir` 加纯命令，即使 runner 同时给了 `working_dir` 也会拆掉安全开头 `cd`，但不放开 shell；若 tests 为空但 artifacts 里有 workspace 内 `test_*.py`，会生成保守 pytest 兜底，避免假绿和无谓的“0 tests ran”；若 artifacts 显示多页静态 HTML 且 runner 未显式声明 `static_site_check`，会自动补一个静态站点机器验收项；当该机器验收已存在时，缺 `command`、`file_path`、`content_pattern` 或 `site_root` 的模型空壳测试会被过滤，避免格式错误的自报清单压过确定性检查。
- `agent_py_agent/agent/subagents/execution_static_site_items.py`：根据 output artifacts 推断 `static_site_check` 测试项，生成 workspace 相对 `site_root` 和 `required_files`，只读路径引用，不读取 HTML 正文。
- `agent_py_agent/agent/subagents/execution_content_checks.py`：从 tests 预处理拆出的内容验收窄口，只把明确期望内容的 `cat <workspace文件>` 转成受控 `content_check`。
- `agent_py_agent/agent/subagents/execution_report.py`：写入和读取 `test_execution.json`，并生成 `test_execution.md` 展示报告；JSON 是事实源，Markdown 不参与机器判断。
- `agent_py_agent/agent/subagents/services/acceptance_machine_evidence.py`：只读 `reports/test_execution.json`，当父级真实测试全部通过且 runner 没有 evidence packet 时，把测试报告作为机器证据链；已有坏 evidence packet 不会被测试报告覆盖。
- `agent_py_agent/agent/subagents/parent_acceptance_controller.py`：生成父代理验收 dry-run 决策，返回 `execute_tests` / `review_patches` / `inspect_only` / `request_human` / `rescue` 等下一步；它只读 refs 和机器事实源，不执行命令、不写 task；预检前会复用 `prepare_test_items()` 归一化安全 cwd 包装，避免真实 runner 常见的 `cd <workspace内目录> && pytest` 被误判成人审；显式写入时生成 `parent_acceptance_decision.json`。
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
- `agent_py_agent/agent/agent_core/dispatch_limiter.py`：runner 启动前的统一限流 helper，接收 `RunnerJobLimitRequest`，先保持 `runner_start_rate` 兼容语义，再提供可选 `runner_role_limits` 角色预算口子。
- `agent_py_agent/agent/subagents/acceptance_test_execution.py`：把显式开启的真实测试执行接入 acceptance findings，生成 `test_execution_recorded` 和 `test_execution_passed`，默认不运行；执行前会复用 tests 预处理，仍只允许 workspace 内目录。`auto_acceptance` / `child_acceptance` 不相信模型自称完成，而是检查 direct child 是否真实存在并全部 `DONE/VERIFIED`；coordinator/root 有 child 但 tests 为空时，会合成一个 `child_acceptance`。
- `agent_py_agent/agent/subagents/services/acceptance_findings.py`：普通 acceptance 的 finding 汇总层；当已有 `reports/test_execution.json` 时，tests_passed 以机器执行报告为准，而不是只相信 `output.json.tests[*].ok`。artifact 检查会把 task `allowed_write_roots` 纳入安全 roots，并对 allowed root 内的绝对路径 typo 做保守后缀恢复，避免真实模型把 case id 写错一位导致误判失败。
- `agent_py_agent/agent/subagents/services/board.py`：构建看板摘要和 child status counts。`SubAgentBoardOptions(include_child_status_counts=False)` 用于 status / startup recovery 等轻量路径，避免默认查询二次加载 child task；完整看板通过已加载 task 索引汇总 child 状态，不读取 runner prompt/response 或 artifact 正文。模型工具层的 `subagent_board` 会把可操作 run ids 放在长 item 列表前，并截断 goal，减少真实 runner 在 board 大输出里丢失关键 id。
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
- `agent_py_agent/agent/settings/config.py` / `agent_py_agent/config/agent_config.yaml`：提供 `acceptance_execute_tests` 和 `acceptance_test_timeout_seconds`，默认保持老验收路径不自动跑命令；`subagent_board_limit` 控制轻量看板摘要数量；`subagent_allowed_tools=[]` 表示自动工具策略，由角色模板、任务目标和调度器判断，只有受限环境才显式填工具名；`subagent_role_template_dirs=[]` 表示使用工作区 `.agent/subagents/roles` 作为用户模板目录；`runner_timeout_seconds="off"` 表示真实 runner 不套外层超时，适合当前 E2E 压测。
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
- `agent_py_agent/agent/subagents/services/takeover_readiness.py`：生成 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md` 接管前必读包；只整理 context bundle refs、checkpoint refs、manifest 元数据和读取顺序，不读取 artifact 正文；并提供 refs-only 的推荐读取顺序解析给 rescue/action apply 使用。
- `agent_py_agent/agent/subagents/services/persistence_security.py`：归一化 `SecuritySignal` 预留字段，让安全信号解析不挤进 persistence 主流程；当前不执行安全策略。
- `agent_py_agent/agent/subagents/services/persistence_identity.py`：归一化 `RuntimeIdentity` 预留字段，让员工/会话/配置 scope 解析不挤进 persistence 主流程；当前只保留审计元数据。
- `agent_py_agent/agent/subagents/services/checkpoint_artifacts.py`：从 task facts 和 `output.json` 构建 compact 可读恢复包，包含 checkpoint、decision ledger、progress、failing tests 和 next actions。
- `agent_py_agent/agent/subagents/services/task_workspace_adapter.py`：把 runtime memory task workspace 路径同步回 `SubAgentTask`，避免 persistence 保存函数继续膨胀。
- `agent_py_agent/agent/memory_archive/task_workspace.py`：subagent 保存路径调用的 runtime memory adapter；创建 `tasks/<root_id>/` task workspace skeleton 和 `agents/<run_id>/legacy_run_ref.json`，但不移动旧工单目录。
- `agent_py_agent/agent/memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 agent run workspace skeleton；旧工单目录仍是兼容读写面，run workspace 先承接恢复、接管、finding 和 compact 链的后续入口。
- `agent_py_agent/agent/memory_archive/compact_subagent_owner.py`：给 `memory-resume --from-compact` 提供子代理 owner 只读引用解析；`subagent_run` / `subagent_session` 会指向 task-local run workspace、legacy adapter refs 和未来 session compact hook 路径，但不写主 memory、不改 runner。
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
- `agent_py_agent/agent/subagents/services/rescue_policy.py`：根据 due-check issue 给 action plan 添加 rescue/escalation 元数据，保持建议可审计但不自动越权执行；当 task 目录存在 `reports/takeover_readiness.json` 时，会优先把该包和推荐读取 refs 放入 `rescue_context_refs`，并生成 refs-only 的 `rescue_packet`。父超时残留子任务会把 `unfinished_child:<child_id>:<status>` 作为结构化 ref 写入 action plan，避免后续自动化解析自然语言。
- `agent_py_agent/agent/subagents/rendering_rescue.py`：渲染 rescue packet 的 retry / manual confirmation / recovery refs 摘要，避免主 `rendering.py` 继续接近 code-size 风险线。
- `agent_py_agent/agent/subagents/result_structured.py`：解析 runner structured output 中的 evidence packets、findings、artifacts、tests、blockers，并写回任务事实。`result_artifact_evidence.py` 承接 artifact refs 合并和 refs-only evidence packet 合成；若真实模型只给 artifacts 而漏写 `evidence_packets`，会从 artifact refs 合成证据包，避免正确产物因为缺证据包被父级误拒。
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
15. persistence 同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享结构化任务事实；messages 追加，findings/evidence 按 id 合并，避免互相覆盖，但不写入主 memory。
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
25. `tool_context_reducer.py` 根据 archive record 决定下一轮 prompt 内容：小输出保留原工具结果，大输出只保留 preview、artifact path、hash、size 和 fail-safe checkpoint path。
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
- `capability_scope.py` 是父级路由的 scope 投影层：从 request 提取 request snapshot、legacy constraints、command allowlist、gap attempted tools 和 escalation chain。
- `manager_capabilities.py` 只负责把路由命中转成 `RecordCapabilityGrantParams`，不直接解释 shell 命令；`capability_route_service.py` 只负责 gap/dry-run/apply report 的 refs-only 记录。
- `CapabilityRouteRecord.request_scope` / `grant_scope` 只用于审计和展示，不触发执行；后续 shell gateway 必须重新检查 grant、cwd、路径、网络和输出预算。
- `shell_gateway.py` 是受控 shell 的策略入口。当前只提供 `ShellGatewayRequest`、`ShellGatewayDecision` 和 `plan_shell_command()` dry-run；后续 execute v1 必须复用这个策略层，不得绕过危险命令、cwd、网络和输出预算检查。
- shell gateway 的 dry-run `allowed=True` 只代表“如果进入执行层，可以尝试执行”；它不表示已执行，也不允许子代理获得裸 shell。
- `shell_gateway_execution.py` 提供第一版执行入口 `execute_shell_command()`；它先调用 `plan_shell_command()`，再用 `subprocess.Popen(..., shell=False)` 执行 argv，并用 `_read_limited()` 持续 drain stdout/stderr。
- `ShellGatewayExecutionResult` 只保存预览、字节数、截断标记和文件 refs；完整输出不会自动塞进模型上下文，调用方要显式读 refs。
- `task_trash.py` 是删除类动作的受控替代层：`ensure_task_trash()` 管目录，`move_to_task_trash()` 只做 workspace/task-local move 和 manifest 记录；shell gateway 仍阻断 `rm`。
- `fallback_report.py` 是父级保存兜底结果的最小写入层；它只写 task-local reports，不替代 runner 正常 artifact 写入，也不把报告内容提升进长期 memory。
- `tool_call_context_reducer.py` 同时保护两条 live prompt 入口：assistant 回复里的大工具调用会摘要，`_record_tool_call` 里的工具 payload 也会摘要；小 dict payload 也渲染为摘要行，避免模型把历史 dict 复制成新工具调用。完整正文只能留在目标 artifact、debug detail 或显式读取的外部文件里。
- `hierarchy_write_policy.py` 的 `requested_child_write_roots()` 会合并父级继承根、显式 `extra_write_roots` 和 child spec goal 中的产物路径；是否授权仍由角色和写入意图决定，coordinator/researcher/tester/acceptor 不因看见路径就拿最终产物写权限。
- `hierarchy_scope_guards.py` 的同父级重复领域去重只使用稳定业务域词；会过滤 generated run-id 片段、数字编号和 `grand/one/two` 泛词，避免把恢复树里的编号 checker siblings 当成重复业务 coordinator。
- `hierarchy_scope_guards.py` 的 child write-root drift guard 会在父级已有权威产物根时阻断 sibling 目录漂移；失败返回 `child_write_root_drift`，让上级重写 child goal 后再创建。
- `dispatch_runner_selection.py` 在 runner 候选执行前预检显式 `include_run_ids`。缺失或越界 id 会生成 `runner_selection/invalid_run_ids`，把 `valid_scope_run_ids` 和保守 `possible_corrections` 返回给父节点；调度器不自动改写 id，也不启动该轮 runner。
