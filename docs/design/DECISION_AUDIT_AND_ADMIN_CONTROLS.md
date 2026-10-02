# 决策开关、超时自调、统一审计与管理员管控

状态：2026-09-25 本地实施（分支 `claude/decision-audit-controls`，基于 main `07fa00fb3`），组件与组合测试、24 个变异、
真实 TUI 验收（见文末）均通过；已合入 main `2093631e5` 并上线。
来源：用户经主线会话转交的 6 项决策/审计要求（2026-09-25 17:5x）。起因是当天 17:10 的真实会话里，
代理 grep 日志得出"决策模型从没被调用、typesafe_decision 不存在"的错误结论——根因是没有结构化的审计入口。

## 1. 每个接入点的"开启 / 观察模式"

`/model` → 决策模型设置 → 某个接入点 → 模式，原来是三选一单选（关闭/只观察/应用），现在是两个勾选：

| 开启 | 观察模式 | 保存的模式 |
| --- | --- | --- |
| 不勾 | 任意 | `off`（不调用） |
| 勾 | 勾 | `observe`（只记录建议，不采用） |
| 勾 | 不勾 | `apply`（正式使用，采用前仍复核） |

存储值仍只有 `off/observe/apply` 三个（`points.<点>.mode`），设置服务、CAS、继承与恢复都不变，只改界面呈现。
新开启的点默认勾着观察模式（先观察再正式用）。实现：`cli/chat_parts/tui_decision_menu.py` 的 `_mode_control`。

**随包默认打开的前提**（J12，2026-10-02）：用户自己在菜单里开哪个点不受限制。但仓库里把某个点位的随包默认模式改成非 `off`，必须先在[决策质量基准](../../scripts/bench/decision_quality/README.md)里有登记成绩，且该成绩的用例与材料摘要都和当前一致、达到该点位阈值；`test_decision_quality_bench.py` 强制检查。

## 2. 决策超时可调，my-agent 可在上下限内自调

- 用户在菜单里改等待时间不受限（仍须有限正秒数）。
- my-agent 经原 `user_config` 工具的 `decision_patch` 调整本人 owner（或当前可信会话）的等待时间，
  所有 `timeout_seconds` 字段（通用前台/阶段/后台与各接入点）必须落在
  `capability_config.yaml` 的 `decision_agent_timeout_min_seconds`（默认 1）与 `decision_agent_timeout_max_seconds`（默认 30）之间，
  0 表示该侧不限制。越界直接拒绝（`DECISION_TIMEOUT_OUT_OF_BOUNDS`），不夹取、不保存同批其它字段；
  `decision_read` 回显 `agent_timeout_bounds`，模型先知道能调的范围。
- 请求字段（2026-09-28 修）：`decision_patch` 只接受 `scope`、`expected_revision`、`changes`，`decision_reset` 只接受
  `scope`、`expected_revision`、`fields`。`user_config` 各动作共用一份扁平 schema，`reason` 等别的动作的字段也能过 schema；
  多带时设置服务仍整笔拒绝、不替模型删字段，但回执（`TOOL_INVALID_ARGUMENTS`，未写入）现在给出
  `unknown_fields` 与 `allowed_fields`，模型按回执删掉字段即可重试。此前回执只说“包含未知字段”，生产上连续被拒三次。
- 选这个入口而不是给 `manage_models` 加动作：`user_config` 本来就是决策设置的模型入口（普通用户也有，只能改自己），
  少一个工具、少一套身份裁决。

## 3. "每条消息都问一次选模型"的评估与精简

事实（主线 17:5x 核对用户会话）：当天 23 次 Jev 调用，完成 9、超时 14；已报输入 50,849 token（完成的 9 次平均约 5.6k，
近期单次约 9k，随会话摘要增长）；前台默认单次等待 2.0 秒，而当天上午 Jev 探针耗时 2.9 秒。

评估：
- 选模型观察在主请求准备阶段同步执行，每条消息最多多等一次单次期限（默认 2 秒）；observe 模式下结果不被采用，
  所以这段等待和 token 只换来一条观察记录。
- 2 秒低于 Jev 实测延迟，约六成调用超时：超时的请求供应商多半照样处理（输入照计费）但没有产出，是最大的浪费。
- 输入里最大的一块是会话摘要：语义摘要之外还带着最多 6000 字符的"原文锚点段"（历史用户原话/最终答复），
  对"下一条消息该用哪个模型"几乎没有增量；14 个候选每个都重复同样两句"容量/工具尚未核对"。

结论：按现状"每条都问"不划算；精简材料后可以保留，但等待时间应与 Jev 实际延迟匹配（见第 2 节，my-agent 现在能自调），
提问频率是否改为"只在结构性变化时问"（新会话、压缩之后、模型目录变化）需要用户拍板，本批不改。
（2026-10-01 已按用户确认实现为可选节奏，见下面“询问节奏”，仓库默认仍每轮都问。）

已做的精简（只删材料，不改"采用前必须复核"：apply 指令、候选冻结、首请求容量/工具/版本核对全部不变）：
- 摘要只带语义部分（去掉原文锚点段），最多 `decision_model_selection_summary_max_chars`（默认 1500）字符；
- 当前消息最多 `decision_model_selection_prompt_max_chars`（默认 4000）字符；
- 截断时在 `state.input_completeness` 里如实写 `truncated`、原长与保留长度，锚点段标 `omitted`，不冒充完整输入；
- 候选公共声明（`capacity_status`、`tool_support`）只在 `state.candidate_facts` 写一次。

效果（14 个候选、3000 字语义摘要 + 6000 字锚点段的合成输入，按真实编码与 `estimate_tokens` 估算）：约 7.5k → 2.7k token/次，减少约 63%。

### 询问节奏（J4，2026-10-01）

- **开关**：选模型点的专属字段 `points.model_selection.cadence`，配置默认 `decision_model_selection_cadence: every_turn`（每轮都问，与以前一样）；
  `structure_change` 时只在结构变化才问。owner 与会话两层都能覆盖：TUI 决策菜单“逐接入点设置 · 询问节奏”，或让 my-agent 用 `user_config` 的 `decision_patch`。
- **结构**：会话的压缩代数 + 候选目录版本（公开候选摘要的哈希）+ 本请求冻结的当前模型档案。会话从没成功问过（新会话）也算变化。
- **判定**：结构与上次成功询问相同就不提交、不调用决策模型，请求标记记 `skipped / structure_unchanged`，到达诊断记同名原因（菜单与 `audit_records` 显示大白话）。
  只在成功拿到回答时记指纹（observe 转后台的在后台完成时记），失败、超时下一轮照常再问。
- **存放**：owner 决策数据目录下的 `model_selection_structure.json`（与结果日志同目录），按会话最多 500 条，坏文件按“没问过”处理。
  `every_turn` 时不读不写这个文件。
- **真实验收**（隔离 home、真实 Gateway + TUI + Jev，打开观察不挡回复）：同一会话 3 条消息只问 1 次；`/model` 换成 M3 后再问；
  新会话第一条问、第二条跳过；Jev 共 3 次调用，请求标记与到达计数（called 3、structure_unchanged 3）一致。

## 4. TUI 统计行：决策段分开标注数据来源（2026-09-28 修订）

格式：`决策 已报 ≈N token · 估算 M token（未完成）· 缺报 K · 成功 X · 失败 Y · 未发出 Z`，没有的段不显示。

**Token 来源：**
- **已报**：供应商回报的输入，按已报调用的平均值外推到全部成功调用；如果有没报输入的成功调用，数字前加 `≈`。
  原先按全部调用外推，超时调用也被按平均值算了一遍；现在未完成的调用另有估算，不再参与外推。
- **估算（未完成）**：发出去之后超时或失败的调用，取发送前的本地估算输入（`usage_breakdown.estimated.unfinished_*`），不是供应商回报。
- **缺报**：只指真的一点数据都没有的调用，有两种：
  - 成功了但一次都没报输入，也没有已报调用可以外推；
  - 链路计时上线前的旧账失败，分不清当时有没有发出请求。
  这类调用不显示成 0。

**次数：**
- 成功 X 取 `finished`。
- 失败 Y 只算发出去之后失败或超时的；旧账分不清是否发出的，也算失败。
- 未发出 Z 是一次 HTTP 尝试都没有的失败，例如准入忙、发送前期限就用完了，不计入失败。
- 进行中的调用都不计。

**非决策调用（LLM 段，2026-09-28 统一）：** 主模型、辅助调用与决策段同一口径，数字是全部用途的原始累计减去决策分区，
同一次调用不会在两段都出现。格式：`LLM 估算 N token（未完成） · 未发出 M · 缺报 K`，没有的段不显示。
- 估算（未完成）：发出去之后超时或失败的，取发送前的本地估算（主模型是发送前的可见上下文估算，辅助调用是请求材料估算）。
- 未发出：一次 HTTP 尝试都没有就结束的，例如准入等待中超时、发送前就失败。
- 缺报：只剩旧账里分不清是否发出的失败。成功但供应商没回报用量的调用，已按本地估算计入会话累计（数字前带 `~`），
  不再算缺报。原先的 `unreported_calls`（没有供应商回报就计）不再有读取方，已删除。

**口径实现：**
- 口径由 `conversation/model_metrics.py` 的 `unfinished_usage_facts` / `split_unsent_failures` 统一给出，`audit_records` 也用这两个函数。
- 汇总只累加原始次数，展示时才推导。原因是迟到的尝试可能落在后一条用量事件里，逐条推导会算错。
- 时间窗边界：迟到的尝试事件还没记进账本或用量快照之前，这次调用会暂时显示成“未发出”；等尝试补记进来，才转成失败并带上估算。统计行和 audit_records 用量行都是这样。
- 次数互不重叠：决策段的成功、失败、未发出可以直接相加。没有用量数据的调用不另起一个“缺报”数，而是在所属次数后面加说明，例如“成功 2（其中 2 次未回报用量）”“失败 3（其中 3 次分不清是否发出）”（ae 复审建议）。LLM 段没有成功、失败次数，所以保留“缺报 K”。
- 旧显示快照：失败构成出现前写下的快照（线程上落盘的显示副本、旧 Gateway 推来的统计）没有 `decision_unknown_failures` 键。
  `public_model_metrics` 按旧用量行同一规则把它的失败整体记为分不清是否发出，不补 0；补 0 会把旧失败说成“未发出”（9b 复审，2026-09-28）。
  这类快照也没有全部用途的失败构成，LLM 段留空，下一次模型边界从账本重算后恢复。
- 数据取自 `conversation/model_metrics.py` 的白名单字段：
  - 决策：原有的 `decision_input_reported_calls/decision_success_count/decision_failure_count`，
    加上 `decision_unfinished_calls/decision_estimated_tokens/decision_unknown_failures`；
  - 全部用途：`failure_count/unfinished_calls/unfinished_tokens/unknown_failures`，LLM 段用它减去决策分区。

**不变的部分：** 约数不写回账本，不参与任何预算或调度；没有用途分区的旧账不计入决策。

## 5. 统一审计工具 `audit_records`

以后所有审计主题都加进这一个工具的 `topic` 枚举，不再为每类审计新建工具。首个主题 `decision`。

- 谁能用：本机主代理与普通 user owner 注册（与 `user_config` 同范围），子代理不可用；每个 owner 默认只能查自己。
  管理员可在 `admin_controls` 里关掉某个用户的审计（`audit_allowed=false`，读失败按不允许）。
- 范围：`current_thread`（当前可信会话）、`owner`（默认，本人全部会话）、`all_owners`（只有当前 owner 是本机管理员
  且管理员自己开启了 `cross_owner_audit_allowed` 时可用；许可只在管理员名下有效，用户手写同名值无效）。
- 只读三类权威结构化记录，不 grep 日志、不读对话/记忆正文：
  1. 决策设置读取：总开关、各点有效模式、等待时间、是否绑定模型；
  2. 各会话 `model_usage` 账本的 decision 用途分区：调用次数、finished/failed/timed_out、已报输入 token、
     超时/失败调用的发送前本地估算输入（`input_tokens_estimated_unfinished`、`estimated_unfinished_calls`，与已报输入分开）、调用最多的会话；
     用量文件修改时间早于时间窗的会话整份跳过，事件按 `created_at` 过滤，无用途分区的旧账单独计数；
  3. Gateway 请求记录里的 `model_selection_observation` 与 `capability_presentation_observation`：
     按 `conversation_claim.thread_id` 归属 owner，字段白名单投影（不含 prompt、工具名清单等），
     队列位置只取宿主写入器的请求路径（`GatewayTaskBindingWriter.decision_audit_observations`），
     一次最多读 300 份窗口内记录，超出如实标 `truncated`；不在 Gateway 回合里时报告 `no_gateway_request_context`。
     选模型采用的两个状态附固定 `status_note`（2026-10-02）：`send_intent_uncertain` 是“已采用、只记发送意图”，设计上不随请求结束结清，实际发往哪个模型看 usage；`commit_unknown` 是线程写入未确认、本请求不发业务请求也不跨模型重发。
- 参数：`topic`（必填）、`scope`、`since_hours`（默认 24，≤720）、`limit`（观察条数，默认 20，≤100）。
- 实现：`tooling/audit_records_tool.py`、`conversation/decision_audit.py`、`gateway_parts/request_audit_records.py`。
- 各主题收集器在返回值里自带 `sources`，工具不再写死来源。

### 决策点“触发了但被挡下”也留记录（2026-09-27，集成方）

**起因**：用户在真实 TUI 里让 my-agent 测 Jev 点位，planning、delivery_quality、action_candidate 等一直没有记录，
my-agent 据此写出“这几个点位宿主代码未接线”的开发需求。核对后发现：
- 这几个点位都接了，用户的开关也都是“开启·观察”；
- planning 那次没触发，是因为那一轮由一条 3186 字的粘贴开启，后面两条短消息是同一轮里的中途插话，不开新一轮；
  “本轮用户原话”超过 1024 字时 planning 按设计不发截断片段、整点跳过，而跳过之前什么都不写，审计里看起来像从未接线；
- 隔离 Gateway（8432，同一份决策设置）里原话在上限以内时，同样的 task_progress read 正常调用 Jev、记下 `planning success`。

**做法**：
- 结果日志新增 `status=skipped` 行：已到触发点、阶段正常且该点已开启，却被结构化条件挡下时记录，`reason` 只是宿主原因码，
  不含正文（`conversation/decision_outcome_log.py::record_decision_skip`）。
- 原因码：
  - `request_too_long`：2026-09-27 起不再使用。planning、delivery_quality、action_candidate 对超出 `decision_request_max_chars`
    （默认 2000）的原话改为首尾节选并标注 `current_request_completeness`，决策照常进行（`backends/decision_protocol.decision_request_excerpt`）；
    空原话仍在扫描归档之前放弃，不记。历史日志里的旧行保持原样。
  - `privacy_url`：要外发的材料含带查询串的 URL（external_material_order、delivery_quality、action_candidate）。材料准备抛
    `DecisionPrivacySkip`（`DecisionInputError` 的子类，带 `reason`），由 `material_or_skip` 记录后放弃。
- 未开启的点位、阶段出错（如 `settings_busy`）都不写 skipped 行，避免刷屏（这类原因改由下一节的到达计数统计）；日志仍有界（最近 1000 条）。
- 配置 `decision_skip_records_enabled`（默认开启）关闭后只是不记，决策行为不变。
- `audit_records` 的说明写明 `skipped` 与原因码的含义，并强调“某点完全没出现只说明窗口内没到触发点，不代表没接线”。

### 结果日志与用量里的链路计时（2026-09-28，B 第 0 步）

**起因**：前台超时多来自连到 Jev 的链路，但阶段耗时只在内存里，分不清慢在代理还是 Jev 服务端；超时调用在用量里是 0 token。

**做法**（设计见[接入设计](DECISION_MODEL_INTEGRATION.md)“链路分段计时”一节）：
- 最近结果行：建了调用记录的行附 `transport`（`attempts` 为空表示请求没发出，如准入忙），含调用终态、`timeout_stage`、`timeout_phase`（超时那一刻所处的传输阶段）、
  `estimated_input_tokens`（发送前本地估算，不是供应商回报），以及各次 HTTP 尝试的状态和分段毫秒 `phase_ms`。
  调用前就结束的行（关闭、冷却、设置变化等）没有这个键，旧行不补。
- 用量行：新增 `input_tokens_estimated_unfinished`、`estimated_unfinished_calls`，来自 `usage_breakdown.estimated.unfinished_*`；
  只统计已发起 HTTP 尝试的超时/失败调用，`input_tokens_reported` 口径不变。

### 没发出去与发出去后失败分开（2026-09-28）

**起因：** pre_recall 和 recall 共用阶段预算，排在后面的 pre_recall 常常一开始就 `budget_exhausted`，1–4 毫秒就返回，请求根本没发出，
却被算成 Jev 超时，拉低了成功率。

**做法**（只改汇总口径，结果日志行与用量记账都不变，与 TUI 统计行同一口径）：
- **用量行**：
  - 新增 `jev_failures`：发出去之后失败或超时的次数，旧账分不清是否发出的也算；
  - 新增 `not_sent_calls`：建了调用记录、却一次 HTTP 尝试都没有的次数（如准入忙、发送前期限用完）；
  - 新增 `failures_send_unknown`：旧账里的失败次数。
  - 原有的 `failed/timed_out` 原始计数不变。Jev 失败率只用 `jev_failures` 算。
- **各点位结果**：失败类状态（`deadline`、`error`、`cooldown`、`configuration_required`）里请求没发出去的，不进 `points`，
  单列到 `not_sent[点位][原因码]`。判定分两种：
  - 有 `transport` 的行，看有没有 HTTP 尝试；
  - 没有 `transport` 的行（没建调用记录的，以及链路计时上线前的旧行），看宿主原因码：`budget_exhausted`、`admission_busy`、
    `notification_capacity`、`settings_busy`、`invalid_input`、`configuration_unavailable`、`connection_backoff`、`point_backoff`，
    或者 status 本身是 `cooldown`。
  - 最近行照原样保留。
  - **两种“未发出”范围不同**：`points.not_sent` 还包括根本没建调用记录的结果（冷却、`budget_exhausted` 等），通常比用量的 `not_sent_calls` 大，两个数不要对比或相加。
- **工具说明**：`audit_records` 的说明同步写明这些字段的含义。

### 每个点位最近为什么没触发（2026-09-27）

**起因**：审计里某个点位调用 0 次时，用户只看到“0 次”，分不清是没打开、没到触发点，还是每次都被条件挡下。

**做法**：
- 唯一来源 `conversation/decision_reach_counts.py`。点位每次到达触发检查都记一次结果：没调用就记宿主原因码，
  真正交给 `decide()` 前记 `called`。计数只在进程内按 owner、小时、点位、原因累加，不逐次写盘；有新计数时每个 owner
  最多每 60 秒持锁合并写一次 `<owner_home>/data/decision/reach_counts.json`（owner 规范路径 `owner_decision_reach_counts_json`，
  Gateway 按用户作用域经 `owner_resolver` 重设），只保留最近 7 天的小时桶。写失败只记日志并把计数放回，不影响点位本身。
- 原因码只来自各点位原有的结构化判定，按“资格条件 → 阶段 → 材料”的顺序取第一个没通过的；判定与原先的整体资格判断一一对应，
  触发行为不变。资格检查仍先于阶段读取，所以关闭的点位多数到达记的是资格原因，是否开启看 `enabled`。
- 共用原因码：阶段错误码原样记（`admin_disabled`、`settings_busy`、`configuration_unavailable`、`invalid_identity`）；
  点位没开记 `point_off`（点位关闭与总开关关闭在阶段层分不开，共用一条说明）；阶段身份不符记 `run_mismatch`；
  材料不合规或超出协议上限记 `bad_material`（`counted_material` 记完原样上抛）；材料含带查询串的 URL 记 `privacy_url`。
- 数量类原因（focus_count、few_candidates、single_page、todo_count、pending_count、memory_count）的界限统一放在
  `conversation/decision_point_limits.py`：点位判定与大白话标签（`decision_reach_counts._limit_labels`）都在调用时现读，
  改一处两边同时生效，标签里的数字不会过时（2026-09-27，分支 `claude/9b-owner-path-scope`）。
- 各点位自己的原因码（中文说明见 `_LABELS` 与 `_limit_labels`）：
  - delivery_quality：`not_test_command`、`not_executed`、`not_verification`、`bad_verification`、`record_mismatch`、
    `focus_count`、`nothing_to_review`，以及 `subagent`、`halted`、`no_request`；
  - action_candidate：`not_observation`、`record_mismatch`、`failed_call`、`bad_observation`、`few_candidates`、
    `no_available_action`、`stale_observation`，以及 `subagent`、`halted`、`no_request`；
  - external_material_order：`not_web_fetch`、`fetch_failed`、`not_external_page`、`not_extract`、`single_page`、`record_mismatch`；
  - planning：`no_run_context`、`subagent`、`ledger_unreadable`、`not_main_scope`、`not_current_plan`、`no_plan_version`、
    `todo_count`、`no_request`；
  - skill_proposal_review：`pending_count`；recall：`no_run_context`、`memory_count`；
    pre_recall：`no_query_fragments`、`no_free_slots`、`no_room`；curator：`nothing_to_label`；
    curator_relation：`nothing_to_compare`、`memory_changed`；
  - model_selection（2026-09-27，分支 `claude/9b-three-point-reach`）：每个 Gateway ask 通过 `_eligible` 算一次到达，
    之前的机制性退出（已调用、非 ask、恢复/续跑/系统任务、已有观察标记、系统斜杠命令、claim 不符）不算；模式关闭记
    `point_off`，但只进内存（`note_decision_reach(..., flush=False)`，保持请求钩子关闭时零 I/O 的合同），随同一 owner
    下一次到期的计数或 Gateway 正常停止一起写盘；阶段原因码、`no_candidates`、`bad_material`、`turn_closed`（提交时
    轮次已关，照常抛出停止），真正调用前记 `called`；预留观察标记失败（同一请求重复）不算。
  - skill_tool：每次新评估算一次到达（Gateway 同一请求只首个尝试评估，本机直连 TUI 每轮评估）；本片已评估过、携带选择的
    恢复都不算。原因：`tools_disabled`、`isolated_scope`（隔离/控制面上下文）、`no_run_context`、阶段原因码/`point_off`、
    `nothing_to_recommend`、`bad_material`。普通模式关闭但实验放行时走只观察实验，这一次到达只记实验的结果：实验阶段拒绝记
    阶段码或 `experiment_forbidden`，否则同样是没有题或 `called`；一次到达最多一个 `called`。
  - subagent_model：每次非 dry_run 的 create_subagents 算一次到达（整批一个决策、每个孩子一道题）；create_subagents 自身校验
    没过（参数、容量）时直接返回，不进入这个点位。原来 `except Exception: return None` 把所有放弃压成 None，现按 `_prepare`
    交回结构化原因：`configuration_unavailable`（设置读不出）、`point_off`、`models_given`（孩子都自带模型或是复用）、
    阶段原因码、`no_candidates`、`nothing_to_ask`（没有可问的孩子）、`bad_material`；发送前复核不通过记
    `candidate_scope_changed`（范围变了，或按原方案把复核读取失败也当作范围失效），真正调用前记 `called`。意外异常仍按原方案
    放弃，用户停止原样上抛，都不计入到达。
- 没登记的原因码照原样显示成“其它原因（码）”，不拒绝。
- 开关复用 `decision_skip_records_enabled`，说明改为“决策点诊断记录”：关闭时跳过行与计数都不写。

**展示**：
- `audit_records topic=decision`：每个 owner 附 `point_diagnostics`，每个点位一行，字段为 `enabled`、`mode`、`covered`、`reached`、
  `called`、`not_called`（`reason`、`label`、`count`，按次数从多到少）。时间窗同 `since_hours`，按小时桶对齐。
  按 owner 统计（`diagnostics_scope=owner`），不随 `current_thread` 缩小。飞书等 IM 里用户问“为什么没触发”时，
  模型直接用 `label` 解释。
- TUI 决策菜单“逐接入点设置”：每行末尾显示“近24小时检查N次、调用M次，最多是因为：……（K次）”。数据来自经
  `execute_model_profile_operation` 的 `decision_read`（本地与 Gateway TUI 同一入口），附近 24 小时的 `point_diagnostics`；
  `user_config` 与阶段内部的设置读取不附。

**边界**：
- 现有 12 个点位都已接入到达计数；以后新增的点位未接入时 `covered=false`，菜单写“未统计未触发原因”，不显示成 0 次。
- 诊断每行另有 `note`（点位适用范围的宿主说明，放在 `decision_reach_counts._POINT_NOTES`）：model_selection 写明“只在经
  Gateway 的对话里判断，本机直连 TUI 不判断”，审计与菜单照原样显示，免得本机直连时的 0 次被看成没接线。
- 进程内还没落盘的计数只在本进程可见：距上次合并不到 60 秒、之后又没有新到达的那部分。审计和菜单读取时会合并本进程
  未落盘的计数；Gateway 正常停止时收尾会补写一次（`cli/gateway_process._flush_decision_reach_counts`），异常退出仍会丢
  这一段，一次性命令行运行可能少计。不注册 atexit，避免解释器退出（包括测试进程）时写盘。
- 意外异常（比如新鲜度权威抛错）不计入到达次数，仍走各点位原有的失败回退。

### 每个点位最近是选中、非选择还是被丢弃（2026-10-02，分支 `worker/ds1-decision-outcome-category`）

**起因**：J10 真实复测里“没出提示”到底是 Jev 没选，还是选了被宿主丢掉，只靠 mode/status 分不出来。

**做法**：
- 结果日志每行新增 `result_category`，只在 `conversation/decision_outcome_log.decision_outcome_row` 一处写入，由纯函数
  `decision_result_category` 从结构化事实推导，所有决策点共用，不做点位专项分支。取值族：
  - `selected`（至少一题给了可判定的选择）；
  - `non_selection:<取值>`（Jev 说不需要/给不了；取值原样透传，收敛判据是宿主已知的非选择取值 `not_needed`/`no_match`/`abstain`/`need_data`）；
  - `dropped:<原因码>`（宿主把建议丢了，原因码原样用既有宿主码，不新造同义码）；
  - `no_selection_recorded`（成功但没有可判定的选择：无响应、逐题错误、空答案、旧替身）；
  - `unrecorded` 只在统计里给“写该行时还没有这个字段”的旧记录，不写回日志。
- 宿主丢弃的落账：原结果行在 `decide()` 落盘后不可改写，所以 `record_decision_dropped(agent, stage, outcome, reason)` 另追加一行
  补充记录（`record_kind="dropped"`，同点位/身份 + `result_category=dropped:<原因码>`）。只在“确有可采用的建议”（`may_apply` 且带响应）时记，
  免得把“本来就没有建议”误记成丢弃；原因码为空不写；写失败只记日志，不影响采用逻辑。审计按点位与时间把两行配对即可分辨“选了被丢”。
- 强制接入点是共用复核门 `decision_service.decision_outcome_is_current`（薄门 + `_adoption_review`），返回第一个不通过原因码：
  `identity_changed`、`adoption_deadline`，或 `_stale` 给出的既有码（`disabled`/`policy_changed`/`settings_changed`/`host_shutdown` 等），异常兜底 `review_failed`。
  另外 10 个消费者点位的既有丢弃出口按变化性质登记补登记码 `sources_changed` / `runtime_changed` / `adoption_deadline`（模块级常量，两处共用）。
- **与发送路径“保留候选”的边界**：`agent/backends/gateway_model_adoption` 的 `candidate_validation_unavailable`、`first_request_not_selected`、
  `candidate_rejected_before_provider`、`request_facts_unknown`、`model_catalog_changed`、`request_capacity_*`、`history_modality_or_projection_unknown`
  是“候选被保留/没提交给模型”，不是“模型给了建议又被宿主丢掉”，这次不并入 `dropped:`，两套码语义不同，展示时也不互相翻译。

**展示**：
- `audit_records topic=decision` 的 `recent` 行：原样保留结构化 `result_category`（机器可读），另加 `result_category_label`（大白话，给人看）。
- `point_diagnostics` 每个点位多一个 `result_categories`：`{category, label, calls}`，按次数降序（旧记录缺字段归 `unrecorded`，展示为“未记录”）。
- TUI 决策菜单“逐接入点设置”行尾追加“；最近结果：<大白话>（N次）”，取次数最多的一项；旧 Gateway 没有这块数据时不显示这一段。
- 汇总 `decision_outcome_summary` 新增 `result_categories`；丢弃补充行不算一次独立调用，不进 `points`/`not_sent`，只进类别统计与 `recent`。

### 主题 `requests`：请求成败（2026-09-26，用户要求“这种东西以后 my-agent 能帮我解决”）

起因：管理员设好密码后没先 `/admin` 就在飞书私聊发消息，请求按飞书普通用户运行，全部 `MODEL_NOT_CONFIGURED`，
当时只能由开发者翻请求文件查。现在 my-agent 可以自己查：

- 内容：Gateway 请求记录里的结构化结果——请求编号、记录时间、归属 owner、渠道、私聊/群聊、状态、错误码、耗时、工具轮数，
  以及按错误码从唯一错误分类表取出的处理建议（类别、可否重试、建议动作、恢复提示）；另附按状态/错误码/渠道的计数。
  不读 prompt、goal、回复正文、用户可见文案，也不读日志。
- 归属：Gateway 在每个请求开始执行时把执行 owner 的规范编号写进响应（`request_execution._executing_owner_id`，
  取宿主解析出的 `agent.home_paths.owner_id`），终态记录里的 `terminal_response.owner_id` 是权威；此前的旧记录没有这个字段，
  退回按 `conversation_claim.thread_id` 归属；两者都没有的只在 `all_owners` 里列为 `unattributed`，不按 user_id 或渠道猜。
- 范围：`owner` 只看本人；`current_thread` 再按会话过滤；`all_owners` 沿用同一套两道门（本机管理员 + 管理员明确开启跨用户审计）。
  飞书用户在绑定管理员之前是另一个 owner，所以查它的失败要 `all_owners`，my-agent 不会自己开许可。
- 管理员身份事实：调用方是本机管理员时附带 `admin_identity`（是否设了管理员密码、IM 管理员开关、哪些私聊已绑定），
  普通用户看不到。
- 扫描与去重同决策观察：窗口内按修改时间新到旧最多读 300 份，同一请求在 done/failed 与 terminal 的两份按编号去重。
- 软提示 `hints`（只给管理员）：只查本人范围时提示“飞书等 IM 私聊在绑定前属于另一个用户，用 scope=all_owners”；
  已设管理员密码、开关打开但没有任何私聊绑定时提示“管理员本人在飞书私聊发 /admin <管理员密码>”。
  `MODEL_NOT_CONFIGURED` 的处理建议同时写明普通用户走 `/model`、管理员本人在 IM 私聊走 `/admin`。
- 实现：`tooling/audit_requests_topic.py`（收集器）、`gateway_parts/request_audit_records.py`（`OutcomeQuery`、
  `request_outcome_records`）、`GatewayTaskBindingWriter.request_audit_outcomes`；回归 `test_audit_requests_topic.py`。
- 真实验收（2026-09-26，隔离 home、127.0.0.1:8432、真实模型，管理员 full-access、已设密码、已开跨用户审计）：
  未绑定飞书私聊发“你好，在吗”→ `MODEL_NOT_CONFIGURED`。本机管理员一次 prompt“我刚才在飞书上给你发消息，一直报错，帮我查一下”：
  第一版 my-agent 只查了本人范围，没看到飞书失败，转去翻 Gateway 文件，结论只提 `/model`；加上软提示与处理建议后，
  它先查本人范围、按提示改查 `all_owners`，2 轮工具就给出“飞书私聊 2 条请求全部 MODEL_NOT_CONFIGURED；已设管理员密码但
  没有私聊绑定；在飞书私聊发 /admin <密码>”，并说明不会替用户执行这一步。证据在 `~/.my-agent/releases/audit-requests-acceptance-20260926/`。

## 6. 管理员专用 `admin_controls`

- 只在 `is_permission_admin(home)`（local/main，唯一管理员判断）时注册，子代理不可用；执行时再按 home 复核。
- 每次调用都要管理员本人确认（`ApprovalPolicy("always")`，自主/Full Access 模式也不免），保证"让 my-agent 跨用户审计"
  只能由管理员明确允许，模型自己开不了。
- 动作：`list` 列出全部用户（local/main 在前）及其开关；`set` 按 `list` 返回的规范 `owner_id` 修改
  `decision_model_allowed`、`audit_allowed`，`cross_owner_audit_allowed` 只能对 `local/main` 设置。
- 存储：目标 owner 自己的 `tool_policy.json` 的 `admin_controls` 块（`owner_admin_controls.v1`），与审批模式、
  长期授权同一文件同一把锁，其它字段原样保留；owner 自己的代理写不进（该文件在 owner 受保护控制路径内）。
  缺块按默认（Jev、审计允许；跨用户审计不允许），坏块/坏文件按不允许；下一次调用即生效，不需要重启。
- Jev 禁用的执行点：`invoke_decision_model_call`（所有 Jev 联网——普通、实验、连接测试——的唯一入口）最前面的硬门，
  拒绝时不建调用记录、不联网、不进连接退避；决策服务映射为 `off/admin_disabled`，`begin_decision_stage` 提前返回
  `admin_disabled` 阶段（不读设置、不准备材料）；连接测试给出明确说明；`user_config decision_read` 回显 `admin_decision_model_allowed`。
- 实现：`tooling/admin_controls_tool.py`、`user_space/owner_admin_controls.py`。

## 错误码（`error_taxonomy.py` 文件末尾独立块）

`DECISION_TIMEOUT_OUT_OF_BOUNDS`（validation，可修参数重试）、`AUDIT_ACCESS_DENIED`（permission）、`ADMIN_CONTROL_DENIED`（permission）。

## 验证

- `test_decision_audit_controls.py`：注册范围、控制默认/失败关闭/保留其它策略字段/0600、只允许管理员写与跨用户许可只在管理员名下、
  owner 编号解析拒绝非规范或不存在、管理员工具 list/set 与即时生效、审计的时间窗/本人范围/观察白名单/跨用户许可。
- `test_decision_service_http.py`：管理员关闭后本机 HTTP 零请求、零调用记录、`admin_disabled`，重新允许后立即恢复；坏策略失败关闭。
- `test_user_config_owner_scope.py`：自调等待时间越界拒绝且不保存、`agent_timeout_bounds` 回显、能力配置 0=不限。
- `test_gateway_model_observation.py`：选模型输入去锚点、按上限截断并如实标注、候选公共声明只写一次。
- `test_decision_usage_metrics.py`：成功/失败计数、约数外推、全部缺报显示 `≈?`、旧账不计入。
- `test_tui_decision_menu.py` 与各接入点集成测试：两个勾选到三种模式的换算与真实按键保存。
- `test_decision_reach_counts.py`：进程内累加、节流合并写盘（跨进程不覆盖）、7 天修剪、坏文件按空、开关关闭不写、写失败放回、
  阶段原因、大白话、`bad_material` 只记输入错误。各点位测试逐场景核对原因码与 `called`；`test_decision_audit_controls.py` 核对审计与
  `decision_read` 附带的诊断；`test_tui_decision_menu.py` 在真实 pipe 里核对菜单行；`test_gateway_per_user_scoping.py` 按字段全集核对
  每个作用域的 owner_* 路径都在自己的 home 内。数量界限：`test_decision_reach_counts.py` 改九个界限核对标签跟着变，六个点位各改一个
  界限核对判定与标签同时变；`test_gateway_decision_shutdown_cancel.py` 核对停止收尾在停 HTTP 之后、写心跳之前落盘，出错只记异常类型。

## 真实 TUI 验收（2026-09-25，本机隔离 home，Gateway 只绑 127.0.0.1:8431）

构建自本分支提交的 wheel（清洁检查通过），隔离 `MY_AGENT_HOME`，管理员 local/main 与普通用户 `providers/local/users/audit-tester`
各开一个 TUI，决策模型为真实 Jev（私有目录副本，用后删除），主模型 MiniMax-M2.7。

1. 管理员在 `/model` → 决策模型设置 → 逐接入点 → 模型选择 → 模式，看到“[ ] 开启 / [*] 观察模式”两个勾选；按空格勾开启、Tab 保存后，
   列表显示“模型选择 · 配置开 · 观察模式 / 当前开 · 观察模式”。
2. 发一条消息：统计行“决策 ≈1.1k token · 成功 1 · 失败 0”；新会话下精简后的选模型输入约 1.1k token，2 秒内返回。
3. “把前台单次等待改成 5 秒”：my-agent 经 `user_config` 改为 5 秒并读回；“改成 60 秒”：工具返回 `timeout_out_of_bounds`
   与 `agent_timeout_bounds {1, 30}`，my-agent 如实说明范围。
4. “查最近 24 小时决策调用”：`audit_records` 报 3 次/成功 3/失败 0/已报输入 3253 与 4 条选模型观察，与当时的用量账本逐条一致
   （第 4 条是本轮开头的观察，其用量在回合结束才落账）。
5. `admin_controls list`、`set`（关闭用户 Jev）在自主模式下仍弹“工具授权”，测试者按 y 后执行；用户的 `tool_policy.json`
   写入 `admin_controls`，其它字段原样保留。用户之后两条消息的选模型观察为 `skipped/admin_disabled`，Jev 零调用；
   用户自己的审计显示 `decision_model_allowed=false`。
6. 未许可时查全部用户：`audit_records` 返回 `cross_owner_not_allowed`，my-agent 没有自行去开许可；管理员明确允许后
   （工具授权按 y）管理员名下写入 `cross_owner_audit_allowed=true`，跨用户审计返回两个用户的数据。
7. 收尾：两个 TUI `/exit`，`gateway stop` 报 `drain_complete=true interrupted_model_calls=0 surviving_background_sessions=0`，
   8431 已释放；模型目录副本、隔离 home 与 venv 已删除，证据在仓库外的验收目录。
