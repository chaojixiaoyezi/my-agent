# Memory Runtime 开发要求

## 目标边界

Memory 不是“把聊天历史塞回模型”，而是运行时档案系统。它负责保存、索引和恢复事实；Compact 负责何时压缩、如何生成摘要、如何组装模型上下文；Subagent 负责在任务工作区内产出状态、证据、artifact 和报告。

后续 memory 开发必须遵守这条边界：

- 主代理拥有长期记忆、每日事件账本、任务索引和全局检索入口。
- 每个用户任务拥有独立 task workspace、task state、timeline、summaries 和 shared 区。
- 每个子代理是 task workspace 里的一个 agent run，不默认拥有全局长期记忆。
- 子代理输出、scratch、工具产物、checkpoint 和 compact summary 留在自己的 run workspace。
- 主代理 memory 只记录任务/run 索引、事件摘要、artifact 引用和恢复入口，不吸收子代理完整上下文。
- 工具大输出必须 artifact 化；事件账本只保存摘要、hash、路径、状态、耗时和可检索字段。
- 长期 memory item 必须带 scope、source、evidence refs、confidence、status 和 TTL/retention 信息。

## 文件系统版 Phase 0

Phase 0 先跑通文件系统版，不引入新依赖，不强推数据库迁移。

目标形态：

```text
.agent/
  main/
    memory/
    daily/YYYY-MM-DD/events.jsonl
  tasks/<task_id>/
    task.yaml
    state.json
    timeline.jsonl
    summaries/
    agents/<run_id>/
    shared/
    artifacts/
  indexes/
```

当前 repo 仍有旧路径和兼容结构，落地时先做 adapter，不一次性搬迁历史目录。

## Task Workspace 要求

Task workspace 是任务事实源。后续开发新增任务级能力时，应优先落到 task workspace，而不是主 memory 文件。

必须支持：

- `task.yaml`：任务目标、范围、约束、预算、retention。
- `state.json`：进度、运行中/阻塞/完成计数、top findings、blockers、latest rollup。
- `timeline.jsonl`：任务事件流水，append-only。
- `shared/blackboard.md`：任务公共白板，只放结构化成果，不放完整上下文。
- `shared/findings.jsonl`、`shared/messages.jsonl`、`shared/evidence_packets/`：子代理协作和证据链。
- `artifacts/`：工具输出、日志样本、报告、临时中间件。

当前 Phase 0 已先落地文件系统 skeleton：subagent 保存时会在 manager workspace 下创建 `tasks/<root_id>/`，写入 `task.yaml`、`state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/blackboard.md`、`shared/messages.jsonl`、`shared/findings.jsonl`、`artifacts/` 和 `agents/<run_id>/legacy_run_ref.json`。这一步只做 adapter，旧 run 目录仍保留原样。

## Agent Run Workspace 要求

每个子代理 run 有自己的目录。目录名可以方便阅读，但父子关系不能靠名字判断，必须靠字段：

- `run_id`
- `root_task_id`
- `parent_run_id`
- `replaces_run_id`
- `lineage`
- `depth`

每个 run workspace 至少应能表达：

- `agent.yaml`：身份、父级、权限、预算、可见性。
- `state.json`：当前状态。
- `task.md`：本 run 接到的任务。
- `timeline.jsonl`：本 run 自己的事件流水。
- `checkpoint.json`：断点恢复状态。
- `summary.md`：当前工作摘要。
- `final_report.md`：完成后的交付报告。
- `compactions/`：compact ledger 和 compact snapshots。
- `findings.jsonl`：本 run 产生的结构化发现。
- `SKILL_SPARKS.md`：本 run 的 skill 学习候选，只是候选，不自动写入长期记忆或正式 skill。

当前 Phase 1 已先落地 agent run workspace skeleton：subagent 保存时会在 `tasks/<root_id>/agents/<run_id>/` 写 `agent.yaml`、run `state.json`、`task.md`、run `timeline.jsonl`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl`，并创建 `inbox/`、`outbox/`、`artifacts/`、`compactions/`。旧 work-order 目录仍继续读写，run workspace 先作为恢复、接管和后续 compact 的兼容面。

## Daily Event Ledger 要求

Daily event ledger 是主代理按天查 task/run 线索的轻量入口，不是任务事实源。

当前 Phase 2 已先落地 `daily/YYYY-MM-DD/events.jsonl`：subagent 保存时追加 `subagent_task_saved` 事件，包含 task/run id、状态、进度、duration、摘要、artifact/evidence refs、workspace 引用和检索字段。ledger 不保存完整用户目标、工具输出或子代理上下文；需要核实时必须回到 task/run workspace 或旧 work-order 文件。

Control-plane query 第一片已落地：`query_memory_control_plane()` 会按 date/task/run/event scope 只读汇总 daily events、task/run refs、compact apply ledger 和 tool output index，返回 counts、summary、refs、preview/hash/path 等轻量信息。它不读取 artifact 正文，不写任何 workspace 文件，也不替代 task/run 目录里的权威事实。

## Artifact 外置要求

工具大输出、日志样本、报告和中间产物必须留在 artifact 文件中。ledger、state、summary、raw tool event 和 memory item 只能保存摘要、hash、路径和状态。

当前 runtime 工具输出外置第一片已落地：工具循环记录归档时，超过阈值的大输出会写入 `memory_archive/artifacts/tool_outputs/<tool>-<call>-<hash>.json`，并追加 `index.jsonl`；`archive_tool_calls` 和 raw tool event 只保存 `output_preview`、`output_hash`、`output_path`、`output_size_bytes` 和 `output_externalized`。当前模型轮的 `tool_context` 仍保留完整工具结果，所以这一步不改变工具执行行为，只改变归档和 compact 输入形态。

当前 Phase 3 已先落地 `artifacts/manifest.jsonl`：subagent 保存时会在 task workspace 和 agent run workspace 各写一份 manifest，把 `artifact_refs` 规范化为 ref、resolved path、exists、size、sha256、summary、kind、source 和 `resolution_status`。manifest 只允许读取 legacy task dir、task workspace、agent run workspace 内的文件；越界绝对路径或 `..` 逃逸路径只登记 blocked 状态，不复制 artifact 正文，也不计算 hash。

## Skill Sparks 要求

`SKILL_SPARKS.md` 是子代理目录内的任务局部经验火花，用于后续 skill 学习流程的输入。

约束：

- 默认不进入主代理长期 memory。
- 默认不进入正式 skill。
- 必须保留在 task/run workspace 中，跟随任务 retention 策略。
- 后续提升为 skill 时必须经过 review/gate，并引用 evidence、artifact 或成功/失败样本。
- 反例、适用范围和触发条件必须和步骤一起记录，避免把偶然经验写成通用规则。

## Compact / Checkpoint 要求

每个 agent run 都可以 compact，多次 compact 必须形成链条。

要求：

- compact 前先写 checkpoint。
- compact 只生成继续工作的压缩上下文，不删除原始 timeline 和 artifact。
- 每次 compact 写 `compactions/compaction_ledger.jsonl`。
- 每次 compact 保存 markdown summary 和 metadata JSON。
- compact 后更新 `summary.md` 和 `checkpoint.json`。
- compact 失败不能删除旧上下文视图，不能假装成功。

当前 Phase 4 已先落地 checkpoint-first compact chain：subagent 保存时会在 run `compactions/compaction_ledger.jsonl` 追加 `checkpoint_snapshot` 事件，并写每次 snapshot 的 markdown summary 和 metadata JSON；run `checkpoint.json` 会记录最新 ledger/summary/metadata/artifact manifest/timeline refs。当前状态明确标记为 `checkpoint_only`，不会删除 timeline、artifact 或旧 work-order 文件，也不等同于正式 compact apply。

全局 `memory-compact --apply` 的第二片已落地为非破坏性 apply：它会基于 dry-run scope 写 `memory_archive/compact_applies/<event_id>.md`、metadata JSON、apply bundle、restore refs、post-compact self-check JSON 和 append-only ledger，成功状态为 `applied_non_destructive`。`restore_refs.json` 会列出原始 archive/snapshot/token ledger 引用，`apply_bundle.json` 会给恢复流程提供入口和核验步骤；如果 self-check 失败，会额外写 `self_check_failed.json` 并把 metadata/ledger 标记为 `blocked_self_check_failed`。这一步只建立恢复入口、自检、失败阻断和审计记录，不删除、不重写、不裁剪 raw/hook/snapshot/token/task/run 文件。后续如果要做 destructive rewrite，必须另加备份、restore、self-check failed rollback 和更高等级验收。

`memory-resume --from-compact` 会生成 `compact_continue_packet`。它不是工具执行器，而是恢复后继续工作的统一契约：包含目标、阶段、下一步、验收、约束、最近测试、推荐读取路径、action guard、subagent owner refs 和 `automatic_tool_execution=none`。主 agent 自动链路拿到 `ready_to_continue=true` 后，可以把这个 packet 注入下一轮 prompt 并继续一次；这只代表 compact 恢复状态一致，不代表子代理业务验收通过。测试执行、acceptance apply 和 rescue 仍必须走 closeout 链路。

`memory_compact_auto_allow_apply` 是自动 compact apply 的显式配置口子，默认开启。开启后 `SimpleAgent.run()` 允许非破坏性 apply、auto resume、continue packet 和 guard 检查；guard 放行时会受控续跑一次，并在续跑轮重新加载 `AGENTS.md`、`SOUL.md`、`USER.md`、`memory.md`。续跑轮不会再次触发 compact，避免循环；不自动 apply acceptance。

## Shared Workspace 要求

Shared workspace 是同一 task 下 sibling 子代理共享任务局部事实的地方，不是主代理长期 memory。

当前 Phase 5 已先落地 `shared/` 结构化同步：subagent 保存时会更新 `blackboard.md` 的状态 rollup，向 `messages.jsonl` 追加 status update，把 `findings` 按 id 合并进 `findings.jsonl`，并把 `evidence_packets` 外置成 `shared/evidence_packets/<id>.json` 加按 id 合并的 `index.jsonl`。这些文件只保存 claim、refs、confidence、status 等结构化事实；不保存完整聊天历史，不让最后一个 sibling 覆盖其他 sibling 的发现，也不自动提升为长期 memory item。

## 接管要求

新 agent 接管旧 run 时，不直接写旧目录。旧目录只读，新 run 新建目录，并记录：

- `replaces_run_id`
- `input_checkpoint`
- `input_summary`
- 接管原因
- 继承的 blockers / findings / artifact refs

## 查询和恢复要求

恢复流程应是：

1. 主代理从每日账本、索引或 LocalStore 找到 task/run。
2. 读取 task state 和 run checkpoint/summary。
3. 需要核实时再读 timeline、findings、evidence packets 和 artifact。
4. 只有经过 gate 的 finding/memory candidate 才能进入长期 memory。

`memory-resume` 应被理解为恢复入口生成器，而不是把子代理内容写入主 memory。

## Schema v2 / Reserved 字段要求

Runtime memory 的轻量索引记录必须能长期扩展，但不能把字段随手塞进各处。当前 v2 固定以下约定：

- 顶层 `version` 必须写 `2`。
- 顶层 `schema` 必须写 `name`、`version` 和 `reserved_keys`。
- 顶层 `reserved` 必须写 `schema_name`、`schema_version`、`extensions`、`compat`、`future`。
- `extensions` 只放实验性、可丢弃、可迁移字段。
- `compat` 只放旧 reader/writer 兼容需要的桥接字段。
- `future` 只放已经预留但还没有正式语义的字段。
- 稳定业务字段必须显式命名，不允许长期藏在 `reserved` 里。
- 读路径必须容忍旧 `version=1` 记录；写路径从 v2 开始统一输出 schema/reserved。

当前已固化到 v2 的记录族：

- `daily_ledger_event`
- `control_plane_query`
- `control_plane_task_run_ref`
- `compact_apply`
- `compact_apply_bundle`
- `compact_apply_ledger`
- `compact_apply_restore_refs`
- `compact_apply_self_check`
- `compact_apply_self_check_failure`
- `compact_work_state_snapshot`
- `compact_resume`
- `compact_resume_consistency_report`
- `compact_resume_handoff`
- `compact_continue_packet`
- `compact_action_guard`
- `compact_suggestion`
- `compact_auto_cycle`
- `compact_subagent_owner_refs`
- `tool_output_archive_record`
- `tool_output_artifact`
- `tool_output_index`

## Bundle 接口规范

后续 runtime memory、subagent、compact、gateway 和 CLI 业务接口统一按 bundle 风格演进。目标不是把每个小 helper 都包装起来，而是让可扩展的业务边界稳定、可审计、好兼容。

命名规则：

- product service / domain / repository / gateway / subagent / memory / tool / skill / delegation 接口不得把散乱 `*args` / `**kwargs` 作为业务参数入口。
- 业务入口的复杂输入使用 `XxxRequest`。如果只是配置开关、筛选条件或构建参数，可以用 `XxxOptions` / `XxxParams` / `XxxContext` / `XxxCommand` / `XxxQuery`，但同一领域内必须保持一致。
- 复杂输出使用 `XxxResult` / 已存在的 `XxxRecord` / `XxxReport`，不要返回松散 tuple。
- CLI 层可以接收 `argparse args`，但进入 manager/service 前必须转换成 Request/Options bundle，不能把 `args` 继续向业务层深传。
- manager/public service 可以短期保留旧的显式 keyword 字段作为兼容 wrapper，但内部应立即构造 bundle，再调用核心实现；业务代码不得再新增函数级 `**kwargs` 服务接口。
- 新增字段优先加到 bundle dataclass 中，不继续拉长函数签名。

适用范围：

- 必须 bundle：跨模块调用、会写文件/状态、会调用模型/工具、未来可能扩展 policy/gate/retention/verifier 的接口。
- 可以不 bundle：纯内部小 helper、单一值转换、局部渲染函数、只有一两个稳定参数且无状态副作用的函数。
- 复杂 helper 如果参数超过 3 个业务含义字段，优先抽成 dataclass bundle。

兼容要求：

- bundle 改造不得破坏旧 CLI 和已有测试；旧入口保留时应作为薄 wrapper。
- 旧入口如需兼容，只能列出显式字段并转换到 bundle；除 retry/decorator/adapter/wrapper 这类必须透明转发任意 callable 参数的底层通用工具外，不允许把 var-keyword 当成兼容层。
- 测试代码中的 mock、fixture、helper 可以保留 `**kwargs`，但不得作为产品接口模式参考。
- 所有 product-code `*args` / `**kwargs` 例外必须登记在架构守卫 allowlist 中，并写明文件、函数和保留原因；新增例外需要先复审再合入。
- bundle dataclass 字段必须有明确默认值或显式必填语义。
- 写入型 Request 必须能表达 `apply/dry_run`、reviewer、note、now/test clock、目标路径或 scope。
- Result 必须带可审计 refs，例如写入文件路径、export log、report path、record id。
- 不允许为了 bundle 化引入新依赖，不允许把 `dict[str, object]` 当作长期替代 Request。

## 当前实现对齐状态

- 已有 `memory_archive` raw/hook/snapshot、token ledger 和 resume brief 雏形。
- 已有 `SubAgentTask` 的 parent/root/depth、status report、checkpoint artifacts、evidence packets 和 findings。
- 已有 LocalStore 作为索引层雏形。
- 已新增子代理 `SKILL_SPARKS.md` 候选文件。
- 已新增 `memory_archive/task_workspace.py`，先创建文件系统版 task workspace 和 legacy run adapter，保持旧 subagent 路径兼容。
- 已新增 `memory_archive/agent_run_workspace.py`，先创建 task-local agent run workspace skeleton，保持旧 subagent work-order 路径兼容。
- 已新增 `memory_archive/daily_ledger.py`，先创建每日事件 ledger，用摘要和 refs 索引 task/run，不吸收子代理完整上下文。
- 已新增 `memory_archive/artifact_registry.py`，先创建 task/run artifact manifests，用 summary/hash/path 规范 artifact refs，不复制大输出正文。
- 已新增 `memory_archive/compact_chain.py`，先创建 run-local checkpoint snapshot ledger，用 append-only summary/metadata 串起 compact 恢复链，不删除原始上下文。
- 已新增 `memory_archive/shared_workspace.py`，先创建 task-local shared blackboard/messages/findings/evidence packet 同步面，不写主代理长期记忆。
- 已新增 `memory_archive/memory_gate.py`，先创建 run-local memory/skill candidate gate：`candidates.jsonl`、`review_queue.jsonl`、`skill_spark_gate.json` 只记录候选、证据、适用范围和 review 要求，默认 `not_promoted`。
- 已新增 `subagents-memory-gate` 显式 review decision 写回：`decisions.jsonl` 记录 reviewer、decision、note 和 `auto_promote=false`；approve 只改变 gate 状态，不执行长期 memory/skill 导出。
- 已新增 Phase 6 显式收口链：retention 只压缩 active review queue 并保留审计；`--export-memory` 只导出 `approve_memory` 候选；`--export-skill` 只生成 draft；`--verify` 写边界检查报告，确认没有自动提升。
- 已新增 `memory_archive/control_plane.py`，先提供统一只读查询入口，把 daily ledger、task/run refs、compact apply ledger 和 tool output index 合成同一 scope 的引用视图，供 compact/resume/debug 继续使用。
- 已新增 `memory_archive/schema.py`，先把 daily ledger、control-plane task/run refs、compact apply 和 tool output index 的写入记录统一到 schema v2 / reserved 三槽。
- 已扩展 `memory_archive/compact_apply.py` 手动 apply 完整化第一片：非破坏性 apply 会写稳定 `apply_id/plan_id`、apply bundle、restore refs、work state snapshot、self-check 和 self-check failed 报告；失败时只阻断状态并保留审计，不回滚、不删除、不改写原始事实源。
- 已新增 `memory_archive/compact_resume.py` 手动 resume 第一片：`memory-resume --from-compact` 只读恢复 compact apply 产物，输出 context block、recommended reads、next actions 和 consistency report；预留 `owner_type/owner_id` 给未来子代理会话压缩，但当前不自动执行工具、不改写 subagent 文件。
- 已新增 `memory_archive/compact_resume_handoff.py` Resume 交接包第一片：compact resume 会额外输出 `compact_resume_handoff`，把目标、阶段、下一步、验收条件、约束、最近测试、推荐读取路径和 action guard 状态整理成稳定结构，并同步渲染进 context block。
- 已新增 `memory_archive/compact_action_guard.py` 自动 compact/resume 安全第一片：compact resume 会输出 action guard；manual 模式要求人工确认，auto 模式缺字段或 refs/self-check 异常时阻断，字段齐全时返回 `allow_automated_continue` / `allowed_to_continue=true`，但仍显式 `automatic_tool_execution=none`，不直接执行工具。
- 已新增 `memory_archive/compact_subagent_owner.py` 子代理 owner refs 第一片：`subagent_run` / `subagent_session` compact resume 会只读解析 task-local run workspace、legacy adapter refs 和已存在的 `compactions/session/latest_continue_packet.json`，输出 `memory_scope=task_local`、`writes_main_memory=false` 和 `automatic_tool_execution=none`；父级可见 `continue_packet_ready` 只表示有恢复包可读，不代表自动执行工具或写主 memory。
- 已新增 `agent_core/subagent_compact_continuation.py` 子代理接续 prompt 第一片：runner 只从 `context_bundle.workspace_refs` 指向的 task-local run workspace 读取 bounded checkpoint/summary/task/findings/latest continue packet 摘要，帮助子代理压缩后继续原任务；它不读取主代理 home 关键文件，也不把子代理经验自动提升到长期 memory。
- 已新增 `subagents/services/compact_continue_packet.py` 子代理闭环写入第一片：每次保存子代理任务时自动生成 task-local `compactions/session/latest_continue_packet.json` 和去重后的 `session_compact_ledger.jsonl`，让父级后续重新 dispatch 同一 run 时能从 packet/checkpoint/summary/progress 接续，而不是依赖主代理长上下文记住子代理细节。
- 已新增 `subagents/services/subagent_session_compact.py` 子代理本地 compact package 第一片：runner compact 信号只写到当前 run `compactions/` 下的 metadata/summary/restore refs，并挂回 `latest_continue_packet.json`；不写主 memory、不自动执行工具。
- 已新增 `memory_archive/compact_resume_completion.py` 半自动补全提示第一片：缺 work_state 字段时返回 `completion_prompt`，展示缺失字段、标签和补全模板；它不自动写 runtime facts，也不把假设变事实。
- 已新增 `memory_archive/compact_suggest.py` 半自动提示第一片：`run` 收尾会根据 token ledger 和上下文窗口返回 compact suggestion 字段，CLI 只打印建议命令，不自动 apply、不自动 resume。
- 已新增 `memory_archive/compact_auto.py` 自动 compact/resume 协调第一片，并已接入 `SimpleAgent.run()` 收尾的默认 auto-apply 分支：结果和 CLI 会显示 `compact_auto` 的状态、下一步和工具执行状态；显式允许 apply 时也只做非破坏性 apply、auto resume 和 action guard 检查，随后停住，不执行工具、不继续改代码。
- 已新增 `memory_archive/compact_work_state_sources.py` Work State 字段来源第一片：compact apply 会只读 workspace 内 task/run 事实源，把 acceptance、constraints、latest_tests 和 read_files 写入 `work_state_snapshot`；当前支持旧 `subagents/<run_id>/`、新 `tasks/*/agents/<run_id>/`、`ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md` 和 `task.json`。
- 已扩展真实 run Work State 回填：没有 `memory_archive/snapshots/*.json` 权威 snapshot 时，compact apply 会从本次 `restore_refs` 指向的 hook/raw JSONL 回填 goal/next_step；验收、约束和最近测试仍必须来自明确 task/run 事实源，缺失时 auto resume 继续阻断。
- 已新增 `memory_archive/runtime_fact_source.py` 运行时事实源第一片：真实 `run --save` 会写 `memory_archive/runtime_facts/<request_id>/task.json`，把显式验收、约束、测试条目暴露给 compact work_state；没有明确标签时不会伪造字段，auto resume 仍按 missing 阻断。
- 已新增 `memory-fact-write` 手动补全事实入口：只写用户显式传入的 acceptance/constraints/latest_tests 到 `runtime_facts/<fact_id>/task.json`，用于把 `completion_prompt` 的人工确认结果接回下一次 compact apply；不得从助手回复或普通描述中推断这些字段。
- 已完成 bundle-first 收敛：runtime/subagent/gateway/log-analysis/memory-archive/audit/local-storage/backends 的服务入口已改为 Request/Options/Params 或显式 keyword -> bundle adapter；架构护栏会扫描业务代码中的函数级 var-positional / var-keyword，当前例外仅限透明转发、协议 override、兼容 adapter 和局部字段选择 helper。

后续主要差距：

- 旧 subagent workspace 尚未迁移到 `tasks/<task_id>/agents/<run_id>/`；当前 agent run workspace 是 skeleton + legacy adapter，不是完整替代。
- task workspace 已有第一版 `task.yaml`、`state.json`、`timeline.jsonl`，run workspace 已有第一版 `agent.yaml`、run-level `state.json/timeline.jsonl` 和 checkpoint-first compact ledger/snapshot 链；全局 `memory-compact --apply` 已有非破坏性 apply、restore refs、apply bundle、work state snapshot、post-compact self-check 和失败阻断，`memory-resume --from-compact` 已能只读生成手动恢复上下文；run-local destructive compact apply 仍未接入。
- 半自动 compact 提示已接入主代理 `run` 返回值；自动 compact/resume 已能在主代理 guard 放行后续跑一次。子代理侧已有 task-local refs、自动 latest continue packet、runner prompt 接续第一版；完整无人值守接管和失败后自动选择新 leader 仍是后续工作。
- daily ledger 已有 append-only 文件入口和 artifact manifest refs，并已接入 control-plane 只读查询；resume 查询优先级还需要下一步显式改造，run-local gate retention 已有保守 active queue 清理。
- artifact manifests 已能规范已有 `artifact_refs`，runtime 大工具输出已能外置到 `memory_archive/artifacts/tool_outputs/` 并追加 index，control-plane 已能统一查询全局 tool-output index；但 task/run artifact manifest 与全局 artifact index 的 content-addressed 去重存储还没做。
- schema v2 已覆盖当前新写的轻量索引记录，但旧 raw/hook/archive 历史记录仍保持原 schema，后续要做迁移只能通过 reader 兼容或显式 migration，不允许原地重写历史事实源。
- shared blackboard/messages/findings/evidence packets 已有最小同步面，但 locks/handoffs 和 sibling 消息协议仍未系统化。
- memory item 写入门禁 / skill spark 提升链已能记录候选、review decision、retention、长期 memory 显式导出、skill draft 显式导出和 verifier 报告；正式 skill 安装仍未实现，后续也必须保持人工确认。
