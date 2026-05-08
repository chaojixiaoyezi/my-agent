# Memory：开发推进记录

## 已完成

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
- 2026-05-08 Resume 交接包增强第一片已落地：`memory-resume --from-compact` 现在返回 `compact_resume_handoff`，并在 context block / CLI 中稳定展示目标、阶段、下一步、验收条件、约束、最近测试、推荐读取路径和 action guard 状态。
- 2026-05-08 自动 Guard 放行第一片已落地：当 work state 字段齐全、refs 存在、self-check 通过且 `resume_mode=auto` 时，`compact_action_guard` 会返回 `allow_automated_continue` / `allowed_to_continue=true`；报告仍明确 `automatic_tool_execution=none`，不会自动跑工具。
- 2026-05-08 子代理 Compact Owner 预留口第一片已落地：`memory-resume --from-compact --compact-owner-type subagent_run|subagent_session --compact-owner-id <run_id>` 会只读解析 `tasks/*/agents/<run_id>/` 和旧 `subagents/<run_id>/` 引用，返回 run workspace、checkpoint、summary、legacy adapter refs；仍不写主 memory、不改 runner、不自动执行工具。
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
- tool output fail-safe checkpoint 解决了“黑盒大输出外置过程中如果失败，可能没有恢复锚点”的问题；现在 externalizer 前先留下 metadata-only snapshot，后续接管代理至少能看到工具名、hash、大小和建议下一步。
- ToolContextReducer live prompt 保护解决了“artifact 已经外置，但下一轮 prompt 仍把完整大正文塞回上下文”的问题；现在模型看到的是恢复安全摘要，想读正文必须显式走 artifact 路径。
- control-plane query 解决了“daily ledger、compact apply、tool output index 和 task/run refs 只能各自散扫”的问题；现在 compact/resume/debug 可以先走统一只读入口，再按 refs 回到权威文件核实。
- schema v2 / reserved 固化解决了“索引记录以后要加字段时没有统一落点”的问题；现在核心 runtime memory 轻量记录都带同一个版本和保留槽，架构评审能区分正式字段、兼容字段和未来实验扩展。
- compact apply 第二片解决了“apply 只有摘要产物，但缺少显式恢复包和失败阻断”的问题；现在恢复时可以先读 apply bundle，再按 restore refs 回查原始事实源，自检失败也会留下机器可读失败报告。
- 自动 compact/resume 协调第一片解决了“直接接自动化容易一压完就继续乱跑”的问题；现在自动链路先有默认 plan-only、显式 allow_apply 和 action guard 停车点，后续再接真实触发器时不会越过安全边界。
- 自动 compact/resume 触发器第一片解决了“协调器写好了但 run 主链路还不知道”的问题；现在普通 run 达到阈值时会露出 auto cycle 停车状态，后续可以在同一接口上逐步接配置和无人值守策略。
- Work State 字段来源第一片解决了“action guard 永远只能看到 unknown”的问题；现在只要任务目录里有验收、约束和测试事实源，compact apply 就能把它们带进恢复基线，缺失时仍按 missing 处理。
- Resume 交接包增强第一片解决了“恢复结果只给路径和简单状态，不够接手”的问题；现在 handoff/context block 直接把接手者最需要看的目标、约束、验收、测试和 guard 状态摆出来。
- 自动 Guard 放行第一片解决了“guard 只能阻断，不能表达安全可继续”的问题；现在字段完整时能给自动流程一个明确 go 信号，但工具执行仍必须由后续更高层策略显式触发。
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
- Refactored memory archive query, resume brief, storage-date, runtime event, and memory routing helpers to reduce near-soft code-size risk.
- Preserved archive query outputs, memory routing receipts, and resume guidance semantics.
- Verified with memory/archive/routing focused tests, ruff, and the global code-size report.

## 2026-05-07 high-risk pre-clean
- Split task workspace state/timeline payload and JSONL helpers into `memory_archive/task_workspace_payloads.py` so `task_workspace.py` stays focused on orchestration and path wiring.
- Preserved task workspace file shapes, legacy run adapter behavior, and existing focused persistence tests.
- Continued bundle cleanup in archive query, resume brief, memory gate review, and routing receipts; these changes keep archive/LocalStore records as clues while task/run/gateway files remain the authority.
## 2026-05-07 LLM annotation coverage update
- Product-code modules, classes, functions, and methods in the active module now carry the required `LLM:` plus `函数用途:` / `类用途:` definition-level double-layer comments format.
- This is a documentation-only maintainability pass: behavior, file formats, workflow semantics, and public interfaces are intended to stay unchanged.
- Future module changes must keep these comments current when changing module/class/def behavior, side effects, bundles, or caller expectations.
