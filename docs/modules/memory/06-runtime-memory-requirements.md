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

## Artifact 外置要求

工具大输出、日志样本、报告和中间产物必须留在 artifact 文件中。ledger、state、summary 和 memory item 只能保存摘要、hash、路径和状态。

当前 Phase 3 已先落地 `artifacts/manifest.jsonl`：subagent 保存时会在 task workspace 和 agent run workspace 各写一份 manifest，把 `artifact_refs` 规范化为 ref、resolved path、exists、size、sha256、summary、kind 和 source。manifest 不复制 artifact 正文；正文仍由原 artifact 文件承担。

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

## Shared Workspace 要求

Shared workspace 是同一 task 下 sibling 子代理共享任务局部事实的地方，不是主代理长期 memory。

当前 Phase 5 已先落地 `shared/` 结构化同步：subagent 保存时会更新 `blackboard.md` 的状态 rollup，向 `messages.jsonl` 追加 status update，把 `findings` 写入 `findings.jsonl`，并把 `evidence_packets` 外置成 `shared/evidence_packets/<id>.json` 加 `index.jsonl`。这些文件只保存 claim、refs、confidence、status 等结构化事实；不保存完整聊天历史，也不自动提升为长期 memory item。

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
- 所有 product-code 例外必须登记在架构守卫 allowlist 中，并写明文件、函数和保留原因；新增例外需要先复审再合入。
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
- 已完成 bundle-first 收敛：runtime/subagent/gateway/log-analysis/memory-archive/audit/local-storage/backends 的服务入口已改为 Request/Options/Params 或显式 keyword -> bundle adapter；架构护栏会扫描业务代码中的函数级 var-keyword，当前只允许 `concurrency/retry.py` 的透明装饰器转发例外。

后续主要差距：

- 旧 subagent workspace 尚未迁移到 `tasks/<task_id>/agents/<run_id>/`；当前 agent run workspace 是 skeleton + legacy adapter，不是完整替代。
- task workspace 已有第一版 `task.yaml`、`state.json`、`timeline.jsonl`，run workspace 已有第一版 `agent.yaml`、run-level `state.json/timeline.jsonl` 和 checkpoint-first compact ledger/snapshot 链，但还没有接入真实 compact apply 和 post-compact self check。
- daily ledger 已有 append-only 文件入口和 artifact manifest refs，但还没接入 resume 查询优先级；run-local gate retention 已有保守 active queue 清理。
- artifact manifests 已能规范已有 `artifact_refs`，但还没自动搬运/截断大工具输出，也还没做 content-addressed artifact 存储。
- shared blackboard/messages/findings/evidence packets 已有最小同步面，但 locks/handoffs 和 sibling 消息协议仍未系统化。
- memory item 写入门禁 / skill spark 提升链已能记录候选、review decision、retention、长期 memory 显式导出、skill draft 显式导出和 verifier 报告；正式 skill 安装仍未实现，后续也必须保持人工确认。
