# Memory Progress

## 2026-08-04 Memory v2 单一候选、后台策展与维护闭环（开发中）

- owner 级正式结构已收敛为 `long_term`、`daily`、`candidates`、`ops`、`curator`、`lessons`、
  `routing`、HOT 与导航各一份权威；Candidate/Daily/Curator 均使用版本化严格 Schema。
- 新增真正接入 Gateway owner maintenance 的 `MemoryCuratorService`：轮次、时间、pre-compact、
  session close/reset、task complete、daily finalize 和 admin 只提交不同 reason，共用同一 state、
  lease、增量 cursor、后台模型和整批提交/恢复链。后台模型不获得工具循环或文件/Persona 写权限。
- Candidate → Promotion 已统一证据核验、scope/subject 冲突、精确 replace/remove/merge、Persona 边界、
  lesson/HOT 阈值和正式落点。`model_inferred` 不会成为正式事实；Tools 成功只由正式 operation store
  适配器核验，不复制 Tools Agent 状态机。
- 一次性 Migration v2 支持只读 plan、完整备份 manifest、幂等 apply、坏数据 fail closed 和中途回滚；
  覆盖旧 ops 候选、learning drafts、task-local memory_gate、daily mirror、dialogue、kind=lesson、
  重复导航/HOT/lesson 与 retention v1。迁移成功后不保留生产双读/双写/fallback。
- Retention v2 覆盖 ConversationStore、audit、Daily、tool artifacts、rejected Candidate、Curator runs、
  Compact 和 completed task；legal hold、非终态恢复数据、损坏状态与执行前变化均优先阻断删除。
- 管理 CLI 收敛为 `my-agent memory candidates|curator|retention|doctor|migrate`；旧 `learn` 和
  `subagents-memory-gate` 生产入口已删除。普通 dialogue 不再自动写 long-term，`ops` 不再保存正文，
  daily 不再镜像正式 Memory 操作。
- 已完成迁移 17 项、retention 7 项、Memory tool/repository/hardening 26 项、Candidate/Curator/Promotion
  扩展测试以及 Goal 指定 Memory 聚焦组验证；当前 14 文件 Memory 聚焦组 168 项全绿。13 文件联合组的
  独立运行是 250/251，通过的部分已覆盖 Memory 主链；唯一失败是 Tools 状态机把共享只读目录写入拒绝投影成
  `TOOL_OPERATION_OUTCOME_UNKNOWN`，而用例期望 `WRITE_FORBIDDEN`，不属于 Memory 合同。
- 最新完整回归共 8,744 项：8,553 通过、160 失败、31 跳过、0 error，pytest 记录 485.147 秒；其中
  107 项在模型调用前统一失败于 Tools 的 `ToolProtocolSelectionError`（默认 `native`，但 Echo/测试后端
  没有 run-start native capability probe），包括若干文件名含 memory 的运行集成测试。剩余失败集中在
  Tools 错误码/operation 投影、Subagent runner 与 Audit/Verification 并行链；14 个 Memory 核心聚焦文件
  在该 JUnit 中仍是零失败。先前 26 个要求旧 dialogue 自动写 long-term、旧 `note/preference` kind、
  Daily mirror 或 Builder 直读 memory/HOT/lesson 的陈旧失败已逐项修复，不能把这 160 项回退解释成
  恢复旧 Memory 语义的理由。完整发布门禁仍未通过。
- 上述完整回归之后，Memory 自己使用 Echo/脚本后端的测试已显式选择 `text` 协议，不再假装完成
  run-start native capability probe；相关 4 文件 82/82 通过，随后 Goal 指定联合组确认 250/251。
  当前 Memory 范围 Ruff 与 `compileall`、`git diff --check` 通过；全仓 Ruff 的 16 项、doc-sync 的
  Verification 文档、strict code-size 的 12 项以及 clean-package 的并行运行产物仍由对应模块收口。
- 真实隔离 Gateway 已用 MiniMax-M2.7 与 DeepSeek `deepseek-v4-pro` 验证相同严格输出合同、轮次/时间/
  pre-compact/close/reset/task complete/daily/admin 触发、过期 lease 重启恢复和同名 reason generation 并发接续。
- 真实大 `read_file` 输出（32,330 bytes）只在 tool-output artifact 保存正文；Daily 只落路径、内容 SHA-256
  和大小，正式 long-term、USER、SOUL、AGENTS、lesson/HOT 均未变化。
- 真实故障已分别留下 `CURATOR_SCHEMA_INVALID`、`CURATOR_MODEL_FAILED`、`CURATOR_MODEL_TIMEOUT` 和
  `CURATOR_COMMIT_FAILED`。超时与撤销 Daily 目录写权限的提交故障都未推进游标、未增加 Daily/Candidate；
  恢复配置/权限后，同一 Gateway maintenance 安全重试成功且 occurrence 未虚增。
- 最终全量、静态发布门禁与八部长文本最后验收仍按 Goal 继续执行，因此本条状态保持“开发中”，不提前宣称完成。
- 2026-08-12 真机重复写暴露 promotion/storage 身份漂移：首次候选是
  `personal/personal`，第二次被模型写成 `personal/local`，旧 `JsonlMemory` 只按 role/kind/正文
  全 owner 去重，返回空提交后 Promotion 又按新 entry_id 查找，最终 `StopIteration -> TOOL_ERROR ->
  UNKNOWN/unfinished`。现将 v2 正式事实幂等键统一为正文 + `subject_key/scope_type/scope_key`，同
  scope 重放仍返回原 ID，不同 scope 的逐字相同正文可以并存；空提交身份漂移改为稳定 conflict，
  不再伪造成功 ref 或抛迭代异常。
- 同轮同时封住不可召回的新 scope：新 add Candidate 统一拒绝 `personal/local` 等类型/键不匹配值，
  但旧 Candidate/正式记录仍可读取，replace/remove 可继承旧目标 scope 继续迁移或清理。新增 store、
  Promotion 与 remember 回归覆盖同 key 重放、同类型不同 key 共存、空提交安全冲突，以及写账本前
  `TOOL_INVALID_ARGUMENTS/not_started`；真实隔离 Gateway 与 testbox 复验仍待本轮后续证据。
- 2026-08-12 同日定版并落地晋升权限合同（`promotion_mode`，seq1227）：候选账本新增持久化枚举
  `auto_eligible|manual_required`，宿主（remember 工具与 Curator）确定性赋值，模型 Schema 与
  任何文本都不能设置或修改。仅非 batch 单条 add 且 user_explicit/tool_verified 的合格新长期事实
  可被标 `auto_eligible`；batch 即使单条、replace/remove、model_inferred、lesson/hot/subagent
  一律 `manual_required`。`automatic=True` 对 manual 候选在证据/状态/target 任何 mutation 之前
  返回稳定 `PROMOTION_MANUAL_REQUIRED`；Curator 选择器与提交后晋升都只消费 `auto_eligible`。
  lesson 自动专线按定版删除（重复观察/多证据组只是人工批准后的晋升门槛）。权限不进入
  `candidate_id`/observation key/identity 判定，同事实不同权限重放不增 occurrence、不能升权，
  落库 merge 单向吸收 manual；legacy 缺失归一 manual（fail-closed），非法值严格拒绝，
  `schema_version` 不 bump。修复了 `_post_commit_report` 不走选择器直接回调晋升的绕过，以及
  batch 单条误判可自动的两处赋权漏洞；4 个测试文件覆盖 10 项验收矩阵与正反例。

## 2026-08-03 Audit 来源工作者的任务状态与工具归档隔离

- 同一父任务下多个来源工作者会并发更新 `work/state.json`。现在读、单调合并和原子替换共用同一
  路径锁，避免两个子代理都从旧快照出发、后写者覆盖先写者的 `child_run_ids`；根任务刷新自身状态时
  也保留已经登记的子运行，不再把并发子代理从任务树中抹掉。
- 工具输出归档新增 `current_run` 读取范围。Gateway 注入的可信 run id 必须与索引记录精确相同，
  否则即使猜中同一 owner、同一 task 下兄弟子代理的 artifact ref 也会拒绝读取；普通 owner/task
  归档读取语义保持不变。
- 删除 task-workspace payload 层不再使用的通用 JSON 写转发，状态写入只保留上述带锁的权威入口，
  没有形成第二份子代理状态或 Audit 专用 Memory。

## 2026-07-28 模型调用物理尝试进入同一运行事实

- `ModelCallLedger` 现在分别保存 logical model turn、物理 model attempt 和 provider HTTP attempt；
  Gateway provider 的每次开始、响应打开、失败与是否安排重试都在请求线程内写入同一线程安全账本。
- 普通失败与超时是终态，迟到的 finish 不会覆盖；最终有界计数进入 `runtime_facts`，供恢复、观测和
  后续 compact 看见真实调用成本。账本不保存 API key、请求正文或响应正文。
- `runtime_fact_source` 只复制 canonical summary，不从日志或模型自然语言重建调用次数；观测回调失败
  不能改变 provider 的真实结果。

## 2026-07-28 原生工具长链复用同一语义 Compact

- `7642c134` 已让原生工具历史按完整 provider 可见输入计量并整对回收，但 1.10 同一真实飞书
  `pyripgrep` 长任务在旧工具对被删除后，再次全盘寻找提示中已明确要求复用的真实 `rg` 路径。
  这证明完整 token 计量已生效，却也证明只有配对删除和数量 marker 不能承接长任务语义。
- 对照 会话运行时 `3418498f0142` 将 compact summary 作为同一 history replacement item、长期助手
  `0b32ff708808` 的会话压缩提交边界与 通道运行时 `05fb8e6e6190` 对 active task/status/精确引用的
  摘要约束后，native active turn 复用既有 `compact_semantic_summary` 后端。达到唯一 90% 阈值时，
  先总结当前同一 IR，再整对回收旧 ToolCall/ToolResult，并以最多一条
  `CompactionSummary` 替换旧段；后续再次压缩会原位替换，不累计多代摘要。
- 该 item 只是模型续接视图，不是第二份 conversation/task compact、Memory 或事实账本。真实用户
  `UserTurn`、近期工具尾部继续保留；raw archive、operation ledger、artifact、workspace 文件和
  transcript 仍是精确事实源。摘要失败时回退既有机械 handoff，不能让 compact 或当前任务崩溃。
- 摘要输入复用原生消息适配器和孤儿净化，工具输出按不可信数据处理；提示明确保留最新用户要求、
  未解决状态、下一步以及路径、ID、URL、端口、哈希和测试数字。摘要本身与 handoff marker 都进入
  同一完整 token 预算，不允许加完摘要后重新越过 recent-tail 目标。

## 2026-07-27 主/子代理共用单一 Compact

- 删除子代理专属 Compact、session continuation、task-local owner adapter 和独立阈值；主代理与每个
  子代理都复用现有通用 Compact，只由各自 workspace 隔离落盘位置。
- 子代理不再生成自己的 `compactions/`、`recovery/` 或 task compact package；当前 run 只在
  `memory_archive/runs/<run_id>/compact_applies` 记录通用 apply，并保留可审计的原始运行证据。
- 恢复权威顺序收敛为：当前 task 的结构化 goal/next actions，高于 runtime guidance、task progress、
  旧摘要、archive wrapper 和工具读取游标。coverage/cursor 仍用于证明读过什么，不会无条件改写
  “接下来做什么”。
- 停止只关闭本次运行，不删除 conversation history、长期 Memory 或通用 Compact 状态；用户随后继续时，
  只有 user-stopped 的同一精确 run 可恢复，管理员取消、模型取消和其他终态仍保持关闭。

## 2026-07-26 真实模型、双飞书用户与执行事实边界

- 参考固定在 会话运行时 `32329b289d05` 的 typed response/tool items、单一 conversation compact 与
  turn-scoped execution state，以及 长期助手 `4be38125af06` 的 memory tool、会话工具循环和
  “工具返回句柄才是事实”边界。实现仍落在 my-agent 既有 owner、Memory、Persona、PromptBuilder 与
  Tool Registry 主链，没有新增 IM 专用记忆、第二套 compact 或 provider 专用执行器。
- 本地 8899 做了基础真实模型回归；MiniMax-M2.7 用独立 owner 完成 600 条长期记忆、9 个工具轮、
  约 9.7 KiB 报告和多轮召回，覆盖 `user_explicit/tool_verified/model_inferred`、重复写、
  subject 冲突、list/replace/remove、恶意记忆信封、敏感一次性凭据拒绝和 hard delete。
  `tool_verified` 只有引用本轮成功工具记录的结构化 ref 才能进入 active memory；模型推测只写候选。
- compact 压力验证按显式 `context_window × 90%` 触发：19K 窗口在 17,100 token 边界进入同一
  compact/resume 链，并连续完成 8 次真实 cycle；24K 人工压力链完成 4 次后保持有界。正式默认仍是
  200K 配置回退，不把压力值写成第二个产品默认，也没有恢复已删除的 task compact。
- hard delete 除权威 long-term/daily 和派生 LocalStore/FTS/vector 外，也按 `tool=remember` 精确擦除
  结构化工具账本中的记忆正文；调用 ID、状态、hash、operation 终态等无正文事实保留。raw conversation、
  gateway audit 和任务事实是另一条明确留存边界：删除长期记忆不会篡改用户真实说过的话或历史回复。
- `run_once` 只在非空 user/assistant 正文时写长期 dialogue，模型空响应不再制造空记忆。
- 两个真实飞书客户端 owner 都完成写入、跨轮召回和隔离验证。A 的新增、召回、list/remove 及 Persona
  清理均有真实工具记录；B 的新增和召回有真实记录，但清理时 MiniMax 连续两次只执行 list 就在正文
  声称 remove 成功。系统以工具归档和权威文件判定它未完成，未把正文当事实，测试数据随后经同一正式
  Memory/Persona 工具主链确定性清理。A/B 的 active Memory、`USER.md` 与检索索引最终均无对方测试值，
  飞书出站没有 `<memory-context>`、执行事实 JSON 或工具协议泄露。
- 为缩短“工具事实离当前问题太远”的提示距离，每个工具轮 prompt 尾部新增
  `current_turn_execution.v1` 有界投影，只来自当前 request 的 canonical tool records，列出成功/失败
  副作用调用和 refs，不解析用户或模型自然语言。最终结果又从同一记录生成
  `operation_verification.v1`：成功要求 `ok=true + operation=succeeded`，按 operation 去重并区分
  failed/not_started/unknown/cancelled/incomplete/unverified。内部逐操作记录留在
  `AgentRunResult`，公开投影只保留工具、Schema action、状态和计数，贯穿 Gateway/HTTP、正常与延迟
  transcript、后台任务、历史索引和 compact；包括零操作回复。当前用户正文不再追加固定核验块；
  机器投影留在 metadata，出口仅用本轮 typed records 精确隐藏意外泄露的内部工具标签。真实 B 反证
  仍定义能力边界：这能证明程序实际做了什么或没做什么，但在
  禁止自然语言语义判断时，不能理解并删除自由正文里的每一句错误自述。
- 真实 MiniMax compact 又复现出“程序只有 remember/list，摘要却说成功删除”。现由
  `conversation_thread.v4.compact_operation_evidence` 将有界程序证据与摘要/cursor 原子保存，下一轮
  在错误摘要之后仍看到唯一 `remember/list` 并正确回答未删除。没有添加摘要关键词修复或第二条
  compact；缺旧 metadata 时只标 coverage partial。
- route 关键词冲突、缺 authority、损坏/缺失恢复包、Gateway、subagent 与 local doctor 已由现有联合
  测试复验；原 ROADMAP “Memory 第二批”移入完成。my-agent 的 active Memory 在锁内原子
  write-through，没有 长期助手 外部异步 provider 的 pending queue，因此没有复制 pre-compact flush。
  只由自身测试调用、会绕过统一 Memory 工具/来源/配额合同直接改 HOT/lesson 的旧 helper 已删除。
- 最终 1.10 复验沿 A/B 原 owner/conversation 运行：A 请求
  `req_1785050485322_1318040_0` 真实完成 add/list/remove/list；B 首轮
  `req_1785050485332_1318040_1` 的机器事实为零操作，同会话纠正请求
  `req_1785050618969_1318040_2` 才形成四条 succeeded operation。双方测试值在 active Memory
  与对方 Memory 中均为 0，Persona/Skill/项目/子代理无改动；随后各一次真实飞书发送 receipt 为 sent。
  最终 8,315 项完整 pytest 与发布门通过，1,008-member wheel
  `b3084c12009b259aa1b50f4954a51c9ebcbfb6f0230990d1a4f1f3200657f1f2` 已于
  2026-07-26 15:19 CST 部署到唯一正式 1.10 Gateway/Feishu。

## 2026-07-25 长期记忆来源、召回与删除边界收敛

- `memory/long_term/memory.jsonl` 继续是每个 owner 唯一可召回长期记忆事实源；没有新增 provider、
  IM 专用记忆或第二套 compact。当时 v1 的 `memory/ops.jsonl` 还混放模型推测候选；该历史路径已在
  2026-08-04 迁入 `memory/candidates.jsonl` 并从生产链删除，当前 `ops` 只保存无正文操作审计。
- `remember` 的 add/replace 新增结构化 `origin/evidence_refs/subject_key`：用户明确事实可写入；
  `tool_verified` 只能引用本轮成功工具归档中的结构化 ref；`model_inferred` 只落候选账本。
  来源与工具成功不从自然语言正文猜测。
- 完全相同的规范化记忆写入幂等；同一 `subject_key` 的不同事实返回冲突和现有稳定 ID，要求
  list 后 replace，不做语义自动合并或静默覆盖。replace 保留旧结构化属性并合并新来源字段。
- remove 改为 hard delete：当时 v1 会清理权威 JSONL 中该 ID 的历史正文、daily mirror、LocalStore/FTS 内容文件、
  向量项、ops 中同正文候选以及结构化 remember 工具账本中的正文，只保留无正文
  tombstone/hash/调用终态审计；精确重试同一 remove 仍幂等。raw conversation 与 gateway audit
  属于独立留存事实，不随长期记忆删除而改写。2026-08-04 的 v2 已删除 daily mirror 与 ops 候选正文。
- JSONL、daily 与 LocalStore 的关键词召回已统一到同一文本规范化函数；召回先取有界候选池，
  再按文本相关度、结构化来源、更新时间确定性排序，并按 subject 去重。配置 embedding 时仍以
  原 RRF 结果顺序为相关性主线，不新增模型 reranker。
- prompt 中的 Related Memory 改为 `<memory-context>` 结构化非权威数据块；加载时再次做安全扫描，
  JSON/markup 转义防止记录伪造闭合标签。CLI、Gateway、飞书等共用用户出口统一删除完整或截断的
  memory-context，内部信封不会成为用户正文。
- 威胁扫描从 capability 私有模块移到 `memory_store/security.py`，Memory、Persona repository、
  Persona tool 与人格文件写守卫共用；旧转发模块已删除。
- 聚焦测试覆盖并发重复写、subject 冲突、成功/失败工具证据、候选不召回、hard delete 多层无明文、
  remove 重放、恶意/截断信封和语义召回；完整 Memory/Persona/Prompting 测试组已通过。

## 2026-07-25 工具失败分层事实进入 compact/recovery

- 工具归档的显式白名单新增 `failure_stage`、`handler_executed` 和非负 `duration_ms`；它们与既有
  `error_code` 分工：错误码说明发生什么，阶段说明坏在哪一层，handler 标记说明真实实现是否进入。
- live context、短输出 index、外置 record/artifact/index、compact semantic summary、机械恢复和
  runtime ledger 使用同一组字段。compact 对中段未成功副作用仍保留精确事实块，不允许语义摘要把
  timeout/effect unknown 改写成成功或自动重试。
- 归档入口逐字段验证 enum、bool 和非负整数；工具私有字段、原始敏感参数和畸形诊断值仍被丢弃。
  幂等重放的本次 `handler_executed=false` 与首次执行的嵌套事实分开保存，恢复时不会谎称再次执行。

## 2026-07-25 工具结果投影贯穿 compact 与恢复

- tool-output archive 继续保存当前 owner/task 的完整原文、hash、大小和稳定引用，但 preview 在落入
  record/index 前已按 `ToolRuntimePolicy.output_policy` 脱敏；索引只额外保存有界的 `tool_output_trust` 与
  `tool_output_redaction`，不保存任意 envelope。
- compact semantic summary、机械恢复、runtime event 和 handoff 使用这两个 typed 字段重建同一模型
  投影。外部网页/MCP/浏览器结果即使通过纯文本 artifact、`read_file` 或 `search_text` 再进入历史，
  仍是数据而非指令；原始 artifact 不被改写，审计事实与模型安全投影保持分层。
- 聚焦 externalizer/compact/runtime event/恢复回归、本地 Qwen 四轮归档再读取和 MiniMax 两轮
  `read_artifact` 通过；完整发布证据以本轮最终门禁与产品事实页为准。

## 2026-07-25 长任务截断续接与运行事实终态一致

- 对照 会话运行时 `会话运行时-api/src/sse/responses.rs`，provider 明确返回 incomplete 仍被视为失败响应；普通聊天
  没有已完成工具结果时保持原合同，立即返回结构化 `MODEL_INCOMPLETE_RESPONSE`，不把半截正文当成功。
- 对照 长期助手 `agent/conversation_loop.py` 的有界 length continuation，只有当前运行已经形成耐久
  tool record/tool output 时，才允许一次继续采样；半截正文和未闭合工具参数全部丢弃，已完成工具结果和
  已写产物保留。一次继续仍截断就终止，不做无界重采样；任意一次正常模型响应后，修复额度按新的
  provider 调用重新计算。
- 若 `/btw` 的 typed UserTurn 已在 mailbox 等待，正在生成的空或 incomplete 旧响应不能抢先结束任务；
  它只在安全点被丢弃，guidance 进入同一个 turn 后再采样。该决定只看结构化 pending input，不解析文字。
- 运行在模型、工具循环或 finalization 阶段异常时，已有 `memory_archive/runtime_facts/<request>/task.json`
  会从 `running` 收敛为 `failed`；用户中断收敛为 `cancelled`。既有工具轮数、已执行工具、artifact 和
  next actions 保留，另写结构化错误，避免 Gateway 已失败而长期 Memory 仍谎报运行中。
- 聚焦回归覆盖普通聊天不续跑、工具后一次续跑、连续截断只续一次、相隔成功轮后的独立空响应、异常终态
  和用户中断终态，共 61 项；另有 2 项 `/btw` 空/incomplete 旧响应测试通过。最终本地全量 pytest
  到 100% 且退出 0，Ruff、import/offline、strict code-size、doc-sync、compileall 与 diff gate 均通过。
  worktree clean-package 正确拒绝保留的未跟踪文件和大体积运行数据；只有新建 wheel 的 artifact gate
  可以作为发布干净度证据。最终 wheel SHA-256 为
  `995dee17dfe6327eb40df4de96686796ad73d5e5aad1ba4d8c7716e688347553`，distribution boundary 与
  artifact clean-package 均通过。

## 2026-07-24 工具参数来源耐久索引

- 工具执行产生的 value-free `input_sources` 现在与既有 read/page window 共用一条白名单投影：
  短输出进入 tool-call index，大输出同时进入 archive record、artifact 和 index。
- 每项只保留参数 JSON 路径、来源类别和结构化 `source_ref`；参数值、命令正文、凭据、换行文本和任意
  envelope 私有字段不会持久化，compact 回放也不能据此扩大权限。
- 没有真实 artifact 的内联短输出不再向模型展示 `read_artifact` 提示；scoped call id 仍保留在耐久
  index 供审计，只有已落盘 artifact 才进入模型可读的恢复链。
- 1.10 重启后的两个真实飞书 owner 分别以请求 `req_1784884678795_420766_0` 和
  `req_1784885085848_420766_1` 留下同构来源索引；两边都只引用自己的
  `write_boundary.task_root`，无参数原值、跨 owner 路径或额外工具记录。

## 2026-07-18 Memory/Persona 单一 repository 与可修正记录

- 长期 Memory 继续只使用当前 owner 的 `memory/long_term/memory.jsonl`，没有建立第二份数据库或 IM 专用记忆。
- `remember` 单工具新增 add/list/replace/remove/batch；操作带稳定 ID、版本、来源、kind 与可选过期时间。
- JSONL 写入在同一文件锁内重读、验证并原子提交；batch 任一项失败整批回滚。当时 v1 的 daily mirror
  已在 2026-08-04 被迁移并删除，当前正式操作只写无正文 `ops.jsonl` 审计。
- SQLite/FTS 与可选向量索引只是派生层；检索返回前对照 JSONL 当前 active state，删除或旧版本不会从陈旧索引复活。
- 新增 `PersonaRepository` 统一 SOUL/USER/AGENTS 的加载与更新：2 MiB UTF-8 文件上限、symlink/owner 边界、逐行威胁扫描、prompt budget、版本/CAS/回滚和结构化诊断。
- USER 仍只允许基于当前用户原话自主维护；SOUL/AGENTS 仍需确认。飞书确认保存点击前 SHA，确认期间发生并发修改会拒绝覆盖。
- Memory/Persona runtime snapshot 已进入 `list_capabilities`，只暴露健康、计数、版本和错误码，不暴露正文或私有路径。
- Memory 的权威 JSONL 与当时 v1 的 daily mirror、Persona 的正文/backup/version ledger 均作为一次 owner quota
  batch 准入；quota lock 在各自 repository/file lock 外层，拒绝时不会留下半批文件。
- Memory/Persona、能力自述和 prompt 聚焦回归与完整本地 CI 通过；提交、1.10 部署与真实多用户验证仍待最终阶段。

## 2026-07-18 compact 百分比阈值收敛

- 删除工具上下文在已达到配置阈值后仍可继续到 95% 的 digest 缓冲状态机；正文工具到达阈值后直接登记
  `CONTEXT_COMPACT_DEFERRED`，统一进入既有 compact/resume。
- 主会话投影不再把尚未发生的最大输出额度加到当前上下文占用；模型返回后，真实 provider input/output
  usage 仍计入 active context。配置 90 因而不再被一条路径提前、另一条路径延后。
- standalone suggestion 的非法百分比回退也统一为正式默认 90%，并删除对应的 pending/inflight 死状态与测试。

## 2026-07-16 compact 与普通任务完成解耦

- compact semantic summary 继续使用独立轻量 `generate(prompt)` 接口，不依赖已删除的
  `run_learning_review` 或任何验收器。
- compact 只压缩并恢复对话、工具 refs、读取游标和结构化 task/goal 状态；它不保存旧验收失败，也不在
  恢复后自动提交验收。普通任务恢复后仍由主模型基于当前事实自然完成，`/goal` 则读取持久 goal state。

## 2026-07-16 工具错误索引保真

- tool-output record、外置 artifact 和 `index.jsonl` 同时保存统一控制用 `error_code` 与来源工具原始
  `reported_error_code`。运行时可按已注册控制码决定重试/修参，排查时仍能看到协作工具、provider 或
  下游系统的真实报码；短输出和大输出走同一字段语义。

## 2026-07-25 副作用终态穿过 compact

- tool archive 现在显式保存 operation id/status/action/replayed、idempotency scope 与
  effect outcome/source ref；`reported_tool_result` 只保留布尔、错误码和引用，不复制工具正文或私有
  envelope。
- 机械续跑逐项恢复这些 typed 字段。中段语义摘要仍是非事实源；其中失败、运行中或 unknown 的副作用
  会额外保留一条精确事实块，防止 compact 后误报全成功或自动重做。成功/失败/unknown 的执行权威仍是
  原 operation store，没有新增 task compact、Saga 或第二账本。
- 聚焦 archive、机械续跑、语义摘要和 operation store 故障回归已通过；完整模型与正式通道证据完成前
  保持候选状态。

## 2026-07-13 compact 正式默认收敛

- `AgentConfig`、memory coercion、runtime policy、standalone suggest/auto-cycle 与包内 YAML 的正式默认
  统一为 90%；50% 仅作为显式压力验证覆盖，未配置和非法配置不会再漂回旧 70%。
- 当前轮触发预算仍取厂商 usage 与本地 prompt 估算的较大值，累计 token 只记账、不参与当前轮判定。

## 2026-07-09 P0 lint 收敛

- `compact_semantic_summary.py` 仅做 Ruff 要求的 `Callable` 导入归属清理；compact、摘要、线程和持久化语义没有变化。

## 2026-06-11 记忆推模式扩展到 planner 决策点（历史 v1，已由 Memory v2 收敛）

- 当时的 **B1 planner 注入**：父代理 planner 出决策前自动注入 planning 类教训——
  `planner_service.append_planner_memory_hint`（查询上下文取 due_issues/active_tasks
  首条 goal），接线在 `_execute_planner_llm` 的 prompt 构造后。软注入：无 memory/
  检索异常一律原样返回，绝不阻断 planner。注入点覆盖从"仅 runner 失败点
  （runner/gate）"扩展到 planner 决策点；dispatch 本身无独立 LLM 决策面
  （planner 即其决策面），机器路径不注入。
- 当时的 **B1 检索缺陷修复**：`memory_push._build_memory_query` 曾写成 `len(goal)>50`
  才把 goal 加入查询——中文短 goal（常态）被整个丢弃，查询只剩英文 trigger 词，
  中文教训永远搜不到，推模式形同虚设（失败点注入同样受害）。现在 goal 非空即入
  查询、超 50 字才截断。
- 当时的 **B2 注入幂等**：planner 注入与 runner 失败注入（`_append_memory_hint`）都做
  "同一 hint 已存在则不堆叠"——失败重试循环不再把同批教训反复追加进派工指令。
- Memory v2 已保留关键决策点主动召回与幂等能力，但删除旧 long-term
  `kind=lesson_general/lesson_task`、`MemoryType/MemoryEntry/MemoryWriteContext` 和直接写入函数。
  当前 `memory_push` 只读正式 Lesson/HOT、按 typed scope 过滤，并与普通运行共用唯一
  `<memory-context>`；失败自省只提交待审 Candidate。对应钉子已更新为正式 authority、
  scope、只读性、错误关闭和单信封测试。

## 2026-06-11 compact 连续失败熔断（防 thrash）

- 新增 `compact_circuit_breaker.py`：compact 连续失败达阈值（默认 3）即 open，
  冷却期（默认 300s）内 `run_memory_compact_auto_cycle` 跳过 apply 返回
  `blocked_circuit_open`，避免反复失败空烧 API（参考 终端交互 circuit breaker，
  实测 thrash 可日烧 250K calls）。冷却过后 half-open 放行重试；一次成功清零回 closed。
- 状态持久化 `workspace/compact/circuit_breaker.json`，跨 turn 生效；`now` 可注入便于测试。
- 钉子测试 `test_compact_circuit_breaker.py`（7 项：阈值/开合/冷却/清零/集成跳过）。

## 2026-06-10 compact_context_bundle 包→单模块

- 3 文件（match/refs/init 转发）合并为 1 个 `compact_context_bundle.py`，删除一跳 facade；
  compact_apply / compact_resume 的导入路径不变，行为不变，focused compact 测试全绿。

## 2026-06-07 恢复模式结构化边界

- 长 `write_file.content` 流式中断由 tool-stream 边界写结构化
  `source_tool`、`path`、`content_field_present`、`streaming_content_*`
  字段；后续 long content recovery 只读取这些字段。
- parse error 的 `raw` 和工具错误正文只保留为诊断/展示文本，不再用正则从里面猜
  `tool/path/content` 来触发恢复模式，避免恢复流转依赖未解析正文或自然语言提示。
- 普通 `write_file` 失败输出里提到 “inline content 过长” 也不会单独触发机器恢复；
  要触发恢复必须由上游工具/流边界提供结构化错误码和字段。
- compact/resume 不再根据 `memory-resume`、`LocalStore`、`restore_refs` 等文本片段过滤
  `next_action` / `next_actions`；机器续接优先级只由结构化 read coverage、cursor、
  `resume_focus` 和 work_state 字段决定。
- 主代理 task workspace 复用只认当前 `work/run_workspace.json`；`work/task.yaml` 是可读说明，
  不再作为旧目录身份兜底。
- compact work-state 的 read coverage 从单一 primary 游标扩展为
  `primary + sources + incomplete_sources`：`incomplete_sources` 用于多文件/多项目任务的
  下一段续接队列，`sources` 用于覆盖审计和 resume context 展示。
- `list_files`、`find_files`、`search_text` 统一写结构化 `page_window`，tool output 归档和
  compact work-state 从该字段恢复分页 offset；机器续接不再依赖输出正文里的 `next_offset` 文案。

## 2026-06-06 Compact 事实源收敛

- compact/resume 的 read cursor 只认当前工具记录的结构化成功事实：`ok: true`，或无错误且
  `status` 为空/`ok`。`succeeded`、`completed`、`done` 等旧字符串不再算已读。
- runtime fact source 的 run phase 只认当前结构化状态：`DONE` 是 final；
  `FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`CANCELLED`、`ABANDONED` 是 terminal；
  `succeeded` 等旧状态别名保持 running，避免 compact 后把旧残留误恢复成已完成。
- raw archive 工具布尔只接受真正 bool 或机器布尔字面量 `true/false/1/0`；`success/error/ok`
  这类状态词不再变成工具成功/失败事实。
- task progress 的 items、coverage 和 soft quality hints 已收回 `task_progress.py` 单入口。
  进度状态只认结构化 `pending/in_progress/done/skipped/blocked`；未知值保留为普通状态文本并归入
  `unknown`，不会被多语言自然词猜成待处理、已完成或阻塞。

## 2026-06-04 收敛

- 主代理长期记忆只写当前 owner `memory/long_term/memory.jsonl`。
- 每日工作记忆写当前 owner `memory/daily/YYYY-MM-DD.jsonl`。
- raw archive 写当前 owner `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 task `work/blobs/tool_outputs/`。
- 保存型任务写 `tasks/<date>/<task-slug>/{output,work}/`；任务名来自短标题，不直接截取整段 prompt。
- 子代理只保留 task-local 状态、事件、artifact refs 和 compact，不拥有长期记忆。
- 普通恢复、compact、tree 和 doctor 走当前 owner/task/run/agent refs，不再扫描 repo `data/*` 作为事实源。
- owner projection/global index 是查找地图，可重建；canonical state 和 task workspace 才是权威。

## 2026-08-12 CLI run ConversationStore 证据补齐

- 真实隔离 B5 发现公开 `my-agent run` 会调用 `remember`，但用户输入只进 audit、不进
  ConversationStore，导致 `user_explicit` 候选缺 `source_message_refs` 并永久停在
  `blocked_missing_evidence`。
- canonical 修复位于共享 `agent.run` 入口：仅 `source=cli_run` 在模型执行前建立 one-shot thread，
  使用 `append_message_once` 写 user，并把精确 thread id 带入本轮；最终 assistant 投影同样幂等落账。
  不预填 `conversation_task_id`，避免把 standalone CLI task 伪造成 active ConversationStore task link。
- 用户输入落账失败时 fail closed，模型调用次数必须为零；assistant 落账失败只通过 typed
  `conversation_persist_degraded/error` 暴露，不把 audit 升格为第二消息权威。
- CLI 继续走 standalone workspace 终态投影；Gateway conversation 的既有跳过分支保持不变，避免
  CLI `state.json` 残留 RUNNING 并制造 `status_conflict` 诊断噪音。
- 新增 focused 测试覆盖真实 remember 晋升、message ref、user/assistant 幂等、失败分级、CLI
  workspace DONE 与 Gateway skip 回归；本轮不重复运行全量 pytest。
- 隔离 testbox B5R3 已用真实 `anthropic_compatible/deepseek-v4-flash` 和普通中文请求完成
  `cli_run -> ConversationStore -> remember -> Candidate -> Promotion -> long_term`：CLI RC=0，user/assistant
  两条消息共享同一 request/run/task lineage；唯一 Candidate 为 `promoted`，唯一
  `source_message_ref` 精确指向本轮 user message，正式长期记忆复用同一证据；workspace=`DONE`、
  unfinished 为空、agent run=`done`、`status_conflict=0`。同 request 的纯存储重放没有再次调用模型，
  messages/Candidate/formal/ops/runtime/tool-operation 计数与 ID 全部不变。
- B5R3 中模型为修正 `remember` 参数真实调用了任务型工具，因此产生一条真实 task link，最终为
  `completed`，`active_task_ids/active_task_links` 均为空。验收口径是“adapter 不伪造 link；没有真实
  task-promoting tool 时 links 为空；发生真实任务晋升时 link 必须终态”，不能把所有场景机械要求为
  `task_links=[]`。生产 Gateway/飞书 PID 与 8420 监听在前后保持不变，隔离端口未监听；脱敏证据保存在
  testbox `/root/memory-evidence/会话运行时-MEM-20260812-B5R3/`。
- B5R-F0（缺显式 text 协议）和 B5R2（测试 shell 使用过期凭证）均在独立目录失败并保留原始证据，
  没有手工补写 Memory。两次模型前失败都暴露既有通用缺口：CLI 打印 traceback，standalone workspace
  残留 `RUNNING`；它们不影响本切片成功链，但必须另开窄修复，不能误报为已解决。

## 下一步

- 把 remaining docs 和 tests 里只服务历史路径/历史字段的描述继续删除。
- RAG/向量层接入时优先索引 `memory/long_term/memory.jsonl`、`memory/daily/`、`lessons/` 和 artifact refs。
- 精确 grep 查询保留 JSONL 明文字段，避免只剩二进制索引。
