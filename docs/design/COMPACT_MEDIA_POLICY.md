# 媒体会话的压缩策略：归档引用为主链，视觉摘要按结构化能力事实开启

状态：用户已定方向（2026-09-24）——A（旧媒体降级为可重新附上的归档引用）是主链和默认；B（含图轮次随文字进摘要）只在模型的视觉能力事实为 `supported` 时启用，事实只能来自档案声明或结构化探针，不能来自模型自述。决策线评审意见已并入本稿（见"评审并入的决定"）。片 A 已合入 main `6bb0467b1` 并双机部署，真实 TUI 验收通过（TESTS.md）；片 B 已本地实现（分支 `claude/compact-media-b`，实现记录与偏差见文末），真实 TUI 验收见 TESTS；片 C 待实施。上位合同见 [TUI 图片视频](TUI_INPUT_MEDIA.md)、[容量审计](../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

## 解决问题

- transcript checkpoint 只能覆盖连续前缀，而摘要模型走纯文字协议：`_split_nontext_transcript_suffix` 从首个非文本回合起保护全部后缀。首张图之后的文字轮永远进不了摘要，会话余量等于窗口减去"图片之后的全部内容"，越窗只能 typed 拒绝（`COMPACT_REQUEST_NON_TEXT`）并让用户新开会话。
- 三处宿主门（preflight、恢复宿主 `select`/`_automatic_noop`、轮内 `_prepare_native_compact_plan`）都以"含非文本即容量未知"为前提，把"能不能摘要"和"能不能计量"两个问题混在一个判定里。

## 原则（对照铁律）

- 自然语言不做判定：视觉能力只来自档案的结构化声明或宿主发起的结构化探针；模型口头说"我能看图"无效。
- 主链优先：A 是默认路径，不依赖任何模型能力；B 是按结构化事实开启的增强。B 的摘要调用失败时沿现有规则不提交、原文保留，**不在同一次请求里悄悄退回 A**；改走 A 只能由持久化的结构化条件触发（见"B 失败后的切换"）。
- 一个概念一个权威位置：canonical 消息行与 `local_file` 引用永不改写；策略选择、命中的能力事实、被归档的附件引用都写进 checkpoint payload 的结构化字段。
- 硬门只守客观事实：图片 token 没有计量器，B 的容量只按配置声明的每块预留计算，不猜；算不下就是算不下。
- 开放世界：已知媒体严格等于运输层 `provider_media_messages` 会展开的集合——顶层 user 行里 `source.type == local_file` 的 `image`/`video` 块；嵌在 `tool_result` 里或 assistant 一侧的媒体块、base64 块与其它未知非文本块，沿现行规则继续保护后缀，不摘要。分类器与投影函数必须用同一判定，避免"分类说能摘、投影却漏换"。

## 结构化事实与判定

1. 块分类（`backends/request_content.py`，新增 `classify_nontext_content(messages) -> NonTextClasses(media=n, unknown=m)`）：`text_messages_supported` 保持严格文字语义不变；新判定 `compact_source_supported(messages, policy)` = 无 unknown 块，且（无 media 块 或 policy 不为 `off`）。三处宿主门改用它判断"能不能摘要"；"能不能计量"另见容量规则。
2. 媒体策略解析（`conversation/compact_media_policy.py`，新模块）：`resolve_compact_media_policy(agent, media_blocks) -> CompactMediaDecision(policy, fact_source, reserve_tokens)`：
   - 配置 `compact_media_policy: auto | archived_refs | off`（默认 `auto`）。`off` 保持今天的后缀保护；`archived_refs` 永远走 A。
   - `auto`：档案 `input_modalities` 含 `image` → 候选 B；声明不含 → A；未声明（未知）→ 运行一次结构化视觉探针；`supported` → 候选 B，其余 → A。
   - 候选 B 还要过容量：`估算文字 token + 图块数 × input_media_token_reserve <= 摘要预算`（`generate_bounded_compact_response` 的 `budget`），否则本次直接选 A。这是请求前的结构化决定，不是失败后的兜底。
   - `video` 块在本设计里只走 A（视觉摘要暂不接视频）。
3. 视觉能力事实：
   - 档案字段 `input_modalities`（可选，`list[str] ⊆ {text, image, video}`；缺省为未知），在 `model_provider_schema.validate_model`、`validate_model_profile` 白名单和 `resolved_model` 三处同步登记，经 `AgentConfig.model_input_modalities` 到运行时。决策线的用途标签可按同样方式加平行字段，不共用容器。
   - 探针 `HttpBackend.probe_vision_capability()`：与 `probe_tool_capability` 同款——按后端实例单飞、只发一次；请求为一张进程内生成的 8×8 纯色 PNG（base64，不落盘）加固定说明，附工具 `my_agent_vision_probe(color: enum[red, green, blue, yellow])`，只认与真实颜色一致的结构化 tool_use 为 `supported`。供应商对媒体块的 typed 拒绝（`ProviderRequestRejected` 类）或答错/不调用 → `unsupported`；网络、额度、认证等 `ProviderRecoverableError/ProviderConfigurationError` 原样上抛、不缓存、本次按 A。阳性与阴性都只缓存在后端实例内（同一 Gateway 进程、同一模型），进程重启即重探；不写档案文件。
   - 事实来源枚举 `fact_source ∈ {declared, probe_supported, probe_unsupported, probe_unavailable, policy_forced}` 进 checkpoint。

## A 路径：归档引用（片 A，已本地实现）

- 实现落点：`backends/request_content.py`（`classify_nontext_content`/`compact_source_supported`/`is_local_media_block`）、`conversation/compact_media_policy.py`（策略解析、`project_archived_media_message`、`media_archive_facts`）、`agent_core/tool_request_projection.compact_request_source_supported`、`compact.py`/`compact_provider_surface.py`/`compact_checkpoint.py` 的接线、`context_pressure.py` 与 `compact_request_recovery.py` 两处门、`AgentConfig.compact_media_policy` 与 YAML。`_summarize_segments` 保持严格 `text_messages_supported`，作为媒体块漏换时的最后一道安全网。旧语义测试全部按 `off` 钉住，`auto` 语义另写。

- 位置：摘要来源构造（`compact_provider_surface.conversation_compact_provider_source` 的 replay 与 `compact._legacy_conversation_summary_prompt` 的 `_summary_content`）。对 user 行里的 `local_file` 媒体块做只读投影，替换为文字块 `[附件引用 image sha256:<前 12 位> 名称:<name> 大小:<bytes>；已归档，未随本次摘要发送；需要重看时请重新添加同一附件]`。不带 owner 目录绝对路径（现有 `project_input_media` 的归档文字带路径，是运输层的另一处，本片不改它）。
- `_split_nontext_transcript_suffix` 的保护判据改为"存在 unknown 非文本块"；media 块不再保护，因此含图回合进入安全前缀，由原分区/checkpoint 覆盖。近期尾部保护（`compact_partitions`）不变，最近的含图回合通常仍留在尾部原样保留。
- 摘要指令追加一句软引导：附件已替换为引用，摘要须保留引用与当时基于图片得出的结论。
- checkpoint payload 新增结构化字段：`media_policy`（`archived_refs`）、`media_fact_source`、`media_blocks_archived`、`media_refs`（sha256 列表）。提交后覆盖行不再重放，图片自然离开模型视野；canonical 行与附件原件不删。
- 宿主门口径同步：preflight 的门槛在 `compact_source_supported` 为真时恢复为压缩点（媒体不再使压缩链不可用），窗口与输出预留硬上限照旧；恢复宿主 `select`（force）只在 unknown 块或 `policy=off` 时报 `COMPACT_REQUEST_NON_TEXT`；`_automatic_noop` 同理。`_prepare_native_compact_plan`（轮内工具 IR 压缩）本片不改，另开一片核对其计量前提。

## B 路径：视觉摘要

- 只在单请求模式生效：`generate_bounded_compact_response` 判定整份来源能容纳（文字估算加每图预留）时，摘要请求保留媒体块，经原 `project_input_media`/`provider_media_messages` 在网络边界展开为 base64，与业务请求同一条路。
- 分段模式（`_summarize_segments`）读的是 JSON 字符流，不能承载图片：进入分段前若来源含媒体块，本次直接以 A 重建来源，不进入 B；这一步同样是请求前决定。
- `_summarize_segments` 的 `COMPACT_SOURCE_NON_TEXT` 检查改为 `compact_source_supported`，unknown 块仍拒绝。
- checkpoint 字段：`media_policy=vision_summary`、`media_fact_source`、`media_blocks_summarized`、`media_refs`。
- B 失败后的切换：B 的摘要调用以 typed 错误失败（供应商窗口/媒体拒绝、截断）时，沿现有 `record_compact_failure` 把码写到线程（新增 `COMPACT_VISION_SUMMARY_FAILED`，`compact_failure_code` 与 `compact_failure_updated_at` 已是结构化字段）；同一代次内下一次压缩读到这个持久事实即选 A，并把 `media_fact_source=policy_forced`、`media_policy_reason=COMPACT_VISION_SUMMARY_FAILED` 写进 checkpoint。谁切：恢复/自动压缩宿主在解析策略时切；条件：线程上记录的最近一次压缩失败码属于 `COMPACT_VISION_*` 且 `compact_generation` 未推进。

## 评审并入的决定（2026-09-24，决策线评审）

- B 不接单请求的供应商窗口错误回退：`generate_bounded_compact_response` 在 B 模式下遇 `ProviderContextWindowError` 直接抛 typed `COMPACT_VISION_SUMMARY_FAILED`，不减半预算转分段（分段不能承载图片，转分段等于同一请求内静默退回 A）。
- B 的请求前决定再加运输层字节预算：`Σ size_bytes <= input_media_max_bytes`，否则选 A；B 模式下 `InputMediaError` 一律归到 `COMPACT_VISION_SUMMARY_FAILED`，避免 `project_input_media` 的归档文字（含绝对路径）进入摘要。
- B 只用于压缩点的自动压缩；强制恢复（窗口或供应商施压，`forced=True`）一律走 A。这是按 force 标志做的请求前决定。
- B 失败的切换条件用专用结构化字段 `compact_vision_failed_generation`（线程上），不用会被无关失败覆盖的 `compact_failure_code`；B 的 typed 失败不计入 `compact_consecutive_failures` 熔断，避免让随后的 A 等 300s 冷却。
- 探针：只有供应商对图片块的 typed 拒绝缓存为 `probe_unsupported`；答错或不调用工具记 `probe_inconclusive`，按工具探针方式有界重试且不永久缓存；探针依赖工具调用，须在 `probe_tool_capability().native_supported` 为真后再发，否则记 `probe_unavailable`。策略解析（读 `input_modalities` 或跑探针）必须与摘要调用处于同一个模型绑定（`model_dependencies_scope`）。
- 探针缓存位置：实测同一 Gateway 进程内 owner Agent 在空闲 60s 后被 `release_idle_owner_agents` 释放，后端实例缓存随之清空，所以"每进程每模型一次"不成立。片 B 采用进程级缓存，键为 provider endpoint + model + 连接修订，放在 owner Agent 之外；阴性口径不变。
- `input_modalities` 校验为任意小写标识符列表（开放世界），解析器只处理认识的值，不认识的保留并忽略；决策线的用途标签平行字段按同一规则。
- 计量缺口：三处门放开后，preflight、`_automatic_noop`、候选接受数仍按"媒体引用字节不算视觉 token"估算，会低估并依赖供应商窗口错误那条恢复路径兜底；片 B 把同一个 `input_media_token_reserve` 加进这几处估算。
- 新近可达路径：会话没有已完成历史、只有本轮带图时，恢复宿主走 `_compact_active_source`（来源只有工具记录与 IR，UserTurn 的图原样留在候选里，不需要 A 投影）；片 A 后续补一例"新会话首条带图、工具循环越过压缩点"的回归。
- `_prepare_native_compact_plan`、模型切换判定（`gateway_model_adoption`/`subagent.model_selection` 里的 `text_request_capacity_known`）保持严格语义不变：它们回答的是"能否换给另一个模型"，要等 `input_modalities` 落地后由决策线处理。

## 配置与错误码

- `agent_config.yaml`/`AgentConfig`：`compact_media_policy`（默认 `auto`）、`input_media_token_reserve`（默认 1600，每个图块的摘要预算预留；仅 B 使用）。两处同时更新并加中文注释。
- 错误码：保留 `COMPACT_REQUEST_NON_TEXT`（unknown 非文本或 `policy=off`）；新增 `COMPACT_VISION_SUMMARY_FAILED`（不可重试、建议保留原文），登记到错误分类表与 `gateway_client_error_message`。

## 验收

- 合同单测（fake backend，不发网络）：含图前缀在 A 下产出带 `[附件引用 …]` 的摘要来源、canonical 行与附件原件不变、checkpoint 字段齐全；unknown 块仍保护后缀；`policy=off` 行为与今天逐字相同（现有媒体测试全部保留）。
- 门口径：`test_media_compact_preflight.py` 的九项矩阵在 `auto/archived_refs` 下改为"带图与纯文字同样按压缩点"，`off` 下保留现矩阵；恢复宿主 force 对媒体不再报 `NON_TEXT`。
- B：声明 `image` 的假后端记录到摘要请求含 base64 图块；超预算时选 A 且无媒体块外发；B typed 失败后写码、下一次同代次选 A 并留结构化原因。
- 探针：正确颜色 → supported 缓存；typed 媒体拒绝 → unsupported 缓存；网络错不缓存；三种都用计数替身钉住只发一次。
- 真实 TUI：M3 会话先贴图再读长文越过压缩点 → 自动压缩成功，checkpoint 有 `media_policy`；压缩后问图：A 下模型如实说明附件已归档并请求重新添加，B 下按摘要作答；M2.7 会话在 `auto` 下探针为 `unsupported`、走 A。

## 片 B 实现记录与偏差（2026-09-24，按代码事实调整，不改原则）

- **落点**：`backends/vision_capability.py`（探针 + 进程级缓存，键 = 工具端点 + 模型 + api_base，同键单飞；只缓存 supported/unsupported）；
  `conversation/compact_media_policy.py`（`resolve_compact_media_policy(agent, forced=, thread=)`、`vision_capability_fact`、
  `vision_summary_admission`、`MediaArchiveFacts.bytes/videos`、`COMPACT_VISION_SUMMARY_FAILED`）；`compact.py::_effective_media_decision`
  在摘要请求构造处做准入；`compact_request_budget.generate_bounded_compact_response(vision_summary=, media_reserve_tokens=)`
  单请求发送并把窗口错误/媒体拒绝/截断/超预算统一抛 typed 码；线程新增 `compact_vision_failed_generation`（默认 -1，压缩成功重置）；
  checkpoint 新增 `media_blocks_summarized`、`media_policy_reason`。
- **档案字段**：`input_modalities` 登记在 `validate_model`、快捷新增白名单与 `resolved_model`（→ `AgentConfig.model_input_modalities`），
  `/model` 表单多一个"输入模态"输入；决策模型不接受。开放小写标识符，只处理 `image`/`video`/`text`。
- **探针时机**：`resolve_compact_media_policy` 只给骨架决定（auto 下为 archived_refs + `vision_candidate`，fact_source `vision_fact_pending`），候选构造在确认压缩范围内确有媒体块后才调用 `resolve_vision_candidate` 读声明或发探针；纯文字压缩零额外请求（首版把探针放在解析处，两个 Gateway 压缩回归多出 2 次模型调用，据此改正）。
- **探针细节**：答错或不调用工具最多重试 2 次后记 `probe_inconclusive`（不缓存）；`probe_tool_capability().native_supported`
  为假记 `probe_unavailable`（不缓存）；网络/额度/认证错误在策略解析处捕获为 `probe_unavailable`、本次按 A、只记异常类型，
  不让一次探针网络错拖垮压缩主链（原稿写"原样上抛"，此处改为在策略层吸收，探针缓存仍不写阴性）。
- **准入顺序**：视频块 → 字节预算（`input_media_max_bytes`）→ 摘要预算（文字估算 + 图块数 × `input_media_token_reserve`）；
  legacy prompt（无 provider 缓存面）不能承载图块，一律 A，reason `legacy_prompt`。
- **计量缺口保留**：preflight、`_automatic_noop` 与请求投影器的候选接受估算仍未加图块预留（原稿"三处门"只在 B 准入与
  `generate_bounded_compact_response` 落地）；低估仍由供应商窗口错误的恢复路径兜底，作为片 C 前的已知偏差。
- **失败切换**：`_compact_pending` 对 `COMPACT_VISION_SUMMARY_FAILED` 只写 `compact_vision_failed_generation`，不增加
  `compact_consecutive_failures`；同代次下一次压缩在解析策略时读到它即选 A，checkpoint 记 `media_policy_reason` 为该码。

## 边界与风险

- 图片 token 没有真实计量，B 的预留是配置声明；供应商实际计费与预留不一致只影响是否选 B，不影响 A。
- A 让模型失去对旧图的直接视觉记忆，这是用户已接受的取舍；引用只含 sha256 前缀、文件名和大小，用户按文件名重新附上即可。
- 探针每个 Gateway 进程每个模型最多一次真实请求（约几十 token）；`archived_refs` 或声明了 `input_modalities` 时不发探针。
