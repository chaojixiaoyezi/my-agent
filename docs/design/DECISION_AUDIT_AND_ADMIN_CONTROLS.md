# 决策开关、超时自调、统一审计与管理员管控

状态：2026-09-25 本地实施（分支 `claude/decision-audit-controls`，基于 main `07fa00fb3`），组件与组合测试、24 个变异、
真实 TUI 验收（见文末）均通过；尚未合并部署。
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

## 2. 决策超时可调，my-agent 可在上下限内自调

- 用户在菜单里改等待时间不受限（仍须有限正秒数）。
- my-agent 经原 `user_config` 工具的 `decision_patch` 调整本人 owner（或当前可信会话）的等待时间，
  所有 `timeout_seconds` 字段（通用前台/阶段/后台与各接入点）必须落在
  `capability_config.yaml` 的 `decision_agent_timeout_min_seconds`（默认 1）与 `decision_agent_timeout_max_seconds`（默认 30）之间，
  0 表示该侧不限制。越界直接拒绝（`DECISION_TIMEOUT_OUT_OF_BOUNDS`），不夹取、不保存同批其它字段；
  `decision_read` 回显 `agent_timeout_bounds`，模型先知道能调的范围。
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

已做的精简（只删材料，不改"采用前必须复核"：apply 指令、候选冻结、首请求容量/工具/版本核对全部不变）：
- 摘要只带语义部分（去掉原文锚点段），最多 `decision_model_selection_summary_max_chars`（默认 1500）字符；
- 当前消息最多 `decision_model_selection_prompt_max_chars`（默认 4000）字符；
- 截断时在 `state.input_completeness` 里如实写 `truncated`、原长与保留长度，锚点段标 `omitted`，不冒充完整输入；
- 候选公共声明（`capacity_status`、`tool_support`）只在 `state.candidate_facts` 写一次。

效果（14 个候选、3000 字语义摘要 + 6000 字锚点段的合成输入，按真实编码与 `estimate_tokens` 估算）：约 7.5k → 2.7k token/次，减少约 63%。

## 4. TUI 统计行："决策 ≈N token · 成功 X · 失败 Y"

- X = 决策分区 `status_counts.finished`，Y = `failed + timed_out`；进行中的调用两边都不计。
- N 是显示用约数：已报输入按已报调用的平均值外推到全部决策调用（超时/失败的调用没有回报用量）；一次都没报显示 `≈?`，不显示成 0。
- 约数不写回账本、不参与任何预算或调度。没有用途分区的旧账不计入决策（原先的 `+?` 标记随之去掉）。
- 数据：`conversation/model_metrics.py` 的 `decision_input_reported_calls/decision_success_count/decision_failure_count`（白名单字段）。

## 5. 统一审计工具 `audit_records`

以后所有审计主题都加进这一个工具的 `topic` 枚举，不再为每类审计新建工具。首个主题 `decision`。

- 谁能用：本机主代理与普通 user owner 注册（与 `user_config` 同范围），子代理不可用；每个 owner 默认只能查自己。
  管理员可在 `admin_controls` 里关掉某个用户的审计（`audit_allowed=false`，读失败按不允许）。
- 范围：`current_thread`（当前可信会话）、`owner`（默认，本人全部会话）、`all_owners`（只有当前 owner 是本机管理员
  且管理员自己开启了 `cross_owner_audit_allowed` 时可用；许可只在管理员名下有效，用户手写同名值无效）。
- 只读三类权威结构化记录，不 grep 日志、不读对话/记忆正文：
  1. 决策设置读取：总开关、各点有效模式、等待时间、是否绑定模型；
  2. 各会话 `model_usage` 账本的 decision 用途分区：调用次数、finished/failed/timed_out、已报输入 token、调用最多的会话；
     用量文件修改时间早于时间窗的会话整份跳过，事件按 `created_at` 过滤，无用途分区的旧账单独计数；
  3. Gateway 请求记录里的 `model_selection_observation` 与 `capability_presentation_observation`：
     按 `conversation_claim.thread_id` 归属 owner，字段白名单投影（不含 prompt、工具名清单等），
     队列位置只取宿主写入器的请求路径（`GatewayTaskBindingWriter.decision_audit_observations`），
     一次最多读 300 份窗口内记录，超出如实标 `truncated`；不在 Gateway 回合里时报告 `no_gateway_request_context`。
- 参数：`topic`（必填）、`scope`、`since_hours`（默认 24，≤720）、`limit`（观察条数，默认 20，≤100）。
- 实现：`tooling/audit_records_tool.py`、`conversation/decision_audit.py`、`gateway_parts/request_audit_records.py`。
- 各主题收集器在返回值里自带 `sources`，工具不再写死来源。

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
