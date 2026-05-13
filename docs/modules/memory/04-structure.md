# Memory：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- memory.py                         # 旧兼容入口，真实存储已拆到 memory_store/
|-- memory_settings.py                # 旧兼容入口，真实配置解析在 settings/memory.py
|-- settings/memory.py                # memory 配置、默认值、warning、安全归一化和参数边界
|-- memory_store/                     # 长期记忆 JSONL 事实流水，可选同步索引到 LocalStore
|-- memory_routing/                   # route index、匹配、required/candidate path、read receipt
`-- memory_archive/                   # hook snapshot、raw archive、留存、token 估算、compact 预演
    |-- agent_run_workspace.py         # Phase 1 task-local agent run workspace 骨架
    |-- artifact_registry.py           # Phase 3 artifact manifest summary/hash/path 和 workspace 边界
    |-- compact_action_guard.py        # 自动 compact/resume 前的动作守门报告
    |-- compact_apply.py               # 非破坏性 memory-compact --apply 编排入口
    |-- compact_apply_ids.py           # apply_id / plan_id / scope hash / 风险摘要 helper
    |-- compact_apply_io.py            # compact apply JSON/Markdown/JSONL 写入 helper
    |-- compact_apply_self_check.py    # compact apply post-check 和失败阻断 payload
    |-- compact_apply_work_state.py    # compact apply work_state_snapshot 恢复基线
    |-- compact_work_state_sources.py  # workspace 内 task/run 验收、约束和测试事实源扫描
    |-- compact_auto.py                # 自动 compact/resume 安全协调器，默认只计划不继续执行工具
    |-- compact_continue_packet.py     # compact resume 后的继续工作包，固定目标/约束/验收/guard 状态
    |-- compact_resume.py              # memory-resume --from-compact 手动恢复上下文
    |-- compact_resume_blocked.py      # compact apply 缺失时的 schema-compatible 阻断 payload
    |-- compact_resume_completion.py   # 缺失 work_state 字段时的半自动补全提示模板
    |-- compact_resume_handoff.py      # compact resume 交接包和可粘贴上下文块
    |-- compact_subagent_owner.py      # compact resume 的子代理 owner/run workspace 只读引用解析
    |-- compact_suggest.py             # 半自动 compact 风险提示和命令建议
    |-- compact_chain.py               # Phase 4 checkpoint-first compact chain ledger
    |-- control_plane.py               # 统一只读查询 daily/task-run/compact/tool-output 轻量索引
    |-- daily_ledger.py                # Phase 2 daily/YYYY-MM-DD/events.jsonl 事件索引
    |-- memory_gate_export.py          # Phase 6 显式长期 memory / skill draft 导出
    |-- memory_gate_retention.py       # Phase 6 保守 retention 计划和 active queue 压缩
    |-- memory_gate_verifier.py        # Phase 6 no-auto-promotion 边界检查
    |-- runtime_fact_source.py         # 真实 run 保存时写入显式验收/约束/测试事实源
    |-- schema.py                      # Runtime memory schema v2 和 reserved 字段统一 helper
    |-- shared_workspace.py            # Phase 5 task-local append/merge blackboard/messages/findings/evidence
    |-- task_workspace.py              # Phase 0 文件系统 task workspace 骨架和旧 subagent run adapter
    |-- task_workspace_rendering.py    # task workspace YAML/Markdown 小文件渲染 helper
    |-- tool_output_externalizer.py    # runtime 大工具输出 artifact 化和 index
    |-- ../../agent_core/tool_context_reducer.py # 大工具输出进入 live prompt 前的摘要化边界
    |-- snapshots/_helpers.py          # snapshot 字段裁剪、hash、工具调用 metadata 归一化 helper
    `-- query/task_sources.py          # task/run 恢复入口推荐，不把子代理内容写入主 memory

agent_py_agent/cli/
|-- memory_commands.py                # memory-route / memory-doctor 等可见诊断命令
|-- memory_archive_commands.py        # memory-archive-list/search/resume 命令
|-- memory_resume_compact_rendering.py # memory-resume --from-compact 人类输出渲染
`-- memory_compact_commands.py        # memory-compact dry-run 和非破坏性 apply 命令
```

## 核心文件

- `memory_store/jsonl.py`：读写长期记忆 JSONL，是最朴素的事实落盘层；LocalStore 只是索引，不替代 JSONL。
- `settings/memory.py`：解析配置，处理非法值回退和 warning；会原地更新 AgentConfig-like 对象。
- `memory_routing/loader.py`：读取 route index；JSON 面向程序稳定性，Markdown 面向人工维护，并兼容常见中英文列表分隔符。
- `memory_routing/models.py`：定义 route、match、path resolution、read receipt 等票据结构。
- `memory_routing/matcher.py`：根据用户输入匹配可能需要读取的长期规则，并区分 required/candidate path。
- `memory_routing/context.py`：把命中的规则变成运行时可注入的上下文片段。
- `memory_archive/models.py`：定义 `CompressionSnapshot` 和 `RawMemoryEvent` 两类归档数据形状。
- `memory_archive/storage.py`：保存 raw archive、hook snapshot 和 `memory_archive/snapshots/*.json` 权威快照；写入都做 readback 校验。
- `memory_archive/runtime.py`：把 run turn 的用户、助手、工具元数据写成 raw archive 事件。
- `memory_archive/snapshots.py`：在 run/gateway/subagent 完成点写轻量恢复 snapshot，并提供压缩前必须成功的 `write_compression_snapshot()` hook。
- `memory_archive/query.py`：把 raw/hook JSONL 读成统一可搜索记录，并整理 resume 线索。
- `memory_archive/query/task_sources.py`：集中维护 subagent 恢复事实源优先级；checkpoint artifacts 优先，传统 `STATUS.md` / `HANDOFF.md` 继续保留。
- `memory_archive/task_workspace.py`：创建文件系统版 task workspace 的最小骨架，并写 `agents/<run_id>/legacy_run_ref.json` 指向旧 subagent work-order 目录；这是 adapter，不迁移历史目录。
- `memory_archive/task_workspace.py`、`agent_run_workspace.py`、`daily_ledger.py`、`artifact_registry.py`、`compact_chain.py`、`shared_workspace.py`：这些 runtime memory 写入入口统一提供 `*Request` bundle；旧参数形态只作为兼容 adapter，新增字段应进入 bundle，避免跨阶段继续拉长函数签名。
- `memory_archive/task_workspace_payloads.py`：承接 task workspace 的 `state.json`、`timeline.jsonl` payload 组装和小型 JSON/JSONL 读写 helper，让 workspace 主文件继续只负责编排。
- `memory_archive/task_workspace_rendering.py`：承接 task workspace 的 `task.yaml`、summary 和 blackboard 初始内容渲染，避免同步编排文件继续膨胀。
- `memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 run workspace 骨架，包含 agent 身份、run state、任务说明、checkpoint、summary、final report、findings 和 inbox/outbox/artifacts/compactions 目录。
- `memory_archive/daily_ledger.py`：维护 `daily/YYYY-MM-DD/events.jsonl`，只追加 task/run 状态、摘要、duration、refs、artifact/evidence refs 和检索字段，不存完整上下文或工具输出。
- `memory_archive/artifact_registry.py`：把 `SubAgentTask.artifact_refs` 规范化为 task/run 两份 `artifacts/manifest.jsonl`，记录 ref、resolved path、exists、size、sha256、summary、kind 和 `resolution_status`；只会读取 legacy task dir、task workspace、agent run workspace 和当前 run 的 `allowed_write_roots` 内的文件，越界绝对路径只登记 blocked 状态，不复制正文也不计算 hash。
- `memory_archive/compact_chain.py`：在 run `compactions/` 下追加 checkpoint snapshot ledger，写每次 summary/metadata，并把最新 compact refs 回写到 run checkpoint；当前只做恢复链，不删除原始 timeline/artifact。
- `memory_archive/compact_apply.py`：把 `memory-compact --dry-run` 的计划显式落成非破坏性 apply 产物，写 `compact_applies/<apply_id>.md`、metadata JSON、apply bundle、restore refs、work state snapshot、self-check JSON、可选 self-check failed JSON 和 append-only ledger；它不删除或重写 raw/hook/snapshot/token/task/run 文件。
- `memory_archive/compact_apply_ids.py`、`compact_apply_work_state.py`、`compact_work_state_sources.py`、`compact_apply_self_check.py`、`compact_apply_io.py`：分别负责稳定 apply/plan 标识、恢复状态基线、workspace 内 task/run 事实源字段读取、自检/失败报告和落盘 IO，让 compact apply 主流程继续保持薄编排，后续接 `memory-resume --from-compact` 时优先复用这些产物。`compact_apply_work_state.py` 优先读取权威 snapshot；没有 snapshot 文件时，只从本次 `restore_refs` 登记的 hook/raw JSONL 回填真实 run 的 goal/next_step。
- `memory_archive/compact_resume.py`：只读读取 compact apply metadata、apply bundle、restore refs、work state snapshot、compact context 和 self-check，生成 `memory-resume --from-compact` 的恢复上下文、consistency report、推荐读取路径和 continue packet；它会把 `owner_type/owner_id` 传给子代理 owner resolver，但仍不读写 subagent runner。
- `memory_archive/compact_resume_blocked.py`：在 apply metadata 缺失时生成和正常 resume 同 schema 的阻断 payload，包含 action guard 和 subagent owner 边界；主恢复编排不承载错误 payload 细节。
- `memory_archive/compact_continue_packet.py`：把 handoff、work_state、action guard、推荐读取路径和子代理 owner refs 组装成 `compact_continue_packet`，给手动、半自动和未来自动流程一个共同继续契约；它只打包已有结果，不读 artifact 正文、不执行工具。
- `memory_archive/compact_resume_completion.py`：当 resume 发现 missing work_state 字段时，生成 `completion_prompt`，说明缺哪些字段、对应人类标签、可复制补全模板和 `memory-fact-write` / 重新 apply / auto resume 建议命令；它只提示，不写 runtime facts。
- `memory_archive/compact_resume_handoff.py`：把 compact resume 的 work state、action guard、推荐读取路径和下一步动作整理成 `compact_resume_handoff`，并渲染可复制到新会话的上下文块。
- `memory_archive/compact_subagent_owner.py`：为 `subagent_run` / `subagent_session` compact resume 只读解析 `tasks/*/agents/<run_id>/` 和旧 `subagents/<run_id>/`，返回 run workspace、checkpoint、summary、timeline、artifacts、compactions、legacy adapter refs 和未来 `session_compact_ledger` / `latest_continue_packet` 预留 hook；输出明确 `memory_scope=task_local`、`writes_main_memory=false`、`automatic_tool_execution=none`。
- `memory_archive/compact_action_guard.py`：在 compact resume 后生成动作守门报告；`manual` 模式要求人工确认，`auto` 模式必须通过一致性、自检、refs 和 work state 字段检查，否则阻断为 `blocked_*`；字段齐全时只返回 `allow_automated_continue` 机器信号，仍不执行工具。
- `memory_archive/compact_suggest.py`：根据累计 token、上下文窗口和 compact dry-run plan 生成半自动提示，返回 `status`、`message`、`recommended_commands` 和 `requires_confirmation`；它只提示，不自动 apply，不切换上下文。
- `memory_archive/compact_auto.py`：串起 compact suggestion、可选非破坏性 apply、auto resume、continue packet 和 action guard；默认 `allow_apply=false` 只返回 `needs_user_confirmation`，配置 `memory_compact_auto_allow_apply=true` 时也只写非破坏性 apply 并在 guard/packet 前停住，不会执行工具或继续改代码。`SimpleAgent.run()` 收尾会把 `compact_auto` 状态、apply_id 和 continue_ready 暴露给结果和 CLI。
- `memory_archive/tool_output_externalizer.py`：在工具循环归档时把超过阈值的大工具输出写成 `memory_archive/artifacts/tool_outputs/<tool>-<call>-<hash>.json`，并追加 `index.jsonl`；archive/tool event 只保存 preview/hash/path/size。
- `agent_core/tool_output_failsafe.py`：在调用 tool output externalizer 前写 recovery snapshot，保留工具名、hash、大小、run/task/request id 和下一步建议；完整输出正文仍只在 artifact 文件里。
- `agent_core/tool_context_reducer.py`：控制工具结果进入下一轮 live prompt 的形态；大输出只注入 preview、artifact path、hash、size 和 fail-safe checkpoint，小输出仍保留原始工具结果文本。
- `memory_archive/snapshots/_helpers.py`：把 snapshot 中的工具调用压成恢复安全 metadata；现在会保留 output hash、size 和 externalized 状态，不保存正文。
- `memory_archive/control_plane.py`：提供 `query_memory_control_plane()` 只读入口，汇总 daily event、task/run refs、compact apply ledger 和 tool output index；返回 preview/hash/path/counts，不读取 artifact 正文，也不写任何 workspace 文件。
- `memory_archive/schema.py`：定义 runtime memory schema v2 的统一版本号、`schema` 描述和 `reserved={schema_name,schema_version,extensions,compat,future}` 结构；daily ledger、task/run refs、compact apply 和 tool output index 先共用这套形状。
- `memory_archive/shared_workspace.py`：同步 task-local shared blackboard、status messages、findings 和 evidence packet 文件；messages 追加，findings/evidence index 按 id 合并，blackboard 从合并后的共享事实重建。这是 sibling 子代理协作面，不写入主代理长期 memory。
- `memory_archive/memory_gate_export.py`：只在显式命令触发时，把 `approve_memory` 候选写入主 JSONL memory，或把 `approve_skill` 候选写成 run-local skill draft；不会自动安装正式 skill。
- `memory_archive/memory_gate_retention.py`：生成 retention 计划，apply 时只从 active review queue 移除 closed 候选，候选、decision 和 export 审计日志继续保留。
- `memory_archive/memory_gate_verifier.py`：写 `verifier_report.json`，检查 decision/export 是否仍保持 `auto_promote=false` 和显式提升边界。
- `memory_archive/runtime_fact_source.py`：真实 `run --save` 会写 `memory_archive/runtime_facts/<request_id>/task.json`，记录 goal、next_actions、运行状态，以及用户 prompt 中明确标注的 acceptance/constraints/latest_tests；`memory-fact-write` 也会把用户确认的补全事实写入同类 `task.json`。普通模型回复不会被解析成验收事实。
- `docs/modules/memory/06-runtime-memory-requirements.md`：定义 memory 作为运行时档案系统的开发要求，明确主代理长期记忆、每日账本、task workspace、agent run workspace、artifact、checkpoint、compact 和 retention 的边界。
- `memory_archive/resume_brief.py`：把归档、LocalStore、任务事实源压成恢复简报。
- `memory_archive/resume_context.py`：在“继续/恢复”类提示里按配置构造自动注入的恢复上下文。
- `memory_archive/tokens.py`：为归档预算提供保守 token 估算，并维护 session 级 token 账本。
- `memory_archive/compact.py`：构建只读 compact plan，汇总 raw/hook、权威 snapshot、token ledger、风险和建议动作。
- `cli/memory_commands.py`：给用户和开发者看 route/doctor 结果。
- `cli/memory_archive_commands.py`：给用户查看归档列表、搜索归档、生成恢复简报，并提供 `memory-fact-write` 写入用户确认的 compact 补全事实。
- `cli/memory_resume_compact_rendering.py`：承接 `memory-resume --from-compact` 的 handoff、continue packet、completion prompt 和推荐路径输出，避免 archive CLI 编排继续膨胀。
- `cli/memory_compact_commands.py`：把 compact plan 暴露为 `memory-compact --dry-run`；显式 `--apply` 时只生成 compact context、metadata、apply bundle、restore refs、work state snapshot、ledger 和 self-check，不做 destructive rewrite。

## 数据流

1. 用户对话或命令触发记忆写入，基础事实先落到 JSONL。
2. LocalStore 可以为旧 memory 补建索引，让搜索和 timeline 能看到它。
3. 当新任务需要规则时，memory routing 根据 query 匹配 route index。
4. 匹配到的 authority path 会被安全读取成上下文片段。
5. token 预算逼近阈值时，run 主链路先写 `memory_archive/snapshots/*.json` 权威快照，再做组合压缩；快照内容必须带上 routed memory context 和 auto resume context，保证 compact 后恢复能回到同一批 authority path 和恢复线索。
6. 长任务和普通保存路径都会继续写 raw event / hook snapshot，方便恢复和审计。
7. raw event、hook snapshot 和权威快照写完后都会读回校验，确保恢复线索真实落盘。
8. subagent 保存时会同步 `tasks/<root_id>/` 的 `state.json`、`timeline.jsonl`、`summaries/current_summary.md` 和 legacy run adapter；旧 `subagents/<run_id>/` 仍是当前兼容事实源。
9. 同一保存流程会同步 `tasks/<root_id>/agents/<run_id>/` 的 agent run workspace skeleton，先写恢复和接管需要的最小 run 文件，不搬迁旧工单目录。
10. 同一保存流程会追加 `daily/YYYY-MM-DD/events.jsonl`，作为主代理按天查 task/run/event/artifact refs 的轻量索引。
11. 同一保存流程会写 task/run artifact manifest，并让 daily ledger refs 指向 manifest；需要正文时再读 workspace 边界内的 artifact 文件本身，越界路径只保留 blocked manifest 记录。
12. 同一保存流程会追加 run `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并让 run `checkpoint.json` 指向最新 compact refs；这不是删除上下文的 compact apply。
13. 同一保存流程会同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享任务局部 facts；其中 messages 追加，findings/evidence 按 id 合并，避免最后一次保存覆盖其他 sibling 事实。这仍然不是主 memory 写入。
14. `subagents-memory-gate` 默认只列出候选或写 `decisions.jsonl`；只有显式 `--export-memory` 才写主 JSONL memory，只有显式 `--export-skill` 才写 skill draft，`--retention-apply` 也只压缩 active queue，不删除审计日志。
15. 用户说“继续/恢复”时，resume context 可以按配置从 archive、LocalStore、daily ledger 和任务事实源生成恢复块；跨天时会同时扫描最近 raw/hook 文件。subagent 任务会先推荐 `reports/checkpoint.json`、`status_report.json`、`progress.md` 等 compact recovery artifacts，再推荐 `STATUS.md`、`HANDOFF.md` 和 `output.json`。`memory-resume --from-compact` 会从某次 compact apply 产物生成恢复块、consistency report 和 action guard。这只是恢复入口推荐，不代表把子代理内容写入主代理长期 memory。
16. doctor 命令检查配置、route index、hook/raw/snapshot 目录和层级一致性 warning。
17. runtime 工具循环遇到大工具输出时，会先写 metadata-only recovery snapshot，再把完整输出外置到 `memory_archive/artifacts/tool_outputs/`，并在 archive_tool_calls / raw tool event 中保存 preview/hash/path/size。
18. tool context reducer 会在下一轮 live prompt 注入前再次检查 archive record：如果输出已外置，只注入 preview、artifact path、hash、size 和 fail-safe checkpoint；完整正文必须通过 artifact 文件显式读取。
19. recovery snapshot 的工具调用 metadata 会保留 output hash、size 和 externalized 状态，方便接管代理知道大输出存在且需要读 artifact；snapshot 不保存完整工具输出正文。
20. `memory-compact --dry-run` 在真实压缩前只读扫描上述事实源，输出计划和风险，不修改文件。
21. `memory-compact --apply` 把同一 scope 的计划落成非破坏性 compact apply 记录：`compact_context`、metadata、apply bundle、restore refs、work state snapshot、post-compact self-check 和 apply ledger。当前成功状态为 `applied_non_destructive`，只建立恢复入口，不裁剪历史内容；如果 self-check 失败，状态会变成 `blocked_self_check_failed` 并写失败报告。
22. control-plane query 只读扫描 daily ledger、task/run refs、compact apply ledger 和 tool output index，给 compact/resume/debug 返回统一引用视图；正文核实仍必须回到 task/run workspace、artifact 文件或 raw archive。
23. `run` 收尾会基于 token ledger 触发默认 plan-only 的 auto compact cycle；达到 70% 以上时会给出 dry-run/apply/resume 命令建议和 `compact_auto` 停车状态。默认仍要求用户确认，不自动执行。
24. `run_memory_compact_auto_cycle()` 是自动 compact/resume 的第一层协调器：默认只生成 plan 和人工确认建议；显式 `allow_apply=true` 或配置 `memory_compact_auto_allow_apply=true` 时只做非破坏性 apply、auto resume 和 continue packet 检查，随后按 action guard 结果停下，仍然不自动执行工具。
25. compact apply 生成 `work_state_snapshot` 时，会只读 workspace 内的 task/run 事实源，例如 `subagents/<run_id>/ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`task.json`、`memory_archive/runtime_facts/<request_id>/task.json`、`memory_archive/runtime_facts/<session_id>/task.json` 和 `tasks/*/agents/<run_id>/`；读取到的验收、约束和最近测试会进入恢复基线，找不到仍标为 missing。没有权威 snapshot 文件的普通 `run --save` 场景，会从 `restore_refs` 指向的 hook/raw JSONL 回填 goal/next_step，但不会从模型回复里猜验收或测试状态。
26. `memory-resume --from-compact` 会把 work state、action guard、fail-safe checkpoint refs、completion prompt、推荐读取路径和下一步动作整理成 `compact_resume_handoff` 和 `compact_continue_packet`，上下文块中会分节展示目标、阶段、验收、约束、最近测试、必须读取、fail-safe checkpoints 和下一步动作。
27. compact 的 `allowed_to_continue=true` 只代表恢复上下文自检和 work_state guard 允许继续，不代表子代理业务验收通过；父级验收、测试执行、apply acceptance 和 rescue 仍由 parent acceptance controller / auto-policy 单独判断。
27. 当 compact resume 指定 `owner_type=subagent_run|subagent_session` 时，系统只读解析 task-local run workspace 和 legacy run adapter refs，给未来子代理会话 compact/resume 留稳定 owner 坐标；这一步不把子代理内容写入主代理长期 memory，也不自动执行工具。
28. Runtime memory 轻量索引记录使用 schema v2：顶层 `version=2`，旁边写 `schema.name/version/reserved_keys`，`reserved` 固定保留 `extensions`、`compat`、`future` 三槽；正式业务字段仍应显式命名，不能把 reserved 当成万能垃圾桶。
29. runtime memory 的跨模块写入入口先把 CLI/manager 参数收敛成 `*Request` / `*Options` bundle，再进入具体 service；这保证后续 memory gate、compact chain、artifact refs、shared workspace 继续扩展时，不影响既有调用方。

## 跨天恢复链路

```text
2026-04-29 raw event
  -> 记录用户当时的任务意图、request_id、run_id、task_id

2026-04-30 hook snapshot
  -> 记录压缩/交接后的恢复锚点、next_actions、task_refs、content_paths

LocalStore subagent_run
  -> 保存可搜索的任务索引，不作为最终事实，只帮助找到 run_id

LocalStore gateway_request
  -> 保存可搜索的 gateway 请求索引，不作为最终事实，只帮助找到 request_id

subagents/<run_id>/
  -> reports/checkpoint.json / status_report.json / progress.md 是 compact-first 恢复入口
  -> STATUS.md / WORK_LOG.md / HANDOFF.md / ACCEPTANCE.md / TEST_CHECKLIST.md 是最终核对事实源

gateway/requests/done/<request_id>.json 或 gateway/requests/failed/<request_id>.json
  -> gateway 请求的终态请求事实源；如果 LocalStore 里还是 processing 路径，resume 会尽量纠偏到这里

gateway/responses/<request_id>.json
  -> gateway 请求的响应事实源；通常要和 request JSON 一起读

memory-resume 或 run(auto resume)
  -> 输出 Recovery Brief，把推荐阅读路径和下一步动作带回父会话
```

## Runtime Memory 目标结构

长期目标以 `06-runtime-memory-requirements.md` 为准：

```text
主代理 = 长期记忆 + 每日事件账本 + 全局索引
任务 = task workspace + state + timeline + summaries + shared + artifacts
子代理 = task 内 agent run workspace + checkpoint + compact chain + findings
共享 = blackboard + messages + evidence packets + artifact refs
```

当前 Phase 0/1/2/3/4/5/6 已创建 task workspace 外壳、agent run workspace 外壳、daily event ledger、artifact manifest、checkpoint-first compact chain、shared 协作面和 run-local memory gate。全局 `memory-compact --apply` 已能生成 `memory_archive/compact_applies/` 下的非破坏性 apply context、apply bundle、restore refs、work state snapshot、self-check 和失败阻断报告，`memory-resume --from-compact` 已能从这些产物生成手动恢复上下文、consistency report、handoff、continue packet 和 action guard；action guard 在 auto 模式字段齐全时可以返回 `allow_automated_continue`，但仍标记不自动执行工具。`run` 已能在上下文风险达到阈值时触发默认 plan-only 的 auto compact cycle，并可在显式配置下做非破坏性 apply + auto resume + continue packet 停车。大工具输出已能进入 `memory_archive/artifacts/tool_outputs/`，control-plane query 已能统一查 daily/task-run/compact/tool-output refs，这几类轻量索引已统一到 schema v2/reserved 结构，但还不会删除、重写或裁剪历史内容，也不会自动继续执行工具。`tasks/<root_id>/agents/<run_id>/legacy_run_ref.json` 会继续指向旧 run 目录；`memory_gate/` 保存 review 候选、decision log、export log、retention report 和 verifier report。只有显式 export 命令才会写主代理长期记忆或生成 skill draft。

LocalStore / sqlite / 搜索索引只帮助定位事实源，不替代 task/run 目录里的权威文件。

日期窗口说明：`--since YYYY-MM-DD` 从当天 00:00 开始；`--until YYYY-MM-DD` 包含当天全天。这样用户按自然日期查跨天交接时，不会漏掉当天白天的 hook snapshot。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/memory_commands.py`，理解用户如何运行 `memory-route` 和 `memory-doctor`。
2. 再看 `agent_py_agent/agent/settings/memory.py`，学习配置如何设置默认值、warning 和安全回退。
3. 再看 `agent_py_agent/agent/memory_store/jsonl.py`，理解 JSONL 事实流水和 LocalStore 索引的区别。
4. 再看 `memory_routing/models.py`，认识 route、match、required/candidate path 和 read receipt 的数据形状。
5. 再看 `memory_routing/matcher.py`，理解关键词和别名如何命中规则。
6. 再看 `memory_routing/loader.py`、`validator.py` 和 `context.py`，理解人工索引怎么读入、怎么校验冲突和死链、authority 文件怎么安全注入。
7. 再看 `memory_archive/models.py`、`storage.py`、`runtime.py` 和 `snapshots.py`，理解归档保存什么、怎么写入、怎么验收、压缩前 hook 为什么必须先成功。
8. 再看 `memory_archive/query.py`、`resume_brief.py` 和 `resume_context.py`，理解“继续任务”时怎么找回线索。
9. 再看 `memory_archive/tokens.py` 和 `agent_core/runtime_mixin.py`，理解 session token 账本和压缩触发点。
10. 再看 `agent_py_agent/agent/memory_archive/task_workspace.py` 和相邻的 runtime sync 模块，理解 `*Request` bundle 如何把 task/run/artifact/checkpoint/shared refs 打包传递，同时保留旧 subagent work-order 兼容路径。
11. 再看 `agent_py_agent/agent/memory_archive/control_plane.py` 和 `agent_py_agent/tests/test_memory_control_plane.py`，理解 daily、task/run、compact apply 和 tool output index 如何先汇成轻量引用视图。
12. 再看 `agent_py_agent/agent/memory_archive/schema.py`，理解 v2 schema 和 reserved 三槽如何让未来字段扩展有明确位置。
13. 再看 `agent_py_agent/agent/memory_archive/memory_gate.py`、`memory_gate_retention.py`、`memory_gate_export.py`、`memory_gate_verifier.py` 和 `agent_py_agent/agent/subagents/manager_memory_gate.py`，理解 lesson/finding 为什么先进入 review queue，以及 reviewer decision 为什么仍然不等于正式提升。
14. 再看 `agent_py_agent/cli/_memory_gate.py`，理解 `subagents-memory-gate` 如何显式写回 decision、导出 approved 候选、生成 skill draft 和跑边界检查。
15. 再看 `agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources` 和 `test_memory_runtime.py::test_auto_resume_context_recovers_cross_day_handoff_task`，理解 subagent 跨天恢复如何从线索回到事实源。
16. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py`，理解真实 gateway 请求和 parent/subagent runner 结果如何通过跨天恢复回到文件事实源。
17. 最后看 `agent_py_agent/tests/test_memory_*.py` 和 `test_memory_first_loop.py`，用测试反推每一层必须保证的行为。

## 当前第一版索引 / 待补齐

本页先讲主结构和阅读路径。后续需要补真实 route index 样例、raw archive 样例、doctor 输出样例、LocalStore 命中样例，以及更长时间的真实跨午夜恢复链路图。
## 2026-05-06 structure update
- 中文说明：memory archive query 和 memory routing matcher 内部已经拆成过滤、payload 构造、gateway payload 收集、resume guidance 渲染、评分规格和 receipt 创建等小职责；公开输出保持稳定。
- Memory archive query logic now delegates filtering, task payload construction, gateway payload collection, and resume guidance rendering to focused helpers.
- Memory routing matcher internals separate scoring specs, score accumulation, and receipt creation while keeping public model outputs stable.

## 2026-05-07 bundle structure update
- 中文说明：memory archive query、resume brief、snapshot helper 和 routing read receipt 现在用显式 Params/Options 字段表达输入；这不改变权威来源模型，task/run/gateway 文件仍是事实源，LocalStore/archive 只是线索。
- `memory_archive/query/query_logic.py` now exposes explicit archive collection/filter fields, and higher-level query service code converts compatibility inputs before filtering.
- `resume_brief.py`, snapshot helpers, and memory route read receipts now use explicit Params/Options-style fields rather than var-keyword option bags.
- Memory remains a fact-source locator: bundle cleanup does not change the authority model where task/run/gateway files stay authoritative and LocalStore/archive records are clues.

## 2026-05-07 high-risk structure update
- 中文说明：task workspace 的 payload/JSON/JSONL IO 从编排入口拆出去，archive query、resume brief、memory gate review 和 routing matcher 也改用 request/params bundle；文件形状和事实源权威关系不变。
- `memory_archive/task_workspace_payloads.py` now owns task workspace state/timeline payload construction and JSON/JSONL helper IO.
- `memory_archive/task_workspace.py` remains the adapter/orchestration entry point and keeps the same public `EnsureSubagentTaskWorkspaceRequest` / `TaskWorkspacePaths` surface.
- `memory_archive/query/query_logic.py`, `resume_brief.py`, `memory_gate_review.py`, and `memory_routing/matcher.py` now use request/params bundles for collection, filtering, reviewer write-back, and read receipts without changing the fact-source authority model.

## 2026-05-07 annotation structure update
- 中文说明：结构文档把 memory 代码里的双层注释纳入同步要求。新增文件、服务、bundle 或 facade 方法时，要同时更新结构页和 in-code 注释。
- Module structure docs now treat the definition-level double-layer comments as part of the code architecture: `LLM:` records model-facing contract/caller/side-effect notes, and `函数用途:` / `类用途:` records beginner-readable purpose and edit guidance.
- New files, services, bundles, or facade methods must update both this structure page and the in-code comments at the same time.
- The global file tree in `CODEBASE_TREE.md` now includes a current architecture map for CLI, agent core, gateway, memory, log-analysis, subagent, tooling, and settings boundaries.

## 2026-05-08 compact resume fail-safe structure update
- `memory_archive/compact_resume_failsafe.py` owns metadata-only tool-output fail-safe checkpoint extraction from restore refs that point to hook JSONL files. The extractor records path, line number, snapshot id, source/status, request/run/task ids, output hash, output size, and next actions.
- `memory_archive/compact_resume.py` now calls that extractor before building recommended read paths, so the main resume orchestration file does not keep growing with JSONL scanning details.
- `memory_archive/compact_resume_handoff.py` now carries the same checkpoint refs into `compact_resume_handoff` and renders a `Fail Safe Checkpoints` section in the context block.
- The structure remains refs-only: `memory-resume` may inspect hook checkpoint metadata, but it does not read or inline externalized tool artifact bodies. Artifact bodies stay behind explicit artifact path reads.

## 2026-05-08 artifact explicit read structure update
- `memory_archive/artifact_reader.py` owns indexed tool-output artifact body reads. It treats `index.jsonl` as the authority, validates the registered path boundary, verifies sha256, and returns explicit slices.
- `cli/memory_artifact_commands.py` exposes `memory-artifact-read`, keeping failed reads metadata-only and successful reads clearly marked with `reads_artifact_body=true`.
- `tooling/artifact.py` exposes `read_artifact` to the model as the controlled runtime tool; ordinary workspace files still go through `read_file`.

## 2026-05-11 artifact copied-prefix recovery structure update
- `memory_archive/artifact_reader.py` 现在在精确 path/hash/call_id 匹配失败、且 ref 看起来像路径时，会尝试用唯一 artifact 文件名回到已登记 index 记录；这是为了修复模型复制路径前缀时把 workspace 根写错的真实 E2E 问题。
- 这个 fallback 仍然是 index-first：同名不唯一、未登记 artifact、目录越界或 sha256 不一致都会失败；成功读取仍只返回显式 slice，不把 artifact 正文塞进普通恢复上下文。

## 2026-05-08 artifact explicit read CI follow-up
- `artifact_reader.py` 的路径根、目录边界、path-like ref 判断和 sha256 helper 现在都有定义级用途说明，后续维护者能直接看到这些 helper 是任意文件读取防线的一部分。
- `__main__.py` 的 CLI 导入顺序已按 ruff 统一格式整理；结构和命令语义不变。

## 2026-05-08 compact/resume safety structure update
- `agent_core/_finalization_service.py` 的 auto compact apply 入口现在必须同时满足配置允许和当前 run `do_save=true`，把 `--no-save` 保持为硬持久化边界。
- `memory_archive/runtime_fact_source.py` 的显式段落解析遇到未知标题会重置 active bucket，避免后续实施说明被升级成 work-state facts。
- `memory_archive/compact_work_state_sources.py` 按字面 id 查找 task/run/runtime_fact 目录，`request_id` / `session_id` 中的 glob 元字符不再参与文件系统模式匹配。
- `memory_archive/compact_resume_completion.py` 渲染 suggested commands 时保留原 compact scope flags，用户复制命令不会意外跑无范围 `memory-compact --apply`。

## 2026-05-11 scoped artifact-ref structure update
- `memory_archive/tool_output_externalizer.py` 写 tool-output artifact 时会同时登记 `request_id`、`run_id`、`task_id` 和 `scoped_call_id`。`scoped_call_id` 的形态是当前 run/task/request scope 加短调用号，用来避免不同子代理、不同轮次都叫 `17-1` 时互相串线。
- `memory_archive/artifact_reader.py` 现在把 `index.jsonl` 当成带作用域的登记表：先按 path/hash/scoped_call_id/call_id 找候选，再优先选择当前 `run_id/task_id/request_id` 匹配的记录；没有 scope 的 legacy 调用只回退到最新候选，不再读第一条旧记录。
- `agent_core/_tool_loop_service.py` 会把当前 subagent run scope 注入 `read_artifact` 参数和工具输出归档记录；`agent_core/subagent_run_flow.py` 也会把 subagent run/task id 传进 `agent.run()`，让工具链能从调用栈拿到稳定 scope。
- `agent_core/tool_context_reducer.py` 的 live prompt 只给模型 scoped 读取线索：artifact path、hash、`output_scoped_call_id`、run/task/request flags 和 preview；完整 artifact 正文仍必须通过 `read_artifact` 显式读取。
- 这条结构规则用于修复真实 E2E 的 prompt/路径误传问题：模型看到的引用不能只是人类可读短号，必须能绑定到当前任务、当前 run、当前 workspace。

## 2026-05-13 artifact read mode and budget structure update
- `memory_archive/artifact_reader.py` 继续是唯一 tool-output artifact 正文读取入口；现在 `ReadToolOutputArtifactRequest` 增加 `mode` 和 `query` 字段，支持 `slice/head/tail/search` 四种窄读方式。所有模式仍先查 `tool_outputs/index.jsonl`、校验目录边界和 sha256。
- `memory_archive/artifact_read_modes.py` 承接正文 shaping：slice/head/tail/search 的 offset、截断、匹配行和附加元数据都在这里处理，避免 artifact index 读取层继续增长。
- `memory_archive/artifact_reader.py` 还提供 `estimate_tool_output_artifact_size()`，只读 index 的 `size_bytes`，不打开正文，用于 `max_chars=0` 这类无界读取的预算预判。
- `tooling/artifact_read_budget.py` 是 read_artifact 的单 run 正文读取预算器；它记录 `run_id -> [(timestamp, chars)]`，只限制带 run scope 的子代理读取，不限制普通主代理聊天。
- `tooling/artifact.py` 把工具参数转成 `ReadToolOutputArtifactRequest` bundle，再先做预算 preflight，成功读取后按实际 `content_chars` 计费。这样预算逻辑不散落到 reader 或 tool loop 里。
- `tooling/filesystem_artifact_guard.py` 负责普通 `read_file` 误读 artifact 包装文件的恢复提示；提示会根据 registry 注入的 `allowed_tools` 判断当前上下文是否有 `read_artifact`，没有时要求上报 `capability_request`。
