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

## 当前实现对齐状态

- 已有 `memory_archive` raw/hook/snapshot、token ledger 和 resume brief 雏形。
- 已有 `SubAgentTask` 的 parent/root/depth、status report、checkpoint artifacts、evidence packets 和 findings。
- 已有 LocalStore 作为索引层雏形。
- 已新增子代理 `SKILL_SPARKS.md` 候选文件。
- 已新增 `memory_archive/task_workspace.py`，先创建文件系统版 task workspace 和 legacy run adapter，保持旧 subagent 路径兼容。

后续主要差距：

- 旧 subagent workspace 尚未迁移到 `tasks/<task_id>/agents/<run_id>/`；当前只有 `legacy_run_ref.json` adapter。
- task workspace 已有第一版 `task.yaml`、`state.json`、`timeline.jsonl`，但 `agent.yaml`、run-level `state.json/timeline.jsonl` 和 `compactions/` 链尚未落地。
- shared blackboard/messages/locks 仍未系统化。
- memory item 写入门禁、retention 清理和 skill spark 提升链路还未落地。
