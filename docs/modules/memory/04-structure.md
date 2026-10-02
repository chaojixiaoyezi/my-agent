# Memory Structure

## 主模型只读检索工具 `memory_search`（J9，2026-10-02，分支 `claude/ae-j9-memory-tool`，默认关，待集成）

- `capability/memory_search_tool.MemorySearchTool`：
  - 开关 `enable_memory_search_tool` 开着时才由 `core._register_orchestration_tools` 注册，效果声明 `read_only`。
  - schema 只收 `query`/`limit`/`kind`，`additionalProperties=false`。
  - 返回有界摘录，以及 `retrieval` 结构化事实。
- `memory_store/jsonl.JsonlMemory.search_scoped_candidates_report`：
  - 与 `search_scoped_candidates` 同一检索、不写访问信号，另返回（记录，检索事实）。
  - 检索事实有 `mode`（semantic/keyword/none）、`fallback_reason`、`scoped_entries`、`semantic_recall`。
  - `_search_scoped` 改为返回二元组，两个原入口取第一项，结果与副作用不变。
- `retrieval/hybrid.HybridRetriever`：每次 `rank` 记下 `last_retrieval_mode` 与 `last_fallback_reason`（`embedder_unavailable`/`embedding_failed`），只是观察，不参与排序。
- `memory_store/jsonl._semantic_unavailable_reason`：没有嵌入端时，按存储层 `semantic_recall` 的 state 和 error_code 推出原因（`semantic_recall_disabled`，或诊断码去掉 `MEMORY_` 前缀转小写，如 `embedding_identity_unavailable`）。
- `memory_store/recall`：
  - 新增 `runtime_long_term_scope`（task 范围加记忆库里实际存在的 project 范围）。
  - 新增 `formal_recall_suppressed`/`is_isolated_recall_context`（`task_local`/`control_plane` 或记忆总闸关）。
  - 这几条从 `agent_core/runtime/loop_support` 原样抽出，自动召回与工具共用，保证“自动召回不读的回合模型也查不到、范围一致”。
- 不改嵌入客户端构建和向量文件格式。设计见 [决策模型接入设计](../../design/DECISION_MODEL_INTEGRATION.md) “P5-A 缺口 2”节。

## 记忆侧决策点登记被丢弃的建议（2026-10-02，ds1，分支 `worker/ds1-decision-outcome-category`，待集成）

- `memory_store/decision_recall.py`（recall 与 pre_recall 两段）、`decision_curator.py`、`decision_curator_relation.py` 的“保留原顺序/不采用”出口，
  在丢掉一条已经拿到的建议时调 `conversation/decision_outcome_log.record_decision_dropped(agent, stage, outcome, 原因码)`，
  追加一行 `record_kind="dropped"` 的补充记录，带 `result_category="dropped:<原因码>"`：
  - 期限到期 → `DROP_ADOPTION_DEADLINE`（`adoption_deadline`）；
  - 运行时身份/后端变更 → `DROP_RUNTIME_CHANGED`（`runtime_changed`）；
  - 候选/材料/正式条目版本变化 → `DROP_SOURCES_CHANGED`（`sources_changed`）。
- 登记只写观察记录，不改召回顺序、不重放、不推进游标；写失败只记日志。结果日志因此能分辨“Jev 选了但被宿主丢掉”。
- 返工（2026-10-02）：`decision_recall._pre_recall_stale` 的两处复核都比对主模型身份，第二次复核比原口径更严（有意收紧）；
  两次复核的结构化上下文统一收进 `_StaleStage`（在全部 import 之后定义）。
- 详见[决策审计与管控](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#每个点位最近是选中非选择还是被丢弃2026-10-02分支-workerds1-decision-outcome-category)。

## 嵌入档案与向量身份（P13+P14，2026-10-02，ds1，待集成）

- `settings/embedding_profile.embedding_model_config`：按档案编号解析嵌入连接（capability="embedding"），空值返回 None；
  失效抛 `ModelNotConfiguredError(profile_id, profile_reason)`。
- `settings/embedding_profile.embedding_identity(profile_id, embedder)`（P14 修正）：从客户端对象取 `protocol`、`model`、`api_base`，
  生成 {档案编号, 线路协议, 端点摘要, 模型名}；缺任一字段返回 None。维度不在身份里。
- `retrieval/embedding`：`OpenAICompatibleEmbedder.protocol="openai_compatible_embeddings"`、`MiniMaxEmbedder.protocol="minimax_native_embeddings"`；
  `_parse_vectors` 要求同一批向量等长且非空。客户端的 `dim` 只是声明的默认值，不进身份。
- `core._embedding_client(agent)`：唯一嵌入客户端入口，服务商凭据与端点全部来自档案；`_build_memory_embedder` / `_build_tool_embedder`
  只守各自开关，失败降级 None 并给结构化状态。`_memory_semantic_channel` 把客户端和身份一起交给 JsonlMemory；身份缺失就两者都不给。
- `retrieval/vector_store.VectorStore`：单文件快照 `my-agent.memory-vectors.v2`（identity/dim/generation/written_at/items）。
  - 写入（`upsert_many`/`replace_all`/`remove`）都在 `common/json_io.locked_json_path` 里完成。
  - 读取先 `_refresh` 比文件指纹；`search` 在同一份快照上裁决身份与 query 维度，失配抛 `VectorIdentityError(reason)`。
  - `summary()` 给条目数（读不了为 None）和快照头；`identity=None` 是不管理身份的通用模式。
- `memory_store/jsonl.JsonlMemory`：`vector_identity` 传入身份；语义召回读侧失配退回关键词，并记 identity/search 健康错误。
  管理动作单独放在 `_JsonlMemoryVectorAdminMixin`：`vector_index_status()` 预览；`rebuild_vectors()` 全有或全无地重嵌，
  嵌入阶段在 `_embed_rebuild_rows`。
- `cli/memory_admin_commands`：`memory vectors status / rebuild --confirmed` 管理入口。rebuild 先经 `owner_access.is_complete_local_admin_owner`，
  再预览，最后显式确认执行。

## 常数整改第三批（P10，2026-10-02，分支 `worker/ds2-p10-batch3`）

memory_store 23 个常数合规：`_MAX_CONDITION_CHARS/_MAX_CONTENT_CHARS/_MAX_REF_BYTES/_ITEM_PREVIEW_CHARS/_AUDIT_PREVIEW_CHARS/
_MESSAGE_PREVIEW_CHARS/_MAX_LIST_ITEM_CHARS/_MAX_SUMMARY_CHARS/_MAX_HOT_RULE_CHARS/_MAX_CANDIDATE_CHARS/_DAY_SECONDS` 补中文说明；
数量类改名补 `_COUNT`（见 02-progress）；`_BM25_B/_BM25_K1` 无物理单位只补说明。数值不变。

## P10 第四批常数整改（2026-10-02，ds1，待集成）

`cli/memory_archive_commands.py::_CLI_MEMORY_ARCHIVE_COUNT`（memory-archive 默认最多返回 20 条）、
`cli/memory_commands/memory_doctor_cmd.py::MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT`（doctor 列出最近 5 个归档文件）、
`cli/memory_commands/memory_query_cmd.py::_CLI_MEMORY_ROUTE_COUNT`（memory-route 默认最多返回 5 条）；
数值不变，随包目录投影与源码一致。

## Curator 固定模型档案（P12，2026-10-01）

`AgentConfig` / `MemorySettings` / `_memory_coercion` 只保留 `memory_curator_model_profile`，默认空。
`MemoryCuratorConfig.model_profile` 是不可变引用，参与 `revision()`；原单独覆盖 provider/model 的字段和配置键已删除，旧 YAML 残留仅告警。

- `settings/curator_profile.curator_model_config` 复用 `selected_model_config`：空值取 owner 选择，非空精确引用本人或已授权共享档案；不按型号匹配，不读当前线程选择。
  原 `resolved_model` 是用途、启用、凭据和连接字段的唯一权威，必须同时具备 agentic；显式 `default` 不是固定档案。
- `core._build_memory_curator_backend` 仅装配：整个配置交原 `get_backend`，只改 `stream_enabled=False`，不拼接聊天端点或凭据。
  引用失败转 `UnconfiguredBackend(ModelNotConfiguredError(...))`，生成不发网络，不创建第二 Agent；前台仍能进入设置修正。
- `ModelProfileError.reason` 由原解析器赋值。缺配置异常只传 `profile_id` / `profile_reason`，Curator 失败码仍为 `CURATOR_MODEL_NOT_CONFIGURED`，
  `curator_failure_retry_seconds` 的一小时退避不变，失败不推进游标。
- 诊断写进**原 run 账**的 `failure_diagnostic` warning JSON（仍不超过 300 字符），新增编号/原因，不改变 run v2 dataclass 键集；
  连接秘密、端点、请求或响应正文不入账，CLI 结果不伪造提炼成功。
- 新键在参数中心是 boundary / `writable=False`；只有 `execute_settings_control` 的完整管理员身份校验通过后，
  在 `user_settings_write_scope` 内允许用户 set/reset/revert。作用域退出还原，模型工具默认拒绝，actor 仅记账不是权限。
  TUI/IM 使用同一详情回执，运行引用与保存引用分开解析型号，保存不等于热生效。
- 后端在 owner 实例装配时冻结；主配置变更仍需重启 Gateway。自动总结 Skill 继续复用原 Curator 后端，因而也采用此固定档案。
  私有引用只在本人目录有效，跨 owner 须已有共享授权；目录损坏或授权撤销均不读其它私有模型做回退。

## P10 第二批常数整改（2026-10-02，待集成）

memory_archive 与 `cli/memory_archive_commands.py` 的常数改名/补说明：`resume_context.py::ARCHIVE_SEARCH_FILE_COUNT`（归档检索最多扫几个文件）、`RESUME_RECOMMENDED_READ_PATHS_COUNT`（恢复简报最多推荐几条读取路径）；数值不变，目录投影随源码一致。

## 缓存不可读时的行为（构造宽松、写入严格，2026-09-29）

`TextVectorCache` 对"缓存文件读不了"（权限/EIO/EMFILE，不是"文件不存在"）分两种态度：
- **构造宽松**：`__init__` 捕获 `OSError`，内存视图从空开始，记 `last_read_error`。读错误**绝不外抛**，
  因为缓存只是派生数据——`mem.add` 是在权威 JSONL 已提交之后才清缓存，读错误冒泡会被调用方当成
  "写入失败"而重试；`search_scoped` 冒泡则会让整次检索失败。
- **写入严格**：`_flush` 读不出磁盘内容时**放弃这次写**，免得把读不出来的缓存当成空、再原子替换成空文件。
- 四个懒建缓存的调用点（`_cached_vectors_for`、`_remember_cached_vectors`、`_forget_cached_vectors`、
  `_retain_text_cache_keys`）都把 `_text_vector_cache()` 放进各自的 `try`。

`remove` 在"盘上本来就没有这些键"时跳过整文件重写（32 MB 缓存删一次约 1.6 秒）。
判据在 **`_flush` 的文件锁内**、比较改动前后的**盘上内容**得出，不能用本实例内存判断"没变"：
另一个实例（父代理 vs 子代理 worker）写的键，本实例内存里没有、盘上有（P5d）。
跳过写时仍用盘上内容刷新 `_items`，本实例内存视图与盘上保持一致。

## 检索侧向量缓存（正文哈希键，2026-09-28）

`memory_store/jsonl._search_scoped` 在混合检索前先向 `retrieval/vector_store.VectorStore` 取一次正文向量缓存：
缓存落在**独立文件** `memory_text_vectors.json`（`retrieval/text_vector_cache.TextVectorCache`），与权威
`memory_vectors.json` 完全分开：
- 拆文件的原因：共处一个文件时，缓存项会挤占 `VectorStore.search` 的 top_k，且只做检索的进程整文件写回
  会把已删除事实的**明文**重新写进 `memory_vectors.json`（违反 `_jsonl_indexing` 的清明文约定）。现在检索路径永不重写该文件。
- 键由 `text_vector_cache.text_cache_key(fingerprint, text)` 生成，`fingerprint = sha256(实现类名 + api_base + model)[:16]`；
  正文用 SHA-256。端点与模型名只以指纹落盘，不存明文；换模型/换端点即自然失效。
- 参与嵌入与缓存键的文本由 `text_vector_cache.index_text(content, attributes)` 统一生成（正文 + `keywords_en`）；
  检索、写入、清理三处共用同一口径，否则带 `keywords_en` 的事实删不掉。
- 缓存向量长度必须与当轮 query 向量一致才算命中；不一致（同名模型换维度）一律视为缺失、现场重嵌，
  避免旧长度向量喂进 `mean_center` 越界导致整轮失败。非 list 值同样按未命中处理。
- `HybridRetriever.rank(cached_vectors=...)` 与 `_vector_order` 只对缺失项调用 `embed`，
  本轮现嵌结果放在 `last_fresh_doc_vectors` 供 `_remember_cached_vectors` 回写。
- 回写前用 active 身份复核：记录已删除或被替换的键不回写（收口"取缓存/写回"之间的竞态）。
- `apply_batch` 在删除/替换提交后按记录级索引文本清项：已删事实取历史全部版本，`replace` 额外取被覆盖的旧版本。
- `index_all` 顺带调 `_retain_text_cache_keys` 回收不属于任何 active 记录的键（换模型/迁移遗留）。
- 孤儿回收也挂进 owner 维护（`user_space/owner_maintenance.run_owner_retention_if_due`，默认 24 小时一次），
  这样不跑手动命令也能自动收口。
  - **路径必须是 canonical 的** `home.owner_memory_long_term_jsonl`。
  - **回收不依赖 embedder**：缓存键形如 `<指纹>:<正文哈希>`，维护进程按 key 里的**正文哈希**比对
    active 记录的 `index_text` 哈希来算保留集合（`TextVectorCache.retain_content_hashes`）。
    此前维护里手拼 `memory/memory.jsonl` 且新建的 `JsonlMemory` 没有 embedder，导致回收恒为 0——
    每天都在 `maintenance.json` 写一个假的 `text_vector_cache_reclaimed: 0`，看起来像"跑过、没有孤儿"。
- 缓存读/写/清成功都会恢复健康状态（`cache_read` / `cache_write` / `cache_purge`），
  失败只记语义健康诊断、不影响检索正确性与权威 JSONL。跨进程写在 `locked_json_path` sidecar 锁内重读合并。
  - `keep()` 判据在**拿到跨进程文件锁之后**才求值；拿锁失败时**不写盘**（只记 `last_write_error`），
    因为此时磁盘内容不可信，写回会复活别的实例已删的键。
  - `remove` **一律在文件锁内从磁盘上减掉请求的键**，不要求"本实例内存里有这个键"——父代理与每个
    子代理 worker 各有自己的缓存实例，只看内存会让子代理删除的事实永久留下孤儿。
- 跨进程合并**只写本次增量**（本次 put 的键 / 本次 remove 的键），不把整份内存快照叠回磁盘——
  否则会把别的进程已经删掉的键重新写出来，复活已删除事实的向量。写失败记进 `last_write_error`
  并反映到健康状态，不静默吞掉。
- `_load` 逐键跳过坏值：一条脏数据只丢该键，不让整个缓存文件作废（否则缓存被永久关掉、每次都全量重嵌）。
  只有"文件不存在"才当成空缓存。
- 旧格式文件（只有 entry_id 键，或旧 `textcache:v1:` 前缀键）不会被新键命中：调用方按缺失处理并全量重算，不做隐式迁移。
- 两个生产 embedder 暴露只读 `model` 与 `api_base` 属性（参与指纹，不含密钥），MiniMax 与 OpenAI 兼容实现同契约。

### 已知内在限制（不是缺陷，是设计边界）

- **同一 `(实现类, api_base, model)` 背后换成另一个同维度模型时，旧向量会被静默使用。**
  指纹只认"实现类 + 端点 + 模型名"，不认端点背后真正部署的权重。若运维在同一地址、同一模型名下
  换了权重而维度不变，缓存命中的是旧权重算出的向量，语义臂仍能工作但排序可能与重算不同。
  换权重请同时改模型名（或端点），让指纹变化、缓存自然失效。
- 缓存键不含维度：长度守卫在读取点按"向量长度 vs 当轮 query 长度"比对完成，不靠键区分。
- **每次 put / remove 都要整文件读一遍、再整文件写一遍**（JSON 单文件形态的固有代价）。
  实测 1000 条事实 × 1536 维：缓存文件约 **32 MB**，load 约 0.5–0.7 秒，put / remove 各一次同量级。
  量级再涨需要换分片或追加式存储，当前规模可以接受。

### v1 遗留项的一次性清理（不需要迁移）

第一版实现把正文缓存塞在 `memory_vectors.json` 里、键前缀为 `textcache:v1:`。**生产从未部署过 v1**，
因此不做自动迁移。若某个开发机残留了这类键：它们是派生数据，直接删掉 `memory_vectors.json` 里所有
`textcache:v1:` 开头的项即可（或整个删掉该文件，下次检索会按需要重建权威向量）。
**注意：`index_all` 的孤儿回收不会清掉它们。** 回收只作用于 `memory_text_vectors.json`，
而 v1 遗留项在 `memory_vectors.json` 里；两个文件互不相干。

## 保留回执与隔离错误

`memory_store/retention_models.MemoryRetentionReport` 的 `errors` 仍是执行期错误加扫描期错误（旧语义）。
末尾新增的 `isolated_errors` 只在 `MemoryRetentionService.apply()` 真正执行的分支里有值，是被隔离保护的扫描期路径级错误；
整份拒绝、法律保留、只规划时为空。`apply()` 用 `replace(_without_errored_subtrees(plan), isolated_errors=plan.errors)` 把它随可执行计划
带进 `retention_apply.execute_retention_plan`，审计事件 `owner_retention_applied` 据此写 `isolated_error_count` 和 `isolated_error_codes`
（按码计数，不含路径）。owner 维护怎么用它，见 [gateway 结构](../gateway/04-structure.md) 开头「维护状态只加键」一段。
执行器里候选清理批次由 `_apply_candidate_phase` 先跑（必须一次批量删除），其余动作再逐条执行。

## 会话删除收集的唤醒文件

`memory_store/retention_scan._conversation_related_paths` 收集一个会话的唤醒相关文件时扫描 `wake_queue/` 下的
`urgent/`、`normal/`、`attempts/`、`quarantine/`、`quarantine/archive/`（满 14 天归档的结案记录）和
`quarantine/replayed/<wake_signal_id>/`，逐个读 JSON 顶层 `thread_id` 判定归属；读不出的文件记
`MEMORY_RETENTION_CONVERSATION_WAKE_INVALID`，不猜归属。`quarantine/ledger/`、`quarantine/unreadable/` 及其在 `archive/` 下的
对应目录无法归属会话，不随会话删除，由运维清理。尝试账与结案记录的权威在 `conversation/store_wake_attempts.py`，归档在
`conversation/store_wake_quarantine_archive.py`。

## 记忆诊断回显的配置名单

`cli/memory_doctor.py` 与 `cli/memory_commands/memory_doctor_cmd.py` 的 `_memory_config_payload` / `_build_archive_doctor` 只回显
`AgentConfig` 上真实存在的 Memory 字段；名单与 `settings/_memory_types.MemorySettings` 同步删减（2026-09-27 去掉
`memory_hook_retention_days`、`memory_rule_receipt_enabled`）。doctor 不是配置的读取方证据：只在这里出现的字段视为无读取方。
doctor 列出最近多少个归档文件自参数减量第 3 批（2026-09-27）起是 `memory_doctor_cmd.MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_LIMIT`（5），
旧入口 `cli/memory_doctor.py` 从那里 import，只定义一次；配置项 `memory_doctor_recent_archive_file_limit` 已删除。

child历史说明在不展示正文时不再提前读取完整来源或计算展示窗口，保留原线程说明及核验；三文件31项通过。三宿主seed物化峰值已定位，后续延后/释放尚未实施，12.4未完成。

`tokens.py::estimate_tokens_from_json_parts`消费已序列化JSON片段，与原`estimate_tokens`共用长度/结构开销计算；调用者必须保持原JSON编码及原顶层结构，不能以Sequence的字符串表示计量。原`_payload_lengths`/有界小JSON判定不变；新增入口不计费、不写账、不引入第二tokenizer。Compact的可重放数组位于conversation模块，普通模型出站仍物化原协议消息。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要分段本地15文件316项通过：复用原循环顺序读取JSON字符、消费后释放窗口；共享估算器改流式累计且数值保持。修复提示纳入预算，发送及来源EOF后复查取消；writer/CAS不变。全链仍有原消息/覆盖驻留，11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

`tool_output_externalizer.py` 从原ToolCall参数接收attempt/turn，并写入原artifact/index；请求字段 `force_externalize`（上下文余量不足，由 agent_core 归档入口判定）让 `read_file` 分页也落 artifact 并在记录写 `output_externalized_reason=tool_result_headroom`，`read_artifact` 分页仍内联；`compact_tool_output_refs.py` 原样带回并按四元身份区分调用。完整身份参与新artifact命名，索引位置和scoped_call_id展示格式保持；旧缺维度只能保留，不能用裸ID隐藏。

Curator 与召回的可选决策入口直接导入 `common.cancellation` 的 `ToolCancelled` 和取消检查；这是与插件宿主共用的唯一进程内异常类型。已删除的 `tooling/cancellation.py` 不再作为兼容入口，记忆来源、游标和正式写入路径没有变化。

Curator 决策输入（`memory_store/decision_curator.py`）的候选释义只在 `state.annotation_criteria` 出现一次，`questions` 每题只带 `instructions.{source_kind,source_id,criteria_key,required_refs}` 与共享引用 `criteria="annotation_criteria.<tag|priority>"`；`backends/typesafe_decision_wire.py::_validate_question` 因此接受 `criteria` 为共享引用字符串（仍按 `_valid_key` 限长、禁控制字符），`score` 仍须内联等级。`state.batch` 仍保留整批原始材料（提取与证据校验的依据），窗口不足时只裁题面前缀：`_fit_items_to_window` 按本点位已授权连接的 `model_context_window_tokens` 从尾部整条移除，被裁来源留在原游标之后由下一轮重放；`_MAX_ANNOTATED_ITEMS` 仍是本地题量保护。

## 记忆配置的单一旋钮（参数减量第 2 批）

- 归档级别：`memory_archive_level`（0-3，默认 3）是运行归档（`_finalization_service`、`runtime/live_archive`）和子代理收尾恢复快照（`subagent_mixin._recovery_snapshot_input`）的共同来源，不再有 hook 专用级别。
- 规则路由：`memory_rule_routing_mode`（off/soft/strict）。`runtime/loop_support._routed_memory_context_for_request` 与 `memory_push._route_formal_lessons` 都以 `mode != "off"` 作为开关；CLI `memory route` 的 `routing_enabled` 同样由它算出。
- 恢复上下文：`memory_archive/resume_context.build_auto_resume_context` 只读 `memory_resume_auto_context_mode`（默认 off）；调用方显式 `enabled=True` 等于本次 always，`enabled=False` 本次关闭。
- 恢复与归档的条数/预览预算（2026-09-28 参数减量 C 组）：`resume_context.RESUME_AUTO_CONTEXT_LIMIT`(5)、
  `RESUME_RECOMMENDED_READ_PATHS_LIMIT`(20)、`RESUME_ARCHIVE_SCAN_LIMIT`(0，含义为不限)、`QUERY_CONTENT_PREVIEW_CHARS`(500)，
  归档检索文件上限复用 `query/archive_io.ARCHIVE_SEARCH_FILE_LIMIT`(30)，artifact 正文默认读取复用
  `artifact/read_modes.ARTIFACT_DEFAULT_READ_CHARS`(4000)。这些键已从 AgentConfig、随包 YAML、`services/_normalize` 规格与
  说明基线删除，用户配置里残留只按未知键告警；CLI 与 runtime 都从同一常量导入，不再读 `agent.config`。
- Curator 批次与重试（2026-09-28 参数减量 C 组）：`memory_store/curator_models.CURATOR_BATCH_MESSAGE_LIMIT`(80) 与
  `CURATOR_MAX_RETRIES`(1) 是 `MemoryCuratorConfig` 的唯一来源，已从 AgentConfig、随包 YAML、`services/_normalize` 规格与
  说明基线删除；curator 的 interval、turn_threshold、max_input_chars、timeout、workers、daily_finalize_hour 仍是用户参数。
- compact 语义摘要的首尾保护、中段阈值与输入预算（2026-09-28 参数减量杂项批）由
  `memory_archive/compact_semantic_summary.py` 的 `_DEFAULT_PROTECT_HEAD`(2) / `_DEFAULT_PROTECT_TAIL`(6) /
  `_DEFAULT_MIN_MIDDLE`(4) / `_DEFAULT_MAX_INPUT_CHARS`(12000) 唯一给出，`semantic_summary_config` 只从配置读 `enabled`。

## 压缩熔断参数的唯一位置

压缩连续失败熔断的阈值 `DEFAULT_COMPACT_FAILURE_THRESHOLD` 与冷却 `DEFAULT_COMPACT_COOLDOWN_SECONDS` 只在
`memory_archive/compact_circuit_breaker.py` 定义；`agent_core/runtime/context_compactor.py` 组装压缩策略时导入它们。
`record_compact_outcome` 标记熔断打开时用的就是这个默认阈值，两边同源才不会出现“判断用 5、打开标记用 3”的错位。

## 自动压缩触发线的绝对上限

触发线只在 `agent_core/runtime/context_compactor.runtime_compact_policy` 算一次：窗口 × `memory_compact_auto_trigger_percent`，`memory_compact_auto_trigger_max_tokens` 大于 0 时再与它取小（`compact_trigger_max_tokens` 规范化，非法与负数按 0）。`RuntimeCompactPolicy.trigger_max_tokens` 记规范化后的上限，`trigger_capped` 表示上限严格小于窗口 × 百分比、触发线被它压低（正好相等时不算封顶，一切按百分比口径）。近期尾部从封顶后的触发线推出；recovery 目标封顶时按触发线 × recovery% ÷ 触发% 等比推导（整数先乘后除，仍不超过“触发线 − 近期尾部”），不封顶时仍是窗口 × recovery% 与“触发线 − 近期尾部”取小。模型请求前预检、工具循环中途与即时压缩、活动回合压缩、会话压缩都直接用 `trigger_tokens`；finalization 的旧归档周期经 `MemoryCompactAutoCycleOptions.trigger_tokens` → `MemoryCompactSuggestOptions.trigger_tokens` 拿到同一条线（只在 `trigger_capped` 时传，否则为 0、按百分比判断）。

## 子代理任务工作区的路径与物化边界

`task_workspace/__init__.py::_task_workspace_path_inputs` 统一计算原 root/run/task 身份；
`subagent_task_workspace_paths` 与原 `ensure_subagent_task_workspace` 共用该输入及 `_paths_for`。
前者只计算路径，后者保持原目录创建、父状态锁内合并、共享状态、agent run、artifact 和日账写入顺序。
路径对象中默认 runtime refs 不构成已写入事实，只读消费方不能据此生成日账事件。

`subagents/services/task_workspace_adapter.py` 将静态字段映射同时用于正式 sync 和任务副本投影；
runner 准备只消费副本，不修改待提交任务。正式 `persistence/service.py` 的 sync 调用保持唯一，未增加另一条保存路径。
这一接缝只解决 canonical 路径差异，不能证明完整首请求容量、模型候选输出 cap 或目录存在性。

`agent_run_workspace.py::AgentRunWorkspacePaths` 同时登记 `findings.jsonl` 与 `lessons.jsonl` 两个工具账本路径。`ensure_agent_run_workspace` 只按 id 合并 findings，从不创建或覆盖 lessons；lessons 由子代理 `record_lesson` 经 `subagents/lesson_ledger.py` 追加，结果收口读回后才进入 owner 候选主链。

## 运行中原生工具历史摘要的窗口与取消边界

`agent_core/_tool_loop_service.py::_native_tool_history_summary` 将同一工作片的停止检查传给
`memory_archive/compact_semantic_summary.py::summarize_live_tool_history`。真实 agent 请求沿原
`conversation/compact_request_budget.py::generate_bounded_compact_response` 发出：可容纳时保持原生
system/tools/messages 前缀，超窗时按原分段合同覆盖完整历史。每段取消和传输失败只使候选失败；
源 IR 回收及 Compact 代次提交仍由上层原事务控制。裸 backend 测试适配保持直接调用，不能当作真实窗口保证。

## 原召回结果中的可选排序

`JsonlMemory.search_scoped_candidates` 与正式 `search_scoped` 共用 active JSONL allowlist、scope predicate 和混合排序，
但不提前登记访问。只有最终注入的补充候选可经 `confirm_scoped_access` 重读正式源，核对 ID、版本、正文和来源范围后登记 touch；
被裁剪、替换或撤销的候选不能写访问信号。原 `search_scoped` 的即时访问语义不变。
`decision_recall.supplemental_query_candidates` 只从完整用户问题的有界语句片段产生至多四个检索文本和局部编号；
原完整查询仍是基线，不从语句解释权限或“无需历史”。原正式上下文准备已有独立 `pre_recall` 消费：
在原召回和预算之后、尚有空位时选一次补充查询，只追加同范围的确认事实；P3 排序与本点共用一个阶段期限。

`memory_store/decision_recall.py` 经共用决策服务提供临时优先级与补充查询；唯一接线位于
`agent_core/runtime/loop_support.py::_formal_memories_for_request` 原预算之后。
发给决策模型的记忆条目走 `_decision_view` 最小投影（`entry_id`/`kind`/`content`/`attributes`）：时间戳与来源渠道
不提供排序增量，不进请求；`attributes` 必须保留，它参与本轮绑定校验（scope 变化要能被比对成 stale）。
`need_data` 的逐题相同说明放在 `state.need_data_note` 一份，题内只留指向本条记忆的 `required_refs`；
`criteria` 的协议形状不变（模型与 backend 都靠它选答案）。单条正文超过
`conversation/decision_point_limits.RECALL_CONTENT_MAX_CHARS`(800) 字符才截断，并带
`content_truncated`/`content_original_chars` 结构化标记；正常长度原样送，不做一刀切截断。
本地一致性校验仍用完整的 `_record_view`。
读取之前即检查原 `task_local`/`control_plane` 范围和 owner `memory_enabled`，不在禁用范围继续扫描正式项目记忆；
原任务本地与控制材料仍按其自身来源准备，此门不删除历史或改变工具权限。
当前选中集合完整保留，HOT/lesson 只绑定不参与排序。P3 排序的来源刷新只查原正式仓库并投影原 ID，不新增检索或访问计数；P5-A 补充查询至多一次，候选未确认前不记访问。开了 `points.pre_recall.fragment_material=with_new_facts`（J8，默认关）时，问 Jev 前每个片段先做一次同规则的候选检索（`FragmentSearch.additions`，不记访问），补不出新事实的片段不给选；采用的正是预检那份，仍经 `confirm_scoped_access` 重读正式源确认。
候选、请求属性、主模型或设置变更时拒绝旧建议；最后采用沿 `decision_outcome_is_current`。
结果驻留原 PreparedRuntimeContext.memories，工具循环/Compact 复用该轮材料。同步本地文件 I/O 不承诺强制中断，迟到建议不会采用。
召回前补充真正追加的记录编号写到 `RoutedMemoryContext.supplement_entry_ids`；上下文包 `memory_refs` 另写 `recalled_refs`（编号、版本、种类、`via` 为 baseline 或 supplement）与 `recall_findings`（`memory_*` 发现码），只写文件、不进提示段，供真实验收核对补充召回有没有带来新事实。
第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

## Live-tool 逐调用来源

`conversation/compact_tool_identity.py` 是唯一工具来源身份，只接受完整的 run/attempt/turn/call 四元 refs。`conversation/compact_checkpoint.py` 写 `conversation_compact_checkpoint.v3`，包含 `source_tool_refs`、`retained_tool_refs`；候选编号 `compact-v3-{generation}-{digest}` 对 scope、摘要基础、来源、保留 refs 及创建时间做内容寻址，读取时逐行复核，篡改的行拒绝读取。裸 call id 只作展示，可以重复或交叉。顶层 request/attempt 仍是提交者，原有"写 checkpoint → 锁内 generation CAS"顺序不变。
- v1/v2 旧行照常读取，但不提供四元来源，因此不隐藏任何记录。这也包括主线 `66a598cf3` 写出的 `source_tool_call_refs` 三元引用：部署前被运行中压缩的旧调用会重新进入模型上下文，不丢失，也不误隐藏。
- `active_turn_compact.py` 只凭已提交的四元 refs 隐藏记录，按记录位置选择来源与尾部。缺完整身份的记录保持可见；全部未知时返回 `compacted=False`，并带 `source_resolution=uncertain` 和 `uncertain_call_count`。
- 完整恢复宿主（`agent_core/compact_request_recovery.py`）在强制恢复且没有 transcript 时，若工具记录存在但身份都无法证明，报 `COMPACT_TOOL_COVERAGE_UNKNOWN`；只有确实没有记录时才报 `COMPACT_SOURCE_EMPTY`。
- 原生 IR 回执带引用时（`agent_core/compact_tool_partition.py`）：每个引用都必须等于同一四元身份原归档记录自己写下的输出位置（`output_path`/`artifact_ref`/`source_artifact_ref`，与 `tool_call_archive_record._projection_refs` 的“完整原始输出”引用同源），整组才可移入摘要来源；摘要素材仍是模型当时看到的原回执，不按引用去取全文。read_artifact 的来源引用指向被读调用，引用上的 sha256/size 描述本次回执，判据只比对引用值。工具自报的引用（记录顶层 `tool_result_refs` 只是回执引用的副本）、媒体引用、json/数据块和没有归档记录的回执仍整组保留；`CarriedToolCompactSource` 用自身 source_records 重算同一判据。修复前任何带引用回执都判不完整，外置输出和 read_artifact 回执会让整轮来源为空，强制恢复报 `COMPACT_TOOL_COVERAGE_UNKNOWN`（2026-09-27 Codex G02）。
- `tool_output_externalizer.py` 直接传递原 call 的 attempt/turn；`compact_tool_output_refs.py` 只对完整四元身份去重，字节完全相同的未知行才合并。不解析 scoped 字符串，也不从 operation 哈希反推缺失身份。
- 生命周期唤醒片的携带记录由 `carried_tool_call_records_for_requests` 读取：来源是（索引根, 是否只用于运行时状态）对，每个根只读自己的 `blobs/tool_outputs/index.jsonl`，不像 `carried_tool_call_records` 那样展开到 `runs/*/*` 与 `tasks/*/*`；逐行流式读取，按精确请求编号过滤，去重口径与前者相同。owner 根索引（前台 Gateway 轮）的记录带 `CARRIED_RUNTIME_ONLY_FIELD=True`，`agent_core/runtime/loop_support._tool_loop_execute_params` 只用它重建运行时状态，`conversation/compact_carry.compact_overflow_carry` 替换携带内容时保留它。来源用 `CarriedIndexSource` 描述（根、结构化来源名、是否只用于运行时状态），每个来源独立读取、各自捕获 OSError，结果 `CarriedToolCallRead` 的 unreadable_sources 列出读不到的来源；调用方据此写不完整事实，不能把剩下的记录当成完整。
- 旧的无身份大历史可能无法安全恢复 Compact，这里不做静默身份迁移。兼容与回滚边界见[依赖拆分合并节](../../design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

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
采集本身常开：`backends/cache_diagnostics.py` 的开关已降为读取点旁的具名常量，不再可配置，不新增另一份用量账。
context-pressure 的连接校准指纹（2026-09-30 起）只收跨进程稳定、不含凭据的分词身份：模型档案、地址、`model_backend`、鉴权方式
（`auth_ref` 只取 `mode`），不再经进程加盐摘要，所以 Gateway 重启后线程上的校准仍可用；密钥与请求头不进指纹。
非标准后端给出的不透明属性仍用进程内对象身份区分，不序列化其 `repr` 或凭据，且不因测试替身字段不可编码而阻断模型请求。

## 前台优先与后台请求预算

决策响应只有临时建议权；Curator 整理标注后通过 `decision_outcome_is_current` 复查原身份、
连接与本次绝对期限，设置变化或过期时送原批次。这个只读门共用决策服务，不复制配置判断或另发请求。

`curator_backend.call_backend_with_timeout` 只保留记忆语义适配，实际等待复用 `backends/bounded_call.py`。
宿主后端实例键保持原隔离规则，原语保留 worker 与精确 InterruptHandle；到期返回不代表资源已经退出。
取消 Event 直接唤醒等待者，慢清理异步去重；无第二份 Curator inflight 集合或额外 join。

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
   `prepare_background_history_or_raise` 对 `unreadable` 抛 `BackgroundHistoryUnavailableError`
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

## 2026-09-26 策展输入预算缩批的边界（真机卡死修复）

- `curator._execute` 在可选决策标注之前调用 `curator_backend.fit_batch_to_input_budget`：按最终提示（`curator_prompt`）实测长度截尾，先截消息、再截审计，各至少留一条。与 R262 超时缩批同一游标契约：只截尾部，尾部留在游标之后被下一轮重放，不写 state。截过时运行结果带 `memory_curator_input_fitted:messages=a->b,audit=c->d`，只含条数；成功运行的 warning 不进运行账（与超时缩批相同），所以再写一行同内容的 WARNING 日志，网关启动日志里可见。
- 收集阶段的条目估算（固定 7000 字符模板余量、审计 1000 字保底且第一条不受上限约束）只是收集上界，不是提示长度的权威；提示是否超预算只以 `curator_prompt` 实测为准。保底后仍超出说明预算小于模板本身，仍报 `CURATOR_INPUT_BUDGET_EXCEEDED`。
- R264 的“每轮先清空”现在覆盖预算早退：`extract_with_retries` 第一行就清空尝试形状，不再用 `reset(token)` 恢复上一调用者（可能是另一个 owner）的形状。改动任何一处都要跑 `test_curator_input_budget.py`。

## 2026-09-26 没配模型的 owner：不原地重试、独立失败码、同源长退避

- `ProviderConfigurationError` 一族（未配模型、4xx 拒绝、地址/代理配置错误）按基类合同不得重试：`extract_with_retries` 遇到即结束，不消耗同输入重试。
- `ModelNotConfiguredError` 记 `CURATOR_MODEL_NOT_CONFIGURED`；其它配置错误仍归 `CURATOR_MODEL_FAILED`。
- 失败退避只有一个口径 `curator_models.curator_failure_retry_seconds`：发现层 `owner_wake_discovery` 与 `curator._run_pending_when_due` 都调用它。未配模型退避一小时，其它失败保持原 5 分钟。改动要跑 `test_curator_model_not_configured.py`。

## R265 策展会话头与失败阶段的边界

- 后台提取的宿主会话在 `curator._execute` 用 `backends/provider_headers.provider_session_scope((owner_id,), run_id)`
  包住整次 `extract_with_retries`：值只由 owner_id + run_id 派生，同 run 的同输入重试/缩批同值，退出即复位；
  不借前台线程会话，也不在 `curator_backend` 里另造会话身份。`bounded_call` 的 `copy_context` 负责把它带进
  供应商调用线程；改动任何一处都要跑 `test_memory_curator_v2.py` 的会话头用例。
- 失败分类只读异常类型与阶段事实：`extract_with_retries` 把供应商调用阶段的 ValueError/TypeError 包成
  `CuratorModelCallError`（`__cause__` 保留原异常）→ `CURATOR_MODEL_FAILED`；裸 ValueError 只剩宿主解析/校验
  失败 → `CURATOR_SCHEMA_INVALID`。超时、still-running 与其它供应商异常类型原样抛出，既有分类不变。

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
  失败诊断走既有 `warnings`，格式固定为 `failure_diagnostic=` + 紧凑 JSON（sort_keys）。键：
  - `error_type`；
  - 可选 `provider_http_status`；
  - 可选 `message`：`str(exc)` 经 `common/log_redaction` 脱敏后的前 200 字；
  - 包装异常（`raise … from …`）另记：
    - `cause_type`：沿显式 `__cause__` 链找到的最底层根因类名；
    - `cause_errno`：根因是 OSError 时记，如磁盘满为 28；
    - `cause_pos`：根因是 JSONDecodeError 时记出错位置。
  - 模型已返回、解析失败（`CuratorResponseParseError`）时另记：
    - `response_chars`（响应字符数）、`truncated`；
    - `stop_reason`（上游短码，不像代码的记 `other`）、`output_tokens`。
    - 用 `cause_pos` 对照 `response_chars` 能分出截断、空内容和中途格式坏。
  - 哪一项不合格（2026-10-02，`_violation_facts`，只读异常上的结构化属性，沿 `__cause__` 链找）：
    - `violation_code`：输出不合合同时是 `curator_schema.CuratorOutputViolation` 的宿主短码（`json_invalid`、`schema_version_unsupported`、`invalid_type`、`enum_mismatch`、`missing_property`、`unknown_property`、`below_minimum`、`above_maximum`、`non_string_key`、`nesting_too_deep`、`bounded_limit_exceeded`）；证据不合格时是 `CuratorEvidenceError.detail_code`。≤40 字。
    - `violation_path`：只由 schema 字段名和下标拼成的 JSONPath，如 `$.candidates[0].confidence`；多出来的键名是模型写的，路径只到所在对象；不知道下标的嵌套字段写 `$..<字段名>`。≤80 字。

  超过单条 300 字符时先缩短 `message`，仍超出就退回只含 `error_type` 的形状（有违规码/路径时一起留，类名只留 100 字），绝不裁 JSON 本体。请求体、响应体、根因正文、记忆内容都不入账。

- `CuratorRunRecord.failure_diagnostic` 是**诊断投影**，不是失败权威：权威仍是 `failure_code` +
  attempt/lease 状态。字段只允许机器可判定形状（异常类名、可选 HTTP 状态码、根因类名/errno/解析出错位置、
  响应字符数/截断/结束原因/输出 token 这类标量），
  任何供应商正文、prompt 片段、记忆内容都不得进入，这条在执行 `_failure_diagnostic` 时强制。
- 新增诊断字段不改变 run 账 schema_version（v2 追加可选字段），旧账本无需迁移。

# Memory Structure

## R256 迁移预检的稳态边界

- `migration.py` 的 `_MigrationServiceCore.apply()` 现在先读 marker 再决定是否 `_scan()`：
  稳态判据 `_steady_state_current` 要求 marker 的 `schema_version` 等于
  `MEMORY_MIGRATION_SCHEMA_VERSION`、`status == "complete"`、marker 读无错，且
  `_legacy_sources_at_contract_paths` 在契约位置未发现遗留目录。任一条件不满足即回落完整扫描，
  因此"迁移该不该做"的裁决权仍在完整扫描与已冻结快照，短路只回答"无事可做"。
- `_scan_legacy_navigation` 判定 memory.md / memory-hot.md 是否为旧正文只认两种精确来源：`home_memory_seeds` 的正式默认导航，
  以及 `home_layout_v2.owner_navigation_seeds()` 给本 home 新 owner 的种子（根级模板副本或同一默认）；两者都不同才是 legacy。
  种子文本因此只有一处权威，新 owner 初始化的文件不会变成 `migrated_legacy` 候选。
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

- 直接展示工具结果时，reducer临时省略本次typed物理自归档refs及对应ref内容块，复用原scoped读锚点。
  canonical refs、完整blob、索引、reader既有登记查询及Compact不变；正文/业务路径不做字符串清洗，
  `source_artifact_ref`继续指向原来源。外置摘要选择保持原样，本片不声称所有投影均已删除物理引用。
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

## P2 Curator 前置决策标注（本地实现）

- `memory_store/decision_curator.py` 在原 `_execute` 收集批次后、`extract_with_retries` 前调用共用
  `decision_service`；每次原 lease 批次只创建一次阶段。`core._wire_memory_curator` 注入既有 agent 的
  callable，不创建第二 Agent、配置库、后台 worker 或记忆写服务。
- 使用宿主关键字 `scope="owner_background"`，params 的 run_id 为原 Curator run，thread/task/request
  为空。决策只读 owner 设置；每个后台点位缺省用 `background_timeout_seconds` 作自己的完整预算，从调用开始计时
  （2026-09-28 起不再共享阶段倒计时），不能借材料中的会话身份
  读取 thread 覆盖。活动会话 runner 与 owner 后台身份冲突时，不允许调用。
- `DecisionStage.enabled_points` 来自 begin 的同一次设置读取，明确关闭时立即返回原批次，不准备或编码材料；
  它只决定是否准备，发送/采用仍由共用服务复读原设置，不能作为采用权限。
- 原批次完整进入决策上下文；最多为 32 条来源各问分类和优先级两题，64 题是本地延迟/输入保护，不是 Jev 官方协议限制。
  超出部分只是不加注释，原提取仍收到全部材料。逐题失败只丢对应建议，原候选绑定与整个输入
  摘要在消费前再次核对；不使用置信度直接入库、删材料或推进游标。
- 每题提供显式 `not_needed`、`need_data`、`no_match`、`abstain` 候选，分别保存在
  `tag_outcome`/`priority_outcome`，不混入普通分类或把错误当弃权。`need_data.required_refs` 只由宿主
  绑定到当前来源；截断消息为精确 `full_source_ref`，其他来源为已有 context/artifact 引用。
  这些只给原 Curator 作临时建议，不授权工具补读、不创建补资料 agent；没有补齐时照常沿原材料处理。
- `CuratorDecisionAnnotation` 与 `CuratorInputBatch.decision_annotations` 仅在内存中承载来源类型、
  精确 ID/hash、标签、优先级、实际模型和输入摘要。prompt 在原稳定说明之后明确标注其非权威性质；
  不进入输出 schema、证据 refs 或持久状态。observe/off 不改变原输入；提示超过原字符预算时全部放弃提示。
- 原超时缩批继续保留消息/审计前缀；`to_model_payload` 只投影仍存在且 hash 一致的标注，尾部建议
  不会成为新证据。提交继续使用原实际 extraction batch，决策自身永不提交或推进游标。
- `curator_backend.extraction_budget_seconds` 是原提取、lease 和可选头寸的唯一总预算公式。
  lease 时长仍为原提取上界加 90 秒，`_annotation_deadline` 读取本次原 lease 的确切取得/到期时间，
  扣除完整提取上界后最多把剩余正缓冲的一半借给可选标注，另一半保留提交。
  转换为冻结 monotonic caller_deadline 后只缩短增强，不延长 lease、不重置阶段；
  极大后台配置、过期或坏 lease 时间都不能借此抢走原提取预算。
- 决策沿原模型账本记录 purpose=decision/auxiliary 和实际 Curator run，thread 为空；
  不虚构用户会话累计记录。原 Curator 未设独立持久模型用量结算，本片没有新增该存储。
- 运行诊断只追加原 run warnings 中的固定 `memory_curator_decision:<mode>:<status>`，
  不改变严格 run schema，不落材料、概率或供应商错误正文。临时批次字段不需要持久 schema 迁移。

上述验证限本地 fake 决策、原有界 worker/账本及原提取/提交组合；真实 Jev 质量、服务端时延、实际 TUI
和部署尚未验收，不能据此宣称正式记忆提取质量提升。

Curator 设置读回的 `runtime_scope=owner_background` 与实际服务一致：后台点位预算不受前台 stage_timeout_seconds 限制，
也不共享后台阶段倒计时；线程 enabled/profile 不覆盖 owner 后台有效值。历史线程后台覆盖只展示供清理，实际标注不消费它们。

## P5-B 来源与正式条目关系提示（第一片，本地实现）

`decision_curator.py::annotate_curator_batch` 只创建一个后台阶段，独立检查 `curator` 和 `curator_relation`。
标签与关系各自拿完整点位预算，共用绝对 caller deadline；后续关系等待结束后还会按标签自己的点位期限复核较早标签的配置与期限。
`decision_curator_relation.py` 只消费当前批次，不新建候选 store、后台代理、提取入口或晋升动作。

到达计数：`decision_recall`、`decision_curator`、`decision_curator_relation` 在每次到达时调用 `conversation/decision_reach_counts.note_decision_reach` 记原因码或 `called`，材料构建包在 `counted_material` 里（输入不合格记 `bad_material` 后原样上抛）。Curator 的阶段只建一次，`_note_stage_misses` 为两个点位各记一次阶段原因。计数不参与任何召回、标注或游标判断，开关 `decision_skip_records_enabled` 关闭时不记。召回重排“普通记忆至少几条”读 `conversation/decision_point_limits.RECALL_MEMORIES_MIN_COUNT`（调用时现读，诊断标签同源）。按用户作用域的 `owner_memory_policy_json` 由 `owner_resolver.home_paths_with_owner` 重设到该用户 home，记忆总闸不再借用本机主用户的策略文件。

- `CuratorFormalMemoryInput.authority_version/content_chars` 是宿主事实：long-term 版本取原 `MemoryRecord.version`，
  正文长度取原规范化全正文。原 `to_model` 不增加字段，因此增强关闭时原 Curator 输入字节不变。
- 完整消息用原消息哈希校验；正式条目需 long_term、精确 ref/ID、正整数版本、完整长度及正文哈希匹配。
  lesson/HOT 不用时间或 hash 冒充版本，audit 不凭 preview 冒充完整工具输出；不满足时保留原处理并记录 `need_data`。
- 每次最多 32 个明确来源—条目对，独立 Choice，不要求题目互相依赖。对数超过上限时按标准库 BM25 词面相似度
  从高到低挑（同分保持原枚举顺序、不引入嵌入调用），并在 `state.coverage` 声明总对数、展示对数与挑选规则；
  `coverage=presented_pair_only` 不证明完整正式库覆盖；未比较材料仍原样交提取。
- 发送前与采用前均用当前 owner 原 `JsonlMemory.all()` 复核精确正式快照；原版本、正文、范围或删除变化使建议失效。
  本地同步读取不承诺强杀；检查前后使用同一绝对期限，迟到结果不发送/采用，模型调用仍走原有界 worker。
- `CuratorRelationAnnotation` 只保留双方 ref/hash、正式版本、关系、实际模型和输入摘要；不含 candidate ID、动作或晋升状态。
  缩批/正式绑定变化后 `to_model_payload` 排除旧关系。超出原提取输入预算则放弃本点提示，不挤掉原材料。
- 原 `curator_prompt` 仅在存在关系时增加非权威说明；输出 schema、`validate_extraction`、`_prepare_outputs`、
  CandidateService/Promotion 和事务保持唯一。默认 `off`，线程不得新增此后台覆盖；有效值与菜单沿原设置元数据。

本地测试使用真实临时正式仓库、fake 决策、原 worker/模型账本和提取提交链；不证明真实语义质量，
不覆盖全库候选合并，也未部署。详见 [P5-B 交接](../../tasks/DECISION_MODEL_P5B_HANDOFF.md)。

恢复摘要来源扩展至同一次冻结的真实原生工具往返，与归档同ref去重后沿原adapter和有界分段器处理。严格恢复空回复/工具调用的机械回退完整保留旧摘要与原模型可见材料；分段修复失败拒绝提交，避免截断摘录获得完整coverage。不新增记忆库或持久状态，不读取外置全文。此片本地联验中，外层重跑传递原生IR仍待实现；见决策模型容量审计末节与TESTS。

`compact_carry.py`仅在同进程同逻辑回合携带原生IR、tool_context和已转发guidance；源attempt保留调用引用，下一执行身份由原DB发布。`active_turn_compact.py`核对显式线程声明与typed宿主视图；无任务后台不借task属性补身份，原transcript授权门独立保持。

retention 的恢复材料扫描同样按这两个规范根进行（`retention_scan._recovery_roots` 返回 `owner_tasks_dir` 与 `owner_runs_dir`，`_iter_task_states` 逐个产出根与其下的 `work/state.json`）：旧 `O/tasks` 与新版 `O/runs` 共用同一套 `work/state.json` 合同，终态判断只读结构化 `status`（`TASK_TERMINAL_STATUSES` 白名单），不看 mtime 也不看目录名，保留天数沿用 `completed_task_days`；`audits` 与会话恢复材料不在本片范围。

retention 扫描根与深度不再写死在本模块：`retention_scan._recovery_roots` 从 `conversation.workspace_paths` 推导规范根（`validated_durable_work_path` 列出全部持久工作根，`canonical_task_root(owner_home, path)` 判断"给定路径是不是规范任务根"），`O/runs/<date>/<key>`、旧 `O/tasks/<date>/<key>`（深度 2）与 `O/audits/<audit_id>`（深度 1）各按自己的深度校验，深一层或浅一层都不算任务根；三处 `rglob`（`_iter_task_states`、`_tool_output_actions`、`_subagent_scratch_actions`）都先过这个判断，`tool_output` 仍只扫旧 `O/tasks`，`audit` 类别（`O/audit/*.jsonl`）一律跳过。`retention.apply` 的整份拒绝维持原样（`plan.errors` 或 `legal_hold` 非空即 `applied=False`、零动作），单棵子树错误只隔离那一棵：`_without_errored_subtrees` 先算出错误路径自身的键集合（逐条解析后取并集）和"错误 + 全部祖先"集合，再对每个动作做两次 O(深度) 查表，判据是单向的"错误是动作的祖先"或"动作是错误的祖先"——两边都取祖先集合求交会命中公共祖先、把兄弟目录误判成同一棵树。

## Curator 按会话主代理模型分组（2026-10-02）

- 路由由组合根注入：`core._curator_model_router` 在没指定整理档案、或指定档案在本 owner 解析不到时生成（后者只有一个组：owner 默认模型 + 整批警告 `curator_profile_unavailable_fallback:<原因>`，由 `settings/curator_profile.curator_model_config_with_fallback` 解析）；没指定时用 `settings/curator_profile.curator_thread_profiles` 只读解析各会话的 `model_profile_id`，建好各组后端，组成 `curator_models.CuratorModelRouting`。
- `curator_routing.route_batch` 挑本次处理的那组（批内第一条消息所在的组），切出只含这组会话的子批；工具审计事件只跟默认组。
- `_CuratorRunMixin.run` 依次调用 `_run_once`（每组一次完整运行），没成功就停；`_extract_for_route` 在会话模型确定性失败时用默认模型补跑一次。路由警告在 `_prepare_outputs` 里排到运行记录警告最前，失败记录也带（`_commit_failure`）。整批路由警告由 `core._curator_route_warnings` 生成：指定档案退回原因、owner 默认模型来源（`settings/curator_profile.curator_default_model_source`）。
- `_replay_breaker` 在分组时用 `group_breaker_history` + `group_cursor_view` 只看本组历史，熔断码与退避仍写 owner 级 state。
- `_CuratorRunMixin._transient_fallback`（`_extract_for_route` 最先调用）用同一份本组历史和 `curator_routing.transient_fallback_code` 判断：非默认组连续 `CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT` 次连接类失败后（本组最近一次推进游标的就是这种默认补跑时降到 `CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT` 次，`_transient_fallback_threshold`），这次直接用默认模型，运行身份先改成默认模型；警告码由 `transient_fallback_warning` 统一生成。

## Curator 提交前的身份冲突剔除与重放熔断（2026-10-02）

- `candidates.merge_candidate_observations` 是候选账本唯一合并入口，保持严格：同一观察身份对应不同类型化主题时，抛 `CandidateIdentityConflictError`（ValueError 子类）。
- Curator 观察身份（`curator_validation._canonical_observation_id`）= 来源（消息/工具/产物/任务/运行）+ 类型化主题 + 规范化内容指纹（`operations.memory_content_hash`，与 `stable_candidate_id` 同一正文规范化）。同主题不同内容是不同观察；规范化后内容相同才是同一观察（2026-10-02 用户拍板第 6 条）。
- `_same_candidate_identity` 与身份计算同口径：主题键按 `fold_key` 比较，范围按 `scope_contract.canonical_scope_key` 规范键比较。
- `candidates.split_identity_conflicts` 是纯函数，用同一个合并函数逐条预演，只剔除身份冲突的那几条。`curator.MemoryCuratorService._without_identity_conflicts` 在生成运行记录前调用它，并记警告。
- 重放熔断的判断是纯函数 `curator_models.curator_replay_breaker_tripped`，读取最近运行记录用 `curator_run_log.CuratorRunLog.recent_finished`，退避由 `curator_failure_retry_seconds` 给出（熔断码 3600 秒）。

## 嵌入用量与召回方式计数（S7，2026-10-02）

- `retrieval/embedding_usage.py`：
  - `EMBEDDING_USAGE` 是进程级计数。
  - `count_embedding_request` 是两个嵌入客户端唯一的计数点。
  - `counted_as` / `embedding_purpose` 按操作标用途。
- JsonlMemory 的标注位置：`_index_vector` 记忆写入，`_search_scoped`、`_semantic_records` 召回，`_embed_rebuild_rows` 重建。`_scoped_retrieval_facts` 每次 scoped 检索记一次召回方式。

## 召回后排序逐条题的候选说明（2026-10-02）

`decision_recall._PRIORITIES` / `_NON_SELECTIONS` 是逐条记忆题的候选说明，只是给决策模型看的软材料：键（first/normal/later 与非排序回答）和 `_ranks` 的处理是机器语义，不随措辞变化。改说明要重跑 `scripts/bench/decision_quality_bench.py --points recall` 并登记成绩。
