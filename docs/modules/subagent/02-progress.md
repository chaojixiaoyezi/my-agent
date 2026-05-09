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
- 2026-05-09 真实 MiniMax 多层级 E2E 修复第二片已落地：`subagents-recovery-tree` 会按 capability 超时阈值把真正活动中的 stale `RUNNING` 后代列为恢复候选，并推荐受控 `takeover_or_reassign`；协调型 `PLANNING` root 不再被 recovery-tree 当作 stale runner。
- 2026-05-09 真实 MiniMax 多层级 E2E 修复第三片已落地：`subagents-due-check --root-id <root>` 和 `subagents-plan-actions --root-id <root>` 可只巡检/规划一棵 subagent 任务树，避免多个真实 E2E 场景共用 workspace 时 `--all` 输出互相污染；带孩子且无 active runner attempt 的 `PLANNING` coordinator 不再按普通 runner timeout 接管，而是报告 `coordinator_heartbeat_stale` / `recover_coordinator_leadership`，提示父代理重新指定 leader。
- 2026-05-09 coordinator 领导权恢复第一片已落地：`subagents-apply-actions --apply --action recover_coordinator_leadership --run-id <stale_coord> --take-over-by <leader_run_id>` 要求新 leader 是已存在 subagent run，显式 apply 后会标记旧 coordinator 为 `TAKEN_OVER`，并把子任务 `supervisor/final_owner` 切到新 leader。
- 2026-05-09 coordinator 领导权恢复第二片已落地：同一受控 apply 现在会真正重挂直接子任务，更新 `parent_id`、`depth`、`supervisor`、`final_owner`，并递推刷新孙级 depth；普通 `save()` 仍保留 child_ids 防覆盖合并，只有 `save_hierarchy_links()` 允许精确移除旧父节点 child edge。
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
