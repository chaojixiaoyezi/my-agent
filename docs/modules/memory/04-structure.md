# Memory Structure

第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

## Live-tool 逐调用来源

`tooling/call_ref.py` 只校验 canonical 三元身份。`conversation/compact_checkpoint.py` 的 v2 行追加 `source_tool_call_refs`、`retained_tool_call_refs`；来源必须完整，未知保留项为 `null`。两组精确 refs 不可重复或重叠，裸 call id 可重复或交叉。带 refs 的新候选把规范 refs 加入内容地址；旧无 refs ID 输出和旧行不重算。顶层 request/attempt 仍是提交者，原写 checkpoint → 锁内 generation CAS 不变。
`active_turn_compact.py` 只凭已提交精确 refs 隐藏，按记录位置选择来源与尾部；未知记录保留并暴露结构化 `uncertain`。`tool_output_externalizer.py` 直接传递原 call 的 attempt/turn，`compact_tool_output_refs.py` 按完整三元组恢复去重；不解析 scoped 字符串或从 operation 哈希反推缺失身份。旧大历史可能无法安全恢复 Compact，不存在静默身份迁移。

`conversation/compact_text_source.py` 只管理同一来源的临时字符窗口和读取一致性，不拥有消息游标或持久覆盖。`backends/request_content.py` 拒绝将非文本块引用当成可完整分段的正文；原检查点仍由原 Compact writer 提交。

## 流式估算与消息扫描

`memory_archive/tokens.py` 唯一估算器按原JSON编码顺序累积字符和UTF8字节，保持旧预算数值与异常回退；仅精确内置、无环、深度有限且最坏JSON UTF8上界不超过512 KiB的小载荷使用公开dumps，避免密集估算累积编码器闭包；其它载荷仍流式，单个JSON值和字典排序仍可能较大。估算不写实际用量账。
`conversation/message_scan.py` 接收canonical路径，复用`store_io.complete_jsonl_end`；有界页只返回完整行和字节游标。append_once在原锁内逐行读到冻结尾界，首个key命中后仍查坏行；Unicode空白按原规则跳过，字段溢出归data_corruption，缺LF尾行禁止追加且不自动修复。分页原语不授予摘要覆盖或scope身份。


## 会话存储读取边界

`curator_inputs.py` 只从同一 ConversationStore 的 `threads` 元数据和 `messages` 增量读取能力收集输入。
`promotion.py` 使用 `messages.by_id_report` 核对精确消息证据；文件布局仍由 `store.storage` 唯一管理。
领域接口改名不移动游标、不改消息内容或正式记忆提交；缺少能力或坏账沿原错误合同处理。

## 缓存前缀诊断

`backends/cache_diagnostics.py` 在 HTTP 实际出站处生成摘要，`ModelCallLedger` 按同 thread 比较。
只追加、历史缩短及 system/tools/model/选项变化分开，超过 512 消息明确部分比较；不改变 Compact 或记忆。
配置 `cache_diagnostics_enabled` 控制采集，关闭不计算摘要，不新增另一份用量账。

## 前台优先与后台请求预算

- 同 Gateway 内按实际后端 HTTP origin 登记完整 `agent.run` 的占用，覆盖工具间隙及 Compact；
  普通 Curator 在同端点被占用时返回 busy，不推进游标、不清 pending、不丢消息。
  `pre_compact` 需要打通屏障，显式管理运行也保留；前台之间不串行化。
- 这是维护启动时的延后，不保证独占模型。已在运行的后台请求、外部程序和不同代理地址的同一服务器
  不由此机制抢占或推断。客户端也不能保证服务端缓存容量及保留时间。
- Curator 自适应预算通过 request-local deadline 进入 HTTP 层，不再被默认 240 秒提前截断；
  普通前台流式超时不改变。超时先关闭本调用传输，旧线程仍存活时禁止同后端叠加重试。
  非协作后端保留失败诊断，不强杀线程，晚到结果没有提交记忆权限；外部服务是否停止计算由服务端决定。

## 2026-09-13 R285 后台历史种子三态 + 任务范围 + 递归等待对称

1. **读不到历史不再静默降级**：`_background_conversation_history_seed` 返回三态
   （`ready` / `unreadable` / `disabled`），异常与 load_errors 都带结构化错误；
   `_background_history_seed_or_raise` 对 `unreadable` 抛 `BackgroundHistoryUnavailableError`
   （error_code `BACKGROUND_HISTORY_UNAVAILABLE`，带 load_errors/detail）→ 本片失败、唤醒不确认、可重试。
   以前 `except Exception: return None` 会让调用方 `include_recent_messages=True` 退回有界摘要继续跑模型，
   把"历史读取失败"伪装成"上下文骤降"。
2. **复用既有结构化任务范围**：种子的行选择改为先取 `_context_bundle` 的任务范围投影
   （detached named task 的创建锚点 + 精确 lineage），再按 `message_id` 过滤未压缩行；
   范围为空即合法空历史。此前直接吞全 thread 未压缩行会把创建锚点之后、属于别的任务的消息带进
   detached 工作。投影只走历史两步（行选择 + provider 消息），不牵入 recent_artifacts。
3. **递归等待与 root 对称**：`task_local_wait_response_for_open_subagents` 保留模型真实正文与真实
   turn-end（不再强制 `interrupted`），等待只作为 `direct_child_wait` 依赖事实 + `SUBAGENTS_ACTIVE`
   状态；删除已无用的 `queue_interim_reply_for_open_subagents` 空壳入口、导出与旧注释，不再维持两套语义。

## 2026-09-13 R283.1 迁移错位修复（真机验收发现）

第一版 LF 边界迁移用正则批量替换，有两处包装错位：`jsonl_lines(reversed(text))`（storage 写后回读校验）
与 `jsonl_lines(enumerate(text), start=1)`（control_plane 只读投影）。前者让 `jsonl_lines` 收到 reversed 对象，
抛 `AttributeError: 'reversed' object has no attribute 'split'`；.10 真机 TUI 多子代理验收里，子代理
`live_archive` 写后回读走这条路径 → runner 直接 FAILED（`runner 执行失败: 'reversed' object has no attribute 'split'`）。
两处已改为"先按 LF 切记录、再 reversed/enumerate"，并为两条路径补 NEL 守卫测试。
**教训**：批量迁移必须逐点复核包装层次，不能只看替换后的文本是否"像对的"；修复后必须真机重放。

## 2026-09-13 R283 JSONL 记录边界统一为物理 LF（真实事故修复）

**事故**：子代理 transcript 的 JSON 字符串里含 U+0085(NEL)，`path.read_text().splitlines()` 在 NEL
处把一条完整记录切成两条 → 2026-09-13 三个 child（`subagent-1789309101-d6832a68/-72be5b55/-ec2d50a9`）
的 `runner_result`/`final_report` 报 `conversation transcript is unreadable`，`append_message_once`
因 `recent_messages_report` 的 load_errors 抛 `DataCorruptionError`，整个 child 判 FAILED。
同一批文件按物理 LF 读：175/162/154 条记录、0 错误；按 `splitlines()` 读：190/163/158 行、19/2/5 个 JSON 错误。

**修复边界**：唯一实现 `common/json_io.py::jsonl_lines()`——只按物理 LF 切记录（末尾容忍一个 `\r`），
保留字符串内的 NEL/U+2028/U+2029/VT/FF/FS 等字符；**不清洗字符、不吞坏行**。真正的半行、截断、
非法 JSON、非对象行仍然逐条产生结构化 `load_errors`（守卫测试两侧都锁）。所有 JSONL 读取点统一改用它，
包括会话账本全量/尾部倒读、协作账本、审计账本读取与重写、memory_archive 各分片、gateway history/late/
http 事件、takeover readiness、CLI resume、identity/daily memory。会按行重写文件的路径（审计清理、
task workspace 摘要同步）同样改用它，避免"读时切开、写回落成 LF"的静默改写。

## R264 策展尝试形状的边界

- `curator_model_attempt={...}` 是**诊断投影**，只含计数/耗时/异常类名，权威仍是 `failure_code` 与事务状态；
  键序固定、单条 ≤300 字符、最多 32 条，走既有 `warnings`（**禁止**为诊断新增 run 账 dataclass 字段：
  字段集是严格 v2 契约，新增会让所有历史行 fail-closed，真机踩过 `CURATOR_RUN_AUDIT_FAILED`）。
- 尝试账用 ContextVar 承载且每轮 `extract_with_retries` 先清空 → 失败审计读到的只能是本轮证据；
  不得把它做成跨轮累积的全局状态。

# Memory Structure

## R262 策展超时缩批的边界

- `curator_backend.extract_with_retries` 现在返回 `CuratorExtractionAttempt`（实际输入快照 + 解析结果 + 缩批次数）；
  调用方**必须用 attempt.batch** 做证据验证、游标与提交，否则缩批后 identity manifest 与实际输入不一致。
- 缩批是**内存输入整形**，不是新状态：不得写入游标/账本/state；截断只允许截前缀，尾部必须留在游标之后被下一轮重放。
- 超时判定只看异常类型（`is_curator_timeout_error`），不得读异常正文；缩批上界与 lease 预算护栏两条公式
  与 `curator._lease_seconds` 必须同步修改（两处注释已标注）。

# Memory Structure

## R257 策展失败账的字段边界
- `CuratorRunRecord` 的字段集是**严格 v2 契约**：`from_record` 要求键集合与 dataclass 完全一致，
  因此**绝不允许**为了一时诊断新增字段（会让所有历史行 fail-closed；真机踩过 `CURATOR_RUN_AUDIT_FAILED`）。
  失败诊断走既有 `warnings`，格式固定为 `failure_diagnostic=` + 紧凑 JSON（sort_keys）。

- `CuratorRunRecord.failure_diagnostic` 是**诊断投影**，不是失败权威：权威仍是 `failure_code` +
  attempt/lease 状态。字段只允许机器可判定形状（异常类名、可选 HTTP 状态码），
  任何供应商正文、prompt 片段、记忆内容都不得进入，这条在执行 `_failure_diagnostic` 时强制。
- 新增诊断字段不改变 run 账 schema_version（v2 追加可选字段），旧账本无需迁移。

# Memory Structure

## R256 迁移预检的稳态边界

- `migration.py` 的 `_MigrationServiceCore.apply()` 现在先读 marker 再决定是否 `_scan()`：
  稳态判据 `_steady_state_current` 要求 marker 的 `schema_version` 等于
  `MEMORY_MIGRATION_SCHEMA_VERSION`、`status == "complete"`、marker 读无错，且
  `_legacy_sources_at_contract_paths` 在契约位置未发现遗留目录。任一条件不满足即回落完整扫描，
  因此"迁移该不该做"的裁决权仍在完整扫描与已冻结快照，短路只回答"无事可做"。
- `_legacy_sources_at_contract_paths` / `_recorded_paths` / `_path_present` 是**只读派生探针**，
  不是第二套权威状态：它只按已知写入形状点名 stat（owner home 根、`owner_data_dir`、
  `owner_tasks_dir/<date>/<task>/work/<legacy-name>`、显式工作区根、marker 记录过的位置），
  符号链接与存在性任一成立都回落完整扫描；`_LEGACY_SOURCE_NAMES` 是唯一权威名清单。
- marker 新增 `legacy_gate_dirs`/`learning_dirs` 两个字段（追加，不覆盖既有字段），
  记录上次 complete 扫描确认过的位置；旧 marker 缺这两个字段按"无记录"处理，不影响既有语义。
- `plan()`（dry-run）保持完整扫描，用于诊断与人工核对；性能优化只作用于每轮策展都会调用的 `apply()`。

## R223 当前边界

- `promotion.py` 的 add 不能用相似度推断 replace；修改身份来自明确 entry_id/version，事实来源核验
  不等于现实真实性鉴定。偏好与 Persona 继续使用原路由，记忆工具示例与自主写入语义一致。
- `jsonl.py` 的 runtime_snapshot.semantic_recall 公开配置/初始化/索引/查询状态；语义降级不删除
  正式 JSONL。诊断只含错误类型，不含凭据、记忆正文或模型地址。
- `local_storage/search.py` 优先从 canonical conversation_runtime.thread_id 解析身份，在 SQL LIMIT
  前过滤 around 查询；缺身份返回明确时间邻居，不猜归属。
- 旧 CompressionService 及其 snapshot 包装已删除。max_tokens 只作为输出预算，不再触发把多条旧记忆
  拼接成“摘要”的辅助路径；真实 Compact generation、checkpoint 和模型辅助调用账本维持原权威。
- `tool_ir_compact.py` 的二分试探只修改同一 IR 的临时副本，异常恢复原历史；最短前缀策略不改变
  `preserve_newest_pair`、摘要覆盖或不可删事实边界，provider cache 成本需另测。

本文只描述当前记忆主链路（单 owner 视角，local/main 下的持久化事实源）。

## 事实源

- 正式长期记忆：`owners/local/main/memory/long_term/memory.jsonl`（晋升后的正式事实）。
- 日常记忆：`memory/daily/`（按日），`memory/lessons/`（经验教训）。
- 候选：`memory/candidates.jsonl`（待结构化证据/冲突/阈值核验，晋升前不具事实权威；只有 SOUL 候选等待用户确认）。
- recovery snapshot：`memory/hooks/YYYY-MM-DD.jsonl`（运行经历归档的轻量恢复线索）。
- 运行事实（runtime facts）与 raw archive：由 `run --save` 写，`memory-resume`
  据此恢复上下文；与 `ConversationStore`（transcript/guidance/goal）分开。
- 任务级事实源（task.yaml / run_workspace.json / runtime.db）是任务生命周期权威，
  记忆层只消费不覆盖。

## 权威与投影

- 记忆晋升、候选审核、HOT/lesson 写入走 `AgentConfig` 开关与统一 Candidate/Curator
  链，入口收敛在 memory 模块；CLI 普通 run 不直接把对话正文写进正式长期记忆
  （`--no-save` 关闭本次运行归档，ConversationStore 与审计仍照常）。
- `promotion_mode` 由宿主根据 typed target/type/origin/action 每次重算，不接受模型或旧账本把
  自主候选永久降为人工。长期事实、USER、AGENTS、lesson 与 HOT 在满足各自证据、精确目标、冲突、
  阈值、CAS、quota 和注入扫描后自主提交；只有 SOUL 进入 owner 用户确认链。
- 所有 active/candidate/daily/lesson/HOT/persona 路径都从当前 owner 的 `HomePaths` 解析。共享 Gateway
  只调度多个 owner，绝不共享这些仓库；投影、索引和恢复包不得把其它 owner 的正文带入当前模型。
- `update_persona` 的写入频率保护也必须以 canonical owner home 分桶；同一 Gateway 的其它 owner 不能消耗
  当前 owner 的额度。限频只是一层软资源保护，拒绝必须结构化标记 `effect_outcome=not_started` 并保留可
  退避错误码，不能污染记忆正文、确认链或副作用未知账。
- `memory_path` 等路径由 home 解析统一给出（显式配置 > MY_AGENT_HOME 环境变量 >
  ~/.my-agent 兜底），记忆模块不自行猜测 owner home。

## 运行中 native 工具历史摘要

- `agent/memory_archive/compact_semantic_summary.py` 是 carried archive 续跑摘要与运行中
  native IR 摘要共用的语义摘要入口，但不拥有 Compact 状态、工具执行或完成判定。
- 摘要后端调用统一经 `agent/conversation/auxiliary_model_call.py` 包装，复用当前 agent 的
  `ModelCallLedger`、provider attempt observer、全局 admission 和指标；Compact 不再拥有第二套线程超时
  或隐形调用计数。调用持续时间由 backend/provider 已配置的网络超时负责收口；账本输入估算包含实际发送的
  system 与 tools，不再只数 prompt/messages。
- 运行中真实 turn 的摘要输入由“上一代 ConversationThread 完整摘要 + 本次 native 工具
  历史 + 当前任务”组成，输出是可独立替代上一代的完整摘要，不是只描述本次增量的片段。
- 真实 native 路径复制主请求的 `CacheStructuredPrompt`、完整 provider history/current IR、system 和 tools；
  Compact 指令只追加到 volatile 尾部。这样既保持 会话运行时 的“完整 history 后追加 synthetic request”顺序，
  又对齐 终端交互 cache-safe fork 的 system/tools/model/messages/thinking 前缀。旧 fake/普通字符串调用保留
  “真实任务 user → native history → synthetic Compact user”的兼容顺序，不从 prose 猜缓存边界。
- 工具 schema 在 Compact 请求里只用于保持 provider cache surface；辅助 wrapper 是单次生成，没有 handler 或
  工具循环。返回任何 native tool block 都视为不可用摘要并转 typed 机械交接，绝不执行或回放。
- `agent/conversation/live_tool_compact.py` 才负责把这份摘要连同精确移除/保留的 tool-call
  ID 写入 checkpoint，并通过 ConversationStore 的同一 CAS 推进 generation；TUI 只投影
  已提交的代次和 token 前后值。
- `_tool_loop_service` 在摘要、计量、checkpoint 与 CAS 的实际边界发送同一个 content-free progress block；
  `LiveToolCompactCommitRequest.after_checkpoint` 只允许在 checkpoint 已成功、CAS 尚未开始时发 committing
  milestone，异常被吞掉，不能反噬会话提交。进度条和 spinner 是投影，不是第二本 Compact 账。
- in-memory 候选优先向 recovery target 裁剪；若固定提示、工具 schema 或必须保留尾部使该目标不可达，但完整
  候选已低于真实 trigger，则仍写 checkpoint/CAS 并推进 generation。只有 `after >= trigger` 才用
  `superseded/candidate_discarded` 撤销展示且恢复原 IR；`failed` 只代表摘要 transport、checkpoint、CAS 等
  真实故障。该边界与 transcript、会话运行时、终端交互 一致，避免重复烧同一摘要却不记代。
- 规划资格使用真实 IR 删除器的副本探测：只移除完整工具对及已被摘要覆盖的 assistant turn，不把 UserTurn、
  RuntimeFacts 或 carried summary 假装成可删内容。探测不写 window/generation；不可删 floor 已高于 recovery
  target 时不启动 live 摘要，避免下一轮立刻重压的浅代次。
- `save` 与 transcript authority 是两个结构化事实：前者决定 `Agent.run` 是否写旧式回复/记忆，后者决定
  当前 exact thread 是否拥有 Compact CAS。后台 main 由 ConversationStore 另行提交回复，因此可以
  `save=False + authoritative=true`；辅助、不保存且非权威的展示回合仍只能临时摘要。任何正文权威的
  main/child/grandchild 都不能在摘要 transport/调用失败后先删除历史，必须整体失败并恢复原 native IR。
- completed-empty 与调用失败是两个合同：前者表示 provider 请求和用量账都已正常结束，只从 typed IR 生成
  `compact-mechanical-fallback.v1` 的有界非权威续接投影并继续 checkpoint/CAS；后者仍抛错、恢复 IR 并累计
  熔断。transcript Compact 对 completed-empty 使用 raw row + structured operation evidence 的同类有界投影。
  live 机械投影独立封顶 4K，不沿用 12K 摘要输入预算；两种机械投影都不判断完成、不授权路径、不替代
  archive、operation ledger、artifact 或真实文件。
- 非空 provider 正文也不能自动成为 live handoff：只接受以 `compact-live-handoff.v1` 开头、六个固定语义栏
  完整且不含供应商工具协议的纯文本。非法正文与 completed-empty 一样使用 typed IR 机械投影，但 reason
  区分 `provider_empty_summary` 与 `provider_invalid_summary_shape`，便于审计而不把模型文本升级为机器状态。
- 同一个 `CompactionSummary` 容器里的 thread summary 与 active-turn carried handoff 以稳定 schema marker
  区分；二次 Compact 只删除 exact 上一代 thread summary，不能吞掉 carried handoff。当前任务由首条
  provider user message 唯一承载，Compact synthetic user 只放摘要指令与上一代 summary。
- 摘要不是执行事实源。精确副作用和交付仍以 raw archive、operation ledger、artifact
  registry、任务工作区和真实文件为准。

## lifecycle wake 的 carried tool archive

- 每个 root task 的 `work/blobs/tool_outputs/index.jsonl` 同时索引 bounded `tool_call` 与外置
  `tool_output`。child lifecycle 后续工作片用 durable task id 定位这一文件，再按 completion 信封的 exact
  `conversation_request_id` 读取，保留 append 顺序，并以 `scoped_call_id` 去重；新行显式保存 turn id，
  旧行仅以同值 `request_id` 兼容，其它 task、同 task 的其它 turn 和 child workspace 不扫描。
- 恢复记录沿既有 `carried_archive_tool_calls` 进入工具循环，重建已执行工具、one-shot key、工具轮基线、
  参数和 artifact refs。索引同时保存宿主执行 `parameters` 与 provider 原始 `model_parameters`：前者只给
  宿主审计、恢复和幂等使用，后者才允许进入 carried 摘要或模型 replay。两者都使用限深、限宽、凭据脱敏
  的 JSON 投影，Todo items、批量派工 items 与 typed covers 不得因嵌套而变成空数组。旧索引只有在
  `input_sources` 能证明字段来源时才提取模型视图。大输出正文仍留在 owner 私有 artifact，按需读取；
  索引损坏时 fail-soft 回到已有 task/transcript 上下文，但不得编造已执行事实。
- 恢复到 live prompt 时再次经过同一大参数 reducer，`write_file.content` 只留下路径、模式、长度、hash 和
  短 preview；chronology index 超限时使用 bounded head + newest tail，并显式记录中段省略数量。完整正文
  只能按 artifact ref 读取，不能因下一工作片启动而整份回灌。
- 工具首次 externalize 时同时写入宿主确认的 bounded `tool_execution` 与 `tool_operation`。carried record
  将它们恢复到现有 handler/failure/operation status 字段，供同一 active turn 的 operation verification
  原样核对；任意诊断私有字段和工具正文不进入索引，没有 typed operation 终态时继续 fail-closed。
- text 协议继续读取机械 tool-context；native 跨进程续跑不能伪造原 provider ToolCall/ToolResult 对，改为
  把同一批 carried 记录压成唯一、有界的 `CompactionSummary` handoff 放回 IR。它随之后每次 provider
  请求持续可见，但不推进 ConversationThread generation，不增加 TUI `compact N`，也不获得副作用权威。
- 这条链只解决同一 active turn 跨后台工作片的连续性，不新建 Compact 账本，不推进 generation，也不让
  自然语言计划获得机器权威。

## Artifact 分页与大输出恢复

- 完整工具输出先交给唯一 externalizer 判定和归档，再产生模型 preview；工具可以用结构化
  `tool_output_policy.requires_recovery_artifact=true` 要求保留完整正文，但不能指定宿主路径。全局大小阈值
  和预览容量仍是另两条通用触发条件。
- artifact 索引为模型提供稳定逻辑 `canonical_artifact_ref`；物理 `artifact_path` 仅供宿主 CLI/审计，TUI
  进度和模型参数不得泄漏它。读取窗口统一返回 `window_start/window_end/total_chars` 与前后剩余方向；只有
  `has_more_after=true` 才提供 `next_offset`。tail 读取即使省略前缀也表示已经到 EOF，search 的截断只代表
  匹配展示上限，不代表正文还有下一页。
- 原始 artifact、append-only index 和 operation ledger 继续是事实源；模型 preview、摘要和逻辑 ref 只是
  有界续接材料，不能证明工具成功、任务完成或授权路径。

## 2026-08-17 测试适配记录

- 记忆相关测试的 `tool_protocol="text"` 配置移除、假后端 native 化、协议快照默认
  native（EXEC-31b 适配，详见 02-progress.md）。
- compact 自动续接链的 native 工具轮差异已记录 xfail，待按 native 语义适配。
