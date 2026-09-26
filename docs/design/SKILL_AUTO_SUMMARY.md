# 自学习 S3：自动总结 Skill

状态：已实施，本地分支 `claude/skill-auto-summary`（2026-09-26），待合入与真实验收。本文是自动总结 Skill 的唯一模块设计；`DESIGN_LEDGER.md` 只保留摘要和链接。

## 1. 背景和用户决定

S1 已有一条自学习链：子代理 lesson → Skill 提案 → 用户用 CLI 确认 → 安装。S2 负责给待确认提案排审核顺序。两条都要用户逐条确认，而且只覆盖子代理 lesson。主代理完成复杂任务后没有任何自动总结，本轮学到的做法下一轮就丢了。

用户在 2026-09-26 定了两件事：

1. 要有“完成任务后自动总结 Skill”的能力。
2. 不要用户逐条审批（“别让用户审批，这个用户没时间审批”）。

所以本设计用一组确定性的自动闸门代替人工确认。每次写入都记进账本，都能单条回滚或删除。`AGENTS.md` 的自学习约束也按这个决定改了（见第 11 节）。

## 2. 参考了什么

对照 Hermes（`9fc7f179`）和 OpenClaw（`37259b7c`）的上游实现，调研材料在会话临时目录，不进仓库。

采用的做法：
- 由结构化阈值触发：本轮工具迭代次数达到阈值才起后台总结；子代理、定时/后台回合和异常结束的回合不触发。
- 默认不改：大多数任务不需要新 Skill，拿不准就 skip。不照搬 Hermes“每次都要学点什么”的配额式提示。
- 所有权结构化：自动流程只改自己生成、且用户没改过的 Skill。内置、共享、插件、工作区和用户手写的 Skill 一律不碰。
- 只改本轮用过的：更新目标必须是本轮 `skill_search get` 成功读过的自学 Skill，相当于 OpenClaw 的“使用回执”。
- 哈希绑定：更新时核对磁盘上的 Skill 仍是登记表记录的版本，不一致就拒绝。
- 写入前检查：脱敏、解析检查、`agent_generated` 来源的 guard 扫描（不 force）。OpenClaw auto 模式不扫描，Hermes 默认关掉 agent 生成内容的扫描，这两处都不照搬。
- 逐条账本，每个版本都留全文，能单条回滚。
- 不记环境故障、“某工具不能用”这类负面结论和没解决的失败。

不采用的做法：给模型开写文件工具让它直接改 Skill；用“使用次数为 0”自动淘汰；关键约束只写在提示词里。

## 3. 总体流程

```text
主代理回合正常结束且任务完成（结构化 turn_end_reason + conversation_task_completed）
  -> FinalizationService 按结构化条件判定（工具轮数、来源、作用域）
  -> 写一条有界请求：<owner_home>/data/skill_learning/requests/<时间>-<键>.json
Gateway 后台记忆整理车道（同一个有界线程池、同一套 owner 轮转）
  -> SkillLearningService.run_pending()：非阻塞运行锁，前台模型在忙就顺延，超每日上限就顺延
  -> 取最早一条请求，组装材料（任务摘要 + 现有 Skill 索引 + 本轮用过的自学 Skill 正文）
  -> 无工具结构化调用 owner 当前选定模型（与记忆整理同一 backend）
  -> 严格解析输出：create / update / skip
  -> 自动闸门 -> 发布到 <owner_home>/skills/learned/<name>/SKILL.md
  -> 更新登记表、写账本、保存版本全文、删除请求
下一轮 SkillsService 快照自然看到新 Skill（逐轮快照，无需重启）
```

后台处理不阻塞用户回复。Gateway 没运行时请求留在磁盘上，下次运行再处理。

## 4. 触发条件

只看结构化事实，不看回复正文。以下全部满足才入队：

- `enable_self_learning=true`：组合根只在开启时装配 `agent.skill_learning`。
- 本轮任务已完成：`conversation_task_completed(task_attributes)`，与记忆整理请求同一判据。
- `do_save=true`，`context_scope != task_local`（排除子代理）。
- `source != background_main_agent`：后台唤醒、定时和 Goal 续跑回合先不学，和上游一致。
- `tool_rounds >= self_learning_min_tool_rounds`（默认 6）。

入队失败只影响本次学习，不影响回复和收口。待处理请求上限 20 条，满了就丢弃新请求，并在账本记 `SKILL_LEARNING_QUEUE_FULL`。

## 5. 请求材料

请求在收口时一次写成，之后只读。字段：

- 身份：`request_key`、`request_id`、`run_id`、`task_id`、`thread_id`、`source`、`tool_rounds`、`created_at`。
- `user_prompt`：用户本轮输入，脱敏后截断到 3000 字。
- `final_response`：最终回复，脱敏后截断到 3000 字。
- `tool_trace`：最多 80 条 `{round, tool, ok, error_code, args}`。`args` 取模型可见参数，经 `redact_sensitive_value` 脱敏后截断到 300 字。
- `used_skill_ids`：本轮成功的 `skill_search action=get` 调用里的 `skill_id`。

请求只在 owner 自己的数据目录里过渡，处理完即删。它不是对话或工具归档的第二份权威，只是交给后台的一份有界工作单。

## 6. 后台执行

- 执行位置：Gateway 后台记忆整理车道。`_safe_run_curator` 在记忆整理之后顺带调用 `run_pending()`；owner 有待处理请求时，即使记忆总闸关了或当日记忆配额用完，也会被准入。准入时照原条件决定要不要跑记忆整理本身，两件事互不影响。`memory_curator_enabled=false` 而自学习开着时，车道仍会为自学习运转。
- 并发：`<dir>/.run` 非阻塞锁（线程锁 + OS 文件锁），拿不到就返回 busy。进程崩溃时锁自动释放。登记表、账本、队列的读改写用 `<dir>/.state` 短锁。
- 让路：前台模型在同一端点上有请求时（`foreground_model_active`）返回 busy，下次再试。
- 每日上限：`self_learning_daily_limit`（默认 20 次模型调用，0 为不限）。计数按 UTC 日期存在登记表里，发请求前加一。
- 失败重试：模型调用失败或超时，请求上的 `attempts` 加一，保留等下次。达到 2 次后丢弃并记 `failed`。输出不合格属于确定性失败，直接记 `rejected`，不重试。
- 超时：`self_learning_timeout_seconds`（默认 180 秒）。复用 `call_backend_with_timeout`，同一 backend 的旧请求没退出前不叠加新请求。

## 7. 模型输出合同

schema 为 `my-agent.skill-learning-output.v1`，扁平对象，所有字段必填（兼容供应商严格 JSON schema）：

| 字段 | 约束 |
|------|------|
| `decision` | `create` / `update` / `skip` |
| `reason` | ≤200 字，只供账本给人看 |
| `update_target` | update 时必须是材料里 `updatable_skills` 列出的名字，其它情况为空串 |
| `name` | `^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`；update 时必须等于 `update_target` |
| `description` | 1–200 字单行 |
| `when_to_use` | ≤300 字单行 |
| `tags` | ≤6 个，每个 ≤32 字，`[a-z0-9-]` |
| `body` | 80–12000 字 Markdown |

skip 时其余字段可为空。宿主只按 `decision` 这个枚举分流；`reason` 和正文都不参与机器判断。

## 8. 自动闸门（代替人工确认）

按固定顺序执行，除第 6 步只改写不拒绝外，任一不过就记 `rejected` 和结果码，不写 Skill：

1. 输出合同：字段、长度、名字格式，以及 update 目标是否在可更新集合里（`SKILL_LEARNING_OUTPUT_INVALID`）。
2. 重名：create 的名字不能和当前快照里任何来源的 Skill 同名，`learned/<name>` 目录不能已存在，也不能在用户删过的名单里（`SKILL_LEARNING_NAME_TAKEN` / `SKILL_LEARNING_NAME_BLOCKED`）。这样既不会遮蔽内置 Skill，也不会覆盖用户 Skill。
3. 数量上限：已登记的自学 Skill 达到 `self_learning_max_skills`（默认 50，0 为不限）时拒绝新建，更新不受限（`SKILL_LEARNING_LIMIT_REACHED`）。
4. 所有权与哈希：update 目标必须在登记表里，且磁盘 SKILL.md 的 sha256 等于登记值。用户改过就视为用户所有，永不自动覆盖（`SKILL_LEARNING_TARGET_USER_OWNED`）。
5. 渲染与解析：在临时目录写 SKILL.md，用 `parse_skill_file(require_frontmatter=True)` 解析，核对 name、description、when_to_use、tags 没被解析器改写（`SKILL_LEARNING_DRAFT_INVALID`）。解析器会把 `#` 之后当注释、剥掉首尾引号、把 `[` 开头的值当列表，所以输出解析时就把这些字符换成全角或去掉；空的 when_to_use 和 tags 整行不写。
6. 脱敏：description、when_to_use、正文先过 `redact_sensitive_text(text, code_file=True)`。它遮蔽已知密钥形态、Authorization 值、URL 口令和私钥，不误伤普通赋值示例。发布脱敏后的文本，事件带 `SKILL_LEARNING_REDACTED` 供人核查。没有选择拒绝，是因为 `Authorization: Bearer <token>` 这类占位符也会命中，拒绝会丢掉有用的 Skill；脱敏后磁盘上同样不留密钥。
7. 安全扫描：`scan_skill(source="agent_generated")` + `install_decision(force=False)`，caution 和 dangerous 都拒绝（`SKILL_LEARNING_GUARD_BLOCKED`）。SkillsService 加载 owner Skill 时按 external 信任级再扫一次，策略相同，所以通过发布的 Skill 一定能被加载。注意这条规则对 `sudo`、提到 `AGENTS.md`/`CLAUDE.md`、`subprocess.run(`、`crontab` 等都会拦截；这是现有产品策略，本期不放宽。

通过后发布：新建时把整个临时目录 `os.replace` 到 `skills/learned/<name>/`；更新时在同目录写临时文件再原子替换 SKILL.md。每个发布版本的全文都存到 `versions/<name>/v<N>.md`，每个 Skill 最多保留 5 个版本。

## 9. 存储与所有权

唯一权威目录是 `<owner_home>/data/skill_learning/`，由 owner 路径解析器登记为 `owner_skill_learning_dir`：

- `requests/`：待处理请求（过渡）。
- `registry.json`：自学 Skill 的唯一登记表（schema `my-agent.skill-learning-registry.v1`），记录每个 Skill 的名字、版本、sha256、创建和更新时间、最近来源 run，以及用户删过的名字 `blocked_names` 和每日计数。
- `ledger.jsonl`：只追加的账本（schema `my-agent.skill-learning-event.v1`）。事件有 `published` / `updated` / `skipped` / `rejected` / `failed` / `dropped` / `reverted` / `removed`，只含 ID、名字、版本、hash、结果码和有界的 `reason`，不含正文。
- `versions/<name>/v<N>.md`：版本全文，供回滚用。
- `removed/<name>-<时间>/`：用户删除的 Skill 移到这里，不物理删除。

发布位置是 `<owner_home>/skills/learned/<name>/SKILL.md`。SkillsService 的 owner 根会递归扫描，目录层级推导出 category `learned`。frontmatter 只写 `name`、`description`、`when_to_use`、`tags`，不在 Skill 里写来源标记；“是不是自学 Skill”只由登记表回答。

## 10. 用户入口

`my-agent skills learned` 只读登记表和账本，不需要 Agent，也不调模型：

- `list [--json]`：列出自学 Skill（名字、版本、更新时间、状态）以及待处理请求数和今日调用数。状态有 `active`、`user_modified`（sha 不符，自动流程不再改）、`missing`（被手动删了）。
- `show <name> [--json]`：登记信息、最近 20 条账本事件、当前路径。
- `revert <name> [--json]`：回到上一个版本；只有 v1 时等同 remove。用户改过的拒绝（`SKILL_LEARNING_USER_MODIFIED`）。
- `remove <name> [--json]`：移到 `removed/`，从登记表去掉，名字加入 `blocked_names`，以后不再自动新建同名 Skill。

不给模型注册任何 learned 管理工具。

## 11. 规则变化和 S1 的调整

- `AGENTS.md`「自学习功能约束」改为：默认关闭；开启后自动总结的 Skill 经第 8 节闸门直接发布；只写 `skills/learned/` 和 S1 的 `lesson-*`；不修改非自学 Skill；每次变更留账、可回滚。依据是用户 2026-09-26 的决定。
- S1：`enable_self_learning` 开启时，新生成的子代理 lesson 提案立即走原 `confirm` 全链（版本、草稿 hash、来源 Candidate、目标不存在、解析、guard），回执标 `confirmed_by=auto`，不再等用户。已经在待确认状态的旧提案仍可用 CLI 处理。S2 只排待确认提案，没有待确认时不起请求。

## 12. 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `enable_self_learning` | false | 总开关（原有），同时管 S1 自动确认和 S3 自动总结 |
| `self_learning_min_tool_rounds` | 6 | 本轮工具轮数达到它才入队 |
| `self_learning_daily_limit` | 20 | 每 owner 每日模型调用上限，0 不限 |
| `self_learning_max_skills` | 50 | 自学 Skill 数量上限（只限新建），0 不限 |
| `self_learning_timeout_seconds` | 180 | 单次总结调用超时 |

YAML 写中文注释，AgentConfig 写默认值，整数走 `_TIMEOUT_INT_FIELDS` 规范化。

## 13. 边界（本期不做）

- 不生成 references、templates、scripts 这类支持文件，只写 SKILL.md。
- 不从后台回合、子代理回合学习（子代理仍走 S1）。
- 不做使用统计、自动合并或按闲置时长归档。
- 不改 SkillsService、快照和 skill_search。Codex 的能力内化正在改这些，本功能只读 `snapshot_for()` 的 entries。

## 14. 测试和验收

- 单测（fake backend，不联网）：触发条件的每条结构化判据；请求有界且脱敏；create、update、skip；各闸门拒绝码；用户改过的不覆盖；重名与删过的名字；数量上限；每日上限；前台忙和运行锁忙时顺延；失败重试两次后丢弃；revert 和 remove；S1 自动确认；Gateway 车道准入（记忆关闭但有请求时只跑自学习）；配置规范化。
- 真实验收：在隔离 home 和隔离 Gateway（8432）上发一个需要多轮工具的真实任务，观察请求入队、后台总结、账本、`skills/learned/` 文件，以及下一轮 `skill_search` 能搜到新 Skill。测试者只发一次 prompt，只看被测对象的日志和产物。

## 15. 后续方向

- 使用回执与统计：记录 `skill_search get` 命中和之后的任务结果，为合并和退役提供证据。
- 定期整理：相似自学 Skill 合并，长期没用的归档（不删除）。
- 后台 Goal 回合学习，以及支持文件。
