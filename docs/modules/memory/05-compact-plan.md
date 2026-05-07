# Memory Compact：后续计划和开发规格

## 定位

`memory compact` 不是一个简单的“摘要器”，而是一套上下文状态迁移系统。它要在上下文变长之前，把大工具输出、任务状态、关键规则、失败尝试、测试状态和下一步动作迁移到可审计、可恢复、可重建的文件事实源里。

目标不是存更多文本，而是让长任务在压缩、跨天、重启、父子代理切换后仍能稳定继续。

## 已有基础

当前 memory 模块已经具备这些底座：

- `memory_archive_level` 已接入 raw archive，不同等级控制原始记录保留粒度。
- `SimpleAgent.run()` 已接入 pre-compression hook，token 超阈值时先写权威 compression snapshot。
- `memory_archive/snapshots/*.json` 已作为严格恢复锚点。
- `memory_archive/tokens/<session>.json` 已记录每轮 token 和累计 token。
- `memory-archive-list` / `memory-archive-search` 已能查看和搜索 raw/hook 线索。
- `memory-resume` 已能从 archive、LocalStore、subagent task fact source 和 gateway request/response 生成恢复简报。
- `memory-compact --dry-run` 第一版已接入，只读扫描 raw/hook、权威 snapshot 和 token ledger。
- `memory-compact --apply` 第二片已接入为非破坏性 apply：生成 compact context、metadata、apply bundle、restore refs、ledger、self-check 和失败阻断报告，不删除、不重写、不裁剪原始事实源。
- runtime 工具输出外置第一片已接入：大工具输出会写入 `memory_archive/artifacts/tool_outputs/`，compact 相关记录只读 preview/hash/path/size。
- `local-rebuild` 已能从 memory/gateway/subagent 文件事实源重建 LocalStore。

## 核心差距

当前已有“压缩前快照”“恢复线索”和第一版 dry-run，但还缺完整 compact 产品链路：

- `memory-compact` 目前支持只读 dry-run 和非破坏性 apply；destructive rewrite / backup / restore 仍未接入。
- dry-run 预演报告已有基础统计，还没有生成可复现 `plan_id` 文件。
- 没有统一 artifact 层处理大工具输出。
- 没有专门面向 compact 的 `TASK_STATE` / structured handoff schema。
- post-compact self check 已有第一版；还需要继续扩展到更完整的目标、约束、测试状态和 artifact 完整性检查。
- 没有聊天内 `/context`、`/compact`、`/artifacts` 这类交互命令。
- `compact / rebuild / backup` 还没有形成长期维护闭环。

## 功能原则

1. 禁止无 snapshot 的 truncation。
2. 摘要不是事实源，只能辅助下一轮推理。
3. task/gateway/subagent 文件事实源优先于 archive/local 摘要。
4. 大工具输出不能直接塞进模型上下文，应先 artifact 化。
5. tool call 和 tool result 必须成对保留或成对摘要，不能切断。
6. compact 失败、空摘要、非法摘要、自检失败都必须 abort 或 retry，不能继续丢中间上下文。
7. 用户明确 `--no-save` 时不能偷偷写 raw archive。
8. 任何删除、清理、重写历史的行为都必须先有 dry-run 和备份策略。
9. memory entry 必须区分事实、决策、约束、偏好和假设，不能把猜测写成事实。
10. compact 最终服务于“继续把任务做完”，不是服务于“存很多文本”。
11. 自动 compact 应该默认尽量少打扰用户，但所有关键动作都必须能在日志和 timeline 中追溯。
12. 自动 compact 不能以“完美压缩”为前提，必须按“可失败、可回滚、可恢复、可重建”设计。

## 上下文分层

compact 后的模型上下文应该由这些层组成：

| 层 | 名称 | 内容 | 是否默认注入 |
| --- | --- | --- | --- |
| 1 | Rules / System Context | 系统规则、项目根规则、安全边界 | 是，但要短 |
| 2 | Hot Context | 最近几轮对话、当前计划、最新工具结果摘要 | 是 |
| 3 | Rolling / Structured Summary | 旧中间历史的结构化交接摘要 | 是 |
| 4 | Long-term Memory | 偏好、长期规则、历史决策、lesson | 检索后选择性注入 |
| 5 | Artifacts | 完整日志、全文、diff、搜索结果、测试输出 | 默认不注入，只给摘要、路径、hash |
| 6 | Fact Sources | task/gateway/subagent 权威文件 | 恢复或验收时读取 |

## 触发策略

第一版建议使用保守阈值：

| 使用率 | 动作 | 说明 |
| --- | --- | --- |
| `< 50%` | 正常运行 | 只记录 token ledger |
| `>= 50%` | checkpoint | 写轻量任务状态和 token 风险提示 |
| `>= 70%` | compact | 执行正式 compact 流程 |
| `>= 85%` | hard guard | 禁止继续内联大工具输出，强制 artifact 化 |
| `>= 95%` | stop | 停止本轮继续膨胀，必须 compact 或人工清理 |

阈值都必须是配置项，默认值走安全保守策略。状态行、`memory-doctor` 和未来 `/context` 都要显示 estimate，而不是假装精确 token。

## 自动化策略

目标是默认自动完成 80%-90% 的压缩维护，让用户只在风险高、信息不确定或恢复失败时介入。

自动化分三档：

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `suggest` | 只提示 compact 风险和建议动作，不改写上下文 | 第一版默认、调试期 |
| `auto_safe` | 自动 checkpoint、artifact 化、写 snapshot；summary/apply 仍保守 | 普通用户默认目标 |
| `auto_full` | 自动 checkpoint、artifact、summary、self check，并在通过后切换上下文 | 后续成熟后启用 |

自动 compact 允许静默完成的动作：

- 写 token ledger。
- 写 pre-compact checkpoint。
- 大工具输出 artifact 化。
- 写 compression snapshot。
- 写 LocalStore audit event。
- 生成 dry-run compact plan。

自动 compact 不应该静默完成的动作：

- 删除或清理原始 raw/hook 文件。
- 覆盖 task/gateway/subagent 权威事实源。
- summary 缺字段仍继续运行。
- self check 失败仍继续运行。
- 把 hypothesis 写成 confirmed fact。
- 自动写正式 skill。

## 无人值守工作状态锁

自动 compact/resume 最大风险不是“摘要写得短不短”，而是压缩后模型继续工作时已经偏离原任务。无人值守模式必须先证明工作状态仍一致，不能只靠模型口头说“我记得”。

自动 compact 后继续执行前，必须通过 Work State Consistency Check：

- `goal`：当前目标必须和 compact 前一致。
- `phase`：当前阶段、当前步骤、下一步动作必须能从 task/run state 或 apply bundle 还原。
- `acceptance`：验收条件、必须通过的测试、用户明确要求不能丢。
- `constraints`：禁止事项、架构边界、不能覆盖的文件必须保留。
- `refs`：关键文件、artifact、restore refs、latest checkpoint 必须仍可定位。
- `git_state`：当前 diff 必须仍属于同一任务范围，不能突然扩大到无关模块。
- `test_state`：最近 focused tests / failing tests / CI 状态不能被摘要吞掉。

恢复后的第一步必须是 Action Guard：

1. 读取 compact context、apply bundle、restore refs 和 task/run state。
2. 对照 compact 前的 work state snapshot。
3. 输出机器可读的 consistency report。
4. 只有 `ok=true` 时才允许继续执行工具或改代码。
5. 如果缺字段、refs 不存在、任务目标冲突或 diff 范围异常，进入 `blocked_needs_human_review`，不得无人值守继续。

这条规则适用于半自动和自动模式：自动 compact 不是“压完就继续”，而是“压完、恢复、对照、确认一致，再继续”。

用户最少动手的理想路径：

```text
50%: 系统自动 checkpoint，不打扰用户，只在状态行提示。
70%: 系统建议 compact；半自动阶段需用户确认，自动阶段也必须先通过 work state consistency check。
85%: 系统自动 artifact 化大输出，并提示上下文高风险。
95%: 系统停止继续膨胀，要求 compact/retry/人工恢复三选一。
```

这不是“永远不让用户动手”。正确目标是：低风险自动，高风险明确停下，所有恢复材料已经准备好。

## 完整 compact 流程

```text
用户消息或工具结果进入系统
  -> 估算当前上下文和工具输出 token
  -> 大工具输出先 artifact 化
  -> 50% 以上写 checkpoint
  -> 70% 以上进入 compact
      -> 写 pre-compact snapshot
      -> 保护 head 规则和最初目标
      -> 保护最近 tail 对话和最新工具结果摘要
      -> 保持 tool call/result 配对
      -> 中间历史生成 structured summary
      -> summary 写 transcript/summary entry
      -> 更新 memory 候选和 task state
      -> 写 compact audit event
  -> post-compact self check
  -> work state consistency check
  -> 重新加载 rules/routes/required paths
  -> 一致性通过后继续执行任务；否则 blocked_needs_human_review
```

## Artifact 层要求

大工具输出优先转成 artifact：

- 测试日志
- grep / rg 大结果
- 大文件全文
- web fetch 内容
- diff 输出
- 编译/运行长报错
- runner 大段输出

上下文里只保留：

```json
{
  "artifact_id": "test-log-20260506-001",
  "kind": "test_output",
  "command": "pytest agent_py_agent/tests/test_x.py -q",
  "exit_code": 1,
  "summary": "2 tests failed; main issue is stale gateway response path.",
  "important_lines": ["tests/test_x.py:143", "tests/test_x.py:188"],
  "path": ".agent/artifacts/test-log-20260506-001.txt",
  "sha256": "..."
}
```

第一版不需要立刻实现复杂 artifact GC，但写入必须可 list/search，并且 compact summary 必须引用 artifact path/hash。

## Task State Schema

compact 前应生成或更新当前任务状态。第一版可以写入 snapshot JSON；后续可以补人类可读文件，例如 `.agent/TASK_STATE.md` 或任务目录下的 `COMPACT_STATE.md`。

固定字段：

```markdown
# Current Task State

## Goal

## Hard Constraints

## Current Progress

## Changed Files

## Important Decisions

## Failed Attempts

## Current Errors

## Current Test Status

## Artifacts

## Next Step
```

对于 subagent 任务，不能替代 `STATUS.md`、`WORK_LOG.md`、`HANDOFF.md`、`ACCEPTANCE.md`、`TEST_CHECKLIST.md`。compact state 只是恢复索引和交接摘要，最终事实仍以任务目录为准。

## Compact Summary Schema

结构化 summary 必须使用固定格式，避免模型自由发挥：

```markdown
# Compact Summary

## 1. User Goal

## 2. Hard Constraints

## 3. Current State

## 4. Completed Work

## 5. Changed / Read Files

## 6. Important Decisions

## 7. Failed Attempts

## 8. Current Errors

## 9. Test Status

## 10. Artifacts

## 11. Snapshot / Task Refs

## 12. Next Best Action
```

必须保留标识符：文件路径、函数名、类名、命令、错误码、run_id、request_id、task_id。

## Post-Compact Self Check

compact 后必须进行自检。第一版可以是确定性字段检查；后续可选模型复述检查。

自检问题：

1. 当前用户目标是什么？
2. 用户硬约束是什么？
3. 已经修改或重点读取了哪些文件？
4. 当前测试状态是什么？
5. 哪些方案已经试过但失败了？
6. 下一步应该做什么？

失败策略：

- summary 为空：abort。
- 缺少 goal / constraints / next step：retry 一次，仍失败则 abort。
- 缺少 snapshot/task refs：abort。
- tool call/result 配对断裂：abort。
- 自检无法回答关键问题：abort 或请求人工确认。

## 日志和恢复设计

compact 必须接入现有 LocalStore / timeline / JSONL 文件事实源。LocalStore 是索引和审计层，不是最终事实源；最终恢复仍回到 snapshot、raw/hook、artifact、task/gateway/subagent 文件。

### 事件类型

建议新增或统一以下事件：

| event_type | 触发时机 | 关键 payload |
| --- | --- | --- |
| `memory_compact_plan_created` | dry-run 生成 compact plan | plan_id、session_id、candidate_counts、estimated_savings、risk_level |
| `memory_compact_checkpoint_written` | 写 checkpoint 后 | checkpoint_id、snapshot_path、token_ratio |
| `memory_compact_artifact_written` | 大输出 artifact 化后 | artifact_id、path、sha256、source_tool、original_chars |
| `memory_compact_snapshot_written` | compression snapshot 成功后 | snapshot_id、snapshot_file_path、archive_level |
| `memory_compact_summary_created` | structured summary 成功后 | summary_id、schema、refs、token_estimate |
| `memory_compact_self_check_passed` | 自检通过后 | summary_id、answered_fields、next_action |
| `memory_compact_self_check_failed` | 自检失败后 | summary_id、missing_fields、recommended_action |
| `memory_compact_applied` | compact apply 成功后 | compact_id、backup_path、rewritten_files、preserved_refs |
| `memory_compact_failed` | compact 任意阶段失败 | phase、error_code、error、rollback_hint |
| `memory_compact_restored` | 从 compact 失败或用户请求恢复后 | compact_id、restored_from、restored_paths |

这些事件都应该能通过：

```powershell
my-agent timeline --event-type memory_compact_failed --details
my-agent timeline --source-type memory_compact --details
my-agent local-search "compact failed" --source-type memory_compact
```

查到。

### 记录模型

compact plan、summary、artifact manifest 应该作为 LocalStore record 索引：

| source_type | source_id | 内容 |
| --- | --- | --- |
| `memory_compact` | `compact_id` / `plan_id` | compact plan、summary、自检结果、引用路径 |
| `memory_artifact` | `artifact_id` | artifact manifest、摘要、hash、来源工具 |
| `memory_snapshot` | `snapshot_id` | snapshot manifest 和权威文件路径 |

LocalStore 只保存摘要、manifest 和可搜索字段；大正文仍在 artifact 文件或原始 raw/hook 文件中。

### 失败恢复路径

compact 出问题时按这个顺序恢复：

1. 查 `timeline` 中最近的 `memory_compact_failed` / `memory_compact_self_check_failed`。
2. 读取 payload 里的 `plan_id`、`snapshot_id`、`summary_id`、`artifact_id`。
3. 打开 `memory_archive/snapshots/*.json` 的权威 snapshot。
4. 打开 compact plan 中列出的 raw/hook 原始文件和 artifact manifest。
5. 如果涉及正式任务，回到 task fact source：`STATUS.md`、`WORK_LOG.md`、`HANDOFF.md`、`ACCEPTANCE.md`、`TEST_CHECKLIST.md`。
6. 如果涉及 gateway 请求，回到 `gateway/requests/done|failed/<request_id>.json` 和 `gateway/responses/<request_id>.json`。
7. 运行 `memory-resume --context-only` 生成恢复块。
8. 如果 LocalStore 索引损坏，运行 `local-doctor`，必要时 `local-rebuild --source memory --reset`。

### 回滚策略

第一版 apply compact 必须采用 append-only 或 copy-on-write：

- 先写 compact plan。
- 再写 backup manifest。
- 再写新 compacted 文件。
- readback 验证新文件。
- LocalStore 写 `memory_compact_applied`。
- 旧 raw/hook 文件默认保留。

只有当用户显式执行 prune/cleanup 时，才允许删除旧文件。删除前必须有 backup，且 timeline 要记录被删除文件路径、hash、backup 路径和恢复命令。

### 与 local-doctor / local-rebuild 的关系

`local-doctor` 需要检查：

- compact plan 是否有对应 applied/failed 终态事件。
- artifact manifest 指向的文件是否存在、hash 是否匹配。
- compact summary 引用的 snapshot/task/gateway 路径是否存在。
- LocalStore 中 `memory_compact` 记录数量是否和 manifest 数量一致。

`local-rebuild --source memory` 需要能从以下来源重建索引：

- raw/hook JSONL。
- `memory_archive/snapshots/*.json`。
- artifact manifest。
- compact plan / compacted JSONL。

这样即使 SQLite 或 FTS 索引坏了，也能从文件事实源重新生成 timeline 和搜索入口。

## Memory Entry 分类

compact 过程中生成 memory candidate 时，必须分类：

| 类型 | 含义 | 写入要求 |
| --- | --- | --- |
| `confirmed_constraint` | 用户明确要求 | 可自动写，但要带来源 |
| `observed_fact` | 从文件、命令、测试看到的事实 | 可自动写，必须带证据路径 |
| `decision` | 已确认技术决策 | 最好带来源和适用范围 |
| `hypothesis` | agent 推测 | 必须标注待验证，不能当事实 |
| `preference` | 用户长期偏好 | 可写，但要允许删除/覆盖 |

自动写正式 skill 仍禁止；只能生成 draft，等待用户确认。

## 用户命令规划

### CLI 命令

第一阶段：

```powershell
my-agent memory-compact --dry-run
my-agent memory-compact --session-id sess_xxx --dry-run
my-agent memory-compact --since 2026-05-01 --until 2026-05-06 --dry-run
```

第二阶段：

```powershell
my-agent memory-compact --apply
my-agent memory-compact --apply --backup
my-agent memory-compact --artifacts-only
my-agent memory-compact --summary-only
```

第三阶段：

```powershell
my-agent memory-backup
my-agent memory-backup --output backups/memory-20260506.zip
my-agent local-rebuild --source memory --reset
```

### Chat slash commands

后续可以补：

```text
/context
/compact
/compact focus changed files, failing tests, hard constraints, next steps
/clear
/memory
/state
/artifacts
/resume
```

## 开发计划

### Phase 0：功能规格和边界确认

产物：

- 本文档。
- `memory-compact` feature spec。
- 明确不碰 subagent task fact source，不删除任务目录。

验收：

- 文档列清楚 dry-run/apply/backup 的边界。
- 与 subagent 并行开发没有文件所有权冲突。

### Phase 1：Dry-run 预演

范围：

- 新增 `memory-compact --dry-run`。
- 扫描 `memory/raw`、`memory/hooks`、`memory_archive/snapshots`、`memory_archive/tokens`。
- 输出候选文件、记录数量、archive level 分布、token ledger 大小、潜在 artifact 化对象。
- 不写文件，不删除文件。

验收：

- dry-run JSON 输出稳定。当前第一版已覆盖 archive、snapshot、token ledger 汇总。
- 空目录、损坏 JSONL、缺字段都能给诊断。
- 不修改工作区任何 memory 数据。
- 写入或预览的 plan schema 能直接作为后续 apply 的输入。

### Phase 2：Artifact 化

范围：

- 定义 artifact record schema。
- 大工具输出转 `.agent/artifacts/`。
- raw/hook/context 只保留摘要、路径、hash、关键行。
- 保持 tool call/result 配对。

验收：

- 大输出不会直接膨胀 prompt。
- artifact 文件可 list/search。
- hash/readback 验证通过。

### Phase 3：Structured Summary 和 Task State

范围：

- 固定 compact summary schema。
- 生成 task state / compact state。
- summary 必须引用 snapshot/task/artifact refs。
- 失败时不删除中间历史。

验收：

- 缺少 goal/constraints/next step 会失败。
- 标识符不会被随意改写。
- 正式任务仍回到 task fact source。

### Phase 4：Post-Compact Self Check

范围：

- 确定性字段检查。
- 可选模型复述检查。
- 失败 abort/retry 策略。

验收：

- 自检失败不会继续运行。
- 失败原因写 LocalStore event。
- 用户能看到 compact 失败原因和建议动作。
- `timeline --event-type memory_compact_self_check_failed --details` 能定位失败 compact。

### Phase 5：Apply Compact

范围：

- `memory-compact --apply`。
- 生成 compacted JSONL 或 compact summary index。
- 默认保留原文件；需要清理必须显式配置。
- 支持 `--backup`。

验收：

- apply 前必须有 dry-run 可复现计划。
- apply 后 `memory-resume` 仍能找到任务/gateway/subagent 事实源。
- `local-rebuild --source memory` 能重建索引。
- apply 默认保留旧文件，cleanup 必须单独显式执行。

### Phase 6：Backup / Rebuild / 自动维护

范围：

- `memory-backup`。
- compact 后自动建议 `local-rebuild`。
- 按大小、天数、token ledger 触发维护建议。
- 默认不自动删除历史。

验收：

- backup 可恢复。
- rebuild 不删除原始 memory/gateway/subagent 文件。
- 自动策略默认只提示，不静默 destructive。
- `local-doctor` 能检查 compact manifest、artifact hash 和 LocalStore 索引一致性。

## 与 subagent 并行开发的边界

为了不影响另一个 agent 开发 subagent，compact 先只拥有这些路径：

- `agent_py_agent/agent/memory_archive/`
- `agent_py_agent/cli/memory_archive_commands.py`
- `agent_py_agent/cli/memory_commands.py` 或后续拆分出的 memory command 文件
- `agent_py_agent/tests/test_memory_*`
- `docs/modules/memory/05-compact-plan.md`

暂时不碰：

- `agent_py_agent/agent/subagents/`
- `agent_py_agent/agent/subagent_workflows/`
- `docs/modules/subagent/`
- subagent runner 结束点统一写 snapshot 的代码

交叉点留到后续集成：

- subagent-run 完成后写 compact/recovery state。
- parent/subagent 批次收束前触发 flush。
- `memory-resume` 从 subagent fact source 恢复后补充 compact state。

## 与 memory 同步开发的边界

compact、resume 和 memory runtime 应该同步推进，但要分清职责，避免几个 agent 同时改同一个入口。

可以并行开发的方向：

- Compact agent：负责 `memory-compact --apply`、apply bundle、restore refs、self-check、work state snapshot、failure blocking。
- Resume agent：负责 `memory-resume --from-compact`、恢复块生成、context-only 输出、consistency report。
- Memory query agent：负责 control-plane query 接入 resume 优先级、daily ledger/task/run/tool-output/compact apply 的统一检索。
- Work-state agent：负责 Work State Consistency Check、Action Guard、Drift Detector 的字段、报告和测试。
- Docs/tests agent：负责文档、CLI help、focused tests、cross-platform tests 和 code-size guard。

必须串行或先约定接口再并行的点：

- `apply_bundle.json` / `restore_refs.json` / `work_state_snapshot.json` 的 schema。
- `memory-resume --from-compact` 的 CLI 参数和返回格式。
- consistency report 的状态枚举，例如 `ok`、`blocked_missing_refs`、`blocked_goal_mismatch`、`blocked_diff_scope`、`blocked_needs_human_review`。
- control-plane query 的公开 bundle 字段。
- 自动 compact/resume 触发策略和无人值守继续条件。

多子代理协作规则：

- 每个子代理只拥有自己模块的写入范围，跨模块字段先写到文档再改代码。
- 不允许多个子代理同时改同一个 schema 文件；schema 变动必须先合并设计。
- 不允许为了赶进度跳过 self-check、doc sync、strict code-size 和 focused tests。
- 自动化相关改动默认只做 suggest / blocked，不直接开启 silent auto continue。
- 所有新写入文件必须保留 v2 schema/reserved 或明确说明为什么还不能升级。

## 下一步开发顺序

现在 dry-run、工具输出外置、非破坏性 compact apply、apply bundle、restore refs、schema v2 和第一版 self-check 已经落地。后续按“先手动，再半自动，最后自动”的顺序推进。

### Step 1：手动 compact apply 完整化

状态：第一片已落地。用户手动执行 `memory-compact --apply` 时，现在会得到更完整、稳定、可审计的压缩恢复包。

范围：

- 固定 apply bundle / restore refs 的字段。
- 给 apply 生成稳定 `apply_id` / `plan_id`。
- 扩展 self-check：检查 goal、constraints、acceptance、latest tests、artifact refs、restore refs。
- 写 work state snapshot，作为后续 resume 对照基线。
- 保持非破坏性，不删除、不重写、不裁剪原始事实源。

验收：

- `memory-compact --apply` 后能列出本次 apply 的全部恢复入口。
- self-check 失败时必须 blocked，不能写成成功。
- hard/high-risk/soft 保持 0。

当前实现说明：

- `apply_id` 使用 `plan_id + 时间` 生成；同一秒重复 apply 会自动追加后缀，避免覆盖旧产物。
- `plan_id`、`apply_id` 会同时写入 metadata、apply bundle、restore refs、work state snapshot、self-check、失败报告和 ledger。
- 新增 `*.work_state_snapshot.json`，记录 goal、phase、next step、acceptance、constraints、changed/read files、artifact refs、restore refs、latest tests、git state、missing fields 和 source quality。
- work state 字段来源第一片已接入：只读 workspace 内 task/run 事实源，例如 `ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`task.json`、旧 `subagents/<run_id>/` 和新 `tasks/*/agents/<run_id>/`；找不到字段时仍写 `missing_fields`，不会猜测或伪造。
- self-check 已检查 context、restore refs、apply bundle、work state snapshot 是否写入，restore refs 是否存在，以及 goal / next actions / acceptance / constraints / test state / risks 是否被带出。
- 当前仍保持非破坏性：不删除、不重写、不裁剪 raw/hook/snapshot/token/task/run 文件。

### Step 2：手动 resume from compact

状态：第一片已落地。用户可以手动指定一次 compact apply，让系统生成可继续工作的恢复上下文。

建议命令形态：

```bash
my-agent memory-resume --from-compact <apply_id>
my-agent memory-resume --from-compact <apply_id> --context-only
```

范围：

- 读取 apply bundle、compact context、restore refs、task/run state。
- 生成恢复块，明确当前 goal、phase、next action、constraints、refs、test state。
- 输出 work state consistency report。
- 不自动执行工具、不自动改代码，只生成恢复上下文和状态确认。

验收：

- 新会话能从 compact apply 恢复到同一任务状态。
- refs 缺失、目标冲突、diff 范围异常时进入 blocked。
- 恢复结果能追溯到原始 fact sources。

当前实现说明：

- `memory-resume --from-compact <apply_id>` 会只读读取 metadata、apply bundle、restore refs、work state snapshot、compact context 和 self-check。
- 输出 `compact_resume` payload、`compact_resume_consistency_report`、`compact_resume_handoff`、推荐读取路径、下一步建议和 `context_block`。
- `compact_resume_handoff` 会稳定包含 goal、current_phase、next_step、acceptance、constraints、latest_tests、changed_files、read_files、recommended_read_paths 和 action_guard 摘要，方便新会话或其他 agent 接手。
- `--context-only` 只打印 `Compact Resume Context`，方便复制到新会话或后续自动注入。
- self-check 失败、restore refs 缺失、apply IDs 串号或 compact context 缺失时会进入 `blocked_needs_human_review`，不会自动继续执行工具。
- `MemoryCompactResumeOptions.owner_type/owner_id` 已接入子代理 owner 只读引用解析；`subagent_run` / `subagent_session` 会返回 task-local run workspace 和 legacy adapter refs，但不触碰 subagent runner，不写主 memory。

### Step 3：半自动 compact 提示

状态：第一片已落地。系统发现上下文风险时提示用户 compact，但执行前需要确认。

范围：

- token ledger / context ratio 达阈值时提示。
- 展示风险、预计收益、将写入哪些文件。
- 用户确认后执行手动 apply 流程。
- apply 后仍需要用户确认是否使用 resume context 继续。

验收：

- 默认不静默切换上下文。
- 用户能看到 compact 原因、范围和失败处理。
- self-check 或 consistency check 失败时不继续。

当前实现说明：

- 新增 `compact_suggest.py`，根据累计 token、上下文窗口和 dry-run plan 生成 `compact_suggestion`。
- `run` 结果新增 `memory_compact_suggested/status/ratio/message/commands` 字段；CLI 只在达到建议阈值时打印提示。
- 提示会给出 `memory-compact --dry-run`、`memory-compact --apply` 和 `memory-resume --from-compact <apply_id> --context-only` 命令，但 `automatic_action=none`。
- 当前默认上下文窗口来自 `memory_compact_context_window_tokens`；未设置时用 `max_tokens * 16` 且不低于 8192 的保守估计，后续接真实模型 context window 时只替换这一层。
- `owner_type/owner_id` 会继续透传给 compact resume；子代理 owner 当前只解析 refs，不触碰 subagent runner，也不自动做子代理会话压缩。

### Step 4：自动 compact/resume

状态：安全第二片已落地。无人值守长任务中，系统未来可以自动 compact 并继续；当前已实现状态锁、动作守门报告和一个默认不自动 apply 的安全协调器。

范围：

- 自动 checkpoint、artifact 外置、apply、self-check。
- 自动 resume 前强制 Work State Consistency Check。
- Action Guard 通过后才允许继续工具调用或代码修改。
- Drift Detector 持续检查任务目标、diff 范围、next action 和测试状态。
- 不确定时进入 `blocked_needs_human_review`，不得静默继续。

验收：

- 自动 compact 后继续执行的第一条动作必须引用 consistency report。
- 状态不一致时不会继续无人值守。
- 不会自动删除历史，不会自动写正式 skill，不会把假设写成事实。

当前实现说明：

- 新增 `compact_action_guard.py`，每次 `memory-resume --from-compact` 都会带 `action_guard`。
- 默认 `manual` 模式输出 `requires_user_confirmation`，表示恢复材料可读，但不能无人值守继续。
- `--compact-resume-mode auto` 会启用严格守门：consistency/self-check/refs/goal/next_step 必须通过，且 `missing_fields` 必须为空。
- 如果 work state 缺 acceptance、constraints、latest_tests 等字段，自动模式会返回 `blocked_missing_work_state_fields`，CLI 退出码为 2。
- 如果 work state 字段齐全、refs 存在、self-check 通过且 `resume_mode=auto`，action guard 会返回 `allow_automated_continue`、`allowed_to_continue=true` 和 `allowed_next_action=continue_after_guard`。
- action guard 和 auto cycle 都显式写 `automatic_tool_execution=none`：这一步只给出 go/no-go 机器判断，不自动运行工具或修改代码。
- 新增 `compact_auto.py`，提供 `run_memory_compact_auto_cycle()`：默认 `allow_apply=false` 时只返回 compact 建议和 `needs_user_confirmation`，不会写 apply 产物。
- 显式 `allow_apply=true` 时，auto cycle 只执行非破坏性 apply 和 `resume_mode=auto` 的 action guard 检查；如果字段不完整会停在 `blocked_after_action_guard`，仍不会执行工具或继续改代码。
- `SimpleAgent.run()` 收尾已经接入 auto cycle 的默认 plan-only 分支；达到 compact 阈值时，CLI 会显示 `compact_suggestion` 和 `compact_auto`，其中 `compact_auto` 只说明当前状态、下一步和工具执行状态，不会自动 apply。
- `compact_subagent_owner.py` 已接入 `memory-resume --from-compact`：指定 `subagent_run` / `subagent_session` owner 后，会返回 `linked_run_workspace`、`legacy_only` 或 `owner_refs_not_found` 状态，以及 task-local refs；当前仍不自动执行工具、不改 runner、不污染主 memory。
- 当前仍不会自动继续工具调用或代码修改；这一步只是把“提示、可选 apply、恢复、自检、停住”的无人值守安全骨架做出来。

这条顺序先让风险可见，再让数据可恢复，再让用户确认流程顺滑，最后才做真正自动化。
