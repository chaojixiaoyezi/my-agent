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
- 2026-05-08 Acceptance Real Execution 第一片已落地：新增 `TestExecutionRecord`，先定义真实测试执行证据的数据结构、序列化、stdout/stderr 截断和 `passed` 派生结果；当前不执行命令、不写 `test_execution.json`，只给后续 `TestExecutor` 和父级验收接入打底。
- 2026-05-08 Acceptance Real Execution 第二片已落地：新增最小 `TestExecutor`，支持 command / file_check / content_check，记录真实退出码、输出摘要、文件元数据和内容匹配结果；当前仍不接入 acceptance 自动写回，也不生成 `test_execution.json`。
- 2026-05-08 Acceptance Real Execution 第三片已落地：新增 `write_test_execution_report()` / `load_test_execution_report()`，可把执行记录写入 `test_execution.json` 和 `test_execution.md`；JSON 是机器事实源，Markdown 只做展示，当前仍需调用方显式触发。
- 2026-05-08 Acceptance Real Execution 第四片已落地：`AcceptanceReviewOptions(execute_tests=True)` 可显式把 `TestExecutor` 和 `test_execution` report 接入 acceptance dry-run/apply 评审，生成 `test_execution_recorded` 和 P0 `test_execution_passed` findings；默认仍不自动执行 tests。
- 2026-05-09 Parent Acceptance Auto Execution 第三层已落地：显式 `--execute-auto-tests` 或 dispatch/watch 的 `execute_acceptance_tests=True` 跑完 tests 后，会写 `reports/parent_acceptance_auto_followup.json`，把后续动作归类为 `ready_for_manual_apply`、`needs_manual_rescue`、`needs_human_confirmation` 或 `needs_manual_tests`；它只记录 refs、失败测试摘要和建议命令，不自动 apply、不自动 rescue、不改 task 状态。
- 2026-05-09 Parent Acceptance 第四到第六层已落地：新增 `--followup` / `--apply-followup` 受控入口，测试通过时显式 apply，测试失败时必须提供 `--take-over-by` 并复用 `takeover_or_reassign` action handler；dispatch/watch 的 follow-up command 指向这个受控入口，完整链路测试覆盖 dispatch -> run_tests -> follow-up -> apply。follow-up apply 前会阻断坏 JSON、run_id 不匹配、测试报告引用不一致和过期测试报告。
- 2026-05-09 真实 10 子代理并发 E2E 已完成：10 个真实 runner 并发暴露 MiniMax 429、结构化输出缺结束标记、子代理自写测试错误和 patch-review 顺序缺口；5 个 clean run 进入 `DONE/VERIFIED`，5 个异常 run 通过 follow-up/takeover 进入 `TAKEN_OVER`，最终 due-check `total_issues=0`。
- 2026-05-09 Parent Acceptance patch-review 顺序修复已落地：父级真实测试通过但存在未审核 `status=applied` patch 时，decision/next-action 现在返回 `review_patches`，推荐 `subagents-patches --review-apply --run-id <run_id>`，不再直接建议 apply acceptance。
- 2026-05-09 Parent Acceptance blocked follow-up 修复已落地：已 `BLOCKED` / failed 的 runner 即使没有 `test_execution.json`，`--followup` 也会给 `needs_manual_rescue`，`--apply-followup --take-over-by ...` 可复用 takeover action gate；非测试原因导致的失败状态也不再强依赖“失败测试报告”才能接管。
- 2026-05-09 多层级 subagent 控制面 E2E 已完成：在 `/Users/example/my-终端应用` 下真实创建 1 主 / 2 子 / 4 孙 run，验证 task tree、own subtree、board、due-check、takeover view、inheritance manifest、failure handoff 和 TestExecutor 安全边界；发现并修复旧父/子快照保存会覆盖 `child_ids` 的层级断链问题，以及 `takeover_candidates` 漏掉 `TIMEOUT` 孙代理的问题。
- 2026-05-09 真实 MiniMax 多层级 E2E 修复第一片已落地：`spawn_subagents` 会把配置里的 `subagent_allowed_tools` 传入创建出的子任务；父级测试和验收 artifact 检查支持安全的嵌套相对路径后缀恢复；旧 follow-up 在任务已变成 `BLOCKED` / failed 后会推荐 rescue/takeover，而不是继续提示 apply。
- 2026-05-09 默认配置同步修复已落地：`agent_config.yaml` 补齐 `subagent_board_limit`、`subagent_allowed_tools` 和 `stream_enabled`，并把原本只写在 YAML 里但不生效的 `task_lock_timeout_seconds` 接入 `AgentConfig` 与归一化规则；新增测试保证公开可配字段和默认 YAML 不再脱节。
- 2026-05-09 真实 MiniMax 多层级 E2E 修复第二片已落地：`subagents-recovery-tree` 会按 capability 超时阈值把真正活动中的 stale `RUNNING` 后代列为恢复候选，并推荐受控 `takeover_or_reassign`；协调型 `PLANNING` root 不再被 recovery-tree 当作 stale runner。
- 2026-05-09 真实 MiniMax 多层级 E2E 修复第三片已落地：`subagents-due-check --root-id <root>` 和 `subagents-plan-actions --root-id <root>` 可只巡检/规划一棵 subagent 任务树，避免多个真实 E2E 场景共用 workspace 时 `--all` 输出互相污染；带孩子且无 active runner attempt 的 `PLANNING` coordinator 不再按普通 runner timeout 接管，而是报告 `coordinator_heartbeat_stale` / `recover_coordinator_leadership`，提示父代理重新指定 leader。
- 2026-05-09 coordinator 领导权恢复第一片已落地：`subagents-apply-actions --apply --action recover_coordinator_leadership --run-id <stale_coord> --take-over-by <leader_run_id>` 要求新 leader 是已存在 subagent run，显式 apply 后会标记旧 coordinator 为 `TAKEN_OVER`，并把子任务 `supervisor/final_owner` 切到新 leader。
- 2026-05-09 coordinator 领导权恢复第二片已落地：同一受控 apply 现在会真正重挂直接子任务，更新 `parent_id`、`depth`、`supervisor`、`final_owner`，并递推刷新孙级 depth；普通 `save()` 仍保留 child_ids 防覆盖合并，只有 `save_hierarchy_links()` 允许精确移除旧父节点 child edge。
- 2026-05-09 批量 coordinator 挂掉的领导权恢复计划第一片已落地：新增 `subagents-leadership-recovery-plan --root-id <root> --leader <leader>...`，只读 `coordinator_heartbeat_stale` 问题并按 `max_children_per_leader` 把旧 coordinator 的直接孩子分摊给多个候选 leader；当前只写 dry-run JSON/Markdown 报告，不改树、不自动接管、不执行 future apply。
- 2026-05-09 批量领导权恢复后续 1-7 步已落地：新增 `subagents-leadership-recovery-apply` 分批重挂入口，apply 前校验 root/leader/直接 child/容量，apply 后写 JSON/Markdown 审计；计划器会把失败/超时且仍有孩子的 leader 当作新恢复源，容量会扣掉 leader 现有 child 防止接管雪球；dispatch/watch 只写 `leadership_recovery_plan` refs-only 记录，不自动重挂。测试覆盖 1 主 / 4 子 / 16 孙 / 48 孙孙子树。
- 2026-05-09 主节点单入口层级烟测已跑通：新增 runner 内部 `schedule_child_subagents` 工具，parent id 只能来自当前 runner；runner 内部 dispatch 默认关闭 workflow auto、只跑当前节点直接 children、排除当前节点自己并暂缓 acceptance apply。真实 MiniMax smoke `hier_main_only_smoke_1778326811` 验证外层只启动 root，root 创建 child，child 创建 leaf，只有 leaf 用 `write_file` 写出 `proof.txt=hierarchy-ok`。
- 2026-05-09 主节点单入口 1/4/16/48 压测修复第一轮已落地：真实 MiniMax case 暴露 repeated dispatch 被 one-shot guard 阻断、下层缺父级目标上下文、coordinator role 被模型误写成 worker、限速 PLANNING child 被误判失败；已修复 dispatch/board 可重复调用、层级 child 继承 bounded parent goal/thought、按工具/depth 推断 coordinator role，并在 runner-context dispatch 响应里返回 direct child progress/continue hint。完整 48 文件 main-node-only pass 仍需复跑。
- 2026-05-10 子代理角色模板第一片已落地：新增外置 JSON role template catalog，内置 `coordinator/worker/bug_finder/tester/acceptor/researcher/writer` 均带中文说明、默认工具和输出契约；用户可在 `.agent/subagents/roles/*.json` 增加广义角色模板，代码只负责加载、校验和只读/写权限边界。`subagent_allowed_tools=[]` 现在明确表示自动工具策略，不再等同于“没有工具”；默认 `max_subagents=1000`，真实并发仍由 runner/scheduler 控制。
- 2026-05-11 Stage7 R12/R13/R14 真实购物站点 E2E 修复已落地：runner 成功写入当前子代理 `output.json` 后会直接合成 `SUBAGENT_RESULT` 收口，不再多打一轮模型；静态 Web 自动验收存在时，会丢弃缺 `file_path` / `content_pattern` / `site_root` 的模型空壳测试；单页 HTML artifact 也会生成 `static_site_check`，避免首页这类单页 leaf 被空壳 content_check 误判失败。
- 2026-05-11 Stage7 R19 真实购物站点 E2E 修复已落地：外层只启动 root，root 自建 4 个一级 coordinator，子层写出购物站 10 个顶层必需文件；本轮暴露并修复 repair leaf 被 `duplicate_leaf_target` 误挡、root/coordinator 无法写 agent-run workspace 报告、`runner_timeout_seconds=off` 仍触发模型调度内 due-check 超时接管、以及 static-site check 误把 JS template literal 当 `${...}` 占位符的问题。R19 仍保留 root recovery 不够 actionable 的 gap，下一轮 R20 需要验证 shared-assets 能创建修复 leaf 并收口。
- 2026-05-11 Stage7 R20 权限/层级/通信修复已落地：层级命名固定为 `小傻妞-*` / `小小傻妞-*` / `小小小傻妞-*` 递增；上层继承下层产物写入根用于检查、接管和救援，但 `product_write_policy=delegate` 会阻止 coordinator/root/tester/reviewer 直接写业务产物，要求创建 worker/writer/leaf_worker；父级明确文件名和 4 层链路会作为硬合同传给下层；新增 `subagent_message`，支持 `direct+descendants` 给少数子孙定向纠偏、`broadcast+descendants` 给自己子树写 scoped shared-board 广播、`direct+peers` 给同父级平级讨论，并阻断跨分支越权通知。
- 2026-05-11 Stage7 R21 真实 E2E 发现并修正长批量派工截断：root-only 购物站测试中，深层 coordinator 一次创建 3 个长 goal 的 leaf_worker，模型工具 JSON 半截截断后进入 BLOCKED。`schedule_child_subagents` 工具入口现在单次最多接受 2 个 child，超过会明确要求拆成多次 1-2 个 child 调用；manager 服务层仍保留批量能力，限制只作用在模型 runner 工具入口，防止真实模型输出过长卡死。
- 2026-05-11 Stage7 R22/R27 真实 E2E 发现并修正禁止文件名误传：`product-detail.html（禁止改成 product.html）` 里的 `product.html` 曾被正则误抽成父级明确文件，导致 child goal 带错必需产物。新增 `required_file_terms.py`，层级 handoff、context bundle 和静态站 required_files 共用它：正向交付文件进入 `required_files`，禁止反例进入 `forbidden_files`，下层必须按结构化合同传递，不再从长散文里二次猜。
- 2026-05-11 Stage7 R27 真实 E2E 进一步暴露长文件写入截断：`style.css` 的完整 `write_file.content` 两次缺少结束标记；parse-error hint 虽然提示了 `append_file`，模型仍重复长写。runner contract 和 write/append 工具说明现在提前要求长 CSS/JS/HTML 先写短骨架，再 `append_file` 分块追加；这借鉴了 会话运行时/模型助手 的结构化工具协议、长期助手 的 schema/structuredContent 和大输出外置、通道运行时 的模板变量、终端交互 的 lineage metadata，核心是把机器契约从自然语言里拆出来。
- 2026-05-08 Acceptance Real Execution 第五片已落地：新增 `subagents-tests <run_id>` CLI；默认只展示已有 `test_execution.json` 摘要，`--re-run` 才显式读取 `output.json.tests`、执行 allowlist 验证并写回 `test_execution.json/md`。
- 2026-05-08 Acceptance Real Execution 第六片已落地：新增 `acceptance_execute_tests` 和 `acceptance_test_timeout_seconds` 配置；默认仍关闭真实执行，`subagents-acceptance --execute-tests/--no-execute-tests/--test-timeout` 可覆盖单次验收。
- 2026-05-08 Acceptance Real Execution CI 收尾：整理 ruff import/UP037，并让默认验收路径继续按旧 `apply/reviewer/note` 调用兼容旧测试替身；只有真实测试执行、超时覆盖或显式时间等新字段启用时才传完整 options 包。
- 2026-05-08 Parent Acceptance Controller 第一片已落地：新增 `plan_parent_acceptance(run_id)` dry-run 决策入口，父代理可先判断 `execute_tests`、`inspect_only`、`request_human` 或 `rescue`；当前只读 `output.json` / `test_execution.json` / handoff refs，不执行命令、不写任务状态。
- 2026-05-08 Parent Acceptance Controller 第二片已落地：新增 `subagents-acceptance-plan <run_id>` CLI 展示父级 dry-run 决策，支持人类视图和 `--json`；继续保持 refs-only，不执行 tests、不读取 artifact 正文、不写任务状态。
- 2026-05-08 Parent Acceptance Controller 第三片已落地：`status --json`、人类 `status` 和 `subagents` 看板新增 `Acceptance Plan` 摘要，展示待验收/失败/阻塞 run 的父级 dry-run 决策；展示层不执行 tests、不读取 artifact 正文、不写任务状态。
- 2026-05-08 Parent Acceptance Controller 第四片已落地：新增 `write_parent_acceptance_decision(run_id)` 和 `subagents-acceptance-plan --write`，把 dry-run 决策写入 `reports/parent_acceptance_decision.json`；这是 refs-only 审计文件，不执行决策、不改任务状态。
- 2026-05-08 Parent Acceptance Controller 第五片已落地：新增 `subagents-acceptance-plan --apply` 和 `parent_acceptance_apply.json`；只有 `inspect_only` 会进入既有 acceptance apply，`execute_tests` / `request_human` / `rescue` 只写拦截审计，不自动跑 tests、不自动 rescue、不绕过人工确认。
- 2026-05-08 Parent Acceptance Controller 第六片已落地：新增 `plan_parent_acceptance_next_action(run_id)` 和 `subagents-acceptance-plan --next-action`，把当前 plan/apply 审计映射成上级可读的显式动作建议；它只展示建议和 refs，不执行动作、不改 task。
- 2026-05-08 Parent Acceptance Auto Policy dry-run 第一片已落地：新增 `plan_parent_acceptance_auto_policy(run_id)` 和 `subagents-acceptance-plan --auto-policy`，写入 `parent_acceptance_auto_policy.json`；当前只判断 allow/blocked 和 would_execute，`executed` 固定 false，不执行 tests、不 apply、不 rescue。
- 2026-05-08 compact/resume 联调第一片已落地：新增测试证明 subagent owner refs、compact continue packet 和 parent acceptance auto-policy 可以串联；compact `ready_to_continue` 不会触发 parent tests/apply/rescue，auto-policy 仍保持 dry-run、`executed=false`、不改 task 状态。
- 2026-05-08 Parent Acceptance Controller 第七片已落地：`status --json`、人类 `status` 和 `subagents` 看板新增 `Acceptance Next Action` 摘要，展示可见待验收/失败/阻塞 run 的 action、reason、command、refs 和 `mutates_task_state`；展示层保持 refs-only，不执行命令、不读取 artifact 正文、不写任务状态。
- 2026-05-08 compact/resume + 业务验收联调第二片已落地：新增端到端 focused 测试串起 compact apply/resume、parent acceptance execute_tests 阻断、显式 `AcceptanceReviewOptions(execute_tests=True)` 真实 file_check、`test_execution.json` 事实源、后续 inspect-only apply；验证通过后任务才进入 `DONE/VERIFIED`，且 memory gate exports 仍不自动写主 memory。
- 2026-05-08 Acceptance Real Execution 事实源修正：普通 acceptance 现在会优先读取已有 `reports/test_execution.json` 判断 tests_passed，避免 `output.json.tests` 没有 `ok` 字段时把真实通过的测试误判为失败；默认仍不自动执行 tests，只有显式执行路径会生成或刷新报告。
- 2026-05-09 Parent Acceptance Auto Policy dispatch/watch dry-run 接入已落地：`subagents-dispatch` 在 acceptance 记录上写入 `parent_acceptance_policy_*` 摘要字段，并生成 `reports/parent_acceptance_auto_policy.json`；`SUBAGENT_DISPATCH.md` 会展示 policy ref、decision、action、would_execute 和 executed。watch 仍只引用 `subagent_dispatch_report.json` / `SUBAGENT_DISPATCH.md`，不重复执行 policy、不跑 tests、不 apply、不改 task 状态。
- 2026-05-09 Parent Acceptance Auto Policy 半自动计划字段已落地：policy JSON 和 CLI 现在展示 `execution_mode=manual_only`、`automatic_execution_allowed=false` 和 `recommended_command`，明确这是人工/后续受控调度参考，不会自动启动进程、不跑 tests、不 apply、不 rescue、不改 task 状态。
- 2026-05-09 Parent Acceptance Auto Policy dispatch 半自动摘要已落地：acceptance dispatch record 和 `SUBAGENT_DISPATCH.md` 透传 `execution_mode`、`automatic_execution_allowed` 和 `recommended_command`，让 watch/调度层明确看到 manual-only 边界；仍不执行建议命令、不 apply、不 rescue、不改 task 状态。
- 2026-05-09 Parent Acceptance Auto Policy preflight 审计已落地：policy JSON 和 CLI 现在展示 `preflight_status`、`ready_for_manual_execution`、`ready_for_automatic_execution`、checks 和 blockers；`run_tests` 可以是 manual_ready，但自动执行第一版固定 false，人工确认/状态修改/非 allowlist 会明确阻断。
- 2026-05-10 Context Bundle v1 第一片已落地：每次 `write_execution_context()` 会从 `SubAgentTask` 生成实时 `context_bundle.json` / `CONTEXT_BUNDLE.md`，包含 goal、plan、acceptance、权限、约束、workspace refs、output contract 和 source refs；同一份 bundle 会镜像到旧 run 工单目录和 `tasks/<root_id>/agents/<run_id>/`，方便 runner、接管代理和后续 resume 读取。
- 2026-05-10 Context Gate v1 已接入 runner prompt：gate 会检查 goal、plan、acceptance、workspace/output/权限/约束等最小字段；缺字段时 prompt 明确要求子代理不要硬做业务实现，而是在结构化结果里返回 `BLOCKED` 并列出需要父代理补齐的字段。当前 gate 是派发自检和 prompt 约束，尚未自动停止所有 runner 调用。
- 2026-05-09 Parent Acceptance Auto Policy dispatch preflight 摘要已落地：acceptance dispatch record 和 `SUBAGENT_DISPATCH.md` 透传 `preflight_status`、`ready_for_automatic_execution` 和 blockers，报告层能看清“manual_ready 仍不等于自动放行”；仍不执行命令、不 apply、不 rescue。
- 2026-05-09 Parent Acceptance Auto Execution bundle/facade 第一片已落地：新增 `parent_acceptance_auto_execution.py` 的 Request/Result bundle、`plan_parent_acceptance_auto_execution(run_id)` manager 入口和 `parent_acceptance_auto_execution.json` 审计文件；当前只生成 dry-run 执行计划，并写入 `execution_allowed=false`、`guard_status=blocked`、`auto_executor_dry_run_only` 等硬闸门，不启动命令、不改 task 状态。
- 2026-05-09 Parent Acceptance Auto Execution CLI 已落地：`subagents-acceptance-plan --auto-execution` 会展示并写入 executor dry-run facade 审计；第一版只打印 recommended command、hard guard、blockers 和 execution ref，不执行命令。
- 2026-05-09 Parent Acceptance Auto Execution dispatch/watch 摘要已落地：acceptance dispatch record 和 `SUBAGENT_DISPATCH.md` 透传 `parent_acceptance_auto_execution_*` 字段，包括 execution ref、status、allowed、executed、guard status 和 blockers；watch 仍只读 dispatch report/Markdown，不启动 recommended command。
- 2026-05-09 Parent Acceptance Auto Execution 手动确认执行第一层已落地：`plan_parent_acceptance_auto_execution(..., options=ParentAcceptanceAutoExecutionOptions(execute_tests=True))` / `subagents-acceptance-plan --auto-execution --execute-auto-tests` 会显式执行 run_tests 并写 `test_execution.json`；仍不 apply、不 rescue、不修改 task 状态。
- 2026-05-09 Parent Acceptance Auto Execution 第二层 dispatch/watch 手动确认已落地：`DispatchParams/WatchParams(execute_acceptance_tests=True)` 和 `subagents-dispatch --execute-acceptance-tests` 可在 acceptance 调度记录中受控调用第一层 run_tests 执行路径，写 `test_execution.json/md` 和 auto-execution 摘要；仍不 apply、不 rescue、不修改 task 状态。
- 2026-05-09 Dispatch 验收展示刷新已落地：当 `subagents-dispatch --execute-acceptance-tests` 本轮真的跑出新 `test_execution.json` 后，会重新 dry-run 当前 run 的 acceptance record，并覆盖本轮 dispatch 展示、单 run 审计和 aggregate acceptance report 里的旧结论；这只修正可见状态，仍不 apply、不 rescue、不修改 task。
- 2026-05-09 真实 3 子代理 E2E 暴露父验收工作目录问题：子代理在产物目录里跑相对 unittest 通过，但父级 `subagents-tests --re-run` 默认在 workspace 根目录执行导致 `NO TESTS RAN`；已新增 tests 工作目录预处理，从 runner artifacts 安全推断 workspace 内 `working_dir`，并让 `TestExecutor` 在报告里记录真实 cwd。
- 2026-05-09 真实 3 子代理 E2E 暴露“测试通过但 evidence packet 缺失仍被拒绝”问题：已新增父级真实测试报告机器证据链兜底；当 runner 没有 evidence packet 且 `test_execution.json` 全通过时，acceptance/verifier 可把父级测试报告作为可追溯证据链，但不会覆盖已有的坏 evidence packet。
- 2026-05-09 真实 5 子代理坏天气 E2E 已覆盖进程被杀和真实测试失败：被 SIGTERM 的 runner 通过 fast due-check 暴露 `run_timeout` / `heartbeat_stale`，action plan 推荐并成功执行 `takeover_or_reassign`；普通测试失败会进入 `plan_rescue`，且已有 `test_execution.json` 现在可自动生成 follow-up rescue 包，不再卡在 missing follow-up。
- 2026-05-09 业务/子代理验收链路联调修正：`runner-retry` 和 `structured-repair` 离线 scenario stub 输出已补齐带 refs 的 `evidence_packets` 和安全 `file_check` tests，真实 CLI `scenario-test --case runner-retry` / `--case structured-repair` 均能走到 `DONE/VERIFIED` 与 `SCENARIO_PASS`。
- 2026-05-09 层级调度器 v1 已落地：新增 `HierarchyScheduleRequest` / `HierarchyChildSpec` bundle、`SubAgentManager.schedule_child_runs()` 和 `subagents-hierarchy` CLI；默认 dry-run，只在显式 `--apply` 时创建 child/grandchild run，并统一限制 `max_depth` / `max_children`。
- 2026-05-09 Reporter / Checker 角色契约第一片已落地：新增 `role_contracts.py`，把 `analyst/reviewer` 兼容映射到 `reporter/checker`；reporter 默认要求输出引用 evidence/artifact refs，checker 默认只读工具并强制 parent final gate / cannot self-accept。
- 2026-05-09 多层恢复闭环第一片已落地：新增 `HierarchyRecoveryRequest` 和 `build_hierarchy_recovery_packet()`，可从 root run 生成 refs-only 子树恢复包，定位 BLOCKED/FAILED/TIMEOUT/ERROR 后代和 takeover/failure/checkpoint refs；不读取 artifact 正文、不自动接管。
- 2026-05-09 并发调度和限流第一片已落地：新增 `dispatch_limiter.py`，把 runner_start_rate 和可选 `runner_role_limits` 收束成 `RunnerJobLimitRequest` bundle；现有 worker pool 行为保持兼容，后续可按 reporter/checker 做角色预算。
- 2026-05-09 层级子代理 CLI E2E 回归第一片已落地：新增 `subagents-recovery-tree` 命令，真实 parser/config/SimpleAgent 测试覆盖 `subagents-hierarchy --apply` 创建 child run 后，再用 recovery-tree 查到阻塞后代和 takeover refs。
- 2026-05-09 半自动到自动执行门第一片已落地：新增 `automation_gate.py`，通过 `SubAgentAutomationGateRequest` 区分 refs-only 自动可放行动作、人工确认半自动动作、以及改状态/跑工具/候选过多的阻断动作；当前不执行命令，只产出 gate 结果。

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
- 继续补 Acceptance Real Execution：下一步做聚合统计、报告保留策略或更细的 allowlist 配置；当前默认验收路径仍保守关闭真实执行。
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
- 本轮 Principal / Conversation 隔离预留 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_security_reserve.py agent_py_agent\tests\test_status_shared_progress.py -p no:cacheprovider` 先因 `RuntimeIdentity` 不存在失败，补实现后 -> `5 passed`。当前只保存并展示 `runtime_identity`、`memory_scope`、`config_scope` 元数据，不启用员工长期记忆，也不允许会话覆盖写入全局配置。
- 本轮 Acceptance Real Execution 记录模型 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_test_execution_record.py -p no:cacheprovider` 先因 `TestExecutionRecord` 不存在失败，补实现后 -> `3 passed`。
- 本轮 Acceptance Real Execution 执行器 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_test_executor.py -p no:cacheprovider` 先因 `TestExecutor` 不存在失败，补实现后 -> `4 passed`。
- 本轮 Acceptance Real Execution 报告存储 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subagent_test_execution_report.py -p no:cacheprovider` 先因 `execution_report` 模块不存在失败，补实现后 -> `2 passed`。
- 本轮 Acceptance Real Execution 验收接入 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_agent\test_subagent_acceptance.py::test_subagent_acceptance_can_execute_real_tests_on_explicit_dry_run -p no:cacheprovider` 先因 `AcceptanceReviewOptions` 未导出失败，补实现后 -> `1 passed`；整组 acceptance 回归 `python -m pytest -q agent_py_agent\tests\test_agent\test_subagent_acceptance.py -p no:cacheprovider` -> `6 passed`。
- 本轮 Acceptance Real Execution CLI TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsSubcommandRegistration::test_add_subagents_subcommands_creates_expected_commands agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_tests_has_view_and_rerun_args agent_py_agent\tests\test_subagents_tests_command.py -p no:cacheprovider` 先因 `cmd_subagents_tests` 不存在失败，补实现后 -> `4 passed`。
- 本轮 Acceptance Real Execution 配置接入 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_config_validation.py::test_acceptance_real_execution_config_defaults_are_conservative agent_py_agent\tests\test_config_validation.py::test_acceptance_real_execution_config_coercion_and_range agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_has_real_test_override_args agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_uses_configured_real_test_defaults agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_cli_override_wins_over_config -p no:cacheprovider` 先因配置字段和 CLI 参数缺失失败，补实现后 -> `5 passed`。
- 本轮 Parent Acceptance Controller TDD 验收：`python -m pytest -q agent_py_agent\tests\test_parent_acceptance_controller.py -p no:cacheprovider` 先因 `SubAgentManager.plan_parent_acceptance` 不存在失败，补实现后 -> `3 passed`。
- 本轮 Parent Acceptance Controller CLI TDD 验收：`python -m pytest -q agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsSubcommandRegistration::test_add_subagents_subcommands_creates_expected_commands agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_plan_has_run_id_argument agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_plan_prints_parent_decision -p no:cacheprovider` 先因 `cmd_subagents_acceptance_plan` 不存在失败，补实现后 -> `3 passed`。
- 本轮 Parent Acceptance Controller status/board TDD 验收：`python -m pytest -q agent_py_agent\tests\test_status_shared_progress.py::test_status_payload_surfaces_acceptance_plan_summary agent_py_agent\tests\test_status_shared_progress.py::test_status_human_prints_shared_progress_failure_handoff agent_py_agent\tests\test_status_shared_progress.py::test_subagents_board_prints_shared_progress_panel -p no:cacheprovider` 先因 payload/展示缺少 `Acceptance Plan` 失败，补实现后 -> `3 passed`。
- 本轮 Parent Acceptance Controller 决策落盘 TDD 验收：`python -m pytest -q agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_plan_can_be_written_as_refs_only_audit_file agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_plan_has_write_argument agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_plan_write_prints_decision_file -p no:cacheprovider` 先因写入入口和 `--write` 缺失失败，补实现后 -> `3 passed`。
- 本轮 Parent Acceptance Controller explicit apply TDD 验收：`python -m pytest -q agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_apply_allows_inspect_only_decision agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_apply_blocks_execute_tests_decision_without_mutation agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_plan_has_apply_argument agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_plan_apply_prints_apply_result -p no:cacheprovider` 先因 manager apply、CLI `--apply` 和 apply 输出缺失失败，补实现后 -> `4 passed`。
- 本轮 Parent Acceptance Controller next-action TDD 验收：`python -m pytest -q agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_next_action_recommends_explicit_test_run_after_blocked_apply agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_next_action_requests_human_for_unsafe_command agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_plan_has_next_action_argument agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_plan_next_action_prints_recommended_action -p no:cacheprovider` 先因 manager next-action、CLI 参数和输出缺失失败，补实现后 -> `4 passed`。
- 本轮 Parent Acceptance Auto Policy dry-run TDD 验收：`python -m pytest -q agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_auto_policy_dry_run_allows_run_tests_without_execution agent_py_agent\tests\test_parent_acceptance_controller.py::test_parent_acceptance_auto_policy_blocks_human_confirmation agent_py_agent\tests\test_subcommands_agents_class.py::TestSubagentsReviewCommandRegistration::test_subagents_acceptance_plan_has_auto_policy_argument agent_py_agent\tests\test_subagents_tests_command.py::test_subagents_acceptance_plan_auto_policy_prints_policy_decision -p no:cacheprovider` 先因 manager auto-policy、CLI 参数和输出缺失失败，补实现后 -> `4 passed`。
- 本轮 Parent Acceptance Controller status/board next-action TDD 验收：`python -m pytest agent_py_agent/tests/test_status_shared_progress.py::test_status_payload_surfaces_acceptance_next_action_summary -q` 先因 shared-progress payload 缺少 `acceptance_next_action_entries` 失败，补实现后 -> `1 passed`；组合回归 `python -m pytest agent_py_agent/tests/test_status_shared_progress.py -q` -> `5 passed`；ruff 定向检查 -> passed。
- 本轮 Parent Acceptance Auto Policy CI 修复：补齐 `cli/acceptance_progress.py`、`cli/shared_progress.py` 和 `manager_acceptance.review_acceptance()` 的定义级注释格式；`TestExecutor` 在没有 `python` 命令的本地环境会回退到当前解释器，保持 allowlist 和无 shell 执行边界不变。
- 本轮真实 3 子代理 E2E 修复验收目录回归：新增 `agent_py_agent/tests/test_subagent_test_item_preparation.py` 覆盖 artifact -> working_dir 推断、显式 working_dir 优先和越界 artifact 忽略；扩展 `agent_py_agent/tests/test_subagent_test_executor.py` 覆盖 command 在显式 working_dir 里执行和越界工作目录阻断。
- 本轮真实 3 子代理 E2E 修复机器证据链兜底：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py` -> `8 passed`，覆盖“无 evidence packet + 父级真实测试通过可验收”和“坏 evidence packet 不能被真实测试报告洗白”。
- 本轮真实 5 子代理坏天气 E2E 修复 follow-up 生成口径：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_followup_control.py agent_py_agent/tests/test_parent_acceptance_controller.py` -> `20 passed`；真实 interval 失败任务从 `missing_followup` 修正为 `needs_manual_rescue`，并通过 `--apply-followup --take-over-by ...` 记录 takeover。
- 本轮多层级 subagent 控制面 E2E 修复验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_local_store_control_plane.py` -> `5 passed`；真实外部目录 E2E 生成 7 个层级 run，CLI `subagents --all --root-id ...` 和 `subagents-due-check --all` 能展示 refs-only 共享进度、接管入口和 P0/P1 问题；`TestExecutor` 确认 `rm -rf /`、shell 链接、越界 working_dir 和越界 file_path 均被拒绝。
- 本轮真实 MiniMax 多层级 E2E 修复验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_followup_control.py agent_py_agent/tests/test_acceptance_helpers_class.py::TestCheckArtifactExists agent_py_agent/tests/test_manager_acceptance_findings.py::TestSubAgentAcceptanceOutputFindingMixin::test_nested_relative_artifact_paths_exist agent_py_agent/tests/test_subagent_test_item_preparation.py agent_py_agent/tests/test_subagents_tests_command.py` -> `35 passed`；真实 CLI 复测 `subagents-tests subagent-1778309080-a1addc2d --re-run` 通过，旧 blocked follow-up 正确转为 `needs_manual_rescue` 并通过 `--apply-followup --take-over-by real-multilevel-parent` 记录 takeover。
- 本轮 recovery-tree 坏天气对齐验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_hierarchy_recovery.py agent_py_agent/tests/test_subagent_hierarchy_cli_e2e.py agent_py_agent/tests/test_subcommands_agents_class.py::TestSubagentsSubcommandRegistration::test_add_subagents_subcommands_creates_expected_commands` -> `5 passed`；真实 CLI 复测两个被终止的 RUNNING 孙代理进入 recovery-tree，并通过 `subagents-apply-actions --apply --action takeover_or_reassign --take-over-by real-multilevel-parent` 变为 `TAKEN_OVER`。
- 本轮 due-check / plan-actions root scope 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_manager_board_class.py::TestDueCheck agent_py_agent/tests/test_manager_board_class.py::TestPlanActions agent_py_agent/tests/test_subagent_commands.py::TestCmdSubagentsDueCheck agent_py_agent/tests/test_subagent_commands.py::TestCmdSubagentsPlanActions agent_py_agent/tests/test_subcommands_agents_class.py::TestSubagentsSubcommandRegistration::test_add_subagents_subcommands_creates_expected_commands` -> passed；真实 CLI `subagents-due-check --root-id subagent-1778309037-dddc7214 --all` 把共享 workspace 的跨树噪音缩到当前 root 相关问题，`subagents-plan-actions --root-id ... --all` 只为当前 root 生成动作，并能报告 stale coordinator 的领导权恢复建议。
- 本轮 compact/resume + parent acceptance 联调回归：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_compact_parent_acceptance_flow.py agent_py_agent/tests/test_parent_acceptance_controller.py` -> `12 passed`；`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py::test_subagent_acceptance_can_execute_real_tests_on_explicit_dry_run agent_py_agent/tests/test_subagents_tests_command.py` -> `10 passed`。
- 本轮 Parent Acceptance Auto Policy dispatch/watch TDD 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_dispatch_and_planner.py::test_subagent_dispatch_dry_run_plans_runner_patch_and_acceptance agent_py_agent/tests/test_agent/test_planner_and_watch.py::test_subagent_dispatch_watch_surfaces_parent_acceptance_auto_policy_refs` 先因 dispatch record 缺少 policy 字段、watch 未生成 policy audit、Markdown 不展示 policy ref 失败，补实现后 -> `2 passed`。
- 本轮业务/子代理验收链路联调 TDD 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_runner_retry_reaches_parent_acceptance agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_structured_repair_reaches_parent_acceptance` 先因 scenario stub 缺少 refs-bearing evidence packet 失败，补齐后 -> `2 passed`；手工 CLI `python3 -m agent_py_agent scenario-test --case runner-retry --workspace /tmp/my-agent-integration-runs --max-runners 1` 和 `python3 -m agent_py_agent scenario-test --case structured-repair --workspace /tmp/my-agent-integration-runs --max-runners 1` 均输出 `SCENARIO_PASS`。
- 本轮真实 1/4/16/48 clean-deliverables 树 E2E：48 个 MiniMax leaf runner 全部 CLI 完成，`deliverables/real_tree_1778318291` 保持用户产物干净、`.my_agent_runtime/real_tree_1778318291` 承载内部运行痕迹；修复 `subagents-tests --re-run` 空报告假绿、`.my_agent_runtime/.../subagents` workspace 推断、`cd <dir> && pytest` 安全归一化、缺 tests 但有 `test_*.py` artifact 的 pytest 兜底。最终产品复跑 48 个 leaf：25 executed passed、22 executed failed、1 zero-tests blocked。
- 本轮主节点单入口 1/4/16/48 stress attempt：只启动 root，观察到 root->child->grandchild->leaf 全链路真实写出部分 leaf 文件；在失败分支上定位并修复 repeated dispatch、父上下文继承、角色推断和 direct child progress 反馈。该 run 被主动停止以避免继续消耗在已知系统性问题上，后续需先跑小型树复测，再跑完整 48 leaf。
- 本轮主节点单入口 1/2/4 小树复测：只启动 root，root 自己创建 2 个 child，child 自己创建 4 个 leaf，4 个 leaf 均写出真实算法产物并通过父级 smoke test；已修复 leaf 写工具缺失和成功重试后残留旧 failure/capability 状态的问题，仍需继续优化空测试命令导致的 `request_human` 验收噪音。
- 本轮子代理调试追踪第一片：新增 `subagent_debug_trace_level`（0-5，默认 0），开启后写内部 `debug_traces/subagent_trace.jsonl`，先覆盖 task_created 和 runner_result_recorded refs-only 事件；该能力用于真实 E2E 观察，不污染用户产物目录。
- 本轮 trace smoke 暴露并修复模型工具别名问题：真实 child 曾给 leaf 下发 `allowed_tools=["write", ...]`，但真实工具名是 `write_file`；现在层级调度会把 `write/read/list/search/append/replace` 等常见别名转成真实工具名，并在 leaf 写文件任务上补齐安全文件工具。复测 `main_node_trace_smoke_fixed_20260509_2218` 已由 root 自己驱动 child/leaf 写出 `proof.txt=trace-hierarchy-ok`。
- 本轮调试追踪第二片：空 command 类型测试项不再触发 `request_human_confirmation`，会作为“不可执行占位检查”进入 refs 摘要并降级到 inspect-only；`subagent_debug_trace_level=2` 现在还覆盖 `hierarchy_schedule_result`、`parent_acceptance_decision` 和 `parent_acceptance_next_action`，用于后续 1/4/16/48 和购物网站 E2E 快速定位调度、验收、人审和救援卡点。
- 本轮主节点单入口 1/2/4/12 E2E 暴露 root 最终总结卡住问题：下层已产出 12 套 leaf 文件后，root 最终模型响应长期不返回；已把 `subagent-run --execute` 改为复用 runner worker timeout 计算和执行边界，避免 CLI 入口裸跑模型导致无人值守任务无限挂住。真实 E2E 记录见 `06-real-e2e-findings.md`。
- 本轮叶子能力边界提示修复：runner prompt 新增明确 contract，要求叶子没有 `shell/command/terminal` 工具时不要因为不能自己跑 `pytest` 上抛 capability_request，而是写好产物、测试文件和推荐命令，交给父级验收器执行。
- 本轮层级路径继承修复：当模型创建下一层时只给出短 goal，scheduler 会把父级目标/边界追加进 child goal，并用补全后的 goal 推断叶子写文件工具，避免产物目录只留在 thought 里、传到孙级后丢失。
- 本轮父超时子任务残留恢复第一片：`due-check` 现在会在父节点 `TIMEOUT` 且仍有未完成 direct child 时报告 `parent_timeout_with_unfinished_children`，`plan-actions` 只生成 `recover_child_after_parent_timeout` refs-only 恢复提示和 `subagents-recovery-tree` 命令；相关 child 以 `unfinished_child:<child_id>:<status>` 结构化 ref 写入 rescue packet，recovery-tree 在 `--hide-healthy` 下也会展示这些残留 child，不自动接管、不执行代码。
- 本轮调试追踪第三片：新增 `debug_trace_reports.py`，`subagent_debug_trace_level=3` 记录 due-check、action-plan、hierarchy recovery、dispatch 和 dispatch-watch 的 bounded 摘要；仍只写内部 JSONL，不展开任务正文、prompt/response、artifact 或 tool output。
- 本轮 runner-context 验收收口修复：真实 MiniMax 复测暴露“父节点已经跑完孩子 tests，但孩子仍停在 `AWAITING_ACCEPTANCE`”的问题；现在 runner 内部 `dispatch_subagents(apply=True, execute_acceptance_tests=True)` 会在测试通过且 follow-up 判断可验收时受控 apply 直接孩子，顶层 CLI/API dispatch 仍默认人工 apply。Focused dispatch 回归 `4 passed`。
- 本轮 coordinator/root child-acceptance 修复：真实模型会写 `auto_acceptance`，也可能让 root/coordinator 有 direct children 但 tests 为空；现在这两类都转成确定性的 `child_acceptance`，只按直接孩子是否 `DONE/VERIFIED` 判断，不相信模型自称完成。真实 case `main_node_coordinator_accept_retest_20260510_113000` 最终 5 个节点全部 `DONE/VERIFIED`。
- 本轮真实路径/命令鲁棒性修复：`cd <workspace dir> && pytest ...` 即使已有 `working_dir` 也会被拆成安全工作目录加无 shell 命令；artifact 检查会把 task `allowed_write_roots` 纳入 roots，并对 allowed root 内的绝对路径 typo 做保守后缀恢复。对应 focused tests 和真实 leaf 独立 pytest 已通过。
- 本轮角色模板真实接入：`create_subagents` / `schedule_child_subagents` 工具规格和 coordinator runner prompt 现在会展示内置角色模板摘要，真实 MiniMax smoke 能主动创建 `worker/tester/bug_finder/acceptor` 四类子代理；默认不传 `allowed_tools` 时由角色模板和任务目标决定工具，所有内置角色都能读取资料并写 task-local 报告/证据，最终业务产物仍由 worker/writer 负责。
- 本轮 dispatch 阶段排序修复：真实 role-template smoke 暴露 `acceptor/bug_finder/tester` 可能先于 `worker` 被 `max_runners` 截断选中；现在 runner 候选会在截断前按角色阶段排序，保证“先拆/先做/再测/再验收”。
- 本轮 runner 超时配置调整：按用户要求，默认 `runner_timeout_seconds` 改为 `off`，`off/none/disabled/0` 明确表示不套外层超时；数字秒数仍是固定超时，`auto` 才使用动态 timeout。真实重跑确认不再 30 秒误杀，但也暴露“无限等待时缺少活跃心跳/请求阶段可观测性”的后续问题，已记录到真实 E2E findings。
- 本轮 runner 阶段心跳第一片：`subagent_debug_trace_level=3` 现在会记录子代理 runner 的模型请求开始、模型响应返回、模型请求失败、工具调用开始、工具调用结束；只写长度、backend、工具名、payload keys、ok 和输出长度，不写 prompt/response/tool output 正文。这样无限 runner 卡住时能先区分是模型请求卡住、模型已返回但工具没跑、还是工具调用卡住。
- 本轮真实 runner trace smoke：MiniMax-M2.7 case `runner_stage_trace_20260510_145432` 已验证 level 3 事件真实落盘，覆盖 4 次模型请求/响应和 3 次工具调用开始/结束；同时暴露 `spawn-subagents --count 1` 默认建 worker，不适合当 root/coordinator 层级测试入口，下一步需补正式 root/coordinator 创建方式。
- 本轮 root/coordinator seed 入口：`spawn-subagents --role coordinator --agent-name root-coordinator --count 1` 会创建真正的 root/coordinator，带调度/看板、读取工具和报告写入工具；coordinator 可以写自己的计划、分工、证据和协调报告，但不能替下层 worker/writer 写最终业务产物。用于真实 E2E 时外层只启动这个 root，后续必须由 root 创建子代理、子代理创建孙代理、孙代理创建孙孙代理。
- 本轮 role-template observer E2E 修正：`dispatch_subagents` 返回值现在透传真实 runner 创建的下级数量、child ids 和 roles，主代理能区分“dispatch 记录数”和“真实孩子数”；内置 `coordinator/bug_finder/tester/acceptor/researcher` 补齐报告写入工具，runner prompt 明确 coordinator 写协调报告、worker/writer 写最终产物。Focused 回归 `22 passed`，真实 MiniMax 观察到 root 自己创建 6 个直接孩子；剩余 gap 是旧模板 run 在创建孩子后仍超时，下一轮需验证新模板下 root 能写自己的 report 并更快收口。
- 本轮 root -> child -> grandchild -> leaf 真实 4 层 guard smoke 已跑通：外层测试控制器只启动 root，root 创建 child coordinator，child 创建 grandchild coordinator，grandchild 创建 leaf，leaf 写出 `proof.txt=hierarchy4-guard-ok`，最终 4 个节点全部 `DONE / VERIFIED`。
- 本轮真实 4 层 E2E 修复三类收口问题：runner 只有 artifacts 但漏写 `evidence_packets` 时会自动补 refs-only artifact evidence packet；空 `test_execution.json` 且没有可执行 tests 但有 artifact/evidence refs 时降级为 `inspect_only`，不再误判 rescue；同一次 `schedule_child_subagents` 若混建 coordinator 和 leaf 会返回 `mixed_coordinator_leaf_children`，避免模型把层级职责拍平。
- 本轮 prompt template 懒加载第一片已落地：角色模板现在分 `role_template_index_text()` 和 `role_template_detail_text()`。主代理常驻工具说明只注入轻量模板索引（id、中文名、能力标签、模板位置），不展开完整系统提示片段；普通 runner 只加载“当前角色”的模板详情，coordinator runner 真正派工时才加载模板全集详情（适用场景、默认工具、输出合同、中文系统提示）。后续真实 E2E 仍最多先测到主/子/孙/孙孙 4 层，但代码不写死 4 层。
- 本轮 role-template report-write boundary 真实复测：外层只启动 main/root，root 真实创建 researcher、worker、writer、bug_finder、tester、acceptor 六类 direct children；复测暴露主模型最终自然语言会编错 child id/path，后续 reporting 必须以 structured refs 为准。已修复 explicit root/coordinator seed 误收 `run_command`/`fetch_url` 的问题，并把 report-only 角色的报告写入能力和最终产物写入根分离：coordinator/researcher/tester/bug_finder/acceptor 只写 task-local 报告，worker/writer/leaf_worker 才继承产品产物目录。
- 本轮 boundary retest after policy fix 暴露工具调用解析上游问题：MiniMax 把完整 `create_subagents` JSON 后多吐一个 `}`，解析失败后模型自我修复时把 goal 缩短到“角色模板边界复测”，导致 root 没拿到 6 类 child 任务。已新增窄口 JSON repair：只修“有效对象后剩余内容全是右花括号”的情况，不吞第二个对象；下一轮真实复测要确认 root 能拿完整 goal 并真实创建 6 类 child。
- 本轮 boundary retest after parser fix 已确认 root 能拿完整 goal 并真实创建 6 类 child；继续暴露并修复两个权限边界问题：显式 root/coordinator 只保留产品路径上下文、不继承最终产物写入根；模型把 `role=child` 但 `agent_name=researcher/tester/acceptor/...` 时，scheduler 会先归一化真实角色再套写入根策略。真实 MiniMax run `role_template_boundary_retest_20260510_200613` 验证 root/report-only roles 只写 task-local，worker/writer 才拿 deliverables。
- 本轮 runner-context partial-progress 第一片：`dispatch_subagents` 在 runner 内返回直接孩子状态时新增 `needs_more_dispatch`、`unfinished_run_ids`、`next_action=continue_dispatch_direct_children` 和 `suggested_tool_call`，让 root/coordinator 面对限速或串行波次未跑完的 child 时拿到机器可读的继续动作，而不只是一句自然语言提示。
- 本轮 runner partial-success 记录第一片：runner 自己 `TIMEOUT` 但已经创建 child 时，dispatch record 会保存 `runner_child_status_counts`、`runner_unfinished_child_ids` 和 `runner_partial_success`，恢复流程不再只能看到单个失败状态。
- 本轮 role-template continue-dispatch 真实复测：CLI 显式 root/coordinator seed 现在只保留产品路径上下文，不再继承最终产物写入根；真实 MiniMax root 自己创建 researcher/worker/writer/bug_finder/tester/acceptor 六类 direct child，并根据 `needs_more_dispatch` / `unfinished_run_ids` 发起第二波 dispatch。tester/bug_finder/acceptor 成功发现 worker 产物的真实 import bug；剩余 gap 是 root 第二波后仍超时，下一轮要做 partial-success finalization / rescue follow-up 闭环。
- 本轮 parent acceptance 安全 pytest 归一化第一片：父级 dry-run 预检和手动 auto-execution 都会先复用 `prepare_test_items()`，把 workspace 内安全的 `cd <dir> && python3 -m pytest ...` 转成 `working_dir + 纯命令`，不再误判为人审；仍不放开 shell，也不允许越界目录。
- 本轮 Context Bundle 多层传递和异常恢复第一片：每个 run 的 `context_bundle.json` 新增 `lineage`，记录 root、parent、depth、自己的 bundle ref 和直接父级 bundle ref；四层 root/child/grandchild/great-grandchild focused 测试确认子孙节点能按 refs 追到父级交接包。`takeover_readiness.json` 和 recovery-tree 节点也会暴露 context bundle refs，失败、阻塞、超时和父超时残留 child 的接管/恢复流程能先读交接包再看 checkpoint/artifact manifest，仍不读取 artifact 正文、不自动接管。
- 本轮真实小烟测观察补强：真实 root->child 链路暴露 child 调用 `schedule_child_subagents` 失败时现有 level 3 trace 只能看到 `ok=false`，看不到模型传参和工具错误正文；新增 `subagent_debug_trace_level=4/5`，level 4 在 JSONL 写短预览，level 5 把完整 prompt、response、tool payload 和 tool output 写到内部 `debug_traces/details/`，只用于 E2E 排障，默认 0 不写。
- 本轮真实四层 trace5 小烟测修复第一片：level 5 追踪确认 root->child->grandchild->leaf 能逐层创建，但 leaf 因模型漏传 `acceptance_checks` 被 Context Gate 阻塞；层级调度器现在会在 child spec 省略验收项时，从自包含 goal/角色派生最小验收项，避免叶子因字段缺失直接停住。工具循环也新增 max-tool-round hard stop：达到工具轮数上限后，如果模型仍输出 `[TOOL_CALL]`，系统返回确定性停止说明，不把新工具请求当最终答复。
- 本轮真实四层 trace5 复测已跑通：外层只启动/观察 root，root 创建 child，child 创建 grandchild，grandchild 创建 leaf，leaf 真实写出 `proof.txt=context-lineage-ok`，最终 root dispatch acceptance 通过。复测同时暴露并修复父级验收里模型输出 `cat <file>` 的问题：现在安全的 workspace-local `cat` 内容检查会被归一成受控 `content_check`，并支持 `content_equals` + `match_mode=exact`，不需要放开 `cat` 命令白名单。
- 本轮 Stage7 购物网站层级 E2E 第一轮真实观察：外层只启动 root/coordinator，root 自己创建 frontend/backend leads，frontend lead 继续创建 auth/shop coordinators，shop coordinator 成功创建 products leaf，products leaf 已写出 `frontend/products/list.html`。同时暴露两个长期问题并已先修代码入口：coordinator 遇到最终产物目录写入保护后，部分模型会误以为该申请自己拿写权限；runner tool-loop 会把大段 `write_file(content=HTML...)` 的工具调用原文塞回下一轮 prompt，导致上下文膨胀和模型请求长时间无返回。现在 coordinator prompt/模板明确要求转派 worker/writer/leaf_worker，不给自己申请产物写权限；新增 assistant tool-call context reducer，只把大工具参数的长度、hash、路径和短预览放回 live prompt。

## 未跑测试

- 当前 runner 阶段心跳已用 focused stub tests 和真实 MiniMax 小 smoke 覆盖；root/coordinator seed 和 continue-dispatch 已用真实 MiniMax 小型角色模板复测覆盖，购物网站 Stage7 已完成第一轮真实暴露和局部修复，但尚未复跑到注册/登录/购买全链路全绿。
- workflow apply 已有实现，但仍需要继续补更贴近真实 dispatch 的端到端回归，尤其是 worker 子工单依赖、验收阻断和失败回放。
- 同步门目前只覆盖 `log-analysis` 和 `subagent` 两个模块；其它模块还需要先补四件套和规则映射。
- parent/subagent 跨天恢复已有确定性 backend 场景和真实 API 多轮恢复记录；后续交付级变更仍应按风险补跑真实 API 冒烟。

## 风险

- 旧文档里已有大量 subagent 设计细节，第一版索引还没有逐段拆入四件套。
- workflow 已经能从规划进入真实 worker 子工单创建，但产品风险还在：什么时候需要用户确认、怎么展示自动选择理由、如何避免高风险任务被过度自动化，还需要继续验证。
- 并行 worker 可能同时补文档，后续需要以模块四件套为主入口，避免再次分散。
## 2026-05-06 code-size cleanup
- 中文说明：这一轮把 subagent workflow routing、manager helper、patch review、runner rendering 和 service utility 拆成更小模块，行为和公开 dataclass/兼容导出不变，只是把大文件风险降下来。
- Split subagent workflow routing, manager helpers, patch review, runner rendering, and service utilities into smaller focused helpers.
- Kept public report dataclasses, patch rendering compatibility, and re-export behavior intact.
- Verified with subagent-focused pytest, ruff, and the global code-size report.

## 2026-05-07 bundle interface completion
- 中文说明：这一轮把 subagent 主要服务入口改成 bundle-first：创建、dispatch、action、patch、acceptance、runner、recovery 等都先构造明确 Params/Options，再进入服务逻辑；新业务不再用散乱 `**kwargs` 扩字段。
- Converted subagent manager/core action, dispatch, indexing, create-run, patch review/apply, runner next-action, state transition, run-subagent, recovery snapshot, and dispatch-loop compatibility paths to bundle-first or explicit keyword -> bundle adapters.
- Added an architecture guardrail that blocks new product-code function var-keyword service interfaces; only transparent retry decorator forwarding remains exempt.
- Focused verification covered policy checks, manager actions/dispatch/indexing, bundle interfaces, dispatch loop/watchdog, subagent runner, acceptance, patch review, and mixin recovery snapshot paths.

## 2026-05-07 hard/soft code-size cleanup
- 中文说明：这一轮清掉 subagent patch/action 的 soft 和 high-risk 体积风险，但不改变 manager facade。patch/action/acceptance 的记录构造、证据构造、渲染逻辑被拆成小 helper，方便后续功能继续加而不挤爆入口。
- Cleared current subagent patch/action soft findings without changing public manager facades.
- `SubAgentPatchMixin` remains a thin adapter over patch services; action application now keeps helper context bundled so follow-up fields do not lengthen service signatures.
- Split acceptance Markdown item rendering into shared helpers, keeping verifier checks, findings, and record summary output identical while reducing function-level near-soft risk.
- Continued high-risk cleanup by moving patch review record helpers, action record/log helpers, acceptance evidence helpers, and orchestration specs behind smaller focused modules while keeping manager/service facades compatible.
- Focused verification covered manager patch, manager actions, strict code-size, ruff, and architecture guardrails.
- 2026-05-07 workflow high-risk cleanup continued in router, planner, and parent acceptance helpers; route fields, compile inputs, and final-gate checklist expansion stay separated so worker self-report cannot become the acceptance source.
## 2026-05-07 LLM annotation coverage update
- 中文说明：这一轮只补产品代码的定义级注释，不改行为、文件格式、工作流语义或公开接口。以后改 module/class/def 的行为、bundle 或副作用，要同步维护 `LLM:` 和 `函数用途:` / `类用途:`。
- Product-code modules, classes, functions, and methods in the active module now carry the required `LLM:` plus `函数用途:` / `类用途:` definition-level double-layer comments format.
- This is a documentation-only maintainability pass: behavior, file formats, workflow semantics, and public interfaces are intended to stay unchanged.
- Future module changes must keep these comments current when changing module/class/def behavior, side effects, bundles, or caller expectations.
## 2026-05-08 status/board takeover view
- `status --json`、人类 `status` 和 `subagents` 看板现在会展示 `Takeover View`，把需要接管的 run、failure handoff ref、takeover readiness ref 和 recommended read order 放到同一个视图里。
- 该视图只读取 `takeover_readiness.json` 恢复索引，不展开 artifact 正文；大输出仍必须走显式 artifact 读取命令或工具。
- 回归覆盖 `status` payload、人类输出和 `subagents` 看板，额外断言大 artifact body 不会被内联进 status payload。
## 2026-05-09 subagent lightweight status guard
- `SubAgentBoardOptions` 新增 `include_child_status_counts`，`status` 和 startup recovery 使用轻量模式，不展开 child status counts。
- 完整 board 仍保留 child counts，但通过本轮已加载的 task 索引计算，避免按 child id 反复读取任务文件。
- 新增 refs-only 守卫测试：默认 `status` payload 不允许读取 `logs/runner_prompt.md`、`logs/runner_response.md` 或大 artifact body。
- 开发规范新增 subagent token / model-call budget：状态、看板、startup、due-check、acceptance-plan 等默认查询不得调用模型、执行命令或展开冷文件。
- 本轮 focused 验收：`python3 -m pytest agent_py_agent/tests/test_status_shared_progress.py -q` -> `8 passed`；`python3 -m pytest agent_py_agent/tests/test_subagent_commands.py -q` -> `16 passed`。

## 2026-05-11 controlled tools stage 1
- 中文说明：能力申请、授权和缺口记录已扩展成 scoped bundle，能表达 shell、MCP、tool、skill、路径、网络和输出预算需求；旧字段仍兼容，旧调用不需要一次性迁移。
- `CapabilityRequest` 新增 `capability_type`、requested tools/skills/MCP/commands、cwd/path/network scope、output budget、risk、fallback 和 escalation 字段。
- `CapabilityGrant` 新增 grant type、MCP tools、command allowlist、path/network scope、output budget、risk、expires_at 和 reserved 字段；这些字段只是授权边界，不代表自动执行。
- `CapabilityGap` 新增 gap type、requested scope、escalation chain、next refs 和 reserved 字段；无解问题能保留原始申请范围，方便父级继续路由或沉淀 skill_spark。
- 结构化 runner 输出里的 `capability_requests` 会保留这些扩展字段；持久化读取会忽略未来未知字段，避免后续 schema 小扩展卡死旧记录。

## 2026-05-11 controlled tools stage 2
- 中文说明：父级 capability route 现在会把申请范围真正带进 dry-run report、grant 和 gap；能解决时是 scoped grant，不能解决时保留 requested scope 和 escalation chain。
- 命中能力卡时，grant 会写入 `grant_type`、command allowlist、MCP tools、path/network scope、output budget、risk 和 request scope reserved；`allowed_tools` 仍只合并明确授权的工具。
- 未命中能力卡时，gap 会写入 `gap_type`、requested scope、escalation chain 和 `capability_request:<id>` ref，方便父级继续上抛、人工处理或后续 skill_spark 学习。
- capability route dry-run record 也带 `request_scope` / `grant_scope`，让人类或上级 agent 在 apply 前就能看到授权边界。

## 2026-05-11 controlled tools stage 3
- 中文说明：新增 `shell_gateway.py` 干跑骨架，只判断命令是否允许，不执行任何 subprocess。
- dry-run 会检查命令解析、shell 高风险字符、危险命令、父级 command allowlist、workspace/cwd/allowed roots、curl 网络范围和输出预算。
- `rm/rmdir/sudo/dd/chmod/chown/kill` 等危险命令即使写进 allowlist 也会被拒绝；后续删除文件走 task trash。
- dry-run 决策会返回 `allowed`、`would_execute`、`blockers`、解析后的 `argv`、cwd、输出预算和 audit 摘要，为 execute v1 复用同一闸门。

## 2026-05-11 controlled tools stage 4
- 中文说明：`shell_gateway_execution.py` 已接入 shell gateway v1，只有 dry-run 闸门通过后才会启动 subprocess，且全程不使用 shell 展开。
- stdout/stderr 通过 pipe 持续读取并只保留预算内字节；超过预算的内容只计总字节和 truncated 标记，避免大日志进入内存或上下文。
- 执行输出写到 workspace 内 `shell_gateway_outputs/` 或调用方传入的 workspace-local artifact dir；越界 artifact dir 会回退默认目录。
- 审计写 `shell_gateway_audit.jsonl`，只记录元数据、输出引用、字节数和阻断状态，不内联 stdout/stderr 正文。

## 2026-05-11 controlled tools stage 5
- 中文说明：新增 `task_trash.py`，给每个任务目录提供 `trash/manifest.jsonl`，子代理后续删除类动作应走 move-to-trash 而不是 `rm`。
- `move_to_task_trash()` 只允许移动 task_dir 或显式 allowed_roots 内的路径，不能移动任务目录本身，也不能再次移动 trash 内的文件。
- trash 目录被用户或清理钩子删掉后，`ensure_task_trash()` 会自动重建，并保证 manifest 文件存在。
- manifest 只记录 source、destination、reason、actor_run_id 和时间，不读取或内联文件正文。

## 2026-05-11 controlled tools stage 6
- 中文说明：新增 `fallback_report.py`，父级可在子代理正常写入失败、通道异常或工具缺失时保存 task-local 兜底报告。
- 兜底报告写入 `reports/fallback_report.md` 和 `reports/fallback_report.json`，只包含摘要、细节和 artifact/evidence refs，不自动展开大文件正文。
- 新增本地 E2E：capability request/grant -> shell gateway execute -> stdout 移入 task trash -> fallback report，验证受控工具链路可以完整闭环。

## 2026-05-11 Stage7 shopping E2E follow-up
- 中文说明：购物网站真实 E2E R2 跑出了 root -> 4 coordinator -> worker/leaf 链路，产出认证和商品页面，但购物车/结账未完成，上层在 480s smoke 边界内超时。
- 已修正两个核心口子：`write_file` 大 payload 现在在 assistant round 和 tool-call record 两处都只回写摘要；worker/writer/leaf_worker 的 child spec goal 自己写出产物目录时，也会参与写入根授权候选。
- 仍未完成：静态站点验收需要拦截缺失页面、`${...}` 模板占位符和失效链接/图片；coordinator 在部分子树成功/失败后还需要更早停止并交接，而不是继续读 refs 到超时。

## 2026-05-11 Stage7 shopping E2E R3 stabilization
- 中文说明：R3 真实跑到 root 创建 4 个 coordinator 后主动停下，定位到三个调度层问题：coordinator 显式工具列表会丢派工能力、dispatch 不能按 root 指定的 child ids 精确推进、live tool context 太像工具调用导致模型复制出 parse error。
- 已修正：coordinator 显式工具会合并内置 coordinator 工具包；`dispatch_subagents` 支持 `run_ids` / `include_run_ids` 精确过滤并按顺序执行；未完成 direct children 的建议工具调用会带 `run_ids`；历史工具记录改成中性 marker 和摘要行。
- 下一步：用干净 R4 重新跑购物网站层级 E2E，重点看 root 是否按 auth/catalog -> cart/quality 的顺序推进，并继续验证完整购物流程、按钮和图片链接。

## 2026-05-11 Stage7 shopping E2E R4 URL write-root fix
- 中文说明：R4 证明 root 能创建 4 个 coordinator，auth 分支能创建 leaf_worker 并写出 `auth.html`；但 catalog 分支暴露图片 URL 被误判成本地写入根，导致 worker 创建被拒。
- 已修正：写入根提取器会先标出 URL 范围，跳过 URL 内的 `s:/` 和 `//host/path` 片段；Windows 盘符匹配也不再允许从单词中间开始。
- 下一步：用干净 R5 复测 catalog 商品页 worker 创建和页面产出，同时补 quality 的阶段依赖，避免完整产物没出来就先验收。

## 2026-05-11 Stage7 shopping E2E R5 hierarchy/artifact stabilization
- 中文说明：R5 真实跑出了 auth coordinator -> auth leaf -> deliverables 产物写入，但也发现 root 会重复创建同域 coordinator，且 deliverables 产物在接管 manifest 里被误标为越界。
- 已修正：同一父节点的 coordinator/checker/tester/reviewer 类 child 会按领域词去重，避免重复 checkout/quality 分支继续膨胀。
- 已修正：artifact manifest 现在把 `allowed_write_roots` 也纳入安全元数据解析边界；被授权的业务产物会显示 `resolved`，未授权外部路径仍然 blocked。
- 下一步：用干净 R6 复测 root -> coordinator -> leaf 链路，重点验证 catalog worker 不再被 URL 拦住、重复分支被挡住，并继续补 producer/quality 阶段顺序。

## 2026-05-11 Stage7 shopping E2E R6 multi-run instruction guard
- 中文说明：R6 真实测试证明同域 coordinator 去重已生效，但 root 同轮调度 auth/catalog/cart 时，把 auth 专属 `runner_instruction` 广播给了三个孩子，导致 cart 分支也创建 auth leaf。
- 已修正：当同一次 dispatch 选中多个 pending runner 且带共享 `runner_instruction` 时，调度器会清空该共享指令并记录 `ignore_multi_runner_instruction`；单 run_id dispatch 仍保留专属指令。
- 已同步：`dispatch_subagents` 工具说明和 coordinator runner prompt 明确 task-specific instruction 只能给单个 run_id；多个孩子需要分别 dispatch，或把通用要求写进 child goal/context bundle。
- 下一步：用干净 R7 复测 root -> coordinator -> leaf，确认身份不再串线，并继续做 producer/quality 阶段依赖和静态购物流程验收。

## 2026-05-11 Stage7 shopping E2E R7 scoped run-id guard
- 中文说明：R7 确认多 runner 指令串线已消失，auth 分支完成，catalog 分支开始写商品页；新的真实问题是 cart coordinator 把自己的 child run_id 抄错，围绕不存在的目录反复读取。
- 已修正：`dispatch_subagents` 在 runner 候选执行前预检 `include_run_ids`；只要显式 id 不存在或不属于当前 parent/root scope，就返回 `runner_selection/invalid_run_ids` 阻断记录，列出可用 direct child ids，并在唯一短后缀匹配时给出 `possible_corrections`。
- 已修正：同父级 coordinator/checker 去重现在会过滤 generated id 片段和 `grand/one/two` 这类泛化编号词，避免误挡 recovery 树里的 `grand-1` / `grand-2` checker siblings。
- 设计边界：工具层只提示，不自动替换 id；这样避免隐形改写模型意图，也避免极小概率后缀撞车时跑错孩子。
- 下一步：用干净 R8 复测 root -> coordinator -> leaf，看 cart coordinator 是否能根据阻断记录重试正确 child id，然后继续补 producer/quality 阶段顺序和完整购物站静态验收。

## 2026-05-11 Stage7 shopping E2E R8 path-drift guard
- 中文说明：R8 真实测试中 auth/catalog 分支完成并写出页面，但 cart coordinator 把父级的 `/build` 目标漂移成 sibling `/stage7_r8_build`，随后继续围绕错误目录和错误 artifact refs 空转。
- 已修正：`hierarchy_scope_guards.py` 会在 child run 落盘前检查 child goal / extra_write_roots 是否仍位于父级继承的权威产物根下；如果模型 invent sibling 目录，会返回 `child_write_root_drift` 并列出 invalid/valid roots。
- 已修正：`read_artifact` 仍以 `tool_outputs/index.jsonl` 为唯一事实源，但现在能用唯一 artifact 文件名修复“路径前缀抄错”的情况。
- 已修正：`dispatch_subagents` 把 `runner_selection_recovery` 放进顶层 payload；`subagent_board` 增加 `actionable_run_ids` 并截断长 goal，让大报告被外置时模型仍先看到该用哪些 run id。
- 下一步：用干净 R9 复测 cart 分支是否能被 path-drift guard 纠回 `/build`，然后继续做 producer/quality 阶段依赖和静态购物流程验收。

## 2026-05-11 Stage7 shopping E2E R9 board/recovery ergonomics
- 中文说明：R9 root 正确创建了 4 个一级 coordinator，但随后使用模型猜出的旧 run id 调度失败；dispatch payload 已给出正确 ids，但 root 又调用 `subagent_board(status="ALL")`，旧逻辑把 ALL 当成真实状态过滤，返回 0 条，导致它继续围绕错误 id 读不存在文件。
- 已修正：`subagent_board` 现在把 `status=ALL/*/ANY` 当成不过滤，保留全部看板条目和 `actionable_run_ids`。
- 已修正：`runner_selection_recovery` 顶层 payload 新增 `valid_run_ids`，从 evidence task refs 提取机器可读 run id，模型无需从中文 message 里解析。
- 设计边界：系统仍只提示正确 id，不自动替模型重写并执行 dispatch；这样避免隐形跑错孩子。
- 下一步：用干净 R10 复测 root 是否能根据 `valid_run_ids` / board `actionable_run_ids` 重试正确 child id。

## 2026-05-11 Stage7 shopping E2E R10 phase gates
- 中文说明：R10 确认 root 能创建 4 个一级 coordinator，并能让 auth/catalog/cart 分支产出真实页面；但 quality coordinator 抢跑，在生产线未完成时开始验收并 BLOCKED，root 后续还绕过 coordinator 直接创建多个 leaf。
- 已修正：runner 候选现在按阶段放行，同一 dispatch 范围内只跑当前最低阶段；producer/coordinator 未完成前，quality/test/review/acceptance 候选会等下一轮。
- 已修正：root 已经拥有 coordinator child 后，再调用 `schedule_child_subagents` 创建 leaf/worker 会被 `root_leaf_bypass_existing_coordinators` 阻断，要求先 dispatch 或修复直接 coordinator children。
- 下一步：用干净 R11 复测 quality 是否延后，root 是否不再直接创建 leaf。

## 2026-05-11 Stage7 shopping E2E R11 static-site acceptance
- 中文说明：R11 复测确认 quality 没有抢跑，root 也没有再直接创建 leaf；root -> coordinator -> worker/leaf 能写出注册、登录、商品、购物车、结账、成功页。
- 新发现：真实产物仍存在静态链路问题，例如 `order-success.html` / `products.html` 里有 `${...}`，`cart.html` / `order-success.html` 链到不存在的 `index.html`；root 在有 blocked child 时会长时间不自然收束。
- 已新增：`static_site_check` 验收方式，父级 TestExecutor 可检查 workspace 内静态站点目录的必需文件、本地 href/src/action、`${...}` 占位符和明显无动作控件，不执行 JS、不访问网络。
- 下一步：把静态站点检查接入购物网站类任务的验收/runner 提示和 rescue 流程，让失败 leaf 能由上级创建修复任务闭环。

## 2026-05-11 Stage7 recovery/duplicate follow-up
- 中文说明：R11 后补了两个“别让父节点空转”的小闭环：直接 child 失败/阻塞时，dispatch 返回会显式给 `recovery_run_ids` 和建议的受控重试工具调用；同父级已有 DONE/VERIFIED leaf 写过同一目标文件时，新的 leaf 创建会被 `duplicate_leaf_target:<file>` 阻断。
- `orchestration_progress_payload.py` 现在区分 `needs_more_dispatch` 和 `needs_recovery`；PLANNING/RUNNING 继续 dispatch，BLOCKED/FAILED/TIMEOUT/CHANNEL_ERROR 进入恢复路径。
- `hierarchy_scope_guards.py` 的 leaf 去重只读取 direct child 的任务元数据和 `output.json.artifacts` 路径引用，不读取产物正文；只有具体文件名重合才阻断，避免 cart/checkout/order-success 这类不同页面 sibling 被误挡。
- 下一步：把 `static_site_check` 自动注入静态 Web 产物的父级验收，让失败结果能自然进入恢复/修复 child 流程。

## 2026-05-11 Stage7 static web auto-test injection
- 中文说明：父级验收预处理层现在会根据 `output.json.artifacts` 自动识别多页静态 HTML 产物；如果 runner 没有显式写 `static_site_check`，系统会自动追加一条机器检查。
- 自动生成的测试只包含 `site_root` 和 `required_files`，执行时仍由 `static_site_validator.py` 检查页面存在、坏链接、`${...}` 占位符和明显失效控件；推断阶段不读取 HTML 正文。
- 已加保护：如果 runner 已经声明 `static_site_check`，不会重复追加；单个 HTML 或非 Web artifact 不触发，避免误把普通文档任务当网站验收。
- 下一步：跑干净 R12 root-only 购物网站 E2E，验证静态检查失败能自然变成父级 follow-up/rescue，而不是由外层人工发现。

## 2026-05-11 Stage7 R15 orchestration dependency guard
- 中文说明：R15 真实购物网站 E2E 暴露了两个底层问题：页面文案 `+/-按钮` 被误判成外部路径 `/-按钮`，以及模型同一轮先创建 child 又立刻用脑补 run_id 调度 child。
- 已修正：写入预检不再把 `+/-按钮`、`</body>` 这类 UI/HTML 内容当绝对路径；`tool_round_execution.py` 会把同轮中依赖前一个 stateful orchestration 结果的后续编排工具延后到下一轮，要求模型读取真实 `created_run_ids` / `actionable_run_ids` 后再 dispatch。
- 已补测试：`test_ui_symbols_and_html_tags_do_not_trip_external_write_guard`、`test_tool_round_defers_dependent_dispatch_after_schedule`、`test_tool_call_parser_unwraps_model_orchestration_bundle`。
- 下一步：用干净 R16 root-only 购物站 E2E 复测 cart-checkout 分支，确认 schedule 成功创建真实 children，下一轮 dispatch 使用真实 run ids，并继续补 root whole-site required-file 验收。

## 2026-05-11 Stage7 R16 artifact/read and whole-site oracle
- 中文说明：R16 真实购物网站 E2E 证明 R15 修复有效：cart-checkout 能先创建真实 child，再用真实 run id 调度；但也暴露 repair worker 直接 read_file 读取外置 tool-output JSON，导致 live prompt 再次膨胀，以及父级静态验收只看已观察 artifact、不看任务要求的顶层 required files。
- 已修正：`read_file` 会拒绝 `memory_archive/artifacts/tool_outputs/*.json` artifact 包装文件，并提示使用 `read_artifact artifact_ref/max_chars` 分片读取；parse error 结果会附带标准 `[TOOL_CALL]` JSON 重试格式提示。
- 已修正：父级验收预处理会从 task goal/thought/description/acceptance_checks 抽取 `index.html`、`style.css`、`app.js` 等静态站点必需文件，并合并进自动 `static_site_check.required_files`，避免分支目录局部通过但顶层购物流程缺页面。
- 已补测试：`test_read_file_rejects_tool_output_artifact_wrapper`、`test_prepare_test_items_merges_task_required_static_files`、`test_static_required_files_from_texts_extracts_static_web_targets`、`test_parse_error_result_includes_retry_format_hint`。
- 下一步：用干净 R17 root-only 购物站 E2E 复测完整注册 -> 登录 -> 商品 -> 购物车 -> 结账 -> 成功页链路，并观察 root/fix coordinator 是否仍有最终流卡住问题。

## 2026-05-11 Stage7 R17 path typo retry guidance
- 中文说明：R17 root-only 真实购物站 E2E 暴露了路径拼写误判：root 创建四个一级 coordinator 时，把一个目标路径写成 `/Users/xiaoyuzei/...`；系统正确拒绝越界路径，但 root 把提示理解成“需要扩大权限”，没有用正确的 `/Users/example/...` 重试。
- 已修正：派工写入预检现在会识别“工作区目录名和后缀一致、但前缀疑似拼错”的绝对路径，返回 `suspected_path_typo=true`、原始 `target`、`workspace_root` 和 `suggested_target`，并明确要求重新调用 `schedule_child_subagents`，不要写 `capability_request`。
- 已补测试：`test_external_write_guard_suggests_workspace_typo_retry`。
- 下一步：用干净 R18 root-only 购物站 E2E 复测 root 是否按 `suggested_target` 重试并成功创建下级，再继续观察最终收口和完整购物流程验收。

## 2026-05-11 Stage7 R18 URL/path recovery hardening
- 中文说明：R18 验证 R17 修复有效，root 成功创建四个一级 coordinator，并由 leaf 真实写出 auth 页面和 cart/checkout/order-success 页面；本轮最终因 MiniMax HTTP 529 过载变成 BLOCKED，不能算完整验收通过。
- 新发现：派工写入预检会把 `https://via.placeholder.com/300x200` 误切成 `s://...` 当成本地路径；普通 `read_file` / `list_files` 遇到 `/Users/xiaoyezei/...` 这种用户名拼错只返回泛化越界提示，模型会继续错误重试。
- 已修正：新增 `path_recovery_hints.py` 统一提供 URL span 和工作区路径 typo 建议；派工写入预检跳过 URL 内部路径片段；文件系统读/列工具遇到 suffix-matching 工作区路径拼写错误时返回 `suspected_path_typo=true` 和 `suggested_target`。
- 已补测试：`test_external_write_guard_ignores_url_image_sources`、`test_filesystem_tool_suggests_workspace_path_typo`。
- 下一步：用干净 R19 root-only 购物站 E2E 复测 URL 不再阻断 catalog leaf，路径 typo 能自我纠偏，并观察 529/blocked 子树的恢复接管链路。

## 2026-05-11 Stage7 R19 recovery/path ergonomics
- 中文说明：R19 root-only 真实购物站 E2E 已跑到 10 个顶层必需文件全部落盘，证明 root -> coordinator -> leaf 链路能产出完整静态站；但 shared-assets 修复 `app.js` 时被重复 leaf 去重挡住，root 恢复提示还不够可执行，外置 artifact 路径也再次出现前缀抄错。
- 已明确架构边界：root 不固定必须创建 coordinator，也不禁止直接创建 worker；实际层数由任务复杂度和 prompt 约束决定。当前“四层”只是某些 E2E 的测试要求，不能写死到通用调度里。
- 已修正：明确 repair/update/fix leaf 可绕过同父级同目标文件去重；coordinator/root 可写自己的 agent-run workspace 报告；`runner_timeout_seconds: "off"` 会同步影响模型可见的 dispatch due-check；静态站 `${...}` 检查会跳过 `<script>/<style>`。
- 已修正：外置工具输出的 live prompt 增加 `output_artifact_ref`、`output_call_id` 和可复制的 `read_artifact` 示例；如果模型把 `memory_archive/artifacts/tool_outputs/*.json` 的绝对路径前缀抄错，`read_file` 也会提示“不要用 read_file，改用 read_artifact + artifact_ref + max_chars”。
- 已对照学习：长期助手 / 会话运行时 / 终端交互 / 通道运行时 / claw-code 都在不同程度上使用结构化 cwd/workspace、相对路径/短 ID、输出截断或 claim-check、路径边界校验；结论是我们也要减少模型复制长绝对路径，让工具用短 ref 和边界校验兜底。
- 已补测试：`test_hierarchy_schedule_allows_explicit_repair_leaf_for_existing_target`、`test_build_execution_context_write_boundary`、`test_dispatch_tool_respects_runner_timeout_off_for_auto_due_check`、`test_static_site_check_allows_javascript_template_literals`、`test_read_file_typo_to_tool_output_artifact_routes_to_read_artifact`、`test_tool_loop_externalizes_large_tool_output_for_archive`。
- 下一步：用干净 R20 root-only 购物站 E2E 复测：root 不被硬性 coordinator 策略绑死，shared-assets 能创建修复 leaf，artifact 读取优先用短 `call_id`，并开始补浏览器级完整注册/登录/购买流程验收。

## 2026-05-11 Stage7 R28 scoped artifact refs
- 中文说明：R28 真实测试定位到一个更底层的 prompt 串线来源：`read_artifact("17-1")` 这种短 call id 在不同 run 里重复，旧逻辑从 index 顶部找第一条，可能把旧 R15/R19 artifact 内容注入当前 R28 prompt。
- 已修正：tool-output artifact 记录和 index 现在带 `run_id`、`task_id`、`request_id`、`scoped_call_id`；子代理 runner 会把当前 run id 透传进工具循环；`read_artifact` 读取短 id 时优先匹配当前作用域，没有作用域时也取最新记录而不是最老记录。
- 已修正：live prompt 的 `read_artifact_hint` 不再只给裸 `call_id`，会优先给具体 artifact path 和 run/task/request scope，降低模型复制短号串线概率。
- 已修正：FailureIntrospector 支持 Markdown JSON 代码块，避免真实模型返回 fenced JSON 时误降级。
- 对照学习：长期助手 session/transcript、通道运行时 per-agent workspace/session、会话运行时/模型助手 类结构化 tool call 都说明同一短编号必须被 run/session/workspace scope 包住，不能裸放在全局空间。
- 已补测试：`test_read_artifact_short_call_id_prefers_matching_run_scope`、`test_read_artifact_short_call_id_without_scope_prefers_latest`、`test_tool_loop_externalizer_falls_back_to_current_subagent_run_id`、`test_introspect_with_llm_fenced_json_success`。
- 下一步：用干净 R29 root-only 购物站 E2E 复测 scoped artifact refs；如果不再读旧 artifact，再强化 root/coordinator 的 rescue 收敛策略，减少反复读 board/artifact 的空转。

## 2026-05-11 Stage7 R29 structured result recovery
- 中文说明：R29 真实 root-only 购物站 E2E 已产出 10 个目标文件，并跑出 root -> 子 -> 孙 -> 孙孙 4 层命名链路；禁止文件名没有落盘，scoped artifact refs 没再把旧 run 内容串进当前 prompt。
- 新发现：root 和两个 HTML coordinator 最后因为缺少 `[/SUBAGENT_RESULT]` 结束标记被判 `BLOCKED/UNVERIFIED`，但业务产物已经存在；CLI 最终自然语言不能作为唯一通过标准，必须以机器 task 状态和验收记录为准。
- 已修正：`parse_subagent_runner_output` / parent planner 共用的结果块提取逻辑现在会在缺尾标记时窄范围恢复完整 JSON；只有标记后面直接是 JSON、`json` 前缀 JSON 或 JSON fence 且 JSON object 完整时才恢复，真正截断的 JSON 仍失败。
- 已补测试：`test_parse_complete_json_without_end_marker`、`test_parse_incomplete_json_without_end_marker`，并回归 fenced/prefixed parser 用例。
- 下一步：用干净 R30 复测缺尾标记恢复是否让 coordinator 状态正确进入待验收；同时补 finalizer/report，避免“产物在但状态 blocked”被汇报成 E2E passed。

## 2026-05-11 Stage7 R30 product-root guard
- 中文说明：R30 复测没有再遇到缺 `[/SUBAGENT_RESULT]` 的结构化解析误判；root -> 子 -> 孙 -> 孙孙链路能跑起来，但模型首次 `create_subagents` 漏掉顶层 `extra_write_roots`，root 没拿到用户指定 deliverables 目录，随后把内部 `tasks/<root>/agents/<root>/build` 当成业务 build，10 个文件都写进内部 runtime，用户产物目录保持 0 文件。
- 已修正：`create_subagents` 对显式 root/coordinator 增加产品写入根门禁。目标像“交付网站/文件”但没有 `extra_write_roots` 或 goal 内绝对产物路径时，系统会拒绝创建，要求模型把 `extra_write_roots` 放在工具 JSON 顶层重试，避免继续发明内部 build 目录。
- 已补测试：`test_explicit_coordinator_product_delivery_requires_write_root`；同时记录 root failed 后顶层 CLI 仍挂起、验收只看内部 build 不看用户 deliverables root 的真实问题。
- 下一步：用干净 R31 复测 root-write-root guard：模型漏传时应被工具拒绝并自我重试；成功后必须写入 `/Users/example/my-终端应用/deliverables/.../build`。

## 2026-05-11 Stage7 R31 evidence and repair hardening
- 中文说明：R31 真实 root-only 复测已跑出 4 层链路：root -> `小傻妞-目录管理` -> `小小傻妞-任务拆分` -> `小小小傻妞-文件创建`；10 个目标文件全部落在用户指定 deliverables build 目录，说明 R30 的产品写入根方向有效。
- 新发现：业务产物完整，但 child/coordinator 的结构化结果缺 `evidence_packets`，孙节点和 root 的 repair 回合又因上下文太胖二次截断，最终机器状态仍是 BLOCKED/FAILED，不算完整 E2E 通过。
- 已修正：runner 结果模板现在明确要求 `evidence_packets`，并说明成功时必须带 `artifact_refs` 或 `evidence_refs`；结构化修复 prompt 会裁剪原 prompt/响应，只保留尾部关键上下文，避免修复输出没空间闭合。
- 已补测试：`test_prompt_contains_result_block_markers`、`test_repair_prompt_clips_large_prompt_and_response`、`test_subagent_runner_repairs_missing_structured_output`。
- 下一步：用干净 R32 复测 evidence-packet 模板和 compact repair prompt；如果 root 失败后顶层 CLI 仍不退出，再优先修 run-loop 的失败收束边界。

## 2026-05-11 Stage7 R32 partial-result recovery and schedule ergonomics
- 中文说明：R32 真实 root-only 复测跑出 root -> `小傻妞-前端协调` -> `小小傻妞-coordinator` -> `小小小傻妞-HTML`，10 个购物站文件全部写进用户指定 deliverables build 目录，并实际测试了 broadcast/direct 消息。
- 新发现：真实模型会只写 `agent_name="小小傻妞"` 这类无后缀层级名，旧命名修正逻辑会抛裸 `IndexError`；leaf/coordinator 结果块也可能在长 artifacts/evidence 列表尾部截断，但前面的 `evidence_packets` 已经完整可追溯。
- 已修正：层级命名 helper 对只有中文前缀的名字回退到 role 后缀，`schedule_child_subagents` 底层参数异常会转成结构化工具错误；runner parser 在成功态截断时可从完整的 traceable `evidence_packets` 恢复最小结果，数组尾部截断时保留前面已闭合的证据对象。
- 已修正：runner contract 提醒大结果优先写 `execution_context.output_json` 短 JSON；工具循环现在识别 `filesystem.path` 形式写出的当前 `output.json`，能触发自动收口。
- 已补测试：`test_hierarchy_schedule_repairs_bare_lineage_agent_name`、`test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error`、`test_parse_partial_success_with_traceable_evidence_packets`、`test_parse_partial_success_with_cut_evidence_packet_array`、`test_tool_round_detects_bundled_filesystem_output_json`。
- 下一步：用干净 R33 复测结果恢复和 `output.json` 收口；如果 root 失败后顶层进程仍长时间不返回，优先做 failed-root run-loop exit。

## 2026-05-11 Stage7 R33 dispatch no-progress fuse
- 中文说明：R33 真实 root-only 复测已跑出 root -> `小傻妞-*` -> `小小傻妞-*` -> `小小小傻妞-*` 四层链路，10 个购物站文件全部写进用户指定 deliverables build 目录，coordinator 直写业务产物被 guard 拦住后能创建 rescue leaf 补齐文件。
- 新发现：root 已 `DONE/VERIFIED` 且产物完整后，顶层进程仍反复执行 `due_check/action_plan/classify_blocker`；这些记录只是在历史 blocked 子节点上重复记账，没有创建孩子、没有状态变化，导致无人值守时看起来像卡死。
- 已修正：`dispatch_loop` 增加 no-progress fuse。连续两轮调度签名完全一样且均为 audit-only / record-only 动作时，循环会标记 `stopped_by_no_progress=True` 并自然停止；真实状态迁移、验收执行、runner 创建 child 不受影响。
- 已补测试：`test_dispatch_loop_stops_when_audit_only_actions_repeat`，并回归 dispatch loop / watchdog / record-only action focused tests。
- 下一步：用干净 R34 复测顶层 CLI 是否能在“root 完成但剩余只是重复 blocked 分类”时自然返回；随后修 authoritative status/handoff sync 和“无证据成功”收口。

## 2026-05-11 Stage7 R34-R36 closeout and work-order JSON hardening
- 中文说明：R34/R35 继续证明真实购物站链路能写出用户 build 目录里的 10 个目标文件，并跑通 broadcast/direct 消息；R35 还验证了所有子代理 `DONE/VERIFIED` 后可以本地收口，不再强制追加一次顶层模型总结。
- 已修正：`output.json` 自动收口会从 artifacts/evidence/report refs 补最小 `evidence_packets`；`dispatch_subagents` 在只剩 audit-only record actions 时返回 `dispatch_terminal`，提示父节点停止空转并汇报 blockers。
- 已修正：顶层主代理刚执行过 `dispatch_subagents` 且当前 subagent workspace 全部 `DONE/VERIFIED` 时，会生成 refs-first 本地收尾回答，不再发起额外模型请求；子代理 runner 内仍只走自己的 `output.json` 契约。
- R36 新发现：初始 work-order JSON 曾写成 JSON string 包 JSON object，导致父级聚合 `.get/.keys` 读取不到字段；leaf worker 也因为 build 目录不存在误判自己缺少 mkdir/shell 能力。
- 已修正：初始 `output.json`、`status_report.json`、`dependencies.json` 现在写机器可读 JSON object；`_read_json_object` 兼容旧双层编码；runner contract 和工具说明明确 `write_file` / `append_file` 会在授权写入根内自动创建父目录。
- 已补测试：`test_subagent_output_json_response_derives_packet_from_report`、`test_completed_dispatch_closes_without_extra_model_call`、`test_creates_machine_readable_default_json_files`、`test_read_nested_json_string_object`、`test_prompt_says_write_file_creates_parent_dirs`。
- 下一步：用干净 R37 复测 leaf 遇到不存在 build 目录时是否直接用 `write_file` 写短骨架并继续 `append_file` 分块；如果仍出现长 HTML 工具截断，再做更硬的分块写入引导或受控文件生成 helper。

## 2026-05-11 Stage7 R37 hierarchy coordinator-role hardening
- 中文说明：R37 真实 root-only 复测确认 work-order JSON 默认文件已是机器可读 object；但 depth=1 coordinator 创建 `小小傻妞-site-writer` 时，被四层链路 guard 误判成 leaf worker，反复返回 `hierarchy_chain_requires_coordinator_until_depth_3`。
- 新发现：层级 guard 不该因为 agent_name 里有 `writer` 就忽略明确的 `role="coordinator"`；父级 goal 里的“禁止改名（如 product.html、old-detail.html、legacy.html）”也会被旧文件名提取器误放进 required files。
- 已修正：四层链路 guard 现在优先尊重 coordinator/lead/tester/reviewer/checker 等协调角色，避免 `site-writer` 这种领域名误触发 leaf 跳层拦截。
- 已修正：required/forbidden 文件名提取器识别“如/例如/比如”这类否定例子，把反例文件放入 forbidden files，不再污染 required files。
- 已补测试：`test_hierarchy_schedule_allows_coordinator_name_with_writer_before_depth_three`、`test_file_contract_treats_negative_examples_as_forbidden_terms`。
- 下一步：用干净 R38 复测 depth=2 coordinator 创建能否继续推进到 depth=3 leaf；随后观察 leaf 是否能在 build 目录不存在时直接 `write_file` 自动建父目录，并继续验证完整购物站产物。

## 2026-05-11 Stage7 R38 shared-asset target and schedule retry hardening
- 中文说明：R38 真实 root-only 复测确认 R37 的 coordinator/`writer` 误判已修好，链路推进到 depth=3 leaf，并真实写出 `index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`。
- 新发现：`cart-writer` 只是“引入 style.css 和 app.js”，但 leaf 产物去重把 `app.js` 当成它要写的目标，导致 `duplicate_leaf_target:app.js`；随后 `blocked=true` 的 schedule 结果又消耗了一次性编排 key，父级修正重试被 one-shot guard 拦住。
- 已修正：leaf target 提取遇到“引入/引用/链接/导入/加载/use/include/import/link to”等引用语义时，只保留引用词之前的主语文件，不把后面的共享资源当成本 leaf 产物。
- 已修正：一次性编排去重只在工具结果真正推进时登记；JSON 输出里 `blocked=true` 的调度结果不登记 one-shot key，允许父级修正后重试。
- 已补测试：`test_hierarchy_schedule_allows_leaf_referencing_shared_assets`、`test_blocked_schedule_result_does_not_consume_one_shot_key`。
- 下一步：用干净 R39 复测 cart/checkout worker 是否能创建并继续推进剩余 5 个文件；同时继续观察长 `write_file`/`append_file` 工具调用的截断恢复成本。

## 2026-05-11 Stage7 R39 product-root output guard
- 中文说明：R39 真实 root-only 复测确认 cart/checkout worker 能创建，shared asset 引用不再触发重复产物 ownership；路径 typo guard 也能在 leaf 抄错 `/Users/example` 时给出 suggested target。
- 新发现：child goal 里的“完成后写 output.json”会被 leaf 理解成在用户 `deliverables/build/output.json` 写内部收口文件，污染用户产物目录。
- 已修正：工具写入边界会阻止 product_write_roots 里的内部 `output.json` 写入，并提示写 task-local `execution_context.output_json`；真实 task-local `output.json` 收口不受影响。
- 已修正：runner contract 和 coordinator 调度提示明确：`output.json` 是内部收口文件，只能写自己的 `execution_context.output_json`，不要在用户产物目录或 product roots 下创建。
- 已补测试：`test_direct_policy_blocks_internal_output_json_in_product_root`、`test_direct_policy_allows_task_local_output_json`。
- 下一步：用干净 R40 复测产物目录不再出现内部 `output.json`；随后优先处理长文件工具调用分块和模型接口超时后的 partial artifact recovery。

## 2026-05-11 Stage7 R40 root-only hierarchy success
- 中文说明：R40 真实 root-only 复测完整跑通 root -> `小傻妞-coordinator` -> `小小傻妞-site-writer` -> `小小小傻妞-小叶子-writer` 四层链路，4 个节点全部 `DONE/VERIFIED`。
- 已验证：10 个购物站目标文件全部写进用户指定 `deliverables/stage7_shop_smoke_20260511_r40/build`，产物目录没有内部 `output.json`；各子代理自己的 `output.json` 留在 runtime/subagents 目录。
- 已验证：leaf 遇到不存在的 build 目录时能继续用 `write_file`，父目录自动创建；`site-writer` coordinator 名称、shared asset 引用、product-root `output.json` guard 都在真实链路里生效。
- 仍需改进：长 HTML/JS 直接塞进工具 JSON 时仍会触发缺 `[/TOOL_CALL]` 的 parse recovery；本轮能自修成功，但真实大项目会浪费轮次并提高超时概率。
- 下一步：优先做长文件写入协议或 bounded write helper，降低模型手写大 JSON 的失败率；随后加入浏览器级 verifier，真实点击注册、登录、加购、结算等流程。

## 2026-05-11 Stage7 R41 tool-call close-marker recovery
- 中文说明：先做长文件稳定性的第一片小修复：当模型已经吐出完整工具 JSON，但忘了写 `[/TOOL_CALL]`，解析器现在会直接恢复执行，不再白白消耗一轮 parse recovery。
- 已修正：`parse_registry_tool_calls()` 缺少结束标记时会先尝试解析 raw JSON；只有 JSON 不完整或不合法时，才返回“工具调用缺少结束标记”的 parse error。
- 已保留：真正截断的 `write_file.content` 仍然不会被误执行，会继续提示模型改成 `write_file` 短骨架 + `append_file` 分块。
- 已补测试：`test_tool_call_parser_recovers_complete_json_without_closing_marker`、`test_tool_loop_executes_complete_unclosed_write_file_tool_call`，并回归 `test_tool_call_parser_reports_missing_closing_tool_marker`、`test_parse_error_hint_recommends_append_for_truncated_write`。
- 下一步：跑 focused/full gates 后提交；随后用新一轮 root-only E2E 验证 R40 那类“完整 JSON 少结束标记”的场景是否少一次模型重试。如果仍频繁长内容截断，再做真正的 bounded write helper。

## 2026-05-11 Stage7 R42 per-agent tool budget guard
- 中文说明：按当前设计先只做“单个代理”的工具调用预算；整个任务树和单次对话不设全局预算，默认不限制。这样可以挡住某个子代理卡住后 10 分钟内疯狂读写/查询，又不误伤其他兄弟代理、主代理普通聊天或长期任务。
- 已实现：`agent_core/tool_agent_budget.py` 增加按 `run_id` 分组的内存滚动窗口预算，默认 `tool_agent_budget_window_seconds=600`、`tool_agent_budget_max_calls=50`。没有 `run_id` 的主代理普通对话不会被该预算限制；不同 run_id 互不抢预算；超过预算时工具循环返回“自检/向父级上报/请求接管或提高预算”的工具结果，而不是直接杀进程。
- 已接入：`_tool_loop_service.py` 在真实执行工具前检查预算；如果超过预算，该次工具不会执行，模型会在下一轮看到预算提示并收口或上报。预算只存在当前长存活 agent 对象内，不写磁盘、不跨进程共享。
- 已同步配置：`AgentConfig`、`ToolConfig`、配置归一化和 `agent_config.yaml` 都有同名字段。`0` 表示关闭对应窗口或次数限制。
- 已补测试：`test_tool_agent_budget_ignores_calls_without_run_id`、`test_tool_agent_budget_blocks_after_per_agent_window_limit`、`test_tool_agent_budget_is_scoped_per_run_id`、`test_tool_agent_budget_prunes_calls_outside_window`、`test_tool_loop_enforces_per_agent_tool_budget_for_run_id`。
- 设计边界：受控 `exec` / shell 读写能力后续应走目录受限网关，常用读写命令可以在工作目录内低摩擦使用，但危险命令、越界路径和大输出必须继续被网关拦住。主代理长存活应由 gateway/daemon/supervisor 承接，不能因为一次任务完成或无人应答就自动挂掉。
- 下一步：设计并实现受控 shell/exec 网关的子代理授权面：工作目录限制、trash 替代 `rm`、输出大小上限、长日志读取分片、工具/skill 申请上报和审计记录。

## 2026-05-11 Stage7 R43 controlled exec grant bridge
- 中文说明：补上受控 exec 的功能框架桥：子代理不能靠自己在工具参数里写 `command_allowlist` 就获得 shell 权限，必须由父级 `CapabilityGrant` 编译成 shell gateway 请求。
- 已实现：新增 `subagents/controlled_exec_gateway.py`，提供 `ControlledExecRequest`、`ControlledExecPlan` 和 `plan_controlled_exec()`。它只相信父级 grant 的命令白名单、路径范围、网络范围和输出预算；没有父级 path scope 会阻断；`rm` / `rmdir` / `unlink` 会转成 `use_task_trash` 提示，不进入 shell 执行。
- 已保留：真实执行仍走现有 `shell_gateway_execution.execute_shell_command()`；本片只做 grant -> shell gateway 的规划层，不新增裸 shell 工具，也不把它自动暴露给所有子代理。
- 已补测试：`test_controlled_exec_uses_parent_grant_scope`、`test_controlled_exec_rejects_command_not_in_parent_grant`、`test_controlled_exec_requires_parent_path_scope`、`test_controlled_exec_routes_delete_to_task_trash`、`test_controlled_exec_uses_parent_network_scope`。
- 下一步：把这个 plan 层接到 capability route / runner context 的可见提示里，让父级 grant 后可以生成可审计的 controlled exec 入口；随后再做真实执行工具包装和输出/trace 审计联调。

## 2026-05-11 Stage7 R44 controlled exec tool apply/trash v1
- 中文说明：在 R43 的 grant bridge 上继续推进 1-5 步：runner context/context bundle 现在会显式暴露 `controlled_exec_grants`；工具 catalog 也注册了 `controlled_exec`，但它只能读取父级写进 `write_boundary` 的 grant，不能相信模型参数里的 allowlist/path_scope。
- 已实现：`tooling/controlled_exec.py` 提供 registry-aware 工具包装。`apply=false` 返回 dry-run plan；`apply=true` 且 shell gateway 检查通过时，复用 `shell_gateway_execution.execute_shell_command()` 执行 argv，并返回 bounded stdout/stderr preview、bytes、truncated 标记和 refs。
- 已实现：`rm` / `rmdir` / `unlink` 继续不进入 shell。`apply=true` 时改走 `task_trash.move_to_task_trash()`，把文件移动到 task-local trash 并写 manifest。
- 已实现：runner prompt 的 `capability_requests` 模板补齐 `capability_type`、`requested_tools`、`requested_skills`、`requested_mcp_tools`、`requested_commands`、`path_scope`、`network_scope`、`output_budget` 和 `reserved`，方便子代理缺工具/skill/MCP/shell 时结构化上报。
- 已补测试：`test_build_execution_context_exposes_controlled_exec_grant_refs`、`test_context_bundle_exposes_controlled_exec_grant_refs`、`test_controlled_exec_tool_plans_from_write_boundary_grant`、`test_controlled_exec_tool_rejects_self_authored_scope`、`test_controlled_exec_tool_apply_runs_with_bounded_audit_refs`、`test_controlled_exec_tool_apply_keeps_large_stdout_externalized`、`test_controlled_exec_tool_apply_routes_delete_to_task_trash`、`test_runner_prompt_describes_scoped_capability_request_loop`。
- 下一步：把受控 exec 接入真实 E2E runner 观察链路，验证主代理只通过子代理/孙代理创建和使用该工具；随后再设计 MCP/tool/skill 申请审批、长日志分片读取和网络工具能力包。

## 2026-05-11 Stage7 R45 controlled exec root-only E2E hardening
- 中文说明：第一轮真实受控 exec E2E 按 root-only 原则跑，外层只启动主代理；root 先错误创建 leaf 被层级 guard 拦住，随后自修为 root -> `小傻妞` -> `小小傻妞` -> `小小小傻妞` 四层链路。
- 新发现：leaf 输出 `status=PENDING_CAPABILITY_REQUEST` 但没有填写 `capability_requests` 数组时，旧状态机把它推进到等待验收，后续验收又可能被错误放行；这会让“还没拿到工具授权”的子代理看起来已经完成。
- 已修正：runner/parser 会从 `pending_steps` 兜底生成可路由 capability request；状态机把 pending capability/tool/skill/shell/MCP 状态固定为 `BLOCKED`；acceptance findings 新增 `no_pending_structured_status`，阻止这种输出被验收通过。
- 已补测试：`test_subagent_runner_parse_recovers_pending_capability_request`、`test_record_runner_result_recovers_pending_capability_request`、`test_record_runner_result_pending_capability_stays_blocked`、`test_pending_capability_output_blocks_acceptance`。
- 下一步：用干净 controlled exec R2 复测父级是否能把兜底生成的能力申请路由成 grant，并让 leaf 真正调用 `controlled_exec` 执行 `pwd` / bounded `python3` 输出 / trash 删除。

## 2026-05-11 Stage7 R46 controlled exec delegation-contract hardening
- 中文说明：R2 真实 root-only 复测跑到了四层，并验证 broadcast/direct 消息和 coordinator 写产物边界都能生效；但 root 原始目标里的 `controlled_exec` 申请、命令范围、输出预算和 trash 验收要求在多层派工中被缩水，leaf 最后只用 `write_file` 伪造了验收文件。
- 已修正：`hierarchy_context.py` 把 `controlled_exec`、`capability_request`、`requested_tools`、`requested_commands`、`path_scope`、`output_budget`、`task_trash`、`stdout_ref/audit_ref/trash_manifest_ref`、`grant` 识别为不能丢的能力/工具/安全合同，并在 child goal 继承块里原样传给下层。
- 已修正：验收层新增 `controlled_exec_contract_satisfied`，只要 goal/acceptance 声明了 controlled_exec，就必须看到真实 `controlled_exec` 工具记录和 stdout/audit/trash refs，否则 P0 阻断，防止只写同名 refs 文件就算完成。
- 已补测试：`test_hierarchy_schedule_keeps_controlled_exec_contract_for_leaf`、`test_controlled_exec_goal_requires_actual_tool_and_refs`。
- 下一步：用干净 controlled exec R3 复测：leaf 应先看到完整 capability contract；没有 grant 时上抛 capability request，父级 route/grant 后再真实调用 `controlled_exec`。

## 2026-05-11 Stage7 R47 formal capability request tool
- 中文说明：R4 真实 root-only 复测跑出四层，但 leaf 没有正式申请能力的工具，只能写 `capability_request.json` 或改 `execution_context.json` 伪造 pending_requests；系统层不会把这些文件当成 OPEN request。
- 已修正：新增 `capability_request` orchestration tool，runner 只能为当前 run 写正式 `CapabilityRequest`，不能替 sibling 或无关 run_id 越权申请；返回值带 `request_id`、`status=OPEN` 和 `next_action=route_capability_request`。
- 已接入：SimpleAgent 工具表、内置角色模板、leaf 默认工具策略都暴露 `capability_request`；runner contract 明确禁止在产物目录写 `capability_request.json` 或改 `execution_context.json` 伪造申请。
- 已修正：runner result 状态机遇到已有 OPEN `capability_requests` 时保持 `BLOCKED/UNVERIFIED/failure_type=capability_request`，不再被 `AWAITING_ACCEPTANCE` 收口误清理；只有父级 route/grant 后才进入下一次真实重跑。
- 已补测试：`test_capability_request_tool_records_open_request`、`test_capability_request_tool_blocks_cross_run_writes`、`test_capability_request_tool_is_registered_for_simple_agent`、`test_capability_request_tool_is_available_to_role_and_leaf_defaults`、`test_record_runner_result_keeps_tool_created_open_request_blocked`。
- 下一步：用干净 controlled exec R5 复测：leaf 应先调用正式 `capability_request` 工具，父级 follow-up route 后生成 grant，再重跑 leaf 使用真实 `controlled_exec` 产出 stdout/audit/trash refs。

## 2026-05-11 Stage7 R48 controlled exec R5 real-test fixes
- 中文说明：R5 真实 root-only 复测确认正式 `capability_request -> route/grant -> controlled_exec` 链路已经跑通到 depth=3 leaf；leaf 真实执行了 `pwd`、bounded `python3` 输出外置，并把删除测试导向 task trash。
- 新发现：模型把命名规则里的 `*` 原样写成 `小小小傻妞-*-*`；capability grant 报告里仍展示 `rm` 进入 `command_allowlist`，虽然执行层实际不会让它进 shell。
- 已修正：层级 agent name helper 会识别星号占位符后缀，改用 role 兜底生成可读名字，避免用户看到模板占位符。
- 已修正：父级 grant 的 shell command allowlist 会过滤 `rm/rmdir/unlink`；删除类请求只保留在 request scope 里用于审计和 task trash 替代，不再作为 shell 授权展示。
- 已修正：`dispatch_subagents` 的直接 child 进度摘要新增 `ready_for_parent_acceptance` / `summarize_direct_children_refs`。当所有直接 child 都已等待验收或完成时，工具返回会明确要求父 runner 只汇总 run_id、状态、产物 refs 和阻塞项，不要反复 `read_file/read_artifact` 打开子产物正文。
- 已记录：上层 coordinator 在 R5 汇总阶段反复读 child evidence，prompt 膨胀到约 92K 后仍未自然收口；单代理 10 分钟 50 次工具预算已有，下一轮要验证新的 refs-first 收口提示是否能减少重复读取。
- 已补测试：`test_hierarchy_schedule_repairs_literal_lineage_wildcard_names`、`test_grant_command_allowlist_excludes_delete_commands`、`test_dispatch_payload_tells_runner_to_summarize_ready_children`。
- 下一步：用干净 controlled exec R6 复测：名字不再带 `*`，grant shell 白名单不含删除命令，task trash 替代仍可用；随后优先做上层验收的 refs-first evidence summary，减少重复读取和上下文膨胀。

## 2026-05-11 Stage7 R49 controlled exec R6 grant-boundary bridge
- 中文说明：R6 真实 root-only 复测跑到 root -> `小傻妞-协调员-001` -> `小小傻妞-协调员-002` -> `小小小傻妞-叶子测试员-003` 四层；leaf 正式 `capability_request`，父级成功 route/grant，且 shell 白名单只含 `pwd`，不含 `rm`。
- 新发现：父级 route 生成的是 `capability_type=tool` 的 grant，`task.capability_grants` 能看到授权，但 `write_boundary.controlled_exec_grants` 为空，导致 `controlled_exec` 工具拒绝执行。大白话：授权单有了，但没放进执行工具真正检查的口袋。
- 已修正：`controlled_exec_grant_refs()` 现在把显式包含 `controlled_exec` 且有 command/path scope 的 tool grant 也编译为受控 exec 边界；工具层仍只信父级注入的 `write_boundary`，不信模型自填 allowlist。
- 新发现：leaf 已用正式工具写入 OPEN capability request，但最终 `[SUBAGENT_RESULT]` 被截断缺闭合标记时，旧状态会显示 `structured_output_parse_error`，掩盖真正的“等待父级 route”状态。
- 已修正：runner result 状态机在解析失败但已有 OPEN capability request 时，优先保持 `BLOCKED/UNVERIFIED/failure_type=capability_request`，并提示 `route_capability_request`；没有能力申请的截断结果仍按 parse error 处理。
- 已补测试：`test_controlled_exec_refs_include_controlled_exec_tool_grants`、`test_parse_error_with_tool_created_open_request_stays_capability_blocked`。
- 下一步：用干净 controlled exec R7 复测：leaf 应拿到 `write_boundary.controlled_exec_grants`，真实执行 `pwd`，并把 `rm` 导向 task trash 生成 manifest；同时继续观察上层是否能 refs-first 收口。

## 2026-05-11 Stage7 R50 controlled exec R7 four-layer guard
- 中文说明：R7 真实 root-only 复测启动后，root 创建了 `小傻妞-r07-d1`，但 depth=1 coordinator 直接尝试创建 depth=2 `leaf_worker`，违反本轮必须四层、depth=3 才是 leaf 的合同。观察者停止本轮，避免外层替它补节点。
- 新发现：层级 guard 只识别“4 层”，没有识别真实中文 prompt 里常见的“4层”和 `depth=3`，导致四层合同漏判。
- 已修正：四层合同检测新增 `4层` 和 `depth=3`；父级明确要求四层/孙孙/depth=3 时，depth<2 的节点不能直接创建 leaf/worker，必须先继续创建 coordinator。
- 新发现：child goal 里提到 runtime `task_dir` 时会被 `child_write_root_drift` 当成用户产物根漂移反复阻断。
- 已修正：写根漂移检查会忽略父级内部上下文根（`task_dir`、`task_workspace_dir`、`agent_run_workspace_dir`）；用户产物根漂移仍继续阻断。
- 已补测试：`test_hierarchy_schedule_blocks_leaf_when_four_layer_token_has_no_space`、`test_hierarchy_schedule_allows_internal_task_dir_context_with_product_root`，并更新 `test_hierarchy_schedule_keeps_controlled_exec_contract_for_leaf` 先验证提前 leaf 被阻断，再按 depth=3 创建 leaf。
- 下一步：用干净 controlled exec R8 复测四层链路和 controlled_exec grant/write_boundary/trash 全链路。

## 2026-05-11 Stage7 R51 controlled exec R8 fake-child recovery hardening
- 中文说明：R8 真实 root-only 复测确认 R50 的四层 guard 已生效：depth=1 没有直接创建 leaf，而是创建了 depth=2 coordinator。但 depth=2 明确目标要求创建 depth=3 leaf_worker，却没有真实创建 child，只在结果里伪造 child run id 和完成状态。
- 新发现：如果 coordinator 的目标是创建下级，系统必须核对真实 `task.child_ids`，不能只信 runner 结果块里的 `child_run_ids`。
- 已修正：acceptance finding 新增 `required_child_spawned`；当 goal/acceptance 明确要求创建下级、leaf_worker 或 depth child 时，真实 `task.child_ids` 为空会 P0 阻断验收。
- 新发现：child 最新 `acceptance_review.json` 已经 REJECT 时，runner-context direct child payload 仍可能把 `AWAITING_ACCEPTANCE` child 当成可汇总对象，导致上层反复读报告而不进入 recovery。
- 已修正：`orchestration_progress_payload.py` 会读取 direct child 的最新 acceptance review；若 decision 是 REJECT，就把 run 放入 `rejected_acceptance_run_ids` / `recovery_run_ids`，并提示 `inspect_or_rescue_direct_children`。
- 已补测试：`test_required_child_goal_without_child_ids_blocks_acceptance`、`test_dispatch_payload_treats_rejected_child_as_recovery`；相关验收/调度 focused suite 45 条通过。
- 下一步：用干净 controlled exec R9 复测：如果 depth=2 仍不创建 leaf，上级应直接看到 recovery；如果它正确创建 depth=3 leaf，则继续验证 capability request -> grant -> controlled_exec -> task trash refs 全链路。

## 2026-05-11 Stage7 R52 controlled exec R9 apply/trash semantics
- 中文说明：R9 真实 root-only 复测跑通四层真实创建，并跑到 leaf 正式 capability request、父级 grant、leaf 多次调用 `controlled_exec`。这说明 R51 的 fake-child 修复有效，R49/R50 的 grant/write_boundary/四层 guard 也继续生效。
- 新发现：leaf 使用 `apply="full"` 表达真实执行，但 controlled_exec 只认布尔 true，导致执行仍是 dry-run，最终 `stdout_ref=null`、`trash_manifest_ref=null`。
- 已修正：controlled_exec 的 apply 参数现在接受 `apply/execute/run/full` 这类模型常见执行意图词。
- 新发现：`rm` 的 dry-run 返回 `allowed=false/action=use_task_trash` 时，工具整体 `ok=false`，模型误以为需要继续申请裸 `rm/mv` 权限。
- 已修正：delete-to-trash dry-run 现在是有效工具计划：工具调用 `ok=true`，payload 仍保留 `allowed=false/action=use_task_trash/trash_hint`，明确“不走 shell 删除，走 task trash 替代”。
- 已增强：blocked child recovery hint 明确要求先按 refs/suggested tool call 推进，不要反复 `read_file/read_artifact` 打开 child 产物正文。
- 已补测试：`test_controlled_exec_tool_apply_full_routes_delete_without_rm_shell_grant`、`test_controlled_exec_tool_dry_run_delete_is_valid_trash_plan`，相关 focused suite 47 条通过。
- 下一步：用干净 controlled exec R10 复测：`apply="full"` 应真实执行，`rm sentinel.txt` 应走 task trash，不再新增裸 `rm/mv` capability request。

## 2026-05-11 Stage7 R53 controlled exec R10 acceptance evidence scan
- 中文说明：R10 真实 root-only 复测完整创建 root -> 子 -> 孙 -> 孙孙四层；leaf 正式申请 capability，父级 grant 只给 `pwd/python3`，随后 leaf 真实执行 `controlled_exec`：`pwd` 和 `python3` stdout 外置，`rm sentinel.txt` 被 task trash 接管并写 manifest。
- 新发现：leaf 的验收文本里“真实创建 leaf_worker”被 `required_child_spawned` 误判成 leaf 还要创建下级，导致 `task.child_ids=[]` 被错误拒绝。
- 已修正：`required_child_spawned` 对 leaf/self 任务不再把“创建 leaf_worker”当作下级创建合同；明确“创建下级/创建直接下级/create depth=/spawn depth=”仍继续硬拦截。
- 新发现：controlled_exec 真实 refs 写在 task/deliverables 的 `controlled_exec_refs.json`、`controlled_exec_test_results.md` 和工具输出 artifact 中，但验收只看 `output.json`，误报缺少 stdout/audit/trash refs。
- 已修正：controlled_exec 验收现在会在 task_dir 和 `allowed_write_roots` 中按固定小文件名查找 refs/summary，最多两层、单文件 64KB，不读取大日志；现场复算 R10 已通过 `child_required=False` 和 `controlled_exec_contract_satisfied=True`。
- 已补测试：`test_leaf_self_creation_text_does_not_require_child_ids`、`test_controlled_exec_contract_reads_deliverable_refs`，相关 focused tests 通过。
- 下一步：用干净 controlled exec R11 复测完整链路：验收层应接受真实 controlled_exec/trash 证据；继续观察父级是否还会在 REJECT/READY 状态下反复读取 child 正文。

## 2026-05-11 Stage7 R54 controlled exec R11 cwd trash fix
- 中文说明：R11 真实 root-only 复测跑通四层，leaf 正式申请 capability、父级 grant 干净、`pwd/python3` 真实 controlled_exec 执行成功。
- 新发现：leaf 在 deliverables 目录写了 `sentinel.txt`，随后用 `cwd=deliverables` 执行 `controlled_exec rm sentinel.txt`，但删除分支把相对路径按 task_dir 解析，导致 task trash 找不到文件并返回 `source_missing`。
- 已修正：controlled exec 删除计划会把相对删除目标按命令 `cwd` 解析；task trash 的 source allowed roots 接受父级 grant 的 `path_scope`，因此授权产物目录里的文件可以被移动到当前 run 的 task-local trash。
- 已补测试：`test_controlled_exec_tool_routes_relative_delete_from_command_cwd`，并回归 4 条 controlled_exec trash focused tests。
- 已记录：root 首次把内部 `.../subagents/tasks/你的run_id` 占位路径写进 child goal 时触发 write-root guard，但 root 能自修并继续创建 child；暂不放宽 guard。
- 下一步：用干净 controlled exec R12 复测：`rm sentinel.txt` 应移动真实 deliverables 文件到 task-local trash，并产生 manifest；随后看验收和上层 refs-first 收口是否自然完成。

## 2026-05-12 Stage7 R55 controlled exec R12-R14 capability loop hardening
- 中文说明：R12/R13/R14 继续做真实 root-only 受控 exec E2E，均由 root 逐层创建到 `小小小傻妞-*` leaf，外层没有直接替下级创建或补产物。
- 已确认：R12 的 leaf 已能真实执行 `pwd`、大输出和 `rm -> task_trash`；但上层 coordinator 验收之前只看自己，没有接收子树 delegated evidence。
- 已修正：controlled_exec 验收现在会沿真实 `task.child_ids` 子树读取少量 child `task.json/output.json`，接受下级 leaf 的真实工具事实；仍不相信普通 summary 文案。
- 已修正：能力 grant 的 command allowlist 会把模型申请的完整命令（如 `python3 -c ...`）归一成 shell gateway 实际检查的 base command（如 `python3`），并继续禁止 `rm/rmdir/unlink` 进入 shell 白名单。
- 已修正：能力申请新增 scope 去重，工具调用写出的 request 和结构化结果里的同义 request 不再各生成一份；`rm` only 追加申请在已有 controlled_exec grant 覆盖时复用旧 grant，不生成空白 shell grant。
- 已修正：runner 结构化结果截断或 repair API 超时时，系统仍会把 runner loop 真实执行过的 `actual_tools` 合并进 `task.used_tools` 和证据摘要；坏 JSON 仍保持失败，不会被当作完成。
- 已补充：runner prompt 明确说明 `rm/rmdir/unlink` 不进入 command_allowlist 是安全设计，已有 controlled_exec grant 时应直接调用 `controlled_exec apply=true`，工具会走 task_trash 并返回 `trash_manifest_ref`，不要再次申请裸 rm。
- 已补测试：`test_controlled_exec_contract_accepts_descendant_tool_evidence`、`test_grant_command_allowlist_normalizes_full_command_strings`、`test_subagent_lifecycle_service_dedupes_equivalent_capability_requests`、`test_record_runner_result_dedupes_parsed_request_against_tool_request`、`test_parse_error_still_records_actual_tool_facts`、`test_rm_only_request_reuses_existing_controlled_exec_grant`、`test_controlled_exec_tool_accepts_duplicate_equivalent_grants_without_grant_id`。
- 下一步：跑干净 R15 真实链路，重点看 leaf 是否不再重复申请 capability，是否直接用 controlled_exec 完成 `rm -> task_trash`，以及上层是否能基于 refs-first delegated evidence 自然收口。

## 2026-05-12 Stage7 R56 controlled exec R15 hierarchy-scope hardening
- 中文说明：R15 真实 root-only 复测没有继续到 leaf，而是在 depth=1 创建 depth=2 时被 `forbidden_child_scope:depth` 卡住；这是父级“不要创建 depth>=4”的文字被当成禁止创建 `depth` 领域任务。
- 已修正：层级 scope guard 的 forbidden scope 提取会过滤 `depth/layer/level` 和通用角色词，只把 `arithmetic/text` 这类真实 sibling 领域当作禁止项；因此“禁止超过深度”不再误伤正常 depth=2/depth=3 派工。
- 已保留：真正的“不得创建 arithmetic 相关任务”仍会被挡住，隐式 text -> arithmetic 串线也继续被 domain mismatch 拦截。
- 已补测试：`test_hierarchy_schedule_allows_depth_limit_text_without_scope_block`、`test_hierarchy_schedule_blocks_forbidden_sibling_scope`、`test_hierarchy_schedule_blocks_implicit_domain_mismatch`。
- 下一步：跑干净 R16 真实链路，验证 depth=1 能继续创建 depth=2，再观察 capability 去重、controlled_exec、task_trash 和 delegated evidence 是否自然收口。

## 2026-05-12 Stage7 R57 controlled exec R16 artifact-ref acceptance hardening
- 中文说明：R16 真实 root-only 复测确认 R56 修复有效，四层链路真实创建成功；leaf 正式申请并拿到 controlled_exec grant，真实调用了 controlled_exec，`rm sentinel.txt` 走 task-local trash。
- 新发现：leaf 的 refs JSON 使用 `command_results.*.artifact_ref` 指向受控工具输出 artifact，stdout/audit refs 存在于 artifact 内容中；旧验收只看 refs 文件本身，不跟随这个明确小文件引用，导致误拒。
- 已修正：controlled_exec 验收会从 output/refs 文本中提取显式 `/memory_archive/artifacts/tool_outputs/controlled_exec-*.json` 引用，在 64KB 上限内读取小 artifact，补齐 stdout/audit refs；不做广泛目录扫描，不读取大日志。
- 已补测试：`test_controlled_exec_contract_follows_small_tool_output_artifact_refs`，并回归 controlled_exec acceptance focused tests 5 条。
- 下一步：跑干净 R17 真实链路，验证 leaf 验收能接受 artifact_ref 形状；随后重点观察上层 coordinator 是否能自然 refs-first 收口。

## 2026-05-12 Stage7 R58 controlled exec R17-R18 runner robustness
- 中文说明：R17 因 Minimax API read timeout 未覆盖业务逻辑；R18 跑到四层和 controlled_exec leaf，但暴露三类真实问题：Python `-c` 中引用内分号被 shell gateway 误挡、空泛 pending capability 生成无用第二 grant、leaf 把 dry-run/trash-plan 误写成 PASS。
- 已修正：shell gateway 改为用 `shlex` punctuation token 阻断未引用 shell 操作符；引用内 Python 分号不再被误判，未引用 `;` 仍会挡。
- 已修正：pending capability 兜底只有在能推出具体工具或命令时才生成 request；纯空泛 `PENDING_CAPABILITY_REQUEST` 不再生成无工具/无命令的 generic grant。
- 已增强：controlled_exec prompt 和工具示例明确要求真实任务必须 `apply=true`，最终 refs 写 stdout_ref/audit_ref；删除只有 `moved=true` 且有 `trash_manifest_ref` 才能 PASS；复杂 Python 命令建议用 argv 数组。
- 已补测试：`test_shell_gateway_allows_quoted_python_statement_separators`、`test_subagent_runner_parse_ignores_empty_pending_capability_request`、`test_runner_prompt_tells_controlled_exec_leaf_to_apply_and_report_refs`，并回归相关 pending capability / shell gateway focused tests。
- 下一步：跑干净 R19 真实链路，确认不再出现空泛第二 grant，Python 大输出可执行，leaf 更稳定地产出 stdout/audit/trash refs。

## 2026-05-12 Stage7 R59 controlled exec R19 delete-policy context
- 中文说明：R19 真实 root-only 复测确认 R58 两个修复有效：没有空泛第二 grant，`python3 -c "print('x' * 2000)"` 能执行并外置 stdout。但 leaf 仍把“rm 不在 command_allowlist”理解成“无法测试 rm”，没有调用 controlled_exec 去走 task_trash。
- 已修正：`controlled_exec_grant_refs()` 在 grant ref 中增加结构化 `delete_policy`，明确 `rm/rmdir/unlink` 通过 `task_trash` 执行、无需进入 command_allowlist，完成条件是 `moved=true` 和 `trash_manifest_ref`。
- 已增强：runner prompt 直接引用 `controlled_exec_grants.delete_policy.mode=task_trash`，要求已有 grant 时直接 `controlled_exec apply=true` 跑删除命令，不要把 allowlist 缺 rm 当成矛盾。
- 已补测试：`test_controlled_exec_refs_include_controlled_exec_tool_grants`、`test_runner_prompt_tells_controlled_exec_leaf_to_apply_and_report_refs`。
- 下一步：跑干净 R20 真实链路，验证 leaf 是否能按 structured delete_policy 生成非空 trash manifest。

## 2026-05-12 Role selection planning note
- 中文说明：后续要把“每类角色怎么用”做成正式角色选择策略，让 root 和所有 coordinator/lead 派工角色在合适时机选择角色，而不是凭感觉创建 worker。
- 目标：主代理常驻只需要知道角色索引、适用场景和选择规则；一旦进入派工，再按需展开模板详情。这样既能让派工更聪明，也不会在普通聊天/简单任务中浪费 prompt。
- 初始角色意图：worker/writer 负责真实产物；researcher 负责查资料和整理事实；tester 负责运行或设计验证；bug_finder 负责找错和风险扫描；acceptor 负责最终验收建议；coordinator/lead 负责拆分、广播、纠偏、接管和汇总，不默认亲自写业务产物。
- 下一步：在 R15/R16 后把角色选择策略落到 `role_templates` 的索引/详情提示里，并补角色选择单测，确保每个模板都能在合适任务中被选中。

## 2026-05-12 Stage7 R60 controlled exec R20-R22 and role-selection index
- 中文说明：R20 发现 root/coordinator 会替未来 leaf 提前申请 controlled_exec，导致树还没长出来就停在能力申请；R21 验证四层链路恢复，但发现执行 payload 里 `dry_run` 标记错误、argv Python 分号仍被误挡；R22 最终跑通 root-only 四层真实 E2E。
- 已修正：coordinator runner contract 和内置 coordinator 模板明确，后代专属能力由真正执行的 child/leaf 申请，coordinator 不要提前申请后停止。
- 已修正：shell gateway 的执行层现在把真实执行 decision/audit 标记为 `dry_run=false`；argv list 命令不再按 shell 字符串扫描分号，仍保留白名单、cwd、path scope 和网络 scope 检查。
- 已验证：R22 四层全部 `DONE/VERIFIED`，leaf 真实执行 `pwd`、`python3` 大输出和 `rm sentinel.txt -> task_trash`；refs 写到 `/Users/example/my-终端应用/deliverables/controlled_exec_e2e_20260511_r22/controlled_exec_refs.json`，trash manifest 非空。
- 已落地角色规划第一片：`role_template_index_text()` 现在把每个角色的 `适用/不适用` 场景放进轻量索引；主代理和 coordinator 常驻看索引，真正派工时才展开完整模板详情。这样每类角色什么时候用有机器可见规则，又不会让普通任务加载全部模板正文。
- 已补测试：`test_shell_gateway_execute_marks_decision_as_not_dry_run`、`test_shell_gateway_allows_argv_python_statement_separators`、`test_role_template_index_is_compact_catalog_metadata`，并回归 controlled_exec / shell gateway / role template focused tests。
- 下一步：继续把角色选择从“可见索引”推进成“选择策略”：让 root/coordinator 在创建 child 前先按任务类型选 coordinator/worker/writer/researcher/tester/bug_finder/acceptor，并用真实 E2E 覆盖每种模板是否能被正确选中和执行。

## 2026-05-12 Role selection strategy doc
- 中文说明：把“每类角色什么时候用”单独写成 `docs/modules/subagent/08-role-selection-strategy.md`，让主代理、coordinator、lead 的派工规则有文档锚点。
- 已明确：常驻只加载角色 id、中文名、适用/不适用、能力标签和模板位置；默认工具、完整 prompt 和输出合同只在真正派工时按需展开。
- 已明确：coordinator/lead 负责拆分、广播、纠偏、接管和汇总；worker/writer 负责真实产物；researcher 负责事实源；tester/bug_finder/acceptor 是横向 QA 角色，可以一次检查多个 worker，不要求一一对应。
- 下一步：把策略文档里的选择规则做成小型 `role_selection` 决策包，并补单测和真实 E2E，验证每个内置角色都能被正确选择、创建、执行和验收。

## 2026-05-12 Stage7 R41 complete shopping E2E required-file fix
- 中文说明：R41 按完整购物网站目标启动 root-only 真实测试，但在 depth=1/2 目标里发现 `product.html` 和 `output.json` 被放进 `父级必需文件/产物名`，虽然它们在 root prompt 中是明确禁止项。
- 已修正：`required_file_terms.py` 现在能识别裸禁止写法，例如 `不允许 product.html/old-product.html/legacy.html`、`不允许在 build 目录写 output.json`，并把这些放进 forbidden files；斜杠分隔的文件反例也会按列表处理。
- 已补测试：`test_file_contract_treats_bare_negative_targets_as_forbidden_terms`，并回归 required/forbidden file-contract 与 context bundle 相关测试。
- 下一步：干净启动 R42，继续完整购物网站 E2E；重点确认 required_files 只剩 10 个用户产物，forbidden_files 单独包含 `product.html/output.json/RUNNER_RESULT.md/execution_context.json`，然后让 tester/bug_finder/acceptor 跑完整流程验收。

## 2026-05-12 Stage7 R42 complete shopping E2E forbidden-label fix
- 中文说明：R42 确认 R41 的第一层 handoff 已干净，但 child 转述成 `禁止文件名：product.html/old-product.html/...` 后，depth=2 又把 forbidden 文件放回 `父级必需文件/产物名`。
- 已修正：`required_file_terms.py` 现在把 `禁止文件名：...`、`禁止文件：...` 这类中文标签也识别为 forbidden list；多层代理换一种说法时，下层仍能保持 required/forbidden 分离。
- 已补测试：`test_file_contract_treats_forbidden_filename_label_as_forbidden_terms`，并回归 bare negative / negative example / required-forbidden split 相关测试。
- 下一步：干净启动 R43，继续完整购物网站 E2E；重点观察 root -> 子 -> 孙 -> 孙孙 全链路 required_files 是否始终只有 10 个用户产物，并进入真实页面产出与验收。

## 2026-05-12 Stage7 R43 workflow auto-interference fix
- 中文说明：R43 启动后发现 root 下面直接出现 producer/critic/repair 三个 workflow children，而不是 root 自己创建 `小傻妞-*` 再逐层向下派工。
- 已修正：顶层 `dispatch_subagents(apply=true, workflow_mode=auto)` 遇到 active root/coordinator 时也会强制 `workflow_mode=off`；不再只有 `execute_runners=true` 时才保护 root/coordinator。
- 已补测试：`test_top_level_apply_dispatch_does_not_spawn_workflow_for_active_root_coordinator`，并回归顶层/runner-context workflow-off 调度测试。
- 下一步：干净启动 R44，继续完整购物网站 E2E；预期 root 下不再自动出现 producer/critic/repair，而是由 root 自己创建 `小傻妞-*`，再由下层继续创建孙/孙孙。

## 2026-05-12 Stage7 R44 hierarchy contract inheritance fix
- 中文说明：R44 证明 R43 的 workflow 干扰已消失，root 能自己创建 `小傻妞-页面协调`；但 child 总结目标时丢了具体 forbidden 文件名和 `4层/depth=3` 链路规则，导致 depth=2 leaf 提前写页面。
- 已修正：`goal_carries_parent_scope()` 现在必须同时确认 required files、forbidden files、层级合同和能力合同都没有缺项，才允许跳过父级继承块。
- 已修正：层级合同提取现在识别 `4层` 无空格写法、`depth=1/2/3`、`max_depth` 和 `小傻妞` 命名规则；下层如果没携带这些锚点，会自动补入“父级层级/协作约束”。
- 已补测试：`test_hierarchy_schedule_preserves_forbidden_file_contract_when_child_goal_summarizes_constraints`、`test_hierarchy_schedule_preserves_no_space_four_layer_contract_and_blocks_leaf`。
- 下一步：干净启动 R45，继续完整购物网站 E2E；重点看 forbidden_files 是否每层都保留，且真实写代码节点是否只出现在 depth=3 `小小小傻妞-*`。

## 2026-05-12 Stage7 R45 root seed raw-contract fix
- 中文说明：R45 发现 root seed 在创建时就被主代理摘要缩水：root goal 只剩“禁止写内部文件”，没有 `product.html/output.json/RUNNER_RESULT.md` 等具体 forbidden 名字，导致 root context bundle 的 `forbidden_files=[]`。
- 已修正：运行循环会临时暴露当前原始用户 prompt 给工具层；显式 root/coordinator 的 `create_subagents` 会从原始 prompt 抽取 required files、forbidden files 和 4层/depth/小傻妞命名合同，若模型写的 root goal 漏项，就追加 compact 继承块。
- 已修正：`_create_tasks()` 现在使用 `_create_run_params()` 生成后的 `run_params.goal`，避免前面补好的 root 合同被原始未增强 goal 覆盖。
- 已补测试：`test_explicit_coordinator_seed_inherits_raw_user_file_and_hierarchy_contract`，并回归 create_subagents、coordinator seed、hierarchy contract、context bundle 文件合同测试。
- 下一步：干净启动 R46 完整购物网站 E2E；先确认 root 自己的 context bundle 已有 required_files 和 forbidden_files，再观察 `小傻妞-* -> 小小傻妞-* -> 小小小傻妞-*` 是否按四层链路写出完整购物网站。

## 2026-05-12 Stage7 R46 root seed forbidden-block parser fix
- 中文说明：R46 证明 root seed 补块已经写进 root goal，但补块标签 `用户原始禁止文件/反例名（禁止创建...）：...` 没被文件解析器当成 forbidden 标签，导致 `product.html/output.json` 仍进入 required_files。
- 已修正：`required_file_terms.py` 新增负向 label 识别，支持 `禁止文件/反例名（...）：`、`forbidden_files...:` 这类带解释括号的机器补块；这些文件进入 forbidden_files，并从 required_files 排除。
- 已补测试：`test_file_contract_treats_root_seed_forbidden_inheritance_block_as_forbidden_terms`，并回归 hierarchy/create/coordinator/context bundle 相关 tests。
- 下一步：干净启动 R47 完整购物网站 E2E；第一检查点要求 root required_files 正好 10 个用户产物、forbidden_files 正好包含 forbidden/internal 文件。

## 2026-05-12 Stage7 R47 inherited forbidden bullet fix
- 中文说明：R47 的 root 合同已正确分离，但 child/grandchild 的继承块用“父级禁止文件/反例名：”标题加 bullet 列表，解析时每个 bullet 独立处理，导致 forbidden 文件同时进入 required_files。
- 已修正：`required_file_terms.py` 的分段逻辑会把负向标题状态带到后续 bullet 行，直到遇到非 bullet 段；`父级禁止文件/反例名：\n- product.html` 现在只进入 forbidden_files。
- 已保留：`父级必需文件/产物名：\n- index.html` 仍正常进入 required_files，不受负向标题状态影响。
- 已补测试：`test_file_contract_carries_negative_header_into_bulleted_forbidden_terms`，并回归 hierarchy/create/coordinator/context bundle 相关 tests。
- 下一步：干净启动 R48 完整购物网站 E2E；目标是 root、depth=1、depth=2 都保持 required/forbidden 分离，再让 depth=3 leaf 真正写出购物网站。

## 2026-05-12 Stage7 R48 thinking-only backend retry fix
- 中文说明：R48 root 合同已干净，但 MiniMax 的 Anthropic-compatible 接口偶发只返回 `thinking` 内容块、没有 `text` 内容块，旧 backend 直接抛错，导致 root 变 `BLOCKED`。
- 已修正：Anthropic-compatible 非流式 backend 如果遇到 thinking-only 且无 text，会自动重试一次；普通空 content 仍然抛错，不会把坏响应伪装成成功。
- 已补测试：`test_generate_retries_once_on_thinking_without_text`，并回归正常 non-stream 和 no-text backend tests。
- 下一步：干净启动 R49 完整购物网站 E2E；继续观察 root -> 小傻妞 -> 小小傻妞 -> 小小小傻妞 是否能跑到真实写文件和 QA 验收。

## 2026-05-12 Stage7 R49 positive no-rename label fix
- 中文说明：R49 发现 `必须文件（禁止改名）：index.html...` 被当成 forbidden label，导致 10 个必需文件全进 forbidden_files，required_files 为空。
- 已修正：负向 label 识别收窄为真正负向标题，例如 `禁止：`、`禁止文件名：`、`禁止文件/反例名：`、`forbidden_files:`；`必须文件（禁止改名）：...` 仍按 required files 处理。
- 已补测试：`test_file_contract_keeps_required_files_when_positive_label_says_no_rename`，并回归 hierarchy/create/coordinator/context bundle/backend 相关 tests。
- 下一步：干净启动 R50 完整购物网站 E2E；目标是 root 合同稳定后继续推进到 depth=3 写文件和 QA 验收。

## 2026-05-12 Stage7 R50 acceptance-check forbidden fix
- 中文说明：R50 的 root goal 自身能分清 required/forbidden，但 acceptance_checks 里的 `无 forbidden_files(...)`、`无内部文件污染(...)` 又把 forbidden 文件塞进 required_files。
- 已修正：文件契约解析器现在把 `无/no/without + forbidden_files/内部文件污染/文件污染` 识别为负向语境；验收项中出现的 `product.html/output.json` 只进入 forbidden_files。
- 已补测试：`test_file_contract_treats_no_forbidden_files_acceptance_as_forbidden_terms`，并回归 hierarchy/create/coordinator/context bundle/backend 相关 tests。
- 下一步：干净启动 R51 完整购物网站 E2E；如果合同干净，就继续跑到 depth=3 leaf 写站点和 QA 验收。

## 2026-05-12 Stage7 R51 location-rule and parenthesized-label fix
- 中文说明：R51 root 能自己创建 `小傻妞-协调者`，但文件合同又暴露两种自然语言漂移：`禁止 style.css/app.js 放进子目录` 被误解成禁止创建 `style.css/app.js`，`禁止文件名（product.html/...）` 这种括号标签没有被识别成 forbidden list。
- 已修正：`required_file_terms.py` 现在能区分“禁止某文件放进子目录”这类位置约束和“禁止创建某文件”这类文件名反例；位置约束不会把必需资源塞进 `forbidden_files`。
- 已修正：负向标签现在支持括号边界，例如 `禁止文件名（...）`、`禁止内部文件（...）`，不再只支持冒号。
- 已补测试：`test_file_contract_keeps_required_files_when_forbidden_location_mentions_them`、`test_file_contract_treats_parenthesized_forbidden_labels_as_forbidden_terms`，并回归 hierarchy/create/coordinator/context bundle/backend 相关 tests。
- 下一步：干净启动 R52 完整购物网站 E2E；目标是 root/depth=1 合同都干净后，继续推进到 depth=2/depth=3，让叶子节点真实写站点并由 tester/bug_finder/acceptor 验收。

## 2026-05-12 Stage7 R52 short no-write fix
- 中文说明：R52 已跑到 root -> `小傻妞-depth1-coord` -> `小小傻妞-depth2-coord`，并验证 coordinator 不能直接写 `.gitkeep` 到业务产物目录；但 machine forbidden_files 漏了 `output.json/RUNNER_RESULT.md`，因为 root 把禁止项总结成了“不写output.json/RUNNER_RESULT.md”。
- 已修正：文件契约解析器现在识别 `不写/不创建/不生成/不产出/不包含` 这类短否定动词，支持中文和文件名紧贴的写法，例如 `不写output.json`。
- 已修正：文件名边界和斜杠列表归一化改成 ASCII 边界，`RUNNER_RESULT.md等` 这种后面接中文的文件名也能正常从列表里抽取。
- 已补测试：`test_file_contract_treats_no_write_short_negative_as_forbidden_terms`，并回归 hierarchy/create/coordinator/context bundle/backend 相关 tests。
- 下一步：干净启动 R53 完整购物网站 E2E；要求 root/depth=1/depth=2 的 `forbidden_files` 都包含 7 个 forbidden/internal 文件，然后继续让 depth=3 写站点并跑 QA。

## 2026-05-12 Stage7 R53 duplicate-domain path fix
- 中文说明：R53 的 root/depth=1 文件合同已稳定成 10 required + 7 forbidden，但 depth=1 创建两个 depth=2 coordinator 时被 `duplicate_child_domain:模型助手` 错挡；原因是两个 child goal 都包含 `/Users/example/my-终端应用/...`，去重 guard 把路径里的 `模型助手` 当成任务领域。
- 已修正：duplicate-domain 兜底从 goal 提取领域词前，会先去掉绝对路径、文件名和继承块，并过滤 `users/模型助手/code/shop/tests/css/js` 这类路径或脚手架词。
- 已保留：checkout/quality 这种真正同父级重复领域仍会被阻断；domain mismatch 保护也没有放松。
- 已补测试：`test_hierarchy_schedule_duplicate_domain_ignores_shared_filesystem_paths`，并回归 duplicate guard 与 domain mismatch 相关 tests。
- 下一步：干净启动 R54 完整购物网站 E2E；目标是 depth=1 能创建不同职责的 depth=2 节点，并继续推进到 depth=3 真实产出和 QA。

## 2026-05-12 Stage7 R54 plain forbidden-list fix
- 中文说明：R54 root 合同仍干净，但 depth=1 goal 用了“禁止创建文件（forbidden_files）：”下一行直接列 `product.html/...`，不是 bullet 列表，导致 7 个 forbidden 又进入 required_files。
- 已修正：负向 label 现在识别 `禁止创建文件...`；负向标题后如果下一行是纯文件列表，也会带着负向语境解析，并在该行后重置，避免污染后续普通说明。
- 已补测试：`test_file_contract_carries_negative_header_into_plain_file_list_line`，并回归 hierarchy/create/context bundle 相关 tests。
- 下一步：干净启动 R55 完整购物网站 E2E；目标是 root/depth=1/depth=2 都保持 10 required + 7 forbidden，再让 depth=3 开始写站点文件。

## 2026-05-12 Stage7 R55 long write tool-call fix
- 中文说明：R55 已跑通 root -> `小傻妞-1` -> `小小傻妞-1` -> `小小小傻妞-1` 四层链路，且四层 context bundle 都保持 10 required + 7 forbidden。新的真实问题出现在 depth=3 写站点：叶子节点把较长 `app.js` 塞进单个 `write_file` JSON，连续丢失 `[/TOOL_CALL]` 结束标记。
- 已修正：runner 合约、write/append 工具说明、parse-error 修复提示统一成“两档策略”：正常分块建议 1500-2000 字符，避免初期测试被过小阈值拖慢；出现工具调用解析失败后，再降级到 800 字符以内且每轮只输出 1 个写入工具调用。
- 已补测试：`test_runner_prompt_tells_leaf_to_chunk_long_file_writes`、`test_parse_error_hint_recommends_append_for_truncated_write`。
- 下一步：干净启动 R56 完整购物网站 E2E；目标是 depth=3 用分块 write/append 写出 10 个文件，然后由父链路推进 tester / bug_finder / acceptor 做真实验收。

## 2026-05-12 Stage7 R56 leaf acceptance and depth-domain fix
- 中文说明：R56 真实跑到 root -> `小傻妞-前端总协调` -> `小小傻妞-前端协调A` -> `小小小傻妞-首页注册样式写手`，叶子真实写出 `index.html/register.html/style.css/app.js`。这证明长写恢复比 R55 更稳，但又暴露两个控制面误伤。
- 已修正：duplicate-domain 去重不再把 `depth/layer/level` 当成业务领域，避免中文 A/B coordinator 都提到 `depth=3` 时被误挡成重复。
- 已修正：`leaf_worker` 不再因为继承父级“创建 depth=3 worker”文字而被要求继续创建下级；最终叶子写文件后可进入正常验收。
- 已补测试：`test_hierarchy_schedule_duplicate_domain_ignores_depth_markers`、`test_leaf_inherited_parent_depth_constraints_do_not_require_child_ids`，并回归原有 duplicate-domain 和 required-child 保护测试。
- 下一步：干净启动 R57 完整购物网站 E2E；目标是让多个 depth=2 中文 coordinator 正常并行分片，depth=3 叶子写完后验收不再误拒，继续补齐剩余 6 个页面并推进 QA 验收。

## 2026-05-12 Stage7 R57 bracket forbidden-label fix
- 中文说明：R57 在 root 合同第一检查点发现 `【禁止文件名】product.html/...` 没有冒号，导致 product/legacy 反例进入 `required_files`。
- 已修正：文件契约解析器现在把 `】` / `]` 也当作 negative label 边界，支持 `【禁止文件名】...`、`【禁止内部文件】...` 这类中文标题格式。
- 已补测试：`test_file_contract_treats_bracket_forbidden_labels_as_forbidden_terms`，并回归 forbidden label、positive no-rename、plain forbidden list 测试。
- 下一步：干净启动 R58 完整购物网站 E2E；先确认 root required/forbidden 分离，再继续跑四层链路和 QA。

## 2026-05-12 Stage7 R58 sentence-period required-file fix
- 中文说明：R58 的 root forbidden_files 已稳定，但 required_files 漏了最后一个 `app.js`，因为任务文本写成 `style.css, app.js.`，句末英文句号被旧文件名边界误判成“还有后缀”。
- 已修正：文件名提取允许句末标点，不再漏掉 `app.js.` 这种自然句子；同时仍然不会把 `app.js.map` 截断成 `app.js`。
- 已补测试：`test_file_contract_keeps_required_filename_before_sentence_period`，并回归 bracket forbidden label、forbidden filename、location-rule 测试。
- 下一步：干净启动 R59 完整购物网站 E2E；目标是 root 合同稳定为 10 required + 7 forbidden 后，继续跑 root -> 小傻妞 -> 小小傻妞 -> 小小小傻妞，并推进到真实写完 10 个购物站文件和 QA 验收。

## 2026-05-12 Stage7 R59 hierarchy-first correction
- 中文说明：购物网站不是本阶段目标本身，只是复杂测试载荷。核心目标是确认 root 只启动第一层、子代理继续启动孙代理、孙代理继续启动孙孙代理，以及合同继承、路径、权限、角色覆盖、恢复、日志和父级验收都稳定。
- R59 结果：四层链路真实跑通并由 leaf 写出 10 个购物站文件，但 QA 角色覆盖和静态验收暴露了框架问题，所以不能按“购物站文件存在”就算完成。
- 已修正：`禁止创建：...` 不再污染 descendant required_files；点名 tester / bug_finder / acceptor 时，验收会扫描真实后代角色，缺角色 P0 阻断；`static_site_check` 能发现本地 JS 的 `validateForm('id')` 指向不存在的 form id。
- 下一步：干净启动 R60。评价标准优先看子代理链路和验收控制是否正确，而不是人工优化购物网站本身。

## 2026-05-12 Stage7 R60 hierarchy truth and timeout guard
- 中文说明：R60 再次跑通四层并写出 10 个购物站文件，静态站点检查通过；但真实 task.json 显示 root 是 `TIMEOUT/UNVERIFIED`、一个孙孙 leaf 是 `BLOCKED/FAILED`，且没有真实 tester / bug_finder / acceptor。最终自然语言报告却把 root/QA 说成成功，这是本轮最重要问题。
- 已修正：工具轮数到上限时，如果本轮执行过 subagent 编排工具，最终答复改为从 task.json 生成的确定性事实报告，列出 role/name/depth/status/blocking run ids，不再让模型自由总结失败链路。
- 已修正：runner timeout 后的旧 daemon thread 不能继续执行工具；工具入口会检查 `_current_subagent_attempt_id`，attempt 已 abandon 或不再 active 时直接阻断 read/write/dispatch/message。
- 已修正：`subagent_board` 输出补充真实 `root_id/parent_id/depth/agent_name/role/child_count/child_status_counts/latest_summary/blocker_count/output_json`，减少模型误读和虚构角色名。
- 已修正：coordinator prompt 明确要求点名 tester / bug_finder / acceptor / reviewer / 找错 / 测试 / 验收时必须创建真实角色 run，summary/evidence 里提到不算覆盖。
- 已补测试：`test_stale_attempt_guard_blocks_abandoned_runner_tools`、`test_dispatch_limit_response_uses_persisted_task_state`，并回归 board tool/rendering 与层级 schedule focused tests。
- 下一步：干净启动 R61。目标不是优化购物网站，而是验证：超时旧线程不再继续写，工具上限报告严格按 task.json，root/coordinator 能主动创建真实 QA 角色，或者系统明确保持 incomplete 而不是假完成。

## 2026-05-12 R61 follow-up: long content, provider timeout, QA auto scheduling
- 中文说明：R61 暴露三类框架问题：长 `write_file/append_file` 工具调用会让 JSON 块损坏；provider timeout 缺少明确错误边界；tester / bug_finder / acceptor 以前主要靠 prompt 和最终验收，调度时不会主动补派。
- 已修正：长内容失败后会进入 `long_content_recovery_mode`，下一轮强制短骨架、小块 append 或受控 exec refs，不再让模型重复输出同一个巨大 JSON 参数。
- 已修正：模型接口请求/流式超时会抛 `ProviderTimeoutError`；子代理 runner 记录 `failure_type=provider_timeout`，CLI 输出可读 handoff 和非 0 退出。
- 当时已修正：新增 `qa_role_contract.py` 和 `hierarchy_qa_scheduler.py`。父任务明确点名 tester / bug_finder / acceptor 时，层级调度会比较本轮 specs 和已存在后代，并提示缺失 QA 角色；最终验收也复用同一套角色识别规则。R67 后，缺失 QA 不再由系统自动创建，而是通过 `quality_advice` 交给 LLM 决策。
- 已修正：duplicate-domain 去重过滤 `run/id/ref/refs/qa` 等结构词，避免自动 QA child 因共享父级 ref 被误判成同域重复。
- 已补测试：长内容恢复、provider timeout、QA 自动补派、QA 去重和原有角色覆盖验收 focused tests 已通过。
- 下一步：重新跑干净真实 E2E，继续用购物网站作为复杂载荷，验证 root-only 下是否能真实创建并完成 tester / bug_finder / acceptor，而不是只在最终验收阶段发现缺席。

## 2026-05-12 R62 follow-up: scoped filesystem bundles and descendant health
- 中文说明：R62 真实 E2E 证明 QA 自动补派已经能触发，四层链路也能写出 10 个购物网站文件；但同时发现两个关键问题：读/search 工具没吃 `filesystem.path` bundle，父级在 QA 后代仍未完成/失败时仍可能 `DONE/VERIFIED`。
- 已修正：文件读取工具现在同时接受顶层参数和 `filesystem.*` bundle 参数；`search_text` 不会因为模型按 bundle 格式传 path 就退回全工作区搜索。
- 已修正：新增 `descendant_health` 父级验收门。父任务有真实 `child_ids` 时，会沿后代 `task.json` 有界扫描；只要后代仍 `PLANNING`、未验收、失败、阻塞或缺失，父级就会 P0 拒绝验收。
- 已记录：R62 还暴露 root 误把已有但 `PLANNING` 的 QA child 当成缺失而重复创建 QA、长内容写入仍会多次自救、外层 CLI 观察仍可能沉默等待。这些保留到下一轮真实 E2E 和 closeout/watchdog 优化。
- 已补测试：`test_search_text_accepts_filesystem_bundle_path`、`test_parent_with_unfinished_descendant_blocks_acceptance`、`test_parent_with_verified_descendants_passes_descendant_health`，并回归 filesystem / acceptance / QA scheduler focused tests。
- 下一步：重新跑干净 R63；预期任何 QA 后代未健康收口时 root 不能假绿，scoped search 只能搜 build 目录，最终 closeout 要尽量从 persisted task facts 汇报而不是沉默等待。

## 2026-05-12 R63-R65 follow-up: QA phase gates
- 中文说明：R63/R64/R65 真实 E2E 证明 QA 自动补派会创建真实 tester/bug_finder/acceptor，但也暴露了新问题：QA 在 build 还是空目录时就创建或执行，会认真报告“没有产物”，造成空转和假失败。
- 已修正：runner dispatch 阶段排序现在优先看 `role/agent_name` 身份，不再因为 coordinator goal 继承了 tester/bug_finder/acceptor 字样，就把 coordinator 误判成 QA 阶段；同一批候选里先跑 coordinator/worker，再跑 tester/bug_finder，最后跑 acceptor。
- 已修正：有产物根的父任务，在 worker/writer/leaf_worker 没有进入 `AWAITING_ACCEPTANCE`、`NEEDS_ACCEPTANCE` 或 `DONE/VERIFIED` 前，不自动补派 QA；模型显式创建 QA child 也会被 `qa_before_implementation_ready` 阻断。
- 已记录设计原则：大型任务里 QA 要按 work 批次和依赖组运行。全部 work 完成时跑整体验证；部分关联 work 完成时只跑局部 QA；单个 work 默认进入 `ready_for_batch_qa`，除非它是独立交付单元或阻塞后续。
- 已补测试：`test_runner_phase_ignores_inherited_qa_contract_for_plain_coordinator`、`test_hierarchy_schedule_blocks_qa_before_implementation_ready`、`test_hierarchy_schedule_defers_auto_qa_until_implementation_ready`、`test_hierarchy_schedule_quality_advice_after_implementation_ready` 等 focused tests 已通过。
- R66 结果：QA 不再提前自动补派，但 root 在未调用工具、未创建 child 的情况下把“需要 schedule_child_subagents”写成 `AWAITING_ACCEPTANCE`，随后被验收拒绝为 `acceptance_failed`。这说明 QA 阶段门生效了，但还需要下一片把“需要派工的 coordinator 不能只分析就提交验收”交给 LLM 继续规划或重试，而不是把调度策略写死。
- 下一步：做 LLM-assisted QA orchestration：让 LLM 读取 work 状态、依赖组、产物 refs 和风险提示后提出 QA/repair/acceptance plan；系统只做边界校验，例如不允许空产物 QA、不允许跳过失败 child、不允许越权广播。

## 2026-05-12 R67 follow-up: QA advice instead of hardcoded QA creation
- 中文说明：根据“不要把流程写死，尽量让 LLM 介入”的开发原则，QA 调度从“系统自动补 tester/bug_finder/acceptor child”改成“系统输出 `quality_advice`，LLM 自己选择 scope、数量、顺序和 repair/acceptance 组合”。
- 已修正：`HierarchyScheduleResult` 新增 `quality_advice`；`hierarchy_qa_scheduler.py` 生成 `implementation_first` 或 `quality_wave_ready` 决策包，包含候选 QA roles、候选 child spec 和红线，但不直接创建 child。
- 已修正：`schedule_child_subagents` 在没有传 `children` 时不再只报错；它会走 scheduler，返回 `no_child_specs + quality_advice`，让模型能先看建议再决定下一次如何派工。
- 保留红线：有产物根但没有 ready worker/writer/leaf_worker 时，显式创建 QA 仍会被 `qa_before_implementation_ready` 阻断；这只是安全边界，不是业务流程。
- 已补测试：`test_hierarchy_schedule_advises_required_qa_roles_without_auto_creation`、`test_hierarchy_schedule_quality_advice_after_implementation_ready`、`test_runner_context_schedule_without_children_returns_quality_advice`。
- 已修正：把 R66 的“只分析不派工却提交验收”转成 runner recovery 红线。如果 coordinator 没工具调用、没 child refs，却说下一步要 `schedule_child_subagents`，收尾会改成 `BLOCKED / needs_child_creation`，让 LLM 继续派工。
- 已补测试：`test_subagent_runner_blocks_analysis_only_coordinator_without_children`。
- 下一步：做一轮真实 root-only E2E 复测，确认主节点看到 `quality_advice` 后能自己选择 QA 波次，并且遇到 `needs_child_creation` 会继续 schedule，而不是停在验收失败。

## 2026-05-12 R67 follow-up: reduce hardcoded contract misfires
- 中文说明：R67 继续证明“流程硬写死”会误伤：自然语言继承块把 `小小小傻妞-*` 漂移成了 `小小的傻妞-*`；`## 禁止文件` 里的反例进入了 required_files；中间 coordinator 因继承父级 QA 目标，被验收器硬性要求自己创建 tester/bug_finder/acceptor。
- 已修正：层级命名合同不再只看 `depth=3` 这类宽锚点；如果父级合同有 `小小小傻妞-*` / `小小小小傻妞-*` 精确前缀，子级摘要必须包含同样前缀，否则系统补回原始合同。
- 已修正：文件契约解析器识别 `## 禁止文件` 这种 Markdown 负向标题；`task.json 里的状态`、`execution_context.json refs` 这类内部引用不会当成用户产物。
- R68 预检又发现 `必须按真实 task.json 阻塞汇报` 这种说法仍会被“必须”误导；已把内部状态文件的正向判断改成必须有 `交付/创建/写入/包含` 等交付动作，单独的“必须按真实 task.json 汇报”不再算产物。
- R69 继续发现继承块里前文 `禁止创建 depth>=4` 的“创建”会污染后面的 `task.json 里的状态`；已让 `task.json 里的/refs/阻塞/汇报/状态` 这类紧邻内部状态语境优先于前文交付动词。
- 已修正：QA role coverage 只读取当前节点自己的 direct goal / acceptance，遇到 `继承父级目标/边界`、`父级层级/协作约束` 等继承块就停止，避免每个中间 coordinator 都背父级全局 QA 硬义务。
- 已补测试：`test_file_contract_treats_markdown_forbidden_heading_as_negative_scope`、`test_file_contract_ignores_internal_state_file_references_without_deliverable_label`、`test_file_contract_ignores_task_json_inside_hierarchy_state_clause`、`test_explicit_coordinator_seed_repairs_wrong_lineage_summary_from_raw_prompt`、`test_inherited_parent_qa_contract_does_not_bind_intermediate_coordinator`；并用 R68/R69 prompt 验证 required_files 只剩 10 个用户交付文件。
- 下一步：干净启动 R68；预期 required_files 只保留 10 个交付文件，命名合同保留 `小小小傻妞-*`，中间 coordinator 不再因继承 QA 文字被误判失败，正确父级仍要通过 `quality_advice` 主动选择 QA 波次。

## 2026-05-12 R70 follow-up: reduce hardcoded hierarchy forcing
- 中文说明：R70 干净复测确认 R69 的 `task.json` 误传已修掉：child context bundle 里的 `required_files` 只剩 10 个购物站交付文件，`task.json` 只作为内部状态引用出现。
- 新发现：root 和 depth=1 coordinator 都尝试创建 worker/writer 分支时，被 `hierarchy_chain_requires_coordinator_until_depth_3` 阻断；这把“至少覆盖一条 4 层链路”误做成“所有 depth<3 都必须是 coordinator”，和 root 可按任务同时派 2/3/4 层分支的设计冲突。
- 已修正：调度阶段不再硬性要求四层链路中的每一层都是 coordinator，也不再因为 root 已有 coordinator child 就禁止 root 直接派 worker/leaf。调度只保留红线：空计划、max_depth、max_children、同批混建 coordinator/显式 leaf、产物根漂移、禁止 sibling 领域、QA 空转和重复已完成产物。
- 验收口径：是否满足“至少一条 4 层链路”、是否跳过失败 child、是否缺 QA 角色，交给 acceptance/descendant health/role coverage 读取真实 `task.json` 后判断，而不是在 schedule 阶段替 LLM 固定流程。
- 已补测试：`test_hierarchy_schedule_preserves_no_space_four_layer_contract_without_forcing_coord_chain`、`test_root_with_coordinators_can_still_create_direct_leaf`，并调整 controlled_exec 继承测试，确认直接 implementation child 仍能继承 controlled_exec/task_trash 等硬合同。
- 下一步：跑 focused tests 后重新启动 R71；预期 root 能按自己的策略创建 coordinator/worker 混合分支，真正的失败只在验收阶段按事实汇报。

## 2026-05-12 R71 real E2E: production chain succeeds, missing QA is blocked by acceptance
- 中文说明：R71 在同一个购物站任务上验证了“少写死流程”的方向：root -> `小傻妞-商品协调员` -> `小小傻妞-商品协调员` -> `小小小傻妞-文件写入员` 这条 4 层真实链路跑通，leaf worker 自己写出了 10 个必需文件。
- 产物结果：`index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`、`cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js` 全部出现在 `/Users/example/my-终端应用/deliverables/stage7_shop_complete_20260512_r71/build`，且没有 `product.html/old-product.html/legacy.html/obsolete.html/output.json/RUNNER_RESULT.md/execution_context.json` 等禁止产物污染 build。
- 恢复能力：leaf 写较长 `products.html/app.js` 时出现过 `__parse_error__`，随后按“短骨架/短内容重试”恢复并继续写入，说明长工具参数恢复路径有效，但后续仍应继续优化大内容写入的外置/分块提示。
- 验收结果：root 最终被标记为 `BLOCKED/FAILED acceptance_failed`，原因是缺少用户点名 QA 角色 `tester, bug_finder, acceptor`；这次失败是正确失败，不是系统误挡。生产链路完成不等于整体完成，QA/找错/验收必须由真实 task 事实证明。
- 架构结论：调度层不再替 LLM 硬编码“每一层必须 coordinator”，但 acceptance gate 必须继续硬守底线；流程选择给 LLM/模板，完成判定看真实 `task.json`、角色覆盖、子树健康和 evidence。
- 下一步：增强 `quality_advice` / root closeout prompt，让 root 在生产 child 已完成后主动按 advice 创建 tester、bug_finder、acceptor；不建议恢复调度时自动补派 QA，避免又变成死流程。

## 2026-05-12 R72: QA advice reads descendant implementation readiness
- 中文说明：R71 暴露的下一层问题是 root 通过 coordinator 链路完成 leaf 后，QA 阶段门和 advice 仍只看直接 child；这会让 root 即使主动查询 `quality_advice`，也可能被误导为还要先实现。
- 已修正：`hierarchy_scope_guards.py` 和 `hierarchy_qa_scheduler.py` 都改为扫描父任务子树里的 ready implementation descendants；只要后代 worker/writer/leaf_worker 已进入 `AWAITING_ACCEPTANCE`、`NEEDS_ACCEPTANCE` 或 `DONE/VERIFIED`，父级就可以进入 QA advice/QA 创建阶段。
- Prompt 同步：coordinator runner prompt 明确写入：生产 child/leaf 已完成但父级仍缺 tester/bug_finder/acceptor 时，不要直接输出最终 `SUBAGENT_RESULT`，先调用 `schedule_child_subagents` 获取或执行 `quality_advice`，再由 LLM 选择 QA 数量、scope、顺序和 repair 策略。
- 保持边界：系统仍不自动创建固定 QA child；调度层只提供事实和候选，真正的 tester/bug_finder/acceptor 数量与顺序仍由 LLM/模板/workflow 决定。
- 已补测试：`test_hierarchy_schedule_allows_qa_after_implementation_descendant_ready`、`test_hierarchy_schedule_quality_advice_after_implementation_descendant_ready`，并更新 coordinator prompt contract。
- 真实 R72 结果：同一个 root-only 购物站 E2E 中，depth=2 coordinator 在 4 个 leaf worker 产出后真实创建并推进了 `tester`、`bug_finder`、`acceptor` 三类 QA 子代理，证明后代实现 readiness 已被 LLM 看见并用于后续派工。
- 真实阻塞：tester 发现 `checkout.html` 到 `order-success.html` 的流程断点；bug_finder 发现低优先级拼写问题；acceptor 给出通过判断但与 tester 冲突。父级最终按验收事实阻塞，root 因最终 `SUBAGENT_RESULT` 缺结束标记进入 `structured_output_parse_error`。这是正确暴露问题，不是调度阶段误挡。
- 下一步：做 LLM 引导的 repair wave / QA disagreement handling。大白话：QA 说“不通过”时，父级应该先让修复 worker 改具体文件，再让 tester/acceptor 复测；不要靠硬编码自动改，也不要让 acceptor 的单方通过盖掉 tester 的失败证据。

## 2026-05-12 R73: QA repair advice without hardcoded repair workflow
- 中文说明：R72 证明 QA 三角色能被创建，但也暴露 tester/acceptor 结论冲突。新的处理不是写死“失败就自动修”，而是在 dispatch payload 里给父级 LLM 一个 `qa_repair_advice`，让它基于失败 refs 自己安排修复和复测。
- 已实现：新增 `orchestration_quality_payload.py`，runner-context dispatch 会扫描当前父节点后代 QA 任务的小型 `output.json` 摘要和状态；只要 tester/bug_finder/acceptor 报告失败、缺陷、缺失、断裂或状态失败，就返回 `needs_repair_wave=true`、失败 QA run ids、短摘要 refs 和可编辑的 `schedule_child_subagents` repair worker 建议。
- 保持边界：系统只给 advice 和 guardrail，不直接创建 repair worker、不自动改文件、不让 acceptor 单方覆盖 tester 失败；父级 LLM 仍要决定修复哪个文件、派谁修、何时重新跑 tester/acceptor。
- Prompt 同步：coordinator prompt 增加 `qa_repair_advice / needs_repair_wave` 处理要求，提醒模型不要直接最终验收，应先按失败 QA refs 创建 scoped repair worker 并复测。
- R73 预检新发现：`duplicate_leaf_target:<file>` 这种底层硬阻断仍会误伤真实协作。理论上它能防两个普通 worker 撞同一文件，但真实项目里共享 `style.css/app.js`、QA 后 repair、父级接管都可能合法触碰同一文件；只要写入根可审计，就不应在调度层硬挡。
- 已修正：重复 leaf 目标从 hard block 改为 `scheduling_warnings`。调度会继续创建任务，同时在工具返回里提示父级协调 ownership、看板和消息；真正的安全边界保留在写入根、深度、数量、领域越界和工具执行层。
- R73 预检还发现 root 越跑越重：production child 已 ready 且父级仍缺 QA 角色时，dispatch payload 只提示可收口，root 便开始反复 `read_file/read_artifact` 自己验产物，prompt 从 4 万多字符涨到 9 万多字符。大白话：我们省上下文的初衷被破坏了，父级把孩子的细节又搬回自己脑子里。
- 已修正：runner-context `dispatch_subagents` 现在也会暴露 `quality_advice`。如果父任务点名 tester/bug_finder/acceptor，且实现后代已经 ready，payload 会返回 `next_action=create_quality_children_from_ready_refs`、候选 QA children 和“不要反复读正文替代 QA”的提示。root 应按 refs 创建 QA 子代理，而不是自己吞正文。
- 对标结论：长期助手 的 delegate 返回 summary、api_calls、tool_trace 和 token 统计；通道运行时 的 context-engine 会跳过重复 bootstrap、记录 prompt cache/usage，并用 context guard 控制窗口。我们应该学习“父级看摘要/refs/状态，不看孩子正文”的方向，而不是靠越来越多硬规则卡工作流。
- 已补测试：`test_hierarchy_schedule_warns_duplicate_verified_leaf_targets`、`test_dispatch_payload_suggests_quality_wave_before_closeout`，并回归 duplicate guard、dispatch child refs、schedule child tool 和 QA scheduler focused tests。
- 下一步：重跑干净 R74。预期 root 不再被 duplicate target 卡住，也不再靠反复读正文自验；应在 production ready 后按 dispatch 返回的 `quality_advice` 创建 tester/bug_finder/acceptor，再进入 QA repair/retest 链路。

## 2026-05-12 R74: delegating parent refs-only guard
- 中文说明：R74 真实复测证明，仅靠 prompt 让 root “不要读正文”不够。root 已有下级时仍会通过 `read_artifact/read_file` 自己翻孩子或产物正文，prompt 从约 3.3 万字符涨到 10 万以上；最重的交易 leaf 也因为任务过大和反复整文件读取涨到 9 万多字符。
- R74 事实：root 创建了 depth=1 `小傻妞-总协调员`，该 coordinator 创建了四个 depth=2 leaf worker，并写出购物站 10 个产物文件；但没有创建要求中的 depth=3 `小小小傻妞-*` 链路，QA/验收链也没有完整收口，所以这轮不能算通过。
- 已修正：新增 `orchestration_body_read_guard.py`，工具循环在执行 `read_file/read_artifact` 前检查当前 runner 是否已经委托 child。只要有 child 且子树里还没有完成的 `acceptor`，父级默认保持 refs-only，不能主动读取产物正文或大 artifact 正文。
- 保留口子：如果用户明确说“子代理做完你自己验收 / 你亲自看一下 / 主代理亲自检查”，当前 run 临时放行父级读正文；普通“验收标准”“主代理观察入口”不会误触发。
- 已修正：新增 `tool_context_orchestration_summary.py`。调度类大输出外置后，live prompt 保留 `next_action`、run ids、状态计数和 suggested tool call，不再默认给模型 `read_artifact_hint` 去展开大 dispatch artifact。
- 已补测试：`test_orchestration_body_read_guard.py` 覆盖委托期阻断、运行元数据放行、acceptor 完成后放行、用户显式 override 和泛化验收文字不误放；`test_tool_context_reducer.py` 覆盖 externalized dispatch/read_artifact 的 compact orchestration summary。
- 下一步：跑 focused verification 后干净启动 R75。预期 root 只看 refs/状态/advice，按建议创建 QA/repair/acceptance 子代理；只有 acceptor 完成或用户明确授权时，root 才做最后正文检查。

## 2026-05-12 R75: guard boundary narrowed to real safety
- 中文说明：R75 真实复测证明 body-read guard 的方向是对的，但边界太宽会让父级“看不到调度状态”；同时 `duplicate_child_domain` 把多个 QA/验收角色共享同一 build 目录误当成硬冲突，直接拖慢测试。
- 已修正：委托期父级仍不能读产品正文或大 child artifact 正文，但可以读取调度类小 artifact，例如 `dispatch_subagents-*`、`subagent_board-*`、due-check/action-plan refs。这让父级能靠 refs 指挥，而不是被迫读正文或盲修。
- 已修正：`duplicate_child_domain:<domain>` 从 hard block 改为 `scheduling_warnings`。重复 checkout/quality coordinator、多个 QA agent 看同一个目录、repair worker 重写同一文件，都只审计提醒，不阻断创建。
- 保留红线：`qa_before_implementation_ready` 暂时仍硬阻断，因为空 build 上跑 QA 会烧模型并产生假失败；写根漂移、越界路径、深度/数量、同批压扁层级等仍是安全/结构红线。
- 对标结论：长期助手/通道运行时 更像把硬安全放在工具执行、cwd/path、危险命令、自毁/系统路径上；规划顺序、QA 波次、repair 策略交给 LLM/模板/验收事实。我们也按这个方向收窄 guard。
- 已补测试：`test_hierarchy_schedule_warns_duplicate_coordinator_domains`、`test_delegating_parent_can_read_orchestration_artifact_before_acceptor_done`，并回归 duplicate guard 和 body-read guard focused tests。
- 下一步：干净启动 R76。预期 root/coordinator 能读取看板/dispatch refs，创建 tester/bug_finder/acceptor 或 repair 子代理，不再被同域 QA/重复目录卡住。

## 2026-05-12 R76: root no longer writes capability requests
- 中文说明：R76 暴露一个更基础的问题：root 没有上级，却能调用 `capability_request` 写 OPEN 请求，结果像“自己给自己请假”，会把 root 卡进等待授权流程。
- 已修正：`capability_request` 工具现在拒绝 root run；只有有 parent 的非 root runner 才能写 OPEN 能力申请。root 遇到任务目录内普通缺口应使用现有工具、调度下级或说明暂不支持；root 自毁/卸载/系统红线暂时不纳入当前能力申请链路，后续再单独设计 root policy。
- Prompt 同步：depth=0/root runner prompt 明确写入 `root 不走 capability_request` 和 `root 当前不应缺能力`，减少模型先走错路。
- 对标结论：通道运行时 的 exec policy 按 `security/ask/allowlist/approval` 在执行边界决策，长期助手 也把危险命令审批放在用户/配置策略层；两者都不是让最上层 agent 生成一条“等上级授权”的内部工单。我们按这个思路把 root 从 child capability 流程里拿出来。
- 已补测试：`test_capability_request_tool_blocks_root_run_requests`、`test_runner_prompt_tells_root_not_to_request_capability`，并回归 capability request tool 和 prompt contract focused tests。
- 下一步：继续干净 R77，重点观察 root 遇到删除/清理/缺工具时是否改为调度下级、记录策略阻止或走未来 root-policy 口子，而不是 OPEN capability request。

## 2026-05-12 R77: root cap request fixed, but state handoff drift remains
- 中文说明：R77 是干净任务目录下的真实 4 层购物站复测，外层观察者只创建第一层并观察，后续由 root/coordinator 继续派 child、grandchild 和 leaf。
- 已确认：root 没有再写 `capability_request`，下级能力申请通道仍保留；leaf 被工具轮数/资源限制卡住后，父级能通过 `subagent_board` 发现 BLOCKED，并创建 `小小小傻妞-rescue-worker` 接管剩余页面。
- 产物事实：rescue worker 最终把 `index.html`、`register.html`、`login.html`、`products.html`、`product-detail.html`、`cart.html`、`checkout.html`、`order-success.html`、`style.css`、`app.js` 写到 `/Users/example/my-终端应用/deliverables/stage7_shop_complete_20260512_r77/build`。
- 新问题 1：第一层 root/coordinator 因 `create_subagents` 未传 `agent_name` 仍落成 `general`，违反“小傻妞-*”命名观察规则；已修正为未传名时默认 `小傻妞-role`。
- 新问题 2：长 `write_file` 仍会先撞 inline 上限，模型会重复几轮后才切到短骨架/分块追加；现有恢复有效但不够丝滑，后续还要继续压低大正文单次工具参数概率。
- 新问题 3：顶层观察入口读取外置 board/dispatch 摘要后出现状态漂移，误判 build 目录为空，并基于错状态重复 dispatch。已先补调度摘要里的 `output_call_id` / `output_scoped_call_id` 和“优先用 scoped id，不抄长路径/hash”的提示，下一轮继续验证是否减少 artifact/path 串线。
- 已补测试：`test_explicit_coordinator_seed_without_name_gets_lineage_prefix`、`test_runner_prompt_tells_root_not_to_request_capability`、`test_dispatch_externalized_result_keeps_compact_next_action_without_read_hint`。
- 下一步：跑 R78 干净复测，重点观察三件事：第一层名字是否稳定为 `小傻妞-*`；root 是否继续不写 capability_request；父级读取 board/dispatch refs 后是否不再把真实产物状态看反。
