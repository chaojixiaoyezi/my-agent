# 记忆与上下文维护状态

常数整改第三批（P10，分支 `worker/ds2-p10-batch3`，2026-10-02）：agent/memory_store 23 个待整改常数合规——数量上限类补
`_COUNT` 后缀改名（如 `_MAX_LIST_ITEMS→_MAX_LIST_ITEMS_COUNT`、`_MAX_ANNOTATED_ITEMS→_MAX_ANNOTATED_ITEMS_COUNT`、
`_MAX_RELATION_PAIRS→_MAX_RELATION_PAIRS_COUNT`、`_EN_KEYWORD_MAX→_EN_KEYWORD_MAX_COUNT`、`_TIMEOUT_SHRINK_LIMIT→_TIMEOUT_SHRINK_LIMIT_COUNT`、
`CURATOR_MAX_RETRIES→CURATOR_MAX_RETRY_COUNT`），`_EN_KEYWORD_MIN_LEN→_EN_KEYWORD_MIN_LEN_CHARS`、`_TIMEOUT_SHRINK_FLOOR→_TIMEOUT_SHRINK_FLOOR_COUNT`；
字符/字节/秒类补中文说明；BM25 参数 `_BM25_B/_BM25_K1` 无物理单位，只补说明并挪入白名单无单位组。数值一律不变。

2026-10-02（分支 `worker/ds1-p10-batch4`）：常数整改第四批。`cli/memory_archive_commands.py` 与 `cli/memory_commands/` 的常数改名/补说明（如 `_CLI_MEMORY_ARCHIVE_LIMIT`→`_CLI_MEMORY_ARCHIVE_COUNT`、`MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_LIMIT`→`MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT`），数值不变。

## P12：固定提炼模型档案（2026-10-01，sol，本地已实现，待集成/部署）

- 解决聊天连接失效拖累后台提炼、旧覆盖键混用聊天凭据的问题：`memory_curator_model_profile` 非空时完整采用固定档案连接，空时保持 owner 选择行为。
- 旧 provider/model 配置键删除，仅未知键告警；新引用参与 Curator 配置修订，不改变模型输出合同、游标或晋升规则。
- 引用不存在、用途不符、停用、缺凭据或目录损坏时不回退：原未配置失败码、退避和 run 账保留，诊断新增编号与结构化原因。
- 登记表按安全边界拒绝模型写入；可信管理员用户 `/settings` 可设置/恢复/回滚，详情及 `/model` 列表展示真实档案编号与型号。
- 定向 235 passed、架构守卫 167 passed，三个变异均被拦截，恢复后 99 passed；详见 TESTS。
- 未验证生产真实模型、运行 Gateway、实际 TUI/IM 展示；下一步由 3a 集成部署并绑定 deepseek-v4-flash 的实际档案，复核真实提炼与渠道显示。

2026-10-02（分支 `worker/ds1-p10-batch2`）：常数整改第二批。memory_archive 各文件与 `cli/memory_archive_commands.py` 的常数补齐中文说明、缺单位的按生成器后缀表改名（如 `ARCHIVE_SEARCH_FILE_LIMIT`→`ARCHIVE_SEARCH_FILE_COUNT`、`RESUME_RECOMMENDED_READ_PATHS_LIMIT`→`RESUME_RECOMMENDED_READ_PATHS_COUNT`），数值一律不变。

正文哈希向量缓存第四轮修正：构造宽松、写入严格（分支 `my-agent/self-dev-2-vcache`，2026-09-29）：
- **必须改**：第三轮把 `_load` 改成只有 `FileNotFoundError` 才当空之后，读错误从**构造函数**抛了出去。
  而 4 个懒建缓存的调用点（`_cached_vectors_for`、`_remember_cached_vectors`、`_forget_cached_vectors`、
  `_retain_text_cache_keys`）都在各自 `try` **之外**建缓存，于是：
  - `mem.add` 在权威 JSONL **已经提交之后**抛 `PermissionError`（V1）——调用方会以为写入失败而重试；
  - `search_scoped` 直接抛异常（V2）——违背模块自己"读失败等价于没有缓存，结果不变"的合同。
- **修法**：`__init__` 捕获 `OSError`、内存视图从空开始、记 `last_read_error`；`_flush` 保持严格
  （读不出磁盘内容就不写，免得把整份缓存清空——那才是第三轮那条建议的本意）；4 个调用点把
  `_text_vector_cache()` 挪进各自 `try`。**不要**把 `_load` 整个改回吞掉所有 `OSError`。
- 顺手：删掉挤进 `_content_hash_of` return 之后的死代码 `keys()`、无调用方的 `_flush_unlocked`；
  写失败一路报给语义健康状态。
- 维护回收改为返回 `(回收数, 错误码)`：错误码写进 `text_vector_cache_reclaim_error`，
  让"建缓存失败"与"确实没有孤儿"分得开。
- 测试增至 45 条；变异 `scripts/mutate_text_vector_cache.py` **19/19 KILLED**（含 MY7、MY8）。
- **⚠️ 更正（2026-09-29 第四轮，9b 复审抓出）**：上一版这里还写着「`remove` 在盘上本来就没有时跳过整文件重写」。
  **当时没实现**——第一版撤掉后替代实现没写进去，`_flush` 仍无条件整文件重写（探针 V4 实测 inode 照样变），
  而注释、TESTS.md 和本文档都写着已做。第四轮真的实现了它，见下条。

## 向量缓存第四轮：真的跳过无意义重写 + 回收错误可见（2026-09-29，分支 `my-agent/self-dev-2-vcache`）

- **「盘上没变就跳过写」真的实现**：`_flush` 在**文件锁内**比较改动前后的盘上内容，没变就跳过原子替换，
  但照常刷新 `_items`。判据必须用盘上内容，不能用本实例内存（P5d：别的实例写的键本实例内存里没有）。
  补 `test_v4_remove_absent_key_does_not_rewrite_file`（断言 inode 与 mtime 都不变）与删真键的对照。
- **V5：回收错误带进维护状态**。缓存文件读不出内容（坏 JSON）时 `_load` 记下 `last_read_error`，
  `reclaim_text_cache_orphans` 改为返回 `(回收数, 错误说明)` 并把它带出来，不再报假的空错误。
- 测试增至 50 条；变异 **23/23 KILLED, 0 survived**（新增 MY9–MY12 钉住这两条机制）。

正文哈希向量缓存第三轮复审返工（分支 `my-agent/self-dev-2-vcache`，2026-09-29，基于 `64f7ee64e`）：
- `remove` 改成**一律在文件锁内从磁盘减掉请求的键**，不再要求"本实例内存里有这个键"——
  父代理与每个子代理 worker 各有缓存实例，只看内存会让子代理删除的事实永久留下孤儿（P5d）。
- `TextVectorCache._flush` 新增 `keep` 参数：判据在**拿到跨进程文件锁之后**求值（P5c）；
  拿锁失败时**不写盘**、只记 `last_write_error`；主路径 rename 失败也留下错误并反映到健康状态（W1/X1b）。
- 新增 `text_content_hash` / `retain_content_hashes`：回收按 key 里的**正文哈希**判定，
  维护进程无需 embedder 即可回收（此前维护回收因"路径错 + 无 embedder"恒为 0，每天写假的 `reclaimed: 0`）。
- `_load` 只有"文件不存在"才当成空缓存。
- 回归见 `test_memory_vector_cache.py`（38 条）与 `scripts/mutate_text_vector_cache.py`（17/17 KILLED）。

记忆整理失败归因（分支 `claude/ae-curator-diagnostics`，2026-09-29，生产主 owner 只读排查）：
- 9/26 恢复以来，主 owner 有 8 次"not strict JSON"解析失败和 3 次提交失败，都在下一轮从同一游标重做成功，没有丢批次。但运行账分不清解析失败的具体原因，提交失败也只剩外层异常。
- 现在解析失败包成 `CuratorResponseParseError`，仍是 ValueError，失败码与不重试语义都不变，附带：
  - 响应字符数、是否截断；
  - 上游结束原因、输出 token。
- `_failure_diagnostic` 沿显式 `__cause__` 链记录：
  - 根因类名；
  - OSError 的 errno；
  - JSONDecodeError 的出错位置。
- 整条诊断仍不超过 300 字符，超出时退回只含类名的形状。运行账键集不变，响应正文和路径都不入账。
- 截断是否改走缩批重试，等诊断数据确认后再定，见 DESIGN_LEDGER 事实 3。
检索侧向量按正文哈希缓存（分支 `my-agent/self-dev-2`，2026-09-28，DESIGN_LEDGER「召回前补充查询」缺口 3，第二版）：`_search_scoped` 原先每次检索都把全部 active 事实送 `HybridRetriever._vector_order` 现嵌，事实越多越贵（召回前补充查询还会再翻一倍）。现在 `HybridRetriever.rank` 接受 `cached_vectors`，只对缺失项调用 `embed`，并把本轮现嵌结果经 `last_fresh_doc_vectors` 交给调用方写回。

第一版把缓存和权威向量混在同一个 `memory_vectors.json`，被独立复审驳回（隐私与正确性）：缓存项挤占 `VectorStore.search` 的 top_k；只做检索的进程整文件写回会把已删除事实的**明文**复活；写入键含 `keywords_en` 而清理用裸正文，带关键词的事实删不掉；`replace` 不清被覆盖的旧正文；同名模型换维度时旧长度向量喂进 `mean_center` 越界，整轮失败。第二版据此重做：

- 缓存独立成 `memory_text_vectors.json`（`retrieval/text_vector_cache.TextVectorCache`），检索路径永不重写含明文的权威向量库；
- 键 = `sha256(实现类名 + api_base + model)[:16]` 指纹 + 正文 SHA-256，不落端点/模型明文；
- 嵌入与键共用的文本由 `text_vector_cache.index_text(content, attributes)` 统一生成，检索/写入/清理三处同口径；
- 命中要求向量长度与当轮 query 一致，否则当缺失现场重嵌；
- 回写前按 active 身份复核，记录已删除或被替换的键不回写（收口竞态）；
- `index_all` 回收不属于任何 active 记录的孤儿键；缓存读/写/清成功后恢复健康状态；
- 跨进程写走 `locked_json_path` sidecar 锁并在锁内重读合并。

旧格式（只按 entry_id，或旧 `textcache:v1:` 前缀）不会被新键命中，视为缺失并全量重算，不做隐式迁移。不新增用户参数，`memory_semantic_recall` 仍是总开关。本机 owner 未配嵌入模型，收益只对开启语义召回的用户生效。见 TESTS.md 顶部。

会话删除时一并收走唤醒毒丸文件（分支 `claude/75-wake-poison-design`，2026-09-28）：`retention_scan._conversation_related_paths`
除待处理唤醒外，也按 `thread_id` 收集 `wake_queue/attempts/`（尝试账）、`wake_queue/quarantine/`（结案记录）和
`wake_queue/quarantine/replayed/<id>/`（重放留档）里属于该会话的文件，只收本会话、不碰其它会话。见 `docs/design/WAKE_POISON_PILL.md`。

生命周期续跑读错分支（分支 `claude/be-wake-fix`，2026-09-28，Codex 审查 B）：`carried_tool_call_records_for_requests` 改收 `CarriedIndexSource`，每个来源独立读取、各自捕获 OSError，读到一半失败的来源整份丢弃，返回 `CarriedToolCallRead`（records 与 unreadable_sources）。修复前 owner 索引一抛错，任务索引就不会被访问，携带记录与一次性编排去重一起变空。见 TESTS.md 顶部。

生命周期唤醒片续接前台轮的工具事实（分支 `claude/be-wake-turn`，2026-09-28，T3 验收观察 2）：`compact_tool_output_refs` 新增 `carried_tool_call_records_for_requests`，按（索引根, 是否只用于运行时状态）读 owner 根和任务 work 两处索引，每个根只读自己的 index.jsonl，按精确请求编号流式过滤，不全量加载；与 `carried_tool_call_records` 共用四元身份去重。owner 索引的记录带 `CARRIED_RUNTIME_ONLY_FIELD`，工具循环只用它重建去重、已执行工具和工具轮数，不进本片工具账和模型可见交接，溢出压缩携带时原样保留。修复前前台轮成功的 `create_subagents` 不在唤醒片的去重集合里，同内容派工会多出一个子代理。见 TESTS.md 顶部。

Gateway 用户的记忆总闸按各自 home 生效（分支 `claude/9b-owner-path-scope`，2026-09-27）：`owner_resolver.home_paths_with_owner` 原先没有按用户重设 `owner_memory_policy_json`，Gateway 里其它用户经 `owner_policy` 读到的是本机主用户的 `memory_policy.json`，与只按 owner home 读的 `owner_wake_discovery` 不一致；现已按用户重设，新用户按自己 home 里的默认种子（开启）生效。`test_gateway_per_user_scoping.py` 按字段全集守卫所有 owner_* 路径都在该用户 home 内。同分支：召回重排的普通记忆下限改读 `conversation/decision_point_limits.RECALL_MEMORIES_MIN`，与诊断大白话共用一处。
参数减量第 2 批（分支 `claude/9a-merge-config`，2026-09-27）：记忆配置三组各只留一个旋钮。`memory_archive_level` 同时决定运行归档与子代理收尾快照的级别（原 `memory_hook_archive_level` 删除，子代理快照改读它）；`memory_rule_routing_mode` 的 `off` 是规则路由唯一开关（原 `memory_rule_routing_enabled` 删除；决策点召回在 `off` 下直接关闭路由，不再报“off 不是合法模式”的 finding）；`memory_resume_auto_context_mode` 默认改为 `off`（等于原默认 enabled=false），单次 `--resume-context` 仍强制 always。旧键写在用户配置里只告警并忽略；回归见 `test_merged_config_knobs.py`。

参数减量第 1 批（分支 `claude/38-delete-dead-config`，2026-09-27）：`memory_hook_retention_days`、`memory_rule_receipt_enabled` 只被 memory doctor 回显，没有清理器或回执逻辑读取，
已从 `MemorySettings`、`AgentConfig`、随包 YAML 和两处 doctor 字段名单删除；`memory_archive.enforce_retention` 仍保留为显式传参的函数，无产品调用方。
同批删除的 `memory_query_default_limit/page_size` 只有归一化器认识。

决策点未触发原因计数（分支 `claude/9b-decision-miss-reasons`，2026-09-27）：记忆侧四个决策点（recall、pre_recall、curator、curator_relation）每次到达都经 `conversation/decision_reach_counts` 记一次结果：没调用记宿主原因码（如 `memory_count`、`no_free_slots`、`nothing_to_label`、`memory_changed`、`point_off`、材料不合格的 `bad_material`），真正调用前记 `called`。只在进程内累加、按 owner 节流合并写盘，召回与整理的结果、警告码和游标都不变。见[决策审计与管控](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#每个点位最近为什么没触发2026-09-27)。

参数中心同名常数收敛（分支 `claude/param-center-dup-constants`，2026-09-27）：记忆诊断两处没有读取方的 `RECENT_ARCHIVE_FILE_LIMIT`
删除（归档文件条数当时改为只读配置 `memory_doctor_recent_archive_file_limit`；参数减量第 3 批又把它降级为 `memory_doctor_cmd.MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_LIMIT`，两处入口共用这一处定义）；压缩失败熔断阈值与冷却只在 `memory_archive/compact_circuit_breaker`
定义，`agent_core/runtime/context_compactor` 改为导入，不再保留同值副本。数值不变。

记忆整理输入预算缩批（分支 `claude/curator-budget`，2026-09-26）：生产本机 owner 自 9/24 15:39Z 起每次都是 `CURATOR_INPUT_BUDGET_EXCEEDED`、游标不动。收集只按条目估算，消息收满后审计仍按保底至少收一条，身份清单里的编号也不在预算内，最终提示超预算；提取前检查不缩批，同一批永远失败。现在在标注前按最终提示实测长度截尾（先消息后审计，各留一条），尾部下一轮重放；同时修复预算早退复用上一调用者尝试形状的诊断残留。DeepSeek 官方接口拒绝 `json_schema` 的问题随后修复：模型档案新增可选 `structured_output`，该接口默认改用 JSON 对象模式并把 schema 写进提示，结果仍由 Curator 严格解析（见 TESTS.md 顶部）。没配模型的 owner 也不再按维护周期反复失败：永久配置错误不原地重试，未配模型记 `CURATOR_MODEL_NOT_CONFIGURED` 并退避一小时（发现层与 Curator 同源）。

新 owner 的导航种子不再被迁移误当旧正文（主线，2026-09-24 晚，同伴在 .9 观察到的模板标题候选）：`home_layout_v2.owner_navigation_seeds()` 成为 memory.md / memory-hot.md 种子的唯一权威（根级模板非空则复制，否则用 `home_memory_seeds` 的正式默认导航，不再是 `# Memory` 短占位）；`migration._scan_legacy_navigation` 把"正式默认"与"本 home 的种子内容"都判为 current，只有与两者都不同的内容才是 legacy 并变成 `migrated_legacy` 候选。测试 `test_memory_migration_v2.py::test_freshly_seeded_navigation_files_are_not_legacy`。

子代理 run 工作区登记 `lessons.jsonl`（2026-09-25，已合入 main `52e0190e1`；已端到端真实验收）：`AgentRunWorkspacePaths` 新增 `lessons_jsonl`，与 `findings.jsonl` 同目录，由子代理 `record_lesson` 工具独占追加；工作区同步不创建也不覆盖它。账本经验经子代理结果收口进入 owner `candidates.jsonl`，成为 `subagent_lesson` 候选：适用场景取 `when_to_use`，证据引用账本条目。晋升规则不变，正式 lesson 仍要 approved、重复独立证据（occurrence≥2）并通过威胁扫描。

召回证据落上下文包（本地分支 `claude/decision-recall-evidence`，已合入 main `ab23a2666`；真实验收随 `2efbbcd68` 记录）：上下文包 `memory_refs` 新增本轮实际注入记忆的来源清单与记忆决策发现码，区分原完整召回与召回前补充；提示段字节不变。为第 14 项 P5-A 的真实收益实验提供结构化证据。

child历史说明在不展示正文时不再提前读取完整来源或计算展示窗口，保留原线程说明及核验；三文件31项通过。三宿主seed物化峰值已定位，后续延后/释放尚未实施，12.4未完成。

12.4来源生命周期首片已本地实现：完整选中消息保留固定文件地址视图，摘要数组按原编码顺序重放；共享token估算提取同一chars/UTF8/结构开销公式，旧小JSON优化保持。load→摘要→CAS内存红绿验证完整来源及精确ID，取消/文件变化/重放隔离及宿主机械类型兼容已独立复核，不能算三宿主全链完成。真实缓存12.7已使用固定旧安装包限定验收，见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要分段本地15文件316项通过：复用原循环顺序读取JSON字符、消费后释放窗口；共享估算器改流式累计且数值保持。修复提示纳入预算，发送及来源EOF后复查取消；writer/CAS不变。全链仍有原消息/覆盖驻留，11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

工具输出原索引与artifact现保留实际run/attempt/模型turn/call身份；Compact恢复按完整身份去重，旧未知不借当前runner补值。同正文同call跨执行轮的新产物路径包含身份摘要，旧引用不迁移。此为恢复来源底座，不代表后台范围恢复已接通。

决策分支本地合入已提交插件基线后，Curator 前置标注与正式记忆召回的取消异常已直接使用唯一 `common.cancellation`；交叉定向九文件 298 项通过。关闭、超时和普通失败仍保留原提取/召回，用户或宿主取消仍传播；完整合并后全仓及真实故障组合尚待验收。

## 召回前可选补充查询（本地首片）

正式 JSONL 检索已有不提前 touch 的 scoped 候选入口和最终访问确认；宿主也可从完整问题产生至多四个有限查询片段，
现已接到独立、默认关闭的 `pre_recall` 决策点与原正式上下文。完整问题的原召回始终先执行，Jev 只能在剩余数量和字符预算内建议一次同 scope 补充；关闭、观察、非选择和故障不改原材料。
候选检索仍可使用原语义 embedding；此处的“无 touch”仅指未注入候选不产生长期记忆访问信号。
六个相关文件联合136项本地通过；受控 JSONL 漏召回样本证明补充查询可只追加并确认原基线遗漏事实。隔离 Jev 的两轮 off/observe/apply 均建议第二查询，但词面基线已命中全部两条事实，应用没有新增，不能算真实质量收益。真实 Jev 漏召回、Gateway 最终来源和净输入 token 尚未验。

## 召回后可选重排（本地已验，未部署）

原授权召回、去重与预算先选好全部材料，决策仅重排长期事实原槽位，HOT/lesson 不移动，任何非选择或错误保留原序。
采用前复读原来源和范围，过期/删除/撤销的旧记录不被建议复活；复用本轮 PreparedRuntimeContext，不另建记忆库或缓存。
原准备→上下文→工具循环组合已验证，真实 Jev 效果尚未验；见 [09 交接](../../tasks/DECISION_MODEL_P3_RECALL_HANDOFF.md)。
正式记忆读取现在先遵守原请求的 `task_local`/`control_plane` 范围与 owner `memory_enabled=false`；
这些模式不扫描正式项目记忆。已有历史或本地任务事实并不因此被删除，原控制平面材料保持原合同；三个入口场景已本地验证。

## 来源与正式记忆的可选关系建议（P5-B 第一片，本地实现）

独立用户后台接入点 `curator_relation` 默认关闭，与分类/优先级使用同一阶段和原 lease 头寸。
完整消息与带真实版本、哈希可核验的短 long-term 正文可获可能重复/更新/冲突提示；发送前和采用前复读原仓库。
lesson/HOT、截断、缺版本或 audit 正文覆盖未知时保留原批次并给 `need_data` 诊断。
提示只进 Curator 临时输入，原候选、证据、晋升、人格确认及游标不变，不做全库语义合并。
原仓库/提取/提交组合已本地离线验证；真实 Jev 的关系识别质量与实际业务收益尚未验。
边界与文件交接见 [P5-B 交接](../../tasks/DECISION_MODEL_P5B_HANDOFF.md)。

## 子代理工作区的只读路径投影（本地已验）

原 task workspace 的 root/run/task 身份和路径计算已提取为保存与上下文准备共用的只读入口。
预览不创建目录，不写共享状态、artifact 索引或日账事件；真实物化仍沿原 `ensure_subagent_task_workspace`。
默认与显式工作区下的 root/child/grandchild 路径、写入边界及原父状态合并已纳入 136 项定向测试并通过。
这是创建前容量的路径切片，尚不涵盖选中模型引用重新冻结、完整首请求展示或逐候选输出预算；
交接与限制见 [容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

## 第8步逐调用 Compact 来源修复

新 live-tool checkpoint 显式追加 `tool_call_ref.v1` 来源与尾部数组，使用原始 `run_id/attempt_id/call_id` 匹配；裸编号允许跨域重号，计数按记录保存。新 refs 参与新候选内容地址，防止同号 orphan 覆盖已提交来源。旧无 refs ID 和账本不迁移。
工具输出索引现在保留 canonical attempt/turn，恢复去重不再使用 scoped 字符串。旧无身份记录保持完整并标为 `uncertain`；全未知大历史返回未压缩，provider overflow 可能无法恢复，这是实际兼容限制。旧 reader 不可直接读取新混源 checkpoint；回滚须保留新账并匹配运行时与数据快照。定向证据见 [独立交接](../../tasks/HANDOFF_STEP8_COMPACT_CALL_REFS.md)。
决策分支吸收 main 后（本地，未合入 main），上述三元 `tool_call_ref.v1` 由 v3 四元身份取代：`tooling/call_ref.py` 已删除；主线写出的 v2 refs 行按 legacy 读取、不隐藏任何记录，未知来源的可见性和 uncertain 结果仍保留。见[依赖拆分合并节](../../design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

本地开发 C 顺序摘要来源：两遍长度/hash 与可释放字符窗口替代整份 JSON 副本。密集 iterencode 估算闭包循环积累已由有界小载荷编码修复；14文件组合333 passed、20项既有xfail，真实TUI尚未验收。

## 第8步本地候选

消息分页与幂等扫描已按完整LF边界拆出，固定尾界／字节预算由调用方显式传入；原锁、目录、游标及错误事实保留。token估算按原JSON顺序流式计数，原数值、结构开销与异常优先级不变。七文件186项通过，模型链组合18文件456 passed／24既有xfail；未部署和真实TUI验收，不代表Compact scope来源链已完成。


## 边界

出站摘要诊断已接入原模型调用账本，区分前缀改动与只追加消息，不记录正文/密钥或删减历史。
服务端缓存淘汰仍不可由客户端证明，诊断返回 unknown；真实 TUI 缓存值与调用记录另行对账。

记忆按 owner 隔离，由该用户代理维护。会话历史、持久记忆、展示归档和摘要检查点是不同概念，不能混用；人格文件确认规则与普通记忆写入规则分别处理。

## 当前实现

- 运行中的原生工具历史 Compact 摘要已在本地改为复用原有界分段发送，并在每段前后读取同一 run/线程停止信号。超大参数、结果和推理历史须完整覆盖；失败或取消只放弃候选，不回收 IR、不提交 Compact 代次。定向回归已通过，真实长上下文与模型切换仍待验。

- 本地 P2 Curator 前置决策已接原批次提取入口，默认关闭；owner 设置支持 off/observe/apply。后台设置现已与线程前台覆盖严格分开，新线程后台覆盖拒绝，旧值只允许恢复继承清理。
  observe 只留调用事实，apply 仅在原输入中附临时分类和优先级，原材料不丢弃、不重排；
  错误、超时、过期或提示超预算继续原提取。原验证、整批提交、晋升和游标仍持唯一权限。
  后台使用真实 Curator run 和 owner 范围，不冒用历史会话；调用进入原模型账本，
  尚不代表已有独立后台用量持久结算或用户会话用量归属。已用 fake 决策/原 worker 和原 Curator
  提取提交链验证，未跑真实 Jev、实际 TUI 或部署验收。
  注释整理完成后再次调用公共消费复核，服务返回后的关闭、配置修改或期限到达也不能采用旧建议。

- 本地 Curator 的有界模型等待已迁入共用 `backends/bounded_call.py`，删除旧线程/队列副本；
  到期不再额外等待清理，准确 worker 或 cleanup 未退出时保持 still-running，禁止重叠重试。
  原缩批、游标与记忆提交规则不变；与取消/HTTP/准入联合 315 项通过，尚未实际 TUI 验收或部署。
- 后台整理的模型调用现在自带宿主会话：`_execute` 按 owner_id + run_id 走与前台同一 `provider_session_scope`
  包住整次提取，同 run 重试/缩批同值、run 结束复位，不冒用前台线程会话。此前要求会话头的服务商在发请求前
  就抛 ValueError，主 owner 自 2026-09-13 起零提取，且被记成 `CURATOR_SCHEMA_INVALID`。供应商调用阶段的
  ValueError/TypeError 现由 `CuratorModelCallError` 标记阶段并归 `CURATOR_MODEL_FAILED`，解析失败仍是
  `CURATOR_SCHEMA_INVALID`；失败诊断附脱敏截断正文。本地三文件 76 项通过，真机 Gateway 尚未部署复验。
- 存储组合后，Curator 通过 `threads.list_report` 和 `messages.after_report` 读取；
  Promotion 通过 `messages.by_id_report` 核验精确消息。原游标、坏账处理和证据匹配不变，不保留旧方法回退。
- 普通后台策展让出正在工作的同模型端点，pending 和记忆游标保留；pre_compact 屏障不被延后。
- 后台自适应预算已传入 HTTP；超时取消连接，旧调用未退出前不叠加重试。资源范围限本 Gateway，
  外部应用抢占及模型服务缓存上限另查，不能误报为会话历史丢失。

- canonical 未压缩历史参与前后台模型输入；recent display window 只作展示。
- 历史读取区分 ready/unreadable/disabled，失败不能静默退成“历史为空”。
- JSONL 按物理 LF 分记录，字符串内 NEL/U+2028/U+2029 保持完整。
- 主子代理共用压缩状态与代次；自动/手动压缩、归档引用和恢复摘要复用统一路径。
- 展示当前上下文、累计用量、缓存读写与真实请求长度不能混为一个数。

## 后续重点

长会话多任务续接、切小模型后的压缩预算、跨工作片的缓存前缀，以及小时级慢模型下的记忆连续性仍需真实验收。不得仅因显示 token 下降就推断丢记忆，也不得未查请求事实就宣称完全保留。结构见 [04-structure](04-structure.md)，开放项见 [STATUS](../../../STATUS.md)。

恢复摘要来源扩展至同一次冻结的真实原生工具往返，与归档同ref去重后沿原adapter和有界分段器处理。严格恢复空回复/工具调用的机械回退完整保留旧摘要与原模型可见材料；分段修复失败拒绝提交，避免截断摘录获得完整coverage。不新增记忆库或持久状态，不读取外置全文。此片本地联验中，外层重跑传递原生IR仍待实现；见决策模型容量审计末节与TESTS。

后续外层overflow接续已本地实现：三宿主保留完整原生工具IR，释放失败经原partial出口保存完成事实；prefix接管摘要时移除旧applied_compact，避免transcript-only重复。无任务后台以宿主冻结视图校验线程，不补task属性误建任务。联合验收及剩余边界见TESTS；媒体、超大历史和真实缓存仍未收口。

2026-09-24 深夜：`ExternalizeToolOutputRequest.force_externalize` 接通上下文余量不足的外置指令——余量由 agent_core 归档入口按 preflight 同口径计算，外置层只执行：`read_file` 分页不再豁免、通用输出直接落 artifact，记录写 `output_externalized_reason=tool_result_headroom`；`read_artifact` 分页保持内联。见[验证模块进展](../verification/02-progress.md)。

2026-09-28 参数减量 C 组第 2 批：恢复/归档条数与预览预算 6 键（`memory_resume_auto_context_limit`、
`memory_resume_recommended_read_paths_limit`、`memory_resume_archive_scan_limit`、`memory_archive_search_file_limit`、
`memory_query_content_preview_chars`、`memory_artifact_default_read_chars`）降为读取点旁的具名常量，数值不变；常量落在
`memory_archive/resume_context.py` 与 `memory_archive/artifact/read_modes.py`，归档检索文件上限直接复用 `query/archive_io.py`
的既有 `ARCHIVE_SEARCH_FILE_LIMIT`。配置面（AgentConfig、随包 `agent_config.yaml`、`services/_normalize` 规格、说明基线）同步删除，
用户配置残留只按未知键告警；`memory_resume_archive_scan_limit` 的 0 仍表示不限。定向测试与静态门禁见 TESTS。

2026-09-28 参数减量 C 组第 3 批：Curator 的 `memory_curator_batch_message_limit`(80) 与 `memory_curator_max_retries`(1)
降为 `memory_store/curator_models.py` 的 `CURATOR_BATCH_MESSAGE_LIMIT` / `CURATOR_MAX_RETRIES`，数值不变；配置面（AgentConfig、
随包 `agent_config.yaml`、`services/_normalize` 规格、`_memory_types.MemorySettings`、说明基线）同步删除，用户配置残留只按未知键告警。
Curator 的 interval、turn_threshold、max_input_chars、timeout、workers、daily_finalize_hour 仍是用户参数。定向测试与静态门禁见 TESTS。

2026-09-28 参数减量杂项批：`memory_compact_semantic_summary_protect_head`(2)、`_protect_tail`(6)、`_min_middle`(4)、
`_max_input_chars`(12000) 降为 `memory_archive/compact_semantic_summary.py` 既有的 `_DEFAULT_*` 常量，`semantic_summary_config`
只保留 `enabled` 一个配置读取；配置面（AgentConfig、随包 YAML、`services/_normalize` 规格、说明基线）同步删除，残留只按未知键告警。
同批还降了 conversation/runtime 的待处理唤醒消费上限与成功完成合并窗口（见 subagent 模块进展）。定向测试与静态门禁见 TESTS。

2026-09-28：Curator 的决策输入（point=curator）按 dev 裁决收口——候选释义/非选择/need_data 语义上移 `state.annotation_criteria`，每题只留身份引用与 `criteria_key`（wire 层 `criteria` 接受共享引用字符串）；audit 事件投影不再发送全行恒空的字段。另增窗口兜底：按本点位已授权决策连接的 `model_context_window_tokens` 按整条来源从尾部裁题面，被裁来源仍在 `state.batch` 原始快照里、留在原游标之后下一轮重放，裁掉时记固定码 `memory_curator_input_fitted:decision_window`；`curator` 纳入 `_JEV_BOUND_POINTS` 有界点位。此前 owner 侧 point=curator 的 7 条 `invalid_input`（5–9ms、从未联网）即由决策请求超模型窗口触发。本地四文件 83 项通过，真机 Gateway 未复验。

2026-09-28 Jev 2（前台点位单一来源、缩小输入）第 1 步：`recall` 点位请求体去重。`state.memories` 改送
`_decision_view` 投影（`entry_id`/`kind`/`content`/`attributes`），不再携带决策从不读取的 `source`/`role`/
`created_at`/`updated_at`/`expires_at`；`attributes` **必须保留**——它参与本轮绑定校验，删掉会让 scope 变化
不再被比对成 stale（实测 `test_current_candidate_and_model_binding_rejects_stale_suggestion` 会红，已在干净基线复核）。
`need_data` 的长说明逐题相同，上移为 `state.need_data_note` 一份，题内只留必须逐题的 `required_refs`。
`criteria` 的字段形状**未改**：它是决策协议的必需字段（模型与假 backend 都靠它选答案），把选项上提成共享引用
会让 10 条既有用例红——curator 线做过同类上移，必须同时改 wire 层让 `criteria` 接受共享引用字符串，本片未做，
留待 dev 裁决。实测收益（10 条 × 520 字符正文，JSON 字符数/4 估算）：整包 11445→9781（−14.5%），
memories 7240→5920，其中元数据 2040→720（−65%），questions 3960→3570。正文 `content` 仍占请求 53%，
是否按 pre_recall 先例（`content[:160]`）截断属决策质量取舍，未做，待 dev 定。定向 66 项通过。

2026-09-28 Jev 2 第 2 步（dev 裁决后）：`recall` 单条正文加安全上限。**不做一刀切截断**——单条超过
`conversation/decision_point_limits.RECALL_CONTENT_MAX_CHARS`(800) 字符才截，截断处给结构化标记
`content_truncated: true` + `content_original_chars`，让模型知道这条被截了、原长多少；正常长度原样送，
不为一刀切省 token 破坏以后评估 apply 时的排序质量。常量出门禁点具名、不新增用户参数。
dev 同时裁定 `criteria` **不上提**成共享引用：它是与 Jev 决策模型之间的 wire 协议必需字段
（`typesafe_decision_wire` 要求它是 dict），改协议影响外部模型格式预期；curator 线最后也退回内联形态，两边一致。
变异验证 5/5 KILLED（不截断/恒定截断/截断不给标记/改上限值/原长标记写错）。定向 81 项通过。

2026-09-28 关系对按词面相关度挑选（DESIGN_LEDGER「召回前补充查询」条缺口 4，dev 派单）：`decision_curator_relation`
原先取消息×正式条目笛卡尔积的**前 32 对**，不看相关度，相关的那一对排在第 33 位之后就永远比不到。现新增
`_select_pairs`：用标准库给每一对算 BM25 词面相似度（词元口径为小写拉丁串 + 单个汉字；文档词频取该对两侧
正文的并集），按 `(-score, 原枚举序)` 稳定排序取前 32；对数不超过上限时原样全取。上限仍是 32，**不引入嵌入
调用、不增加网络请求和费用**。挑选是纯函数，同批两次构造必须得到同一 `revision`（`annotate_curator_relations`
采用前的陈旧复核依赖这一点，不确定会永远判 stale）。覆盖范围仍如实声明为"仅展示的对"，并在 `state.coverage`
新增 `total_pair_count`（全批笛卡尔积对数）、`selection`（规则标识 `bm25_then_source_order`）与 `selection_limit`，
下游可据此看出未比过全部，不会误以为已经全库比较。缺口 3（语义检索重复嵌入）仍待办，本片只用词面、不新增嵌入。
新增测试 `test_decision_curator_relation_selection.py` 9 项：越界相关对可达、同输入同输出、空材料三种边界、
覆盖声明能区分"全部比过"与"只比了一部分"、同分退回原枚举序、无嵌入入口。变异两处（分数方向反转、去掉同分
原序兜底）都被杀死，各恰好 1 条红。定向回归 125 项通过（含 relation 原 44 项、curator、架构护栏、打包边界）。
真机 Jev 样本对比未做，已合入 main（`80b4afed8`），等 Jev 复测取数。

2026-09-28 retention 扫描覆盖新版运行根（dsh-9b 盘点零风险缺口，dev 派单）：`MemoryRetentionService` 原来只扫
`O/tasks`，新版运行工作区 `O/runs` 不在范围内，那部分恢复材料永远不会被回收。现新增
`_recovery_roots(home)`（返回 `owner_tasks_dir` 与 `owner_runs_dir` 两个规范根）与 `_iter_task_states(home)`
（逐个产出「根 + 该根下的 `work/state.json`」），`_task_actions` 与 `_tool_output_actions` 都改用它。
两个根共用同一套 `work/state.json` 合同，判定逻辑一行未改：状态取自结构化 `state.json`
（`task_id`/`status`/`updated_at`），不在 `TASK_TERMINAL_STATUSES` 白名单里就整棵保留，
**不看 mtime、不看目录名**；保留天数沿用现有 `completed_task_days`，未新增参数。新增
`test_memory_retention_runs.py` 9 项（含"把终态任务所有文件 mtime 改成刚刚仍被回收"这条反证）。
变异两处（扫描根退回只有 tasks、终态白名单放宽成状态非空）各杀死 4 条与 2 条。定向 23 项通过，
五项静态 gate 全过。真机 `home-retention --apply` 未跑（会真移生产数据），只验证 plan。

2026-09-29 retention 扫描根与隔离（dsh-9b 复审第二、三轮跟进，dev 派单）：根列表与各根深度改从
`conversation/workspace_paths.py` 推导——`canonical_task_root(owner_home, path)` 是唯一权威
（`O/runs/<date>/<key>` 与旧 `O/tasks/<date>/<key>` 深度 2，`O/audits/<audit_id>` 深度 1，由
`validated_durable_work_path` 列出全部持久工作根，不在扫描里写死目录名或深度）。`_recovery_roots`
据此返回三个规范根并去重；三处 `rglob`（`_iter_task_states`、`_tool_output_actions`、
`_subagent_scratch_actions`）都先过 `canonical_task_root`，深一层或浅一层都不算任务根，运行中任务
output 里嵌套的"像任务"目录不会被整棵移走；`tool_output` 侧仍只扫旧 `O/tasks`（未扩范围）；
`audit` 类别（`O/audit/*.jsonl`）一律跳过、原地保留——类别名与实际数据不符，等存储登记表定了语义再处理。
`apply` 的整份拒绝规则不变（`plan.errors` 或 `legal_hold` 非空即 `applied=False`、一个动作都不执行），
单棵子树的错误只按"错误路径与动作路径互为祖先/后代"过滤那一棵，被保护的事实随回执如实返回。
隔离过滤是**纵深防御**：三类扫描在读不懂父任务 `state.json` 时都整棵跳过，合法输入构造不出"坏子树里
带动作"的计划，所以它只比较 `action.path`、不比较 `related_paths`（后者不含任务根）。
性能上每条路径只解析一次、错误路径及其全部祖先进集合、动作只做两次 O(深度) 查表（生产 8161 动作 ×
355 错误从数百秒回到亚秒级；隔离本身只占 0.059 秒，整份 `plan()` 的百秒级开销在扫盘 I/O）。
双语义默认只用本平台 `Path` 语义，Windows 语义由调用方显式注入 `PureWindowsPath`，避免"文件名里合法的
反斜杠被当分隔符"的多判。

2026-09-29 隔离过滤"错误是动作的祖先"方向修复（9b 探针 S1）：`error_self` 原来把**生成器**传给了只接受
单条路径的 `_path_keys`，`str(generator)` 得到 `"<generator object ...>"`，集合里只有一个垃圾字符串，
于是这一半判断恒不命中（方向是死的，MR16 变异在 36 个相关文件下存活）。现改成
`frozenset().union(*(_path_keys(error.path) for error in plan.errors))`，逐条解析后取并集；`if not
error_self` 的提前返回只是快速路径，语义上不改变结果（错误路径为空串时两个键集合都为空，逐条判断本来
也不会剔除任何动作）。

2026-09-29 自动压缩触发线的绝对上限（分支 `claude/be-compact-trigger-cap`，基于 `89af6b07a`，38 调查的方案 A）：新增 `memory_compact_auto_trigger_max_tokens`（0 = 不封顶，默认 0）。`agent_core/runtime/context_compactor.runtime_compact_policy` 在它大于 0 时把触发线取成 min(窗口 × 百分比, 上限)，近期尾部与 recovery 从封顶后的触发线推出；前台、后台、finalization 与会话压缩都读这一处。finalization 的旧归档周期原来只按百分比判断（`memory_archive/compact_suggest` 的百分比钳在 50–100，表达不了 1M 窗口下的 30 万），现在 `MemoryCompactAutoCycleOptions` / `MemoryCompactSuggestOptions` 多一个 `trigger_tokens`，只在触发线被封顶时由 finalization 传入，不封顶时仍按百分比，原行为不变。`/context` 在封顶时写明“N tokens 触发（绝对上限封顶）”。

2026-09-29 压缩触发线上限的跟进（分支 `claude/be-compact-cap-followup`，38 复审两条 should-fix 加自助修改）：`trigger_capped` 改成上限严格小于窗口 × 百分比才算，正好相等时 finalization 与 `/context` 按原百分比口径；封顶时 recovery 按触发线 × recovery% ÷ 触发% 等比推导（上限 30 万时 20 万，整数先乘后除），不封顶时原公式不变；`memory_compact_auto_trigger_max_tokens` 加进 `settings/user_config_capability.TUNABLE_KEYS`，只拒非整数和负数，生效时机与两个百分比项相同（重启 Gateway 后生效）。

2026-09-29 保留回执单列被隔离的错误（分支 `claude/9b-maintenance-apply-outcome`，基于 step16l `5ec2db2e0`）：
- `MemoryRetentionReport` 末尾新增 `isolated_errors`；`apply()` 把被隔离的扫描期路径级错误随可执行计划带进执行器，合并回执时原样带出。
- 审计事件 `owner_retention_applied` 新增 `isolated_error_count` / `isolated_error_codes`：执行了、但有子树被隔离时，审计不再显得「全干净」。
- `errors`、`load_errors`、`ok` 的含义都不变。
- 计数是错误条数、不是子树数：解析不了的坏 state.json 会被 completed_task 与 tool_output 两个扫描器各报一次（去重待定）；能解析、只缺 status 的旧格式（生产那 355 条）只报一次。
- owner 维护侧的新字段见 gateway 02-progress 同名节。
- 38 复审跟进（同分支补充提交，纯抽取、语义不变）：`execute_retention_plan` 越过函数长度 soft 线，候选批次阶段抽成
  `retention_apply._apply_candidate_phase(candidates, plan)`，返回结果与错误两个新列表，后续逐条动作继续追加。

2026-09-29 唤醒毒丸第 4 步（分支 `claude/be-wake-ops`）：`retention_scan._thread_wake_files` 把 `wake_queue/quarantine/archive/` 顶层（满 14 天归档的结案记录，带 thread_id）加进会话删除清单；`archive/ledger`、`archive/unreadable` 与原来的留档目录一样无法归属会话，不随会话删除。
