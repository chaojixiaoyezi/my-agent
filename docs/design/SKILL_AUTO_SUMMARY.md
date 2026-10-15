# 自学习 S3：自动总结 Skill

状态：已合入 main 并双机部署（2026-09-26），隔离真实验收通过（过程与发现见 TESTS.md 顶部“自学习 S3”节）；`enable_self_learning` 仓库默认仍为 false。本文是自动总结 Skill 的唯一模块设计；`DESIGN_LEDGER.md` 只保留摘要和链接。

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
- 不记绕过安全策略、权限、审批或拦截的做法。真实验收里模型曾把“删除命令被拦截就改用 apply_patch 删”写进 Skill；这只能靠提示词软约束（不按正文做机器判断），更新时也要求删掉旧正文里的这类内容。

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
- 宿主会话：后台线程没有前台的会话上下文，而部分服务商要求每个请求带会话编号头，没有就在发送前拒绝（真实验收里首次暴露为 `ValueError`）。所以调用外面按 owner_id + `skill-learning:<请求键>` 自绑 `provider_session_scope`，与记忆整理同一原语；同一请求的重试共用同一会话值，不冒用前台线程会话。

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

**TUI 与 IM 入口（2026-09-27）**：用户几乎不用命令行，所以同样的操作也能在 TUI 和飞书等 IM 里用聊天命令完成，经 Gateway 控制通道执行，
只作用于当前范围解析出的 owner（与 `/model` 同一解析），回执不含本机路径：

- `/skills`：待确认的技能提案（有审核顺序建议时按其排列并标注）加自动总结 Skill 的数量；
- `/skills proposals [all]`、`/skills show <提案编号>`、`/skills confirm <提案编号> <版本>`、`/skills reject <提案编号> <版本>`：
  提案编号可用至少 6 位的前缀，不唯一时要求多输入几位；确认和拒绝必须带列表或详情里显示的当前版本，服务端锁内复核，对不上就拒绝；
- `/skills learned`、`/skills learned show|revert|remove <名称>`：名字按生成合同的安全名字规则校验，先在登记表里精确查找，不用它拼路径。

事实推导与回滚/删除抽到 `capability/skill_learning_report.py`，CLI 与聊天共用；执行入口 `gateway_parts/skill_control_service.py`。
自学习审核顺序点（skill_proposal_review）也因此能在 TUI/IM 里触发：列出 2—30 条待确认提案时调用，与 CLI 同一入口。

失败回执只说能证实的事：编号不对、版本对不上、登记表里没有这类可预期失败都发生在改动之前，回执说明原因；确认、拒绝、回滚、
删除遇到其它异常时，改动可能已经生效（例如删除已归档目录并写好登记表，追加账本才失败），回执只说结果没能完整确认、请先用
`/skills` 或 `/skills learned` 查看当前状态再决定是否重试，不承诺“没有改动”；只读子命令的异常只说暂时读不到。

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
- 自然采用：真实验收中，相关的 learned Skill 在提示词索引里，模型仍可能不读就直接做（第 4 轮），用户提示后才读（第 5 轮）。展示和采用规则属于通用 Skill 路由，不为 learned Skill 加专项分支；先用线上请求里的 `used_skill_ids` 观察命中率，再决定是否做通用调整。
- 定期整理：相似自学 Skill 合并，长期没用的归档（不删除）。
- 后台 Goal 回合学习，以及支持文件。

## 16. 学习后维护评估（2026-10-14，未实施）

本节回应自动升降、闲置淘汰与持续更新的需求。核对 main `56617f7a8`，只使用隔离目录和 fake backend；没有读取生产学习台账，不能据源码断言具体用户未更新的现场原因。第 1—15 节记录已有行为，本节不改变发布策略。

### 16.1 当前事实与缺口

| 项目 | 当前可证实行为 | 尚缺的部分 |
|---|---|---|
| 自动生成 | 开关开启后，完成主任务满足阈值时排队总结，模型返回 create/update/skip | 没有独立的试用成熟度阶段，格式/安全闸不证明方法有效 |
| 自动更新 | `_material/_updatable` 只纳入本轮成功 get、登记在册、路径与 hash 一致的 owner learned Skill，最多三个候选 | 后台/定时/Goal 回合被排除，未重读的既有方法不在集合；只输出一个更新目标；缺少跨任务证据聚合 |
| 子代理学习 | S1 从来源 Candidate 生成 `lesson-<正文 hash>`，安装后写原提案 committed | 不进入 S3 learned 登记，改写经验正文会得到另一名字，缺统一后续维护/版本链 |
| 使用观察 | 有工具读取账、方法沿用记录与 `used_skill_ids` | 读取不等于采用，任务完成不等于该版本方法有效；没有持久的版本质量统计与自动升降 |
| 移除/回滚 | 用户可 remove/revert；remove 归档目录并阻止同名自动重建；保留最多五份版本全文 | 没有自动冷藏、闲置归档、自动恢复和固定保护的闭合生命周期；不得把人工删除名单直接用作自动冷藏 |
| 容量 | 默认最多新建 50 个 S3 learned，达到上限拒新建，更新不受限 | 不会自动整理以腾出活跃名额；S1 lesson 路径不受这份数量上限统管 |

已有手写、内置、共享、插件/能力包 Skill 由原来源和授权管理。私有自动学习不得覆盖这些文件，也不能因使用效果好就自动获得共享发布资格或管理员工具权限。

### 16.2 已复现的更新与回滚交错问题

复现通过原 `SkillLearningService.run_pending()` 和发布/回滚函数，复用现有测试 helper，仅在临时 home 内：

1. 自动创建 v1，使用后正常更新到 v2，保留历史 v2 内容。
2. 下一次总结读取 v2 材料；fake backend 在返回结果前模拟用户执行 revert，登记及正文回到 v1。
3. backend 返回基于原 v2 的 update；结果为 `updated`，当前又变成 v2，用户回滚的内容未保留，历史 `versions/v2.md` 被新正文覆盖。

结果摘要：`summary_input_version=2`、`version_after_user_revert=1`、`final_version=2`、`user_reverted_content_preserved=false`、`old_v2_archive_overwritten=true`。
当前 `_update_locked` 检查的是提交时磁盘与当时登记一致，没有核对总结开始时的版本/hash；回滚将版本减一，后续更新再加一会复用版本文件名。
这是隔离机制复现，非生产事故取证。应先补提交期 expected revision/hash 校验和不可变版本身份，再扩大自动更新范围。

### 16.3 推荐维护语义

- 分开质量和热度：新学方法处于试用；不同任务中存在可核验的有效采用后提高推荐；确认失效时降低推荐或停用。没被使用表示证据不足，不能直接判质量差；相关性仍是召回首要条件，不能让高频 Skill 占据所有任务。
- 使用与结果事实沿原工具/任务/方法账登记准确 owner、skill stable ID、内容 hash、run/task/job、读取来源与验证引用；同一 run 重试去重，周期任务连续成功不能刷成大量独立质量证据。无法归因时记未知，不将整轮成功率等同 Skill 成功率。
- 网络不可达、凭据过期、权限拒绝、限流或用户取消不直接触发方法降级。模型可建议原因与修改内容，状态变更必须依赖结构化引用、已授权策略与可验证事实，不从最终回复里的“成功/失败”词语作裁决。
- 闲置先冷藏（降低默认展示、仍可发现），再按保留期可恢复归档；不默认永久删除。用户固定、正在运行/等待恢复任务引用，以及未删除的定时/监控引用（包括暂停、年度和远期规则）均不因低频自动淘汰。观测数据读不到时不归档，历史条目初次纳管先建立观察基线。
- 恢复只恢复原身份和授权范围；已被用户停用、删除或撤销授权的条目不得因使用统计自动复活。用户删除与自动冷藏分别记账，保留稳定来源和身份，不能靠改名绕过已明确删除的对象。
- 更新优先改已有自学方法，保留旧版，候选通过原解析、脱敏、guard 和可执行验证后按选定策略切换；缺少效果证据的新内容保持试用。更新不改变当前在途任务冻结的版本；不能仅依赖原子替换文件保证旧快照还能读到旧内容。
- S1 lesson 和 S3 learned 复用同一维护权威，S1 原提案继续只作为来源，不再建另一套学习库。通过原 committed 记录、精确目标路径与 hash 显式迁移；来源不明、用户已改过的内容保持受保护，不因目录名为 lesson 就认定可自动改。
- 同类方法可以提出合并候选，但先验证新版本和原引用关系，不凭标题相似就删除旧方法。手写、管理员发布、公共能力包仍归原发布方；用户/群可反馈和保留获准的私有改进，不修改他人或公共源。

### 16.4 复用位置、规模与发布边界

复用 `SkillLearningStore` 的登记、事件、队列与版本，`SkillsService/SkillSnapshot` 的索引/读取，原后台车道和 `/skills learned` 控制；观察汇总只是投影，不能成为另一份权限或内容事实源。自动维护数据与策略不写进可由 Skill 正文伪造的 frontmatter。

版本修复优先考虑单调修订和不可变正文 hash：回滚激活旧内容但不抹掉已发布修订；学习候选冻结输入 revision/hash，提交冲突就丢弃或重新评估，不能覆盖用户在学习期间的回滚/编辑/停用。活跃引用的版本不被固定“五份”清理策略误删。

几十万员工场景应按有新事件的 owner 增量排队、错峰、有界清理；统计和冷藏规则不逐条调用模型，仅内容更新/合并候选才消耗受限模型预算。同一失败原因有退避和去重，部署级总预算与现有 owner 日限额一起守门，不为每名员工新建常驻线程或每天扫描全部正文。

新维护会影响模型内容、存储和后台成本，应有显式可配置策略，默认先观察；不能因原 `enable_self_learning=true` 就默默开启自动归档。配置位置沿现有自学习配置权威，不增加第二个同义开关；细项和默认阈值待真实使用分布核对后确定，不提前写死“闲置 N 天必删”。

本会话给出的 AGENTS 文本仍要求发布前确认；当前仓库 AGENTS 与 2026-09-26 设计/代码却记录开启后自动发布。此次只记录差异、不改运行策略；实施时按最新用户决定统一文档和机器配置，内容发布、推荐调整与可恢复归档分别有明确边界。

### 16.5 参考与验收建议

已检查 Hermes 合同索引，未找到精确生命周期模块条目；直接读本机 `0a62610f1` 的 `tools/skill_usage.py`、`tools/skill_provenance.py`、`agent/curator.py`。可复用其来源与托管授权分离、固定条目/定时引用保护、可恢复归档/恢复模式；不照搬内置 Skill 清理开关，不把其 view/use/patch 活跃计数当方法正确性的证明。

本轮现有 `test_skill_learning.py` 与 `test_skill_learning_integration.py` 通过；另有上述临时故障注入确认未覆盖缺口。没有新建或修改测试、没有真实模型或生产 owner 验收。
后续至少验证：读取未采用不刷质量、环境故障不误降、年度/暂停监控不误归档、S1/S3 迁移不接管用户文件、自动归档可恢复、用户删除不复活、公共包不可改、普通/群不能影响他人、原始和编码凭据不进入候选/版本/账本、在途旧版本可读、并发回滚/更新拒旧结果、撤权后禁止继续使用、进程重启不重复发布、总预算及冷却实际生效。

## 17. 满额后的学习、复刻与定向分享（2026-10-14，讨论方案，未实施）

本节只讨论容量与接收语义，不表示助手复刻或跨 owner Skill 分享已经实现，也不授权复制任何真实用户数据。

### 17.1 当前 50 个的准确含义

`self_learning_max_skills=50` 在 `_require_new_name()` 内按 `SkillLearningRegistry.skills` 的数量限制 S3 新建；满额返回 `SKILL_LEARNING_LIMIT_REACHED`，不自动腾位。更新同一登记条目不经过这条新建数量门。当前这不是全库、全部来源或所有导入入口的统一配额，S1 lesson 也不受这份数量上限统管。本轮重新执行现有 `test_max_skills_limits_creation_only` 通过；未测试不存在的复刻/分享功能。

### 17.2 建议分开三种预算

| 预算 | 作用 | 满额处理 |
|---|---|---|
| 常用推荐数量 | 控制默认索引/推荐的体量，50 可作为候选默认值，不是已经测定的最佳值 | 同源去重、按相关性/使用事实调整；不删除正文，不禁止从完整授权目录搜索/显式读取 |
| Owner 持久存储配额 | 所有私有接收入口共用，计入唯一条目、实际资源/版本字节、归档和有界暂存；保留部署级总预算 | 真正硬限制；接收前预检和提交前原子复核，不接半份、不无限排队、不默默丢旧能力 |
| 单任务加载预算 | 按需读取本轮相关方法和资源，控制实际上下文；库的大小不等于请求体大小 | 沿现有索引、分页、读取和上下文机制选择，不将整库/整份复刻塞进 prompt |

这是建议的新语义，不能原地把旧 `self_learning_max_skills` 偷换成“常用推荐数”。实施时需明确配置迁移和唯一计数权威，沿原学习登记、SkillsService 和 owner 配额实现，不再创建并行能力库。

推荐名单之外的普通已保存 Skill 仍可搜索和按需使用，不等同被停用、撤权或归档；归档继续是第 16 节单独的可恢复维护动作。用户固定及运行/监控引用不因新导入被挤掉；若固定项已经占满常用名额，新能力仍能保存并在当前任务按预算读取，不承诺强行加入常用名单。已有超额或配额下调只阻止新增超量提交，不能自动截断现有能力。

### 17.3 三个入口共用接收规则

| 场景 | 常用名额已满时建议行为 | 硬存储配额不足时建议行为 |
|---|---|---|
| 用户要求学新方法 | 先辨认是否原方法更新；新的合格方法保存入库，当前任务可按需使用；常用推荐按策略调整 | 清楚说明未保存及不足的配额，不假称学会；自动学习采用有界延后/停止，不循环浪费模型调用 |
| A 向群投递若干 Skill | 按准确来源/版本接收完整允许分享的正文和资源，沿原检查入库；重复条目不占新的唯一条目数；不必用模型重写一遍 | 接收前报出本次所需量；不通过另一路目录写入绕过同一 owner 配额 |
| A 向群进行一次助手复刻 | 先冻结授权范围内的快照与清单，预检完整导入量；保存全部约定内容，再按目标场景选常用项，其余保持可发现 | 提交前停止，不出现只复制前 50 个却报完整复刻；不消耗成功复刻次数，重试仍关联原操作 |

例：群已有 50 个唯一 Skill，收到 3 个，其中 1 个是同一来源/修订且内容完全相同，存储配额足够时是库内 52 个、常用最多 50 个，而非删除 2 个旧能力。相同名字不算重复；不同来源或正文冲突要保留准确身份并报告，不能静默覆盖。语义相近只能成为合并候选，不是机械删除依据。

同一 Skill 的多个修订不占多个常用名额，但正文/资源历史仍占存储；公共包沿现有授权引用发现，不为每个用户复制一份计作自学条目，其目录暴露和加载仍受相应预算。去重不能向未授权 owner 泄露另一人的条目是否存在。

### 17.4 复刻与分享必须保持的边界

- “每个 agent 只能复刻一次”建议定义为接收方稳定身份的一次成功初始化导入，计数由宿主记录，不能因换会话、重试或进程重启重置；本节仅提出此解释，尚非用户确认的最终产品语义。失败不记成功，重复提交不重复计数；源助手是否限制分享次数属于另一项规则，不混入这个次数。
- 复刻应固定源版本，导入后默认独立演化；源方随后更新、删除不静默改变群的副本。不允许转移的公共/受管内容继续沿原授权引用，不能借“复刻副本”逃逸撤权；引用式共享和已授权独立副本的撤销语义需分别记录。
- Skill 与记忆分别计量、校验、报告。记忆迁入群相当于向群授权可见数据，不能把全部私聊历史、凭据、他人数据、权限和管理员配置一并搬入；已有群记忆不被默默覆盖。所谓完整复刻只能指约定的可迁移清单，不能承诺复制权限和一切记忆。
- 分享须由发送方有权导出的内容进入目标已授权接收入口；群助手不能根据一句转述就直接读取 A 的私人 owner 目录。正文、脚本和资源仍是需检查的内容，不因熟人或另一个 agent 发来就取得执行授权。
- 容量预留、暂存校验、激活和成功次数沿同一原子提交/幂等记录；并发学习与导入不能各自通过旧余量后一起超配。保护被引用版本，失败留明确终态；不将配额不足伪装为“没有可学内容”。

后续验收重点：50 满额接收新方法、全部常用项被固定、同源重复与同名冲突、批量超过常用上限、实际存储不足、归档/版本/暂存计费、并发导入抢余量、崩溃重试不半份复刻、不重复计成功、个人与群隔离、记忆不越权及凭据不随 Skill/资源迁移。本轮只有源码与既有用例核对、设计记录，没有新建接收接口或调整配额。
