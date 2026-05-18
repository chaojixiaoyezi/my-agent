# Memory：开发推进记录

## 已完成

- 2026-05-14 Compact Continue Packet typed envelope 第一片已落地：`memory-resume --from-compact` 返回的 continue packet 仍保留旧字段，同时新增 `typed_envelope.kind=compact_continue_packet`，把 apply/plan、work_state、guard、next_actions 和 recommended_read_paths 转成机器可读恢复包；它仍不执行工具、不改任务状态。
- 2026-05-17 Main Agent Context Bundle 与手动 compact/resume 对齐已落地：主代理保存型 run 会写 `Main Agent Context Bundle v1`；`memory-compact --apply` 会登记最近一次主代理任务卡，API 可显式传 `main_context_bundle_ref`；`memory-resume --from-compact` 会把这张任务卡放进 `main_context_bundle`、推荐读取路径、handoff/context block 和 continue packet。旧 apply 包没有该字段也可继续恢复。
- 2026-05-17 Main Agent Context Bundle 合同完整性已落地：context bundle 现在包含 RunScope、ToolManifest、Acceptance Contract、ArtifactRef、自检、schema migration policy、prompt budget 和 subagent-compatible owner model；`memory-compact --apply` 自动绑定最近任务卡前会做 scope match，避免 compact 老任务时误用最新任务卡；新增 `context-bundle latest --json` 只读观测入口。
- 2026-05-17 Context Bundle / Compact Apply 体积守卫清零：把 context bundle 渲染拆到 `context_bundle_rendering.py`，把 runtime 到 bundle 的桥接拆到 `runtime_context_bundle.py`，把 compact apply restore refs/apply bundle/ledger payload 拆到 `compact_apply_payloads.py`；strict code-size 已回到 `hard=0 high-risk=0 soft=0`。
- 2026-05-18 主代理内核硬化第一批已落地：公共模型调用路径新增 `model_call_ledger` 和动态首 token / provider wall timeout 记录；工具执行 envelope 增加 `tool_protocol_v2` 镜像；新增 `file_write_session` 大文件分块写入工具，支持 begin/append/finish/abort、chunk 幂等、manifest、sha256 校验和原子提交。focused tests、architecture guardrails、fast tests、ruff、strict code-size 均已通过。
- 2026-05-18 主代理执行合同层第一片已落地：新增 `agent/contracts/error_taxonomy.py`、`state_machine.py`、`idempotency.py` 和 `e2e_matrix.py`；ToolManifest failure taxonomy 已改用统一错误代码；这四个合同只描述错误、状态、幂等键和真实 E2E 场景，不直接阻断工作流，避免继续堆 prompt guard。
- 2026-05-18 执行合同层已接入 create/dispatch：`create_subagents` 输出 `operation_contract`，`current_turn_run_state` 输出 `state_machine_contract` 和 `recovery_recommendations`；显式命名的小傻妞按结构化名字复用，默认泛名仍按 goal/write-root 区分，减少重复创建和重复调度。
- 2026-05-18 工具结果与 E2E 矩阵继续接入合同层：`ToolExecutionResult` 失败时自动带 `error_code/recommended_action/recovery_hint`，typed tool result envelope 同步这些字段；新增 `e2e_matrix_runner.py` deterministic runner 第一片，先跑中文路径、大输出 artifact 元数据和工具失败分类，真实模型用例明确标为 skipped。
- 2026-05-17 compact/resume 体积边界同步整理：compact apply 的 Markdown 渲染拆到 `compact_apply_rendering.py`，compact resume 的 handoff/continue packet 派生输出拆到 `compact_resume_payloads.py`，新增 context bundle 专项测试拆到独立测试文件，避免主编排文件和大测试文件继续接近 code-size high-risk。
- 2026-05-17 Tool Output Artifact Refs 第一片已落地：`memory-compact --apply` 会只读扫描 `memory_archive/artifacts/tool_outputs/index.jsonl`，按 request/run/task scope 登记同任务的大工具输出 artifact refs；`work_state_snapshot.artifact_refs` 和 `memory-resume --from-compact recommended_read_paths` 都会带上这些路径。它只登记路径、hash、size 和 call id，不读取 artifact 正文。
- 2026-05-17 Artifact Read Hints 第一片已落地：`memory-resume --from-compact` 会从 `work_state_snapshot.artifact_refs` 生成 `artifact_read_hints`，在 handoff、context block 和 continue packet 中给出 `read_artifact` 的 `artifact_ref/offset/max_chars`；优先使用 scoped call id，避免恢复模型复制长路径出错。
- `memory_store/` 已承接长期记忆 JSONL 存储，根层 `memory.py` 保留兼容入口。
- `memory_routing/` 已有 route index 加载、匹配、校验、上下文读取和 receipt 结构。
- `memory_archive/` 已有压缩前 snapshot、raw event、每日 hook/raw JSONL、留存和 token 估算骨架。
- compression hook 已接入 `SimpleAgent.run()`：token 超阈值时先写 `memory_archive/snapshots/*.json` 权威快照，再做保守组合压缩。
- `memory_archive/tokens/` 已开始按 session 记录每轮 input/output/tool token 和累计 token。
- `memory-route`、`memory-doctor`、`memory archive` 相关 CLI 和测试已存在。
- `memory-compact --dry-run` 已能只读扫描 raw/hook、权威 snapshot 和 token ledger，输出 compact plan、风险提示和下一步建议。
- `memory-compact --apply` 已能生成非破坏性 compact context、apply metadata、apply ledger 和 post-compact self-check；当前不会删除、重写或裁剪 raw/hook/snapshot/token/task/run 文件。
- `memory-route --validate` 已能检查重复关键词、跨 route 冲突、死链和非法 `inject_mode`。
- `memory-archive-list --level <N>` 已能按 archive level 验证不同粒度落盘。
- capability gap 已接通 memory route，把相关长期规则路径补进子代理 `context_manifest.required_read_paths`。
- `memory-doctor` 已扩展 snapshot JSON 可读性和 hook/snapshot 层级一致性检查。
- `SimpleAgent.run()` 已有 routed memory 和 raw archive 的回归测试覆盖。
- `settings/memory.py`、`memory_store/jsonl.py`、`memory_routing/models.py`、`memory_routing/matcher.py` 已补齐更详细的 `LLM:` / `新手说明:` / 参数和返回说明。
- `memory_archive/` 整圈、`memory_routing/loader.py`、`memory_routing/context.py`、`cli/memory_commands.py`、`cli/memory_archive_commands.py` 已补齐同等级中文教学注释、字段说明、参数说明和返回说明。
- raw archive 写入现在和 hook snapshot 一样执行 readback 校验，避免“写了但没真的落盘/字段不完整”仍被当成成功。
- route index 的人工列表字段已明确支持英文逗号、中文逗号、英文分号、中文分号和竖线分隔。
- 已补真实跨天恢复 fixture：前一天 raw event、第二天 hook snapshot、任务目录 STATUS/HANDOFF，验证 `memory-resume` 和 `run()` 自动恢复都能回到任务事实源。
- `memory-resume --until YYYY-MM-DD` 现在按“包含当天全天”处理，避免用户写日期边界时漏掉当天白天的归档记录。
- gateway 请求跨天恢复已接入：LocalStore 命中 gateway_request 后，`memory-resume` / auto resume 会把 gateway request/response JSON 作为事实源推荐阅读。
- gateway 真实后台进程跨天恢复演练已接入 `scenario-test --case gateway-cross-day-resume`，不再只依赖手写 fixture 证明恢复逻辑。
- `memory-resume` 会把已移动的 gateway processing 请求路径纠偏到现存的 done/failed 终态路径，避免恢复提示指向过期临时文件。
- parent/subagent runner 跨天恢复演练已接入 `scenario-test --case parent-subagent-cross-day-resume`：真实 runner 工具回合写回后，`memory-resume` 能回到任务事实源路径。
- subagent checkpoint recovery artifacts 已进入 `memory-resume` 推荐路径：恢复简报会优先提示 `reports/checkpoint.json`、`status_report.json`、`progress.md`、`decision_ledger.json`、`failing_tests.json`、`next_actions.json`，再回到 `STATUS.md` / `HANDOFF.md` 等传统事实源。
- 2026-05-07 runtime memory 新目标边界已记录到 `06-runtime-memory-requirements.md`：memory 定位为运行时档案系统，主代理 memory 只索引任务/run/事件/artifact 引用，subagent 仍留在 task/run workspace，不能默认写主长期记忆。
- 2026-05-07 Phase 0 Task Workspace 骨架已落地：subagent 保存时会在 manager workspace 下同步 `tasks/<root_id>/task.yaml`、`state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/`、`artifacts/`、`agents/<run_id>/legacy_run_ref.json`；旧 `subagents/<run_id>/task.json` 和工单 Markdown 仍保持兼容事实源。
- 2026-05-07 Phase 1 Agent Run Workspace 适配已落地：`tasks/<root_id>/agents/<run_id>/` 现在会生成 `agent.yaml`、run `state.json`、`task.md`、run `timeline.jsonl`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl`、`inbox/`、`outbox/`、`artifacts/`、`compactions/`，并继续用 `legacy_run_ref.json` 指向旧工单目录。
- 2026-05-07 Phase 2 Daily Event Ledger 已落地：subagent 保存时会追加 `daily/YYYY-MM-DD/events.jsonl`，只记录 task/run 状态、摘要、duration、artifact/evidence refs、workspace 路径和检索字段，不写入完整 goal、工具输出或子代理上下文。
- 2026-05-07 Phase 3 Artifact 外置规范已落地：subagent 保存时会写 `tasks/<root_id>/artifacts/manifest.jsonl` 和 `tasks/<root_id>/agents/<run_id>/artifacts/manifest.jsonl`，把 `artifact_refs` 规范化为 summary/hash/path/size/exists 记录，不复制 artifact 正文。
- 2026-05-07 Phase 4 Checkpoint/Compact Chain 已落地：subagent 保存时会在 `tasks/<root_id>/agents/<run_id>/compactions/` 追加 `compaction_ledger.jsonl`，写每次 checkpoint snapshot 的 summary/metadata，并把最新 compact refs 回写到 run `checkpoint.json`；当前是 checkpoint-first 恢复链，不做 destructive compact apply。
- 2026-05-07 Phase 5 Shared Workspace 已落地：subagent 保存时会同步 `tasks/<root_id>/shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，只写任务局部结构化 facts 和引用，不进入主代理长期 memory。
- 2026-05-07 Phase 6 Memory Gate / Skill Spark 提升链第一片已落地：subagent 保存时会在 `tasks/<root_id>/agents/<run_id>/memory_gate/` 写 `candidates.jsonl`、`review_queue.jsonl` 和 `skill_spark_gate.json`，把 runner lessons / findings 变成带 evidence、scope、review 要求的候选；当前只排队 review，`promotion_status=not_promoted`，不会写主代理长期 memory 或正式 skill。
- 2026-05-07 Phase 6 review decision 写回已落地：`subagents-memory-gate <run_id> --candidate-id <id> --decision ...` 会把 review 结果写入 `memory_gate/decisions.jsonl`，并更新候选和 checkpoint 的 gate refs；approve 只表示允许后续显式导出流程继续，不会自动写长期 memory 或正式 skill。
- 2026-05-07 Phase 6 显式收口链已落地：`subagents-memory-gate` 现在支持 `--retention-dry-run/--retention-apply`、`--export-memory`、`--export-skill` 和 `--verify`；retention 只压缩 active queue 并保留审计，memory export 只处理 `approve_memory` 候选，skill export 只生成 draft，verifier 检查无自动提升边界。
- 2026-05-07 bundle 接口规范已写入 runtime memory 开发要求：复杂业务入口统一 Request/Options/Params，复杂输出统一 Result/Record/Report；CLI args 必须在 CLI 层转换，manager 可保留旧签名作为兼容 wrapper。
- 2026-05-07 P0 安全切片已落地：runtime compression snapshot 现在会带上 routed memory context 和 auto resume context；artifact manifest 只允许读取 legacy task dir、task workspace、agent run workspace 内的 artifact；shared workspace 的 findings/evidence 改为按 id 合并，避免 sibling 子代理互相覆盖。
- 2026-05-07 Compact Apply 语义拆分第一片已落地：`memory-compact --apply` 不再等同于 destructive rewrite，而是写 `memory_archive/compact_applies/` 下的 compact context、metadata、ledger 和 self-check，状态标记为 `applied_non_destructive`。
- 2026-05-07 Artifact Externalizer 第一片已落地：runtime 工具循环会把超过阈值的大工具输出写入 `memory_archive/artifacts/tool_outputs/*.json`，并追加 `index.jsonl`；`archive_tool_calls` 只保留 preview/hash/path/size，当前工具上下文仍保留完整结果，不改变本轮模型行为。
- 2026-05-08 Tool Output fail-safe checkpoint 已落地：大工具输出写 artifact 前会先写 recovery snapshot；snapshot 的 `tool_calls` 会保留工具名、调用 id、ok、output hash、size 和 externalized=pending 元数据，但仍不保存完整工具输出正文。
- 2026-05-08 ToolContextReducer live prompt 保护已落地：大工具输出外置后，下一轮 prompt 只注入 preview、artifact path、hash、size 和 fail-safe checkpoint；完整正文只留在 artifact 文件里。
- 2026-05-07 Control-plane Query API 第一片已落地：`query_memory_control_plane()` 会只读汇总 `daily/YYYY-MM-DD/events.jsonl`、task/run refs、`memory_archive/compact_applies/ledger.jsonl` 和 `memory_archive/artifacts/tool_outputs/index.jsonl`，按 date/task/run/event scope 返回轻量引用；它不读取大工具正文，不写入 workspace，也不替代 task/run 事实源。
- 2026-05-07 Schema v2 / Reserved Fields 已固化第一片：`daily_ledger_event`、`control_plane_task_run_ref`、`compact_apply` / `compact_apply_ledger` / `compact_apply_self_check`、`tool_output_archive_record` / `tool_output_artifact` / `tool_output_index` 现在统一写 `version=2`、`schema` 和结构化 `reserved={schema_name,schema_version,extensions,compat,future}`；后续新增字段优先走明确业务字段，实验性扩展只能放入 reserved 三槽。
- 2026-05-07 Compact Apply 第二片已落地：`memory-compact --apply` 现在除 context/metadata/self-check/ledger 外，还会写 `*.apply_bundle.json` 和 `*.restore_refs.json`，把原始 archive/snapshot/token refs 和恢复步骤串起来；如果 post-compact self-check 失败，会写 `*.self_check_failed.json` 并把 metadata/ledger 标记为 `blocked_self_check_failed`，仍然不删除、不重写、不裁剪历史事实源。
- 2026-05-08 手动 Compact Apply 完整化第一片已落地：`memory-compact --apply` 现在会生成稳定 `apply_id/plan_id`，并把同一组 ID 写进 metadata、apply bundle、restore refs、work state snapshot、self-check、失败报告和 ledger；新增 `*.work_state_snapshot.json` 作为后续手动 resume 和无人值守状态锁的对照基线。
- 2026-05-08 手动 Resume From Compact 第一片已落地：`memory-resume --from-compact <apply_id>` 会只读恢复 compact apply 产物，输出 `Compact Resume Context`、consistency report、推荐读取路径和下一步动作；`owner_type/owner_id` 已预留给未来子代理自动会话压缩，当前不触碰 subagent runner。
- 2026-05-08 半自动 Compact 提示第一片已落地：`run` 收尾会基于 token ledger 和上下文窗口返回 compact suggestion 字段，CLI 在达到阈值时提示 dry-run/apply/resume 命令；当前 `automatic_action=none`，仍需用户确认。
- 2026-05-08 自动 Compact/Resume 安全第一片已落地：`memory-resume --from-compact` 现在会输出 `compact_action_guard`；`manual` 要人工确认，`auto` 模式在缺 acceptance/constraints/latest_tests 等 work state 字段时会阻断并返回非零退出码。
- 2026-05-08 自动 Compact/Resume 协调第一片已落地：`run_memory_compact_auto_cycle()` 默认只生成 `compact_auto_cycle` 和人工确认建议；显式 `allow_apply=true` 时也只做非破坏性 apply、auto resume 和 action guard 检查，随后停住，不执行工具、不继续改代码。
- 2026-05-08 自动 Compact/Resume 触发器第一片已落地：`SimpleAgent.run()` 收尾已经接入 auto cycle 的默认 plan-only 分支，结果和 CLI 会暴露 `compact_auto` 的 status / next_action / tools 字段；当前仍不会自动写 apply 产物。
- 2026-05-08 Work State 字段来源第一片已落地：compact apply 会从 workspace 内 task/run 事实源读取 `ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`task.json` 等文件，把 acceptance、constraints、latest_tests 和 read_files 写入 `work_state_snapshot`；找不到时仍显式保留 missing，不猜测。
- 2026-05-08 真实 run Work State 回填第一片已落地：当没有 `memory_archive/snapshots/*.json` 权威 snapshot 文件时，compact apply 会只读 `restore_refs` 中登记的 hook/raw JSONL，从 hook recovery snapshot 或 raw 用户事件回填 `goal` 和 `next_step`；验收、约束、最近测试仍必须来自明确事实源，缺失时 auto resume 继续阻断。
- 2026-05-08 运行时 Fact Source 第一片已落地：真实 `run --save` 会写 `memory_archive/runtime_facts/<request_id>/task.json`，并把目录放入 recovery snapshot 的 `content_paths`；只有用户 prompt 中明确标注的验收、约束、测试条目或真实测试工具命令会进入 work state，普通回复不会被升级成验收事实。
- 2026-05-08 半自动补全提示第一片已落地：`memory-resume --from-compact` 在缺 work state 字段时会返回 `completion_prompt`，并在 CLI/context block 展示可复制的“验收条件/约束/测试”补全模板；这只是提示，不自动写事实源。
- 2026-05-08 手动补全事实写入第一片已落地：新增 `memory-fact-write`，只把用户显式传入的 acceptance/constraints/latest_tests 写入 `memory_archive/runtime_facts/<fact_id>/task.json`；后续按同一 request/session/task/run scope 重新 `memory-compact --apply` 时，work state 可以读取这些事实并让 auto guard 放行。
- 2026-05-08 Resume 交接包增强第一片已落地：`memory-resume --from-compact` 现在返回 `compact_resume_handoff`，并在 context block / CLI 中稳定展示目标、阶段、下一步、验收条件、约束、最近测试、推荐读取路径和 action guard 状态。
- 2026-05-08 自动 Guard 放行第一片已落地：当 work state 字段齐全、refs 存在、self-check 通过且 `resume_mode=auto` 时，`compact_action_guard` 会返回 `allow_automated_continue` / `allowed_to_continue=true`；报告仍明确 `automatic_tool_execution=none`，不会自动跑工具。
- 2026-05-08 子代理 Compact Owner 预留口第一片已落地：`memory-resume --from-compact --compact-owner-type subagent_run|subagent_session --compact-owner-id <run_id>` 会只读解析 `tasks/*/agents/<run_id>/` 和旧 `subagents/<run_id>/` 引用，返回 run workspace、checkpoint、summary、legacy adapter refs；仍不写主 memory、不改 runner、不自动执行工具。
- 2026-05-08 Continue Packet 第一片已落地：`memory-resume --from-compact` 现在返回 `compact_continue_packet`，把目标、阶段、下一步、验收、约束、最近测试、推荐读取路径、action guard 和 subagent owner refs 固定成统一继续契约；它只表达恢复上下文是否可继续，不代表业务验收通过。
- 2026-05-08 半自动 Resume 第二片已落地：`completion_prompt` 新增 `suggested_commands`，给出 `memory-fact-write --from-compact`、同 scope 重新 `memory-compact --apply` 和 `memory-resume --compact-resume-mode auto` 的闭环提示；仍只写用户显式确认事实，不解析助手回复。
- 2026-05-08 子代理 Compact Hook 预留第二片已落地：subagent owner refs 会带 `reserved_hooks`，预留 run-local `session_compact_ledger.jsonl` 和 `latest_continue_packet.json` 路径；当前 `enabled=false`，不写主 memory、不自动执行工具、不改 runner。
- 2026-05-08 Auto Compact/Resume 第一版增强已落地：新增 `memory_compact_auto_allow_apply` 配置，默认 false；开启后 `SimpleAgent.run()` 会做非破坏性 apply、auto resume、continue packet 和 guard 检查；guard 放行时主 agent 会把继续包注入下一轮 prompt 并受控续跑一次，阻断时仍停车。
- 2026-05-08 Auto Compact/Resume 持久化边界修正：即使配置开启 `memory_compact_auto_allow_apply`，`run(..., save=False)` / `--no-save` 仍会阻止自动 apply 写入 `memory_archive/compact_applies/*`，继续只返回人工确认建议。
- 2026-05-08 Runtime Fact Source 解析边界修正：显式验收/约束/测试段落遇到未知标题会停止当前桶，避免“实施步骤”等后续段落被误收为 acceptance/constraints/latest_tests。
- 2026-05-08 Compact work-state scope 安全修正：request/session/task/run id 现在按字面路径解析，`*`、`[]` 等 glob 字符不会扩大扫描 `tasks/*/agents/*`；半自动 completion 命令也会保留原 `--session-id/--request-id/--task-id/--run-id` scope。
- 2026-05-08 compact + parent acceptance 联调第一片已落地：新增 focused 测试串起 subagent task、compact apply/resume、continue packet、parent acceptance apply 阻断和 auto-policy dry-run；断言 auto-policy 仍 `executed=false`、`mutates_task_state=false`，且 task 状态不被 compact 自动链路改动。
- 2026-05-13 Code-size high-risk 清零第一片已落地：`memory_archive/query/resume_guidance.py` 承接 `ResumeGuidanceRequest` bundle，CLI/runtime 恢复建议不再用散装参数；相关 focused tests、ruff、strict code-size 已验证 `hard=0 high-risk=0 soft=0`。
- 2026-05-13 Home Runtime 读取侧迁移第一片已落地：`memory_store/jsonl.py` 的搜索/recall 会把 `~/.my-agent/memory/daily/YYYY-MM-DD.jsonl` 作为旧 `memory_path` 的补充事实源并去重，`all()` / `index_all()` 仍保持旧 memory_path 语义；新增 `home_runtime_query.py`，统一读取 daily memory、`workspace/tasks/{date}/{task_slug}` 和 home status。
- 2026-05-13 Home Runtime CLI/Doctor 第一片已落地：新增 `home-status`、`memory-daily-list`、`task-workspace-list`；`memory-doctor --json` 会报告 home 入口文件、关键目录和计数；`memory-resume --task-id/--run-id` 在旧 subagent 工单不存在时可回退到主代理 task workspace 的 `state.json` / `timeline.jsonl`。
- **记忆推模式** (`memory_push.py`)：在关键决策点自动查询并注入相关记忆，实现"推模式"记忆系统。
  - `MemoryType` 枚举：`LESSON_GENERAL`、`LESSON_TASK`、`LESSON_TEMP`、`CONTEXT`、`FACT`
  - `push_relevant_memories()` 函数：根据触发类型搜索相关记忆
  - `push_timeout_memories()`、`push_failure_memories()`、`push_planning_memories()` 快捷函数
  - `write_memory_with_type()` 写入带类型标签的记忆
  - `format_memories_for_injection()` 格式化记忆供上下文注入
- **dispatch_mixin 记忆注入**：runner 失败（BLOCKED/TIMEOUT）后自动注入相关教训记忆
- **failure_analyzer 记忆支持**：`FailureAnalysis` 增加 `relevant_memories` 字段

## 解决的问题

- 把“长期记忆”从纯聊天上下文，拆成可存储、可路由、可诊断、可归档的几个层次。
- `memory_rule_auto_read_limit=0` 表示不自动读取，避免配置为 0 时反而扩大读取范围。
- `--no-save` 不写 raw archive，保留用户显式隐私边界。
- 旧 memory 可以通过 LocalStore 补建索引，减少事实源和搜索索引断裂。
- 小白读者现在能从注释里区分：配置归一化、JSONL 事实流水、LocalStore 索引、route match、required/candidate path 和 read receipt 分别是什么。
- LLM 后续维护时可以更快识别哪些函数会写文件、哪些函数只是纯匹配、哪些函数会原地修改配置对象。
- 新手读者现在能沿着 archive 的 models/storage/runtime/snapshots/query/resume/context/CLI 一路看懂：事件怎么生成、怎么落盘、怎么读回、怎么搜索、怎么恢复。
- raw event readback 解决了“冷归档流水只 append、不验收”的问题；以后恢复线索不会因为半截写入被悄悄放大。
- Markdown route 分隔符兼容解决了“用户手写索引时用了中文标点/竖线，触发词没有被拆开”的问题。
- 跨天恢复测试解决了“只在同一天 happy path 里证明 resume 可用”的问题；现在能证明 archive 线索、LocalStore 索引、subagent 事实源在跨天 handoff 里能合流。
- date-only `until` 修复了解析边界偏机械的问题：用户说“到 2026-04-30”时，系统按 2026-04-30 全天理解。
- gateway 恢复解决了“只能找回 subagent 任务目录，普通 gateway 请求只能看到摘要”的问题；现在会把响应 JSON 带回 Recovery Brief。
- 真实 gateway 演练解决了“恢复测试只覆盖伪造请求，没有覆盖后台进程、request worker、response 落盘”的问题。
- processing 路径纠偏解决了“LocalStore 记录的是处理中文件，但第二天文件已经归档到 done/failed”的恢复断链问题。
- parent/subagent runner 演练解决了“只有手写跨天 fixture，还没证明真实 runner 写回后能被恢复入口找回”的缺口。
- checkpoint-first 推荐解决了“恢复时只能读长 Markdown 事实源，不能先读 compact 可恢复摘要”的缺口；现在 compact 后可以先读结构化恢复包，再核对原始任务文件。
- runtime memory 开发要求解决了“memory、task workspace、subagent workspace 概念混在一起”的风险；后续要按主代理档案馆、task 项目空间、agent run 工作位分层推进。
- Phase 0 task workspace adapter 解决了“只有旧 run 目录、没有任务级事实容器”的第一层缺口；现在能先按 root task 聚合状态、summary、timeline、shared/artifact/agents 目录，同时不迁移或污染旧路径。
- Phase 1 agent run workspace adapter 解决了“task 下有 agents 目录但 run 工作位仍只是 legacy 指针”的缺口；现在每个 run 有自己的恢复、接管、summary、finding 和 compact 预留面，但旧工单仍可读写。
- Phase 2 daily ledger 解决了“有 task/run 事实源，但主代理还没有按天索引 task/run/event/artifact 引用”的缺口；恢复入口可以先用 daily ledger 找线索，再回到 task/run 文件核实。
- Phase 3 artifact manifest 解决了“artifact_refs 只是散乱字符串，daily ledger 也只能看到原始 ref”的缺口；现在大输出保留在文件里，ledger 和 task/run workspace 只引用 manifest、summary、hash 和路径。
- Phase 4 compact chain 解决了“run workspace 只有 compactions 空目录，没有 append-only checkpoint snapshot 链”的缺口；现在每次保存都会留下可追踪的 checkpoint summary/metadata，且 checkpoint 明确标记原 timeline/artifact 被保留。
- Phase 5 shared workspace 解决了“shared/ 只有空文件，sibling 子代理没有结构化任务局部事实面”的缺口；现在 evidence packets、findings、status message 和 blackboard rollup 都能在 task workspace 内共享，但不会污染主 memory。
- Phase 6 memory gate 解决了“经验火花和 finding 没有提升门禁文件”的缺口；现在 lesson/finding 先进入 run-local review queue，必须补齐 evidence、适用范围、限制/反例和人工或 verifier 确认，后续流程才能考虑进入长期 memory 或正式 skill。
- Phase 6 review decision 解决了“候选只有排队，没有可审计 reviewer 结论”的缺口；现在 approve/reject/needs_evidence 会保留在 gate 文件里，而且后续 task save 会保留已写回的 decision。
- Phase 6 显式收口链解决了“approve 之后仍缺导出、清理和验收闭环”的缺口；现在 closed 候选能从 active review queue 清出但不删除审计，长期 memory 写入和 skill draft 生成都必须由 CLI 显式触发。
- bundle 接口规范解决了“函数参数散、Params/Options/Request 混用且后续扩字段会拉长签名”的问题；当前落点已覆盖 runtime memory workspace sync、memory gate export/review/retention、acceptance review、patch review/apply、action apply、lifecycle status、gateway/log-analysis/memory-archive/audit/local-storage/backend 和 CLI 边界转换，并新增架构护栏阻止业务代码继续引入函数级 var-keyword 服务接口。
- compression hook 门禁解决了“压缩前没有可靠快照也会继续执行，导致恢复锚点缺失”的问题。
- authoritative snapshot JSON 解决了“hook JSONL 适合搜索但不适合作为严格恢复锚点”的问题。
- capability gap 与长期规则联动解决了“子代理已经发现自己缺什么，但相关规则没有自动回流到执行上下文”的问题。
- archive level 过滤和 session token 账本解决了“不同粒度无法直接验收、token 只能估一轮”的问题。
- compact dry-run 解决了“还没压缩前不知道会碰到哪些归档、snapshot、token ledger 和风险”的问题；真实 apply 前可以先审计计划。
- compact apply 语义拆分解决了“checkpoint_only 和真正 apply 混在一起”的问题；现在 apply 先形成可审计恢复入口和自检报告，明确保留原始内容，后续才能继续做更激进的上下文裁剪。
- artifact externalizer 解决了“大工具输出只能混在工具上下文或归档摘要里”的问题；现在 compact/resume 能从 index 找到完整 artifact，而 raw archive、token ledger 和 apply metadata 不需要复制大正文。
- tool-output artifact refs 解决了“外置文件已经存在，但 compact 恢复包没有把它当作一等恢复事实”的问题；现在同 scope 的大工具输出会进入 restore refs、work state artifact refs 和 resume 推荐读取路径，后续接手者能按 artifact 路径窄读正文。
- artifact read hints 解决了“给了路径但没有告诉模型怎么读”的问题；恢复上下文现在会直接给出 `read_artifact` 分片读取参数，仍不自动读取正文、不执行工具。
- tool output fail-safe checkpoint 解决了“黑盒大输出外置过程中如果失败，可能没有恢复锚点”的问题；现在 externalizer 前先留下 metadata-only snapshot，后续接管代理至少能看到工具名、hash、大小和建议下一步。
- ToolContextReducer live prompt 保护解决了“artifact 已经外置，但下一轮 prompt 仍把完整大正文塞回上下文”的问题；现在模型看到的是恢复安全摘要，想读正文必须显式走 artifact 路径。
- control-plane query 解决了“daily ledger、compact apply、tool output index 和 task/run refs 只能各自散扫”的问题；现在 compact/resume/debug 可以先走统一只读入口，再按 refs 回到权威文件核实。
- Home Runtime 读取侧迁移解决了“家目录写了 daily memory 和主代理 task workspace，但搜索、resume、doctor 和 CLI 还看不到”的问题；现在 daily memory 可以被 `agent.recall()` 和 `memory-daily-list` 找到，主代理任务可以被 `task-workspace-list` 和 `memory-resume` 找到，doctor 能检查 home 骨架是否完整。
- schema v2 / reserved 固化解决了“索引记录以后要加字段时没有统一落点”的问题；现在核心 runtime memory 轻量记录都带同一个版本和保留槽，架构评审能区分正式字段、兼容字段和未来实验扩展。
- compact apply 第二片解决了“apply 只有摘要产物，但缺少显式恢复包和失败阻断”的问题；现在恢复时可以先读 apply bundle，再按 restore refs 回查原始事实源，自检失败也会留下机器可读失败报告。
- 自动 compact/resume 协调第一片解决了“直接接自动化容易一压完就继续乱跑”的问题；现在自动链路先有默认 plan-only、显式 allow_apply 和 action guard 停车点，后续再接真实触发器时不会越过安全边界。
- 自动 compact/resume 触发器第一片解决了“协调器写好了但 run 主链路还不知道”的问题；现在普通 run 达到阈值时会露出 auto cycle 停车状态，后续可以在同一接口上逐步接配置和无人值守策略。
- Work State 字段来源第一片解决了“action guard 永远只能看到 unknown”的问题；现在只要任务目录里有验收、约束和测试事实源，compact apply 就能把它们带进恢复基线，缺失时仍按 missing 处理。
- Resume 交接包增强第一片解决了“恢复结果只给路径和简单状态，不够接手”的问题；现在 handoff/context block 直接把接手者最需要看的目标、约束、验收、测试和 guard 状态摆出来。
- 自动 Guard 放行第一片解决了“guard 只能阻断，不能表达安全可继续”的问题；现在字段完整时能给自动流程一个明确 go 信号，但工具执行仍必须由后续更高层策略显式触发。
- Continue Packet 解决了“resume 输出能看但缺统一继续契约”的问题；现在手动、半自动和自动 compact 都能读同一份 packet 判断目标、约束、验收、测试、refs 和 guard 状态。
- compact + parent acceptance 联调解决了“恢复上下文可继续”和“子代理业务验收通过”容易混淆的问题；现在测试明确这两条链路相邻但不互相越权。
- P0 安全切片解决了三类恢复风险：compact 快照不会丢 routed/resume 恢复线索；artifact manifest 不会越界读本机任意绝对路径；shared workspace 不再由最后一次保存覆盖 sibling 已登记的 finding/evidence。

## 下一步

- 扩展 `scripts/check_doc_sync.py` 后续规则时，继续保持 memory 的 `02-progress.md` 和 `04-structure.md` 同步更新。
- 继续补损坏 snapshot、task 权威文件缺失、默认注入过多等异常场景联合测试。
- 继续补 subagent checkpoint artifact 缺失、损坏和旧任务未保存新字段时的恢复降级测试。
- 继续把 Phase 6 显式收口链接入更高层 verifier / acceptance 报告：当前已经有确定性 CLI 和 focused E2E，后续可把 `verifier_report.json` 纳入父级验收摘要。
- 记忆推模式接入更多决策点：planner 决策前自动注入 context 类型记忆（已实现：subagent_mixin.py run_parent_planner 前调用 push_planning_memories）
- 验证推模式记忆注入后 agent 行为是否正确改善

## 已跑测试

- 历史记录显示 memory 专项测试覆盖 config、routing、routing context、runtime、archive、archive CLI、archive runtime。
- 相关测试入口包括 `agent_py_agent/tests/test_memory_*.py` 和 `agent_py_agent/tests/test_memory_archive*.py`。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。
- 本轮 memory 注释同步 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `50 passed`。
- 本轮 memory 注释同步全量回归：`python -m pytest` -> `241 passed`。
- 本轮 archive/routing 第二刀快速验收：`python -m pytest agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_routing.py` -> `16 passed`。
- 本轮语法验收：`python -m py_compile ...memory_archive... memory_routing\loader.py memory_routing\context.py cli\memory_commands.py cli\memory_archive_commands.py` -> passed。
- 本轮 memory 第二刀 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `67 passed`。
- 本轮同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python -m pytest` -> `243 passed`。
- 本轮跨天恢复 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `14 passed`。
- 本轮跨天恢复 memory focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `69 passed`。
- 本轮跨天恢复同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮跨天恢复全量回归：`python -m pytest` -> `245 passed`。
- gateway fact source focused 验收：`python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `16 passed`。
- gateway fact source 宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_doc_sync.py` -> `72 passed`。
- gateway fact source 同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- gateway fact source 全量回归：`python -m pytest` -> `247 passed`。
- 真实 gateway 跨天恢复场景 focused 验收：`python -m pytest agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `16 passed`。
- 真实 gateway 跨天恢复宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `76 passed`。
- 真实 gateway 跨天恢复全量回归：`python -m pytest` -> `250 passed`。
- parent/subagent runner 跨天恢复 focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `2 passed`。
- parent/subagent runner 跨天恢复 CLI 文档验收：`python3 -m pytest agent_py_agent/tests/test_cli_reference.py -q` -> `1 passed`。
- 本轮 focused 组合验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `8 passed`。
- 本轮同步门验收：`python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python3 -m pytest -q` -> `251 passed`。
- 本轮记忆闭环 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_memory_first_loop.py agent_py_agent/tests/test_memory_archive.py agent_py_agent/tests/test_memory_archive_runtime.py agent_py_agent/tests/test_memory_cli.py agent_py_agent/tests/test_memory_archive_cli.py agent_py_agent/tests/test_memory_routing.py agent_py_agent/tests/test_memory_runtime_basics.py agent_py_agent/tests/test_memory_runtime_archive.py agent_py_agent/tests/test_memory_routing_context.py agent_py_agent/tests/test_agent/test_subagent_lifecycle.py` -> `59 passed`。
- 本轮记忆闭环全量回归：`python3 -m pytest -q` -> `365 passed`。
- 本轮推模式 focused 验收：`python3 -m pytest agent_py_agent/tests/test_memory_push.py agent_py_agent/tests/test_dispatch_loop.py -q` -> `39 passed`。
- 本轮 subagent checkpoint resume focused 验收：`python3 -m pytest agent_py_agent/tests/test_memory_archive_cli_query.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources agent_py_agent/tests/test_memory_runtime.py::test_auto_resume_context_recovers_cross_day_handoff_task -q`。
- 本轮 Phase 0 Task Workspace focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `3 passed`。
- 本轮 Phase 1 Agent Run Workspace focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `4 passed`。
- 本轮 Phase 2 Daily Event Ledger focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `5 passed`。
- 本轮 Phase 3 Artifact Manifest focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `6 passed`。
- 本轮 Phase 4 Checkpoint/Compact Chain focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `7 passed`。
- 本轮 Phase 5 Shared Workspace focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `8 passed`。
- 本轮 Phase 6 Memory Gate focused 验收：`python3 -m pytest agent_py_agent/tests/test_subagent_persistence_service.py -q` -> `9 passed`。
- 本轮 P0 安全 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_memory_first_loop.py::test_runtime_compression_receives_routed_and_resume_context agent_py_agent/tests/test_subagent_persistence_service.py::test_subagent_persistence_writes_artifact_manifests agent_py_agent/tests/test_memory_workspace_safety.py` -> passed。
- 本轮 compact apply focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py` -> `5 passed`。
- 本轮 artifact externalizer focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_tool_output_externalizer.py` -> `2 passed`。
- 本轮 Tool Output fail-safe checkpoint 验收：`python -m pytest -q agent_py_agent\tests\test_tool_output_externalizer.py` -> `3 passed`。
- 本轮 ToolContextReducer live prompt 保护验收：`python -m pytest -q agent_py_agent\tests\test_tool_output_externalizer.py agent_py_agent\tests\test_tooling_base.py::TestToolExecutionResult` -> `6 passed`。

## 未跑测试

- 暂未做真实跨午夜等待；当前跨天通过固定 archive 时间模拟。
- 暂未用真实外部模型跑 parent/subagent 跨天恢复场景；当前 scenario 使用确定性 backend，但会经过真实 runner、工具和 task fact source 写回路径。

## 风险

- memory 相关设计散在旧 ledger/backlog/test 文档中，第一版四件套还没有搬完全文。
- route index、raw archive、LocalStore、daily memory 同时存在，新手可能混淆“事实源”和“索引/摘要”的区别。
- 后续如果改恢复链路但不更新结构图，会很快重新变成散乱文档。
## 2026-05-06 code-size cleanup
- 中文说明：这一轮拆 memory archive query、resume brief、storage-date、runtime event 和 memory routing helper，输出语义不变，主要降低 near-soft 体积风险。
- Refactored memory archive query, resume brief, storage-date, runtime event, and memory routing helpers to reduce near-soft code-size risk.
- Preserved archive query outputs, memory routing receipts, and resume guidance semantics.
- Verified with memory/archive/routing focused tests, ruff, and the global code-size report.

## 2026-05-07 high-risk pre-clean
- 中文说明：这一轮先把 task workspace 的 state/timeline payload 和 JSONL helper 拆到 `task_workspace_payloads.py`，让 `task_workspace.py` 继续只管编排和路径；同时继续把 archive query、resume brief、memory gate、routing receipts 收进 bundle。
- Split task workspace state/timeline payload and JSONL helpers into `memory_archive/task_workspace_payloads.py` so `task_workspace.py` stays focused on orchestration and path wiring.
- Preserved task workspace file shapes, legacy run adapter behavior, and existing focused persistence tests.
- Continued bundle cleanup in archive query, resume brief, memory gate review, and routing receipts; these changes keep archive/LocalStore records as clues while task/run/gateway files remain the authority.
## 2026-05-07 LLM annotation coverage update
- 中文说明：这一轮只补 memory 模块产品代码的双层注释，不改行为、文件格式、工作流语义或公开接口。后续改恢复链路、bundle 或副作用时要同步维护。
- Product-code modules, classes, functions, and methods in the active module now carry the required `LLM:` plus `函数用途:` / `类用途:` definition-level double-layer comments format.
- This is a documentation-only maintainability pass: behavior, file formats, workflow semantics, and public interfaces are intended to stay unchanged.
- Future module changes must keep these comments current when changing module/class/def behavior, side effects, bundles, or caller expectations.

## 2026-05-08 compact resume fail-safe checkpoint refs
- `memory-resume --from-compact` 现在会从 compact restore refs 指向的 hook JSONL 中提取 `tool_output_externalizer` 的 fail-safe checkpoint。
- 新增输出字段 `fail_safe_checkpoints`，只包含 checkpoint path、line_no、snapshot_id、source/status、request/run/task id、工具名、调用 id、output hash、size、externalized 状态和 next_actions。
- `compact_resume_handoff` 和 `Compact Resume Context` 新增 `Fail Safe Checkpoints` 小节；`recommended_read_paths` 会把这些 checkpoint path 提前放入必读入口。
- 该流程只读 metadata-only hook checkpoint，不自动读取 `memory_archive/artifacts/tool_outputs/*` 的完整正文；完整大输出仍必须后续显式按 artifact 路径读取。

## 2026-05-08 compact resume fail-safe code-size split
- 为避免 `compact_resume.py` 和 `test_memory_compact.py` 继续接近 code-size 软上限，checkpoint JSONL 扫描逻辑拆到 `compact_resume_failsafe.py`，新增回归测试拆到 `test_memory_compact_failsafe.py`。
- 行为边界不变：memory-resume 只展示 fail-safe checkpoint refs / hash / size / next_actions，不自动读取 tool-output artifact 正文。

## 2026-05-08 artifact explicit read progress
- 新增 `memory-artifact-read <artifact_ref>`，只读取 tool output index 已登记 artifact；未登记普通文件会返回 `artifact_not_registered`，不打印正文。
- 新增 `read_artifact` 工具，模型只能通过 artifact path/hash/call_id 显式读取正文切片；默认 `max_chars=4000`，`max_chars=0` 表示读取完整正文。
- artifact reader 会校验路径仍在 `memory_archive/artifacts/tool_outputs/` 下，并校验 artifact content sha256，避免把 artifact ref 变成任意文件读取后门。

## 2026-05-08 artifact explicit read CI follow-up
- 补齐 `artifact_reader.py` 私有 helper 的双层用途注释，符合 code-size 脚本对产品代码可维护性的检查要求。
- 该修复只补充 reader helper 的边界说明和入口导入排序，不改变 `memory-artifact-read` / `read_artifact` 的 refs-only 读取边界。

## 2026-05-08 compact/resume safety review follow-up
- `SimpleAgent.run()` 的 auto compact apply 现在同时受配置开关和当前 run 的 `save` 边界控制：`save=False` 永远不写 `compact_applies`。
- runtime fact 显式段落解析、work-state scoped id 扫描、completion suggested command 都新增 focused 回归，防止事实串桶、glob 扩扫和 scope 丢失。
- 本轮 focused 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_memory_runtime_basics.py::test_run_no_save_blocks_opt_in_auto_compact_apply agent_py_agent/tests/test_memory_compact_runtime_facts.py::test_runtime_fact_source_stops_sections_at_unknown_headings agent_py_agent/tests/test_memory_compact_auto.py::test_memory_compact_work_state_treats_scope_ids_as_literal_paths agent_py_agent/tests/test_memory_compact.py::test_memory_compact_apply_reads_hook_recovery_state_without_snapshot_file` -> `4 passed`。

## 2026-05-11 subagent artifact allowed roots
- 中文说明：子代理 leaf 被授权写入 `allowed_write_roots` 后，artifact manifest 现在会把这些目录当成安全元数据解析根；这样 deliverables 里的真实产物能显示为 `resolved`，不会在接管包里误标越界。
- 行为边界不变：manifest 仍只记录 ref/path/exists/size/hash/status，不复制正文；未授权外部绝对路径仍然是 `blocked_outside_workspace`。
- 本轮 focused 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_artifact_allowed_roots.py agent_py_agent/tests/test_memory_workspace_safety.py::test_subagent_artifact_manifest_blocks_outside_workspace_refs` -> passed。

## 2026-05-11 artifact copied-prefix recovery
- 中文说明：真实 E2E 里模型会把 artifact 路径前缀抄成当前代码仓库路径，导致明明 index 里有登记，却按错误绝对路径读不到正文；现在 artifact reader 在精确匹配失败时，只允许用唯一 artifact 文件名回到 index 记录。
- 行为边界不变：修复只信任 `index.jsonl` 已登记记录，仍会检查 artifact 位于 `memory_archive/artifacts/tool_outputs/` 边界内，并校验 sha256；同名多条或未登记文件继续失败，不会变成任意文件读取。
- 本轮 focused 验收：`python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_memory_artifact_read.py::test_read_artifact_tool_repairs_wrong_prefix_with_unique_artifact_name` -> passed。

## 2026-05-11 scoped tool-output artifact refs
- 中文说明：真实多层子代理 E2E 暴露出 `read_artifact("17-1")` 这类短调用号会撞到旧轮次记录；模型收到的是“短号”，但 memory index 里同名旧记录排在前面，最终就像 prompt/路径被误传。参考 长期助手/通道运行时/会话运行时/模型助手 Code 的共同边界，工具结果引用必须带 session/run/workspace 作用域，不能靠全局短号猜。
- `tool_output_externalizer` 现在把 `request_id`、`run_id`、`task_id` 和 `scoped_call_id` 写进 archive record、artifact body 和 `index.jsonl`；subagent runner 调用模型时会把当前 `run_id/task_id` 传入 `agent.run()`，工具循环也会在 `read_artifact` 调用缺 scope 时补当前 subagent run scope。
- `artifact_reader` 现在解析 path/hash/scoped_call_id/call_id 时会优先匹配当前 run/task/request scope；没有显式 scope 时才回退到最新匹配记录，避免旧 run 的 artifact 被当成当前 run 的事实源。
- `tool_context_reducer` 的 live prompt 提示现在优先展示 artifact path、`output_scoped_call_id` 和 scope flags；模型要读正文时会拿到可复制的 scoped `read_artifact` 参数，而不是只看到全局短号。
- 行为边界不变：artifact 正文仍只通过显式 `read_artifact` 读取，仍校验 index、目录边界和 sha256；这次修复只改变“引用怎么定位”，不把大输出重新塞回 prompt，也不开放任意文件读取。
- 本轮 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_memory_artifact_read.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_failure_introspector.py agent_py_agent/tests/test_tools/test_tool_loop.py` -> `39 passed`。

## 2026-05-13 artifact read modes and budgets
- 中文说明：真实测试里模型会为了找一小段线索反复展开大 artifact。现在 `read_artifact` 不只有 offset slice，还支持 `mode=head/tail/search/slice`：看开头、看尾部、按关键词搜匹配行、或按 offset 分片读正文。
- `memory-artifact-read` CLI 同步支持 `--mode` 和 `--query`；`mode=search` 只返回带行号的匹配行，不把整个 artifact 搬回 prompt。
- 新增单 run artifact 正文读取预算：`tool_artifact_read_budget_window_seconds=600`、`tool_artifact_read_budget_max_chars=240000`，`0` 表示关闭。预算按 `run_id` 隔离，不限制普通主代理无 run_id 的聊天，也不做整棵任务树总预算。
- `max_chars=0` 仍兼容“读全部”，但如果有 run scope 和预算，会先从 `tool_outputs/index.jsonl` 的 `size_bytes` 做预判，过大就提前阻断，避免读 1G 日志这种事故先进入正文加载。
- `read_file` 误读 `memory_archive/artifacts/tool_outputs/*.json` 包装文件时，会根据当前 `allowed_tools` 给更准确提示：有 `read_artifact` 就直接让模型用它；没有就要求向父级发 `capability_request`，不再让受限 leaf 空转。
- 对标吸收：长期助手 的工具输出外置 + preview + 分段读，通道运行时 的响应前缀限制/事件截断，会话运行时/模型助手 Code 的“工具结果正文不要自动回灌 prompt”。my-agent 选择 index-first、refs-first、按需窄读，不把大 artifact 当普通文件读。
- 本轮 focused 验收：`/Users/example/ai_claw/bin/python -m pytest agent_py_agent/tests/test_memory_artifact_read.py -q` -> `12 passed`。

## 2026-05-13 backend config extraction
- 中文说明：把 memory / compact / artifact 的默认读取长度、预览长度、扫描文件数、恢复推荐路径数等行为参数集中到 `AgentConfig` 和 `agent_config.yaml`，避免继续散在代码里。
- `memory-artifact-read` 和 `read_artifact` 现在默认读取长度来自 `memory_artifact_default_read_chars`；显式传 `--max-chars` / `max_chars` 仍可覆盖。
- archive query / resume 的扫描文件上限、LocalStore 命中预览长度、recommended read paths 数量现在从 `memory_archive_search_file_limit`、`memory_query_content_preview_chars`、`memory_resume_recommended_read_paths_limit` 读取。
- raw archive 事件 preview 和 level=2 summary 的长度现在通过 `memory_archive_preview_level_*_chars` 和 `memory_archive_summary_chars` 控制；默认值保持旧行为。
- 这轮不改变现有归档文件格式，不删除旧数据，不把 artifact 正文自动塞回 prompt；只是把运行默认值抽到后端配置层。

## 2026-05-13 home runtime bootstrap
- 中文说明：单用户 `~/.my-agent` 暂不接飞书/QQ，但已经接入主代理本地运行时。`SimpleAgent` 启动会初始化 `my_agent_home`，普通保存型 run 会创建 `workspace/tasks/{date}/{task_slug}/outputs`、`runtime`、`agents`、`logs` 和 refs-only 状态文件。
- `JsonlMemory` 保留旧 `memory_path` 兼容，同时按配置镜像到 `memory/daily/YYYY-MM-DD.jsonl`，后续 query/resume 可以逐步迁移到按天流水。
- `PromptBuilder` 对 home-backed root 每轮按 `AGENTS.md`、`SOUL.md`、`USER.md`、`memory.md` 顺序读取四个家目录入口文件，并用 lesson 文件名和当前任务文本做轻量匹配；不会每轮全量读取整个 lessons 目录。
- provider trash 从 `provider_space.py` 拆到 `provider_trash.py`，新增按配置保留天数清理旧 trash 日期目录；破坏性操作仍默认走同空间 trash 和审计。
- 本轮 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_home_runtime_bootstrap.py agent_py_agent/tests/test_provider_space.py agent_py_agent/tests/test_config_normalize.py::TestNormalizeAgentConfig::test_normalize_home_provider_risk_fields agent_py_agent/tests/test_agent/test_memory_and_basic.py agent_py_agent/tests/test_prompting_builder.py` -> passed；strict code-size 维持 `hard=0 high-risk=0 soft=0`。

## 2026-05-13 compact auto continuation bridge
- 中文说明：主 agent 的自动 compact 已从“生成恢复包后停车”推进到“guard 放行后受控续跑一次”。第二轮 prompt 会重新带上 `AGENTS.md`、`SOUL.md`、`USER.md`、`memory.md`，并在 Runtime Injection 放入 `# Compact Auto Continuation`，要求只从 `Next Step` 继续。
- 防重复边界：续跑轮会跳过再次 compact，避免低阈值测试或长任务中出现 compact -> continue -> compact 的循环。字段缺失、自检失败、refs 异常或 `save=False` 时仍不会续跑。
- 本轮 focused 验收：`python -m pytest -q agent_py_agent/tests/test_memory_runtime_basics.py agent_py_agent/tests/test_memory_compact_auto.py agent_py_agent/tests/test_home_runtime_bootstrap.py agent_py_agent/tests/test_prompting_builder.py` -> passed。

## 2026-05-13 subagent task-local compact continuation
- 中文说明：子代理 compact/resume 先落地“任务本地接续”第一版。runner prompt 会在有 run workspace refs 时注入 `Task-Local Compact Continuation`，只读取 `tasks/<root>/agents/<run>/` 里的 checkpoint、summary、task、findings 和 `compactions/session/latest_continue_packet.json` 短片段，不读取主代理 `SOUL.md` / `USER.md` / 长期 memory。
- `compact_subagent_owner.py` 现在会把已存在的 `latest_continue_packet.json` 作为只读 ref 暴露给 `memory-resume --from-compact owner_type=subagent_run`；`reserved_hooks.continue_packet_ready=true` 只表示父级能看到恢复包，不会自动执行工具，也不会写主 memory。
- `context_bundle.workspace_refs` 同步补齐 agent run workspace 的 task/checkpoint/summary/final_report/findings/timeline/compactions/shared refs，方便父级、接管代理和 runner 都从同一 refs-first 工单包恢复。
- 闭环补齐：`SubAgentManager.save()` 现在会在每次保存后自动写 `compactions/session/latest_continue_packet.json` 和去重后的 `session_compact_ledger.jsonl`；父级下一次 runner/dispatch 重新构建 prompt 时，会自动读取这个包继续原任务。
- 本轮 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_subagent_prompt_contract.py agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_memory_compact_auto.py` -> passed；strict code-size 维持 `hard=0 high-risk=0 soft=0`。

## 2026-05-14 compact resume configured subagent workspace
- 中文说明：`memory-resume --from-compact --compact-owner-type subagent_run` 现在会使用真实配置里的 `subagent_workspace`，能找到测试目录或用户目录下的子代理 run workspace。
- 子代理恢复输出保持 task-local：推荐读取 `latest_continue_packet.json`、checkpoint、summary、task、timeline、findings，不推荐主代理 `SOUL.md` / `USER.md` / 长期 memory。
- resolver 现在把 owner id 当作字面路径段处理，不再让 `*` / `[]` 这类 glob 字符扩大扫描范围。
- 本轮真实验证：case09 tester run 恢复为 `linked_run_workspace`、`writes_main_memory=false`。

## 2026-05-14 compact auto multi-hop and scoped archive reads
- 中文说明：主 agent 自动 compact 现在可以按 `memory_compact_auto_continue_max_depth` 连续多次受控续跑；默认仍是保守值 1，测试或长任务可调高。
- 新增后端配置：`memory_compact_context_window_tokens` 控制 compact 阈值估算窗口；为 0 时继续走保守估算。
- runtime fact source 会读取 `# Compact Auto Continuation` 注入中的显式 acceptance、constraints、latest_tests，避免第二轮之后 work_state 字段丢失。
- compact work_state 从 shared raw/hook JSONL 文件读取记录时，会重新按 `session_id/request_id/run_id/task_id` scope 过滤。restore refs 指向的是文件，不代表整个文件都属于当前 compact。
- 真实验证：case11 clean4 低阈值 E2E 生成 4 个 apply 包，均 `missing_fields=[]` 并最终回复 `已完成`。
- 遗留：子代理自己的模型会话内多次 compact/apply/resume 仍待下一片实现；当前已具备 task-local continue packet 和 `memory-resume` owner refs。

## 2026-05-14 subagent session compact package
- 中文说明：子代理的 compact 第一片现在保持“任务本地”。当 `save=False` 的子代理 runner 触发 compact 建议信号时，不写主 memory archive，而是在该 run 的 `compactions/` 下写本地 compact package。
- package 固定包含 `latest_metadata.json`、`latest_summary.md`、`restore_refs.json` 和 append-only `session_compact_ledger.jsonl`。这些文件只保存恢复线索、状态、token budget 和 refs，不复制产物正文。
- `latest_continue_packet.json` 会带 `session_compact` 字段，父级恢复/重新 dispatch 时可以优先读 metadata/summary，再读 checkpoint/summary/task refs。
- 遗留：这不是完整的子代理模型会话内自动续跑。下一片要让 runner 在 package 就绪后受控接续，并用真实 E2E 验证一个子代理连续 compact 多次。

## 2026-05-15 LocalStore memory_path isolation
- 中文说明：真实子代理 E2E 发现临时 `memory_path` 仍可能从共享 LocalStore 索引里搜到旧任务记忆，导致 root 把旧任务当成本轮任务继续做。
- `JsonlMemory` 写 LocalStore memory 索引时现在会记录 `metadata.memory_path`；搜索时只接受当前 `JsonlMemory.path` 对应的索引命中。
- 旧的未带 `memory_path` 索引记录不会污染隔离 run；这些记录仍可通过原 JSONL/daily 文件链路读取，不作为当前临时记忆文件的 LocalStore 命中。
- 行为边界不变：LocalStore 仍只是搜索加速和线索入口，权威事实仍在当前 memory JSONL、daily ledger、task/run workspace 和 archive refs。
- 本轮 focused 验收：`python3 -m pytest -q agent_py_agent/tests/test_local_store.py::test_jsonl_memory_local_store_search_is_scoped_by_memory_path -p no:cacheprovider` -> passed。

## 2026-05-17 repeated compact lineage
- 中文说明：为“长任务多次 compact 不丢状态”补上 `lineage` 链路。每次 `memory-compact --apply` 都会从 append-only ledger 找同一 `plan_id` 的上一包，记录 `cycle_index`、`previous_apply_id`、上一包 metadata/apply bundle 引用和当前包引用。
- `memory-resume --from-compact` 和 `compact_continue_packet` 会带出同一份 lineage，让手动恢复、半自动恢复和后续自动恢复都能知道“这是第几次压缩、上一轮恢复包在哪里”，不用靠自然语言猜。
- 行为边界不变：lineage 只读 ledger、只追加新 apply 记录，不删除、不重写、不裁剪 raw/hook/snapshot/token/task/run 文件；旧 apply 包没有 lineage 也能继续恢复。
- 新增 focused 验收：连续 5 次 apply/resume 同一任务 scope，验证 apply id 不覆盖、cycle 连续递增、previous_apply_id 指向上一包、主代理 context bundle、tool-output artifact read hints、work_state 目标和下一步持续保留。

## 2026-05-18 main-agent foundation and artifact acceptance
- 中文说明：主代理基础测试新增固定入口 `main_agent_foundation_runner.py`。默认只跑不调用模型的确定性测试：工具失败分类、大输出 artifact refs、确定性 E2E matrix；真实模型用例明确 `SKIPPED`，不把未测试说成通过。
- 真实模型隔离测试跑在 `/Users/example/my-终端应用/main-agent-foundation-20260518_010636/`，使用独立 `home/` 和 `workspace/`，禁用 subagent。主代理独立完成 HTML 产物、路径记错后的 CSV 整理、手动 compact/resume、带 resume context 继续写交接说明。
- 真实测试发现：模型回复里说 HTML 已自检“无坏链”，但机器扫描发现 21 个 `href="#"` 占位链接。新增 `artifact_acceptance.py`，把 HTML 产物问题变成结构化 findings；主代理读取 findings 后修复同一文件，再次验收 `after_findings=0`。
- 对标吸收：会话运行时 的结构化工具输出和输出截断测试、通道运行时 的 E2E/live/docker/package acceptance、长期助手 的“live path 前必须 E2E”和动态 toolset 可用性过滤。my-agent 采用“模型自检只是说明，机器验收才是证据”的原则。
- 2026-05-18 继续收口：新增 `my-agent real-e2e` 正式 CLI，默认跑主代理基础确定性矩阵并写 refs-first 报告；`--artifact` 可把真实模型产物接入统一验收。`artifact_acceptance.py` 扩展为通用入口，当前覆盖 HTML、JSON、CSV、XLSX、PDF 和未知格式非空检查，后续浏览器/Excel/PDF 渲染验收可继续挂在同一合同下。
- 2026-05-18 主代理真实任务批量测试计划第一片已落地：新增 `main_agent_real_task_suite.py` 和 `main_agent_real_task_suite_cases.py`，`my-agent real-e2e --real-task-suite` 会生成家具 HTML、购物站点、GitHub 升星 XLSX、DeepSeek 论文翻译 PDF 的 prompt refs、acceptance refs、expected artifact refs、worker slot 和 timeout；默认只规划不调用模型，避免继续手动乱开多个长期进程。
- 2026-05-18 主代理真实任务受控执行第一片已落地：新增 `main_agent_real_task_execution.py` 三件套和 `main_agent_real_task_acceptance.py`，`my-agent real-e2e --run-real-tasks` 会给每个 case 写独立配置、命令、stdout/stderr、acceptance report 和 workspace，并按 `--real-task-max-workers` 并发执行。执行完成后按 expected artifact 合同验收产物，避免“进程 0 退出但没交付文件”被误判成功；未传 `--real-task-base-config` 时使用离线 echo 配置，传真实配置才会调用真实模型。
- 2026-05-18 主代理真实任务复验第一片已落地：新增 `main_agent_real_task_revalidation.py` 和 `my-agent real-e2e --revalidate-real-task-report`，可以读取已有 execution report 并只读复验产物，不重新启动模型进程。真实 API 跑完后可反复检查文件质量和验收报告。
