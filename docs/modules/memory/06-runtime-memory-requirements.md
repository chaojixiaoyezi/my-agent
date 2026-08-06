# Runtime Memory Requirements

## 后台 Curator 配置

这些字段位于 `agent_py_agent/config/agent_config.yaml`；同名 `AgentConfig` 和 normalization 必须保持一致。

| 配置字段 | 默认值 | 中文含义 |
| --- | --- | --- |
| `memory_curator_enabled` | `true` | 是否启用后台策展。关闭时不调用辅助模型，但保留 state、cursor 和 pending reason。 |
| `memory_curator_provider` | `auto` | 后台模型服务商；`auto` 继承主后端，也可显式使用已支持 provider。 |
| `memory_curator_model` | 空 | 后台模型 ID；空值继承主模型，填写时只影响 Curator。 |
| `memory_curator_interval_seconds` | `10800` | 有新经历且距上次成功达到多少秒时触发，合法范围 60 到 604800。 |
| `memory_curator_turn_threshold` | `10` | cursor 后累计多少个新用户轮次触发，合法范围 1 到 1000。 |
| `memory_curator_batch_message_limit` | `80` | 单批最多读取多少条 ConversationStore 消息。 |
| `memory_curator_max_input_chars` | `40000` | 单次后台模型输入字符上限；工具大输出只传 preview/ref。 |
| `memory_curator_timeout_seconds` | `90` | 单次后台模型调用超时秒数。 |
| `memory_curator_max_retries` | `1` | Schema、超时或模型错误后的有界重试次数；失败不推进 cursor。 |
| `memory_curator_daily_finalize_hour` | `23` | owner 当地时间每日收尾小时，范围 0 到 23。 |
| `memory_curator_auto_promotion_policy` | `conservative_v1` | 自动晋升策略版本；也可设 `manual_only` 关闭自动晋升。 |
| `memory_lesson_min_occurrences` | `2` | 非用户显式 lesson 晋升所需最少独立观察次数。 |
| `memory_hot_min_occurrences` | `3` | 正式 lesson 晋升 HOT 所需最少独立观察次数。 |

旧 `auto_save_memory` 字段只控制运行归档、恢复事实和 task workspace 是否落盘；它不再表示“把普通 user/assistant 正文自动写入 long_term”。Gateway、CLI chat 和普通 run 都不得恢复旧 dialogue 长期记忆写入。

## 统一触发原因

所有触发都调用 `MemoryCuratorService.request(reason)`，写入同一 `memory/curator/state.json`，再由 Gateway owner maintenance 或管理员命令调用同一 `run`：

| reason | 触发时机 |
| --- | --- |
| `turn_threshold` | 新用户轮次数达到阈值。 |
| `interval` | 有新经历且超过时间间隔。 |
| `pre_compact` | Compact 前提交高优先级提炼请求。 |
| `session_close` | 正式会话关闭。 |
| `reset` | 正式会话 reset。 |
| `task_complete` | 结构化任务完成。 |
| `daily_finalize` | owner 当地每日收尾。 |
| `admin` | 管理员 CLI 显式触发。 |
| `migration` | 一次性迁移产生需要统一处理的经历。 |

Curator 失败不能使 Gateway、Compact 或用户请求崩溃。常见稳定错误码包括 `CURATOR_SCHEMA_INVALID`、`CURATOR_MODEL_TIMEOUT`、`CURATOR_MODEL_FAILED`、`CURATOR_INPUT_BUDGET_EXCEEDED`、`CURATOR_COMMIT_FAILED`、`CURATOR_COMMIT_RECOVERY_FAILED`、`CURATOR_LEASE_LOST` 和 `CURATOR_RUN_AUDIT_FAILED`。

每种 reason 都有单独的耐久 generation。lease 获取时冻结 generation；成功提交只消费该代。运行期间再次收到相同 reason 会递增 generation，第一次运行结束后仍会由 Gateway maintenance 接续，避免“同名去重”丢掉新经历。不同 reason 也不会被当前运行一并清空。

## 后台模型权限

后台模型可以看到：有界对话、audit 预览、工具/artifact 引用、task 结果摘要和有界正式记忆。它只返回结构化 `daily_events/candidates/processed refs/unresolved refs/warnings/next_cursor`。

后台模型没有 Registry、tool loop 或 Skill 运行面，因此不能获得 `shell`、`write_file`、`edit_file`、`remember`、`update_persona`、Skill 安装或任意文件写入权限。Daily、Candidate、state 和 run audit 都由宿主程序提交；long-term、lesson、HOT、USER、SOUL、AGENTS 只能由 Promotion/Persona 主链更新。

大工具输出正文仍只在 `tasks/.../work/blobs/tool_outputs/`。Curator 通过 runtime-memory control plane 的只读查询，按当前 owner 根和精确 `run_id/tool_call_id` 取得路径、SHA-256 与字节数；模型输入和 Daily/Candidate 都不接收 index 中的参数、来源路径或正文。该查询不是执行权威，`tool_verified` 自动晋升仍必须由 Tools operation store 核验成功终态。

## 冲突、时间与 scope 示例

### 不同 scope 共存

```text
subject_key=device.os, scope=personal:personal, content=个人电脑使用 macOS
subject_key=device.os, scope=company:company, content=公司服务器使用 Linux
```

两条事实不互相替换，因为 scope 不同。

### 同 scope 冲突

```text
旧：subject_key=device.os, scope=personal:personal, content=个人电脑使用 macOS
新：subject_key=device.os, scope=personal:personal, content=个人电脑使用 Windows
```

新消息时间更晚仍不会自动覆盖。候选必须使用 `proposed_action=replace` 和旧记录的精确 `target_entry_id`，经过审核后才更新；否则进入 `blocked_conflict`。

### 稳定偏好与临时要求

```text
我通常喜欢简洁回答。                -> personal USER 候选
这次架构问题请详细解释。            -> temporary/session，不写 USER
```

LLM 不能只靠措辞“看起来像偏好”自动决定；来源必须是用户消息，scope 和有效期必须结构化。

### 工具成功与未知副作用

只有 operation store 中 `status=succeeded`、`result.ok=true` 且 `effect_outcome` 不是 `unknown/not_started` 的记录可以支持 `tool_verified`。模型正文说“已经写完”、失败归档、timeout 或 cancelled 都不能代替正式终态。

## 统一管理员 CLI

```text
my-agent memory candidates list [--status STATUS] [--limit N] [--json]
my-agent memory candidates review <candidate_id> --decision <approve|reject|reopen|expire|supersede>
my-agent memory candidates promote <candidate_id> [--automatic] [--confirmed]
my-agent memory curator status [--json]
my-agent memory curator run [--force] [--json]
my-agent memory retention plan [--json]
my-agent memory retention apply [--json]
my-agent memory doctor [--index PATH] [--json]
my-agent memory migrate [--apply] [--json]
```

- `review` 的 `--proposed-action` 取 add/replace/remove/merge/none。
- `--promotion-target` 取 user/long_term/lesson/hot/soul/agents/none。
- `--target-entry-id` 是 replace/remove/merge/HOT 的精确正式目标，不能按正文模糊选择。
- `promote --automatic` 仍执行 conservative policy，不会放宽证据或冲突规则。
- `--confirmed` 只表达当前管理员操作已确认；SOUL/AGENTS 候选本身仍必须具有用户明确来源。
- retention `plan` 只读；`apply` 会在锁内重新计划并重验证，不执行外部保存的裸路径列表。
- migrate 默认 dry-run；只有 `--apply` 才备份、迁移和写 marker。

旧 `my-agent learn ...` 和 `my-agent subagents-memory-gate ...` 不再是生产命令；对应能力已经并入 `memory candidates ...`。

## 一次性迁移

`MemoryMigrationService.plan()` 只读检测，`apply()` 在 owner migration lock 内再次扫描。迁移覆盖：

- `memory/ops.jsonl` 中旧候选正文；
- workspace `data/learning_drafts/*.json`；
- task-local `memory_gate/candidates.jsonl`、`review_queue.jsonl`、`decisions.jsonl`、`exports.jsonl`；
- 旧 daily 长期操作镜像和新旧 Daily 混合文件；
- long-term 中旧 `kind=dialogue` 与 `kind=lesson`；
- 旧 `memory.md`、自由文本 HOT 和未带正式 marker 的 lesson；
- `retention.v1` 策略。

apply 前会在 `<MY_AGENT_HOME>/backups/memory-migration/<run_id>/` 生成完整文件备份和 `manifest.json`。长正文按有界 Candidate 分段，或以 backup manifest artifact ref 保全，不静默截断。成功后写 `memory/migration.json`；重复 apply 幂等。任一坏 JSON/JSONL、正式 lesson/HOT/migration marker、symlink 或中途写入失败都会 fail closed，并恢复 Candidate、Daily、long-term、ops、导航、HOT、lesson、routing 和 marker。ConversationStore 不参与迁移修改。

迁移完成后生产运行只读 v2 路径；不存在永久 legacy 双读、双写或 fallback。

## 已删除或收敛的重复路径

- 删除 `agent/subagents/services/learning.py` 与 `agent/subagents/learning_similarity.py` 的独立 learning 状态机。
- 删除 `agent_py_agent/cli/learning.py` 和 `learn` 命令；`data/learning_drafts` 只作为迁移输入。
- 删除 `agent/memory_archive/memory_gate/` 的 candidates/review/decision/export/retention 实现。
- 删除 `agent/subagents/services/memory_gate.py`、`agent_py_agent/cli/_memory_gate.py` 和 `subagents-memory-gate` 命令。
- 子代理 task-local 只保留原始结果、evidence refs 和状态；父代理统一调用 CandidateService。
- `memory/ops.jsonl` 不再保存候选正文，只保留无正文正式操作审计。
- 删除 long-term 到 daily 的操作镜像和 `daily_memory_mirror_enabled` 配置。
- `remember(kind=lesson)` 不再提供正式 lesson 双写；教训提议进入 Candidate，正式正文只写 `memory/lessons/*.md`。
- 删除旧 `memory_push` 的 `MemoryType/MemoryEntry/MemoryWriteContext/write_memory_with_type/trigger_conditions` 长期记录旁路；失败自省现在产生 `origin=model_inferred,promotion_target=lesson` 的统一 Candidate，规划/失败时只召回正式 Lesson/HOT。
- planner/runner 的主动召回只渲染唯一 `<memory-context>`；旧 `[相关记忆提示]` 格式和 long-term `kind=lesson_general/lesson_task` 运行时召回均已删除。
- 普通 dialogue 不再自动写 `memory/long_term/memory.jsonl`。
- `memory.md`、HOT、lesson 的重复正文通过一次性迁移收敛为导航、短规则和唯一 lesson 正文。

## Retention 执行边界

- `legal_hold=true` 或目标 task 在 `legal_hold_task_ids` 时不删除。
- 非终态 task 及恢复所需 artifact 受保护；终态只读结构化 status，不解析自然语言。
- 策略损坏、task 状态损坏、symlink、fingerprint 变化或执行前状态变化都跳过并记录 typed error。
- 普通过期文件优先移入可恢复 trash；正式长期记忆 hard delete 会清除权威正文、FTS/SQLite 内容文件、向量项和 Candidate 重复正文，只保留无正文 tombstone/hash/ops。
- 删除长期记忆不会改写 ConversationStore 中用户历史真实消息。

## 实际检查过的参考实现

这里只记录本轮实际打开或定位过的文件，不代表通读整个仓库。

### 长期助手

- `agent/memory_provider.py`：读取了 provider lifecycle，包含 `prefetch/sync_turn/on_session_end/on_pre_compress/on_delegation` 等隔离 hook。
- `agent/memory_manager.py`：读取了单外部 provider、后台 sync/prefetch、工具 Schema 规范化和 memory-context 清洗边界。
- `tools/memory_tool.py`：读取了单一 memory tool、MEMORY/USER 两个有界文件、冻结 Prompt snapshot、文件锁、威胁扫描和漂移 fail-closed。
- `agent/curator.py`：确认它当前主要是 Skill curator，按 idle/interval 运行可配置辅助模型；不是可以直接照搬的长期 Memory Candidate/Promotion 主链。

my-agent 借鉴后台隔离、辅助模型和耐久状态，但不采用 长期助手 的“外部 provider 可各自拥有 schema/工具/存储”作为核心权威，也不让后台 Agent直接修改 Memory/Skill。

### 通道运行时

- `src/auto-reply/reply/agent-runner-memory.ts`：读取了 pre-compaction memory flush 的阈值、独立 run、可写边界、session 状态和失败处理。
- `src/memory/root-memory-files.ts`：确认 canonical `MEMORY.md` 与 legacy `memory.md` 分离。
- 定位并核对 `docs/concepts/memory.md`、`docs/reference/session-management-compaction.md` 的 daily/long-term 与 pre-compaction 说明。

my-agent 借鉴 daily、pre-compact 请求和分层提炼，但把模型直接写 Markdown 改为严格 Curator Schema → Candidate/Daily 宿主提交，并保留唯一 JSONL 正式事实源。

### 会话运行时

- `会话运行时-rs/core/src/compact.rs`：读取了 compaction item、history replacement、pre/post hooks、重试和 initial context 边界。
- `会话运行时-rs/rollout/src/recorder.rs`：读取了 canonical rollout JSONL 的后台 writer、flush/shutdown 和 session/thread metadata。

my-agent 借鉴原始会话/rollout 与 Compact 分离、结构化 item 和持久恢复；不会把 Compact summary 当长期事实，也不会让工具正文变成 Memory 权威。

### 终端交互

- `src/services/SessionMemory/sessionMemory.ts`：读取了按 token/tool-call 阈值触发、forked background agent 和 session Markdown 更新。
- `src/services/compact/sessionMemoryCompact.ts`：读取了 SessionMemory 与 Compact 的等待/截断、保留最近消息以及 tool-use/tool-result 配对边界。

my-agent 借鉴后台 fork 与安全触发，但不采用单个 session Markdown 既做工作摘要又参与 Compact 的双重职责；Daily、Candidate、正式 Memory 和 Compact 分别持有自己的合同。

## 真实验收边界

- 真实链路必须使用隔离 `MY_AGENT_HOME`、owner、workspace 和真实 Gateway 进程。
- Curator 必须调用真实 provider/model；Fake/Echo/直接 Service 调用只能做单元测试。
- 每个独立真实场景只提交一次普通中文请求，测试者只观察，不手工编辑 Memory 文件。
- 日志保存脱敏的 request/thread/run/curator run ID、配置、state 前后差异和落盘结果，不保存 Key、Token、Cookie 或完整隐私正文。
- 缺模型凭据、第二个可用模型、Gateway 或网络时必须标“未验证”，不能标记 Goal 完成。
