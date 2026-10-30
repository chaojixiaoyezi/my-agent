## 2026-10-29 e905-sol-4：代际清单、遗忘恢复与检索权威复核（分支已实现，待集成）

- `generation_manifest.py::MemoryGenerationStore` 的 manifest 指向 active/cold 分片，`StorageTombstones` 按永久 ID/hash 屏障排除已遗忘事实；`JsonlMemory` 只读权威读取/检索热路径不物理删文件。GC、历史明文擦除和派生索引失效由显式维护与删除恢复承担，逻辑不可见不代表物理清理完成。
- `_jsonl_maintenance.py::purge_tier_copies` 在 quota→policy→formal 锁内清 pending tombstone 残留，透传已有 admission；无根时严格读 legacy 并用 `_compact_memory_events` 保留存活事实最新版本后发表首根。首次与重启收尾均先验 `store.events(root)` 才能用 schema marker 覆盖 legacy；失败保留 cleanup pending，重试不重放业务删除，同字节永久账不改 inode。
- `jsonl.py::search` 在短 formal 锁内绑定 legacy、旧墓碑、manifest、永久墓碑四个控制文件指纹，嵌入/向量调用在锁外。返回及登记访问前重新同锁核验；无漂移复用快照，漂移最多另做一次原行数/字节预算扫描，刷新正文/版本并丢弃已删或未覆盖候选，不回退 `all()`、不重新排名。
- 有界读取的 partial/continuation 只表达本次覆盖；游标绑定根 operation_id 与分片位置，不是删除完成或恢复旧正文的许可，也不在 search 内无限追扫。删除恢复依精确 entry/version/source 墓碑及候选 CAS 接续原清理责任/回执，不生成新删除。
## 2026-10-29 e905 sol-5 R3：留存阻断项与消费者合同（分支候选）

按终审 B1/B2/B3/N1/N2 收口：规划共享一次严格 pin 视图及来源错误；scratch 每动作复核来源代次，逐文件 FD 位置检查保持。规划和执行统一保护所属 run/task 身份及任何被引用子成员，真实仅 ID 引用的外置信封和工具索引在清理前后核验保持成功。首次 RUNNING state 发表的失效测试覆盖 tasks/runs 两种布局，坏源、legal hold、活锁和旧计划重核门保持。

晋升消费者测试同步耐久履约义务及五分钟退避，身份篡改必须越过退避并严格拒绝；坏权威仍以原类型化异常返回精确错误码，approved 和权威字节不变；doctor 对齐候选 v3。Curator 只跳过非法 scope 的当前范围，合法锚点保留。指定十文件最终 406P、0F/E/skip，完整分支 22 Python 文件整文件中文双层注释缺口 0。复现、红绿与未验证边界见根 [TESTS](../../../TESTS.md)；不是合入、发布或全仓验收声明。

## 2026-10-29 e905 sol-5 R2：跨引用补全重复计票修复（分支候选）

候选旧单端引用与本次 operation→call→artifact 链按稳定别名连通性合票，多跳反向输入迭代至固定点；先沿明确历史 run 收敛，再处理空 run，明确不同 run 不合并，不同 artifact 不能仅凭共同 run 合票。原始证据和学习来源覆盖不改写，正式晋升仍走原核验。首批 12 项先 10F/2P，独审追加 8 个排列先 4F/4P；最终候选义务文件 81P、含 Curator 的聚焦组合 321P，严格门与全文注释审计通过。额外核心 237P/3F 保留失败；完整证据及复核边界见本轮交接，不外推上线状态。Curator 模块及完整分支范围补齐中文双层注释。
## 2026-10-29 e905 sol-9：规范 CLI 与统一 Doctor（开发分支，未部署）

- 内置命令收敛为 24 个规范根。新调用使用 `memory remember/list/search`、`memory route`、`memory archive resume`、`memory doctor`；旧平铺输入只在根边界转换，不另建诊断实现。完整目录见 [CLI Reference](../../../CLI_REFERENCE.md)。
- 删除 `cli/memory_doctor.py`、`cli/memory_commands/memory_doctor_cmd.py` 及其 re-export；诊断聚合归 `agent/user_space/home_doctor.py::DoctorService`，`cli/memory_admin_commands.py` 注入原只读工厂、格式化并保留各视图退出合同；`cli/memory_commands/__init__.py` 不再导出已删除 handler。
- `memory doctor` 默认 memory 视图，runtime 不装配记忆维护组件；all 保留两侧独立事实，`common` 各字段固定以 `runtime` / `memory` 子键表达，不因内容相同而折叠。只读取 stats/status/plan，不执行 apply、正式晋升或模型；字段值与实际回归结果由集成者核验。
- [人工规则与记忆候选控制](../../../CLI_REFERENCE.md#人工规则与记忆候选控制组合候选未发布) 恢复现行说明与跨文档锚点；历史手册只用于迁移核对，不生成新调用。此处记录源码和文档职责，不宣称运行包、真实 IM 或服务生命周期已验收。

## 2026-10-27 CI 全绿收敛：记忆候选兼容读口、自动晋升补全与产物大索引流式过滤

- `memory_store/candidates.py` 与 `_candidate_read_compat.py`：保留完整五元组 stat 缓存、坏行/坏 UTF-8 必报与深拷贝隔离，兼容对 `_load_unlocked` 的测试覆盖。
- `memory_store/promotion.py`、`_condense_scope`、`jsonl.py` 与 `retention_scan.py`：兼容显式 `fill_long_term_target` 覆盖与独立候选根下的 `pending_condense.json` 定位，在保持 `quota -> policy -> memory` 锁序的前提下支持同线程重入与精确错误上抛。
- `memory_archive/artifact/reader.py`：超大 `index.jsonl` 在超缓存预算时使用单遍有界流式扫描替代全量列表驻留，将峰值内存控制在 `< 3 MiB` 预算内。

## 2026-10-26 全量对向复核二轮收口（消息级禁学游标推进、访问冲刷锁序对齐与直连会话引用覆盖，已实现）

- `memory_store/curator_inputs.py`、`curator_validation.py`、`curator_routing.py` 与 `curator.py` 补齐 `skipped_message_cursors`：当单条消息携带 `learning_excluded=True` 时，`_collect_messages` 向前多页扫描收集后续正常消息，并在无有效提取输出时仍推进已跳过消息的分页游标，防止单页禁学消息造成游标永久停滞。
- `memory_store/jsonl.py` 在 `flush_access_events` 与 `flush_access_events_from_source` 外层按 `self.path.absolute().parents[2]` 定位 `owner_home` 获取 `_quota_admission` 与 `_managed_policy_scope`，严格维持 `quota -> policy -> memory` 统一锁序；`memory_store/retention_scan.py::_conversation_roots` 与 `local_storage/records.py::owner_protected_ref_ids` 同时纳入 `<owner_home>/conversations` 直连会话库与 runtime 会话库。

## 2026-10-26 Wave 5 Track A Round 5（分支修复，待集成）

- 渲染器、预算入口及最终访问确认共用原正文扫描/来源白名单准入门；对 JSONL 重读的当前记录也复核，隔离项不登记访问、不进入最终集合或获得补充来源 ID 信用。直接调用渲染器的隔离计数和转义保留，原 scope/版本/来源及固定来源优先级不变。
- F2 测试挂接真实临时长期库，不给生产增加缺记忆库时跳过确认的降级。12 项新增回归含 10 个先红反例和 2 个原安全行为控制；访问集合对照实际渲染信封、pending 和刷盘后账本，原 R4 回归及副作用断言保持。指定七文件最终 **243 项全过**，准确结果、严格门与静态独审范围见 [TESTS](../../../TESTS.md)；不代表已合入或部署。

## 2026-10-26 Wave 5 Track A Round 4（分支修复，待集成）

- 基础与补充召回只选候选，最终去重和信封预算之后才重核正式 JSONL 并登记访问；预算淘汰项不再影响冷热判断。独立补充调用默认合同、原 `latency_safe` 与隔离总闸保留。
- 新增 28 项真实账本回归，21 个缺陷反例先红后绿，7 个直接调用/隔离/材料模式控制保持；指定六文件最终 307 项全过。来源 ID 随成功 refresh 更新且仅含本次实际返回的固定项，覆盖 HOT 撤销回落 JSONL 及新增未入选固定源的同 ID 竞态；待刷/持久访问、既有 pending、去重及最终撤销/版本/范围变化的复现见 [TESTS](../../../TESTS.md)。

## 2026-10-26 Wave 5 Track A Round 2 + Round 3（分支修复，待集成）

- 前台基础召回和补充候选召回启用缓存向量/词面低延迟模式；audit-only 冷 owner 不再被判空。冷归档写后校验不依赖 Python 3.11 API。
- Retention 补齐冷候选完整引用及版本游标分片 pin；坏冷详情关闭相关清理。提交后报告失败沿实际已提交游标记账；中断更新恢复严格读取正式/墓碑全部行，不跳坏行补 promoted。
- R3 将原行缓存移出 dataclass 字段/asdict，并将 retention 运行时钟透传至删除提交；顺手归档仅处理未晋升终态，显式维护仍可冷归档 promoted。指定八文件 **385 项全过**，保留 29/30 天边界和 31 天 promoted 逐字段原样断言；不外推未复测的人格快照链路。
- 21 项新增回归先红后绿；完整定向门禁、旧测试路径适配及独立复核限制见 [TESTS](../../../TESTS.md)。源码候选不代表已合入、部署或通过 Python 3.10 实机验收。

## Wave4 A1 产物索引纯解析复用（分支已实现，待三方集成验收）

- `memory_archive/artifact/reader.py` 的逻辑引用先匹配字面ID，不解析无关path；完整scoped ID在主根命中后不枚举历史目录。路径ref、短ID的scope优先级与basename歧义继续走原规则。
- 索引先安全打开，再以root/path和该FD的dev/ino/size/mtime/ctime复用有界解析快照；命中记录深复制，缓存不承担路径授权。正文仍须重新通过原nofollow、安全范围与摘要校验，更新或替换索引失效。
- c5的新分层合同取代旧eager筛选：纯字面筛选不触发路径错误，真正命中坏路径仍抛原错误；索引解码、晚到UTF8和大整数错误不隐藏。相关热路径测试增加最终命中坏目标的断言，而非删除异常验证。
- 回归、命名差异、死代码清理与未验证的生产耗时见根[TESTS](../../../TESTS.md) Wave4节；不把调用次数改善写成线上绝对加速倍数。

## 2026-10-26 记忆候选只读行借用、$O(1)$ 索引合并与空临时目录按年龄回收（已实现）

- `memory_store/candidates.py` 新增 `CandidateService.readonly_rows` 供 `retention_scan.py` 与 `learning_exclusions.py` 在锁内构造只读扫描元组，并将 `_merge_existing_candidates` 升级为基于 `candidate_id` 与 `(dedupe_key, target, scope)` 的字典索引查找，将合并复杂度从 $O(N \times M)$ 降至 $O(N + M)$。
- `memory_store/retention_apply.py` 在完成过期文件清理后，通过 `_prune_empty_ephemeral_dirs` 对 `cache`/`tmp`/`staging` 树下严格早于 `cutoff` 且无新建祖先的空目录执行 `O_NOFOLLOW` 安全回收。

## 2026-10-25 WP11：候选大文件五元 stat 摘要缓存与只读预过滤（工作区已实现，待组合验收）

- `candidates.py::_candidate_load_probe` 以 `(mtime_ns, size, ino, dev, ctime_ns)` 为版本事实；大于等于 1 MiB 且五元组未变时复用有界 `_CANDIDATE_STAMP_DIGEST_CACHE` 摘要，小文件仍读取全文并计算摘要。冷读核对尺寸与读后 stat，竞态或超出 128 MiB 缓存预算时不发表该探针。
- `_drop_candidate_load_cache` 在原写边界同时使摘要与解析快照失效；保留最多 8 路径和原候选完整坏行/schema/重复 ID 校验，不靠缓存生成新的审核或晋升资格。
- `CandidateService.list()` 在原候选锁内经 `_borrow_candidate_readonly_rows` 借用已完整验证的缓存元组，先按 `statuses` 过滤、按创建时间/ID 排序和 `limit` 切片，最后只 deepcopy 实际返回结果；不把内部候选引用交给调用方。不限量返回全表时仍复制全表，不宣称零拷贝或无 O(N) 遍历。
- `stats()` 热缓存直接把只读行交给 `_candidate_stats_from_rows` 聚合数量、状态和类型，不复制或排序候选。`_load_unlocked` 被实例/子类覆盖时不绕过原加载器，`list` 被覆盖时统计仍尊重原列表；缓存未命中继续完整账本校验。该接线在本轮文档同步期间并行补齐，已按新版本源码复核。
- 下方 128 MiB、`get()`/`locked_snapshot()` 单条深拷贝是此前已有优化。列表/统计的最新源码变化不等于真实 Gen2 GC 与生产 CPU 收益已验证；本轮只改文档，不跑性能测试、不改候选数据。设计范围见 [设计台账](../../../DESIGN_LEDGER.md)，测试复现入口见 [TESTS](../../../TESTS.md)。

## 2026-10-25 记忆候选自动审核归档恢复、32 条晋升队头去阻塞、30 天未晋升候选定期清理与进程 locale 对齐（已实现）

`memory_store/promotion.py`、`memory_store/curator.py`、`memory_store/candidates.py`、`memory_store/retention_scan.py`、`memory_store/retention_apply.py` 与 `tooling/process_registry.py` 完成以下修复：
- **自动审核归档恢复（`promotion.py`）**：恢复 `MemoryPromotionService` 对通过宿主学习来源（`learning_source.can_learn=True`）、结构化权威、证据核验与安全门的非 `AGENTS`/`SOUL` 候选自动审核归档（`_automatic_policy_block` 末端返回 `""`，`automatic=True` 时经 `_apply_automatic_policy` 将 `pending_review` 转为 `approved` 并完成正式库写入与候选落盘）。`AGENTS.md`（`AUTO_AGENTS_WRITE_FORBIDDEN`）与 `SOUL.md`（`host_promotion_mode == "manual_required"` -> `AUTO_POLICY_AUTHORITY_REQUIRES_REVIEW`）仍 100% 仅限用户当场显式修改/确认。
- **32 条晋升队列去阻塞（`curator.py`）**：`MemoryCurator._pending_promotable_candidate_ids` 先全量读取缓存候选并过滤掉 `promotion_target in {"agents", "soul"}`、`promotion_exclusion != ""`、明确不可学来源（`not projection_is_learnable`）以及已处于 `approved` 但未达重复门槛（`lesson < 2`、`hot < 3`）的候选，再按“可学且已达重复门槛优先”排序后截取 `limit=32`，彻底消除队头单次 `approved` 经验堵死后续 `long_term` / `user` 晋升的问题。
- **30 天未晋升陈旧候选定期清理（`candidates.py`, `retention_scan.py`, `retention_apply.py`）**：定义 `RETENTION_ELIGIBLE_CANDIDATE_STATUSES = CANDIDATE_STATUSES - {"promoted"}`，将定期清理范围从仅 `rejected/expired/superseded` 扩展至全部严格早于 `rejected_candidate_days`（默认 30 天，`updated_at < cutoff`）的非 `promoted` 候选；带正式目标版本绑定（`_is_binding_tombstone`）的陈旧候选在清理时转为 `superseded` 脱敏墓碑，保留版本绑定且不再重复触发清理。
- **进程出生时间 locale 固定（`process_registry.py`）**：`capture_process_birth_token` 与 `_process_is_zombie` 的 POSIX `ps` 分支显式传入 `env={**os.environ, "LC_ALL": "C"}`，确保中文等非英文 locale 下 `ps -o lstart=` 与原生内核出生身份（`libproc.proc_pidinfo`）的秒级投影严格一致。

## 2026-10-25 `candidates.jsonl` 大账本 `128 MiB` 缓存与单条深拷贝、排除名单幂等短路（已实现）

`memory_store/candidates.py` 与 `memory_store/learning_exclusions.py` 针对线上 `candidates.jsonl` 突破 `32 MiB`（`46.9 MB` / `9,880` 行）后引发 `memory-curator` 晋升阶段缓存失效与 Gen-2 GC 停顿完成优化：
- `_CANDIDATE_LOAD_CACHE_MAX_BYTES` 从 `32 MiB` 提升至 `128 MiB`（同步 `constants_catalog.json`），保持 `_CANDIDATE_LOAD_CACHE_MAX_PATHS = 8` 路径 LRU 与 `((mtime_ns, size), blake2b)` 摘要强一致校验。
- 新增模块函数 `_load_one_candidate_unlocked` 与 `_cached_candidate_rows`：`CandidateService.get` 与 `CandidateService.locked_snapshot` 在命中进程内缓存时按 `candidate_id` 只 `deepcopy` 匹配的单条 `MemoryCandidate`，消除每条晋升复核深拷贝与重排全量近万条候选的 $O(N)$ 内存分配。
- `learning_exclusions.py::sync_learning_exclusion_marks` 跳过已带有 `promotion_exclusion == LEARNING_SOURCE_EXCLUDED` 的候选，避免无变更时获取候选账本写锁；`excluded_candidate_ids` 在候选集不含非空 `thread_id` 消息引用时直接返回空集，省去冗余 `conversation_store.threads.list_report(limit=0)` 全表扫描。

## 2026-10-24 第二波（W2-2）工具输出 `blob_v1` 二进制直写与 `read_artifact` 切片直读（已实现）

`memory_archive/tool_output_externalizer.py`、`memory_archive/artifact/reader.py`、`memory_archive/artifact/page_reader.py` 与 `artifacts/registry_projection_cache.py` 落地 `W2-2`：
- 新增 `tool_output_storage_format` 配置（`"compat"` 默认 / `"blob_v1"`）。在 `"blob_v1"` 模式下，大型工具输出正文以纯 UTF-8 流式直写 `<call_id>.blob`（`write_private_text_file_atomic_unlocked`），JSON 包装文件仅保留 `artifact_format: "blob_v1"`、`blob_filename`、`blob_size_bytes`、`blob_sha256`、`line_offsets` 与前 2000 字符预览，消除大字符串 JSON 二次转义放大。
- `read_artifact` 对 `artifact_format == "blob_v1"` 强制以声明格式为唯一权威（忽略注入的内联 `content` 字段），校验 `blob_filename` 严格匹配 `<stem>.blob`（兼容含 `..`、冒号目录或路径分隔符的合法 `.blob` `call_id`、相对侧车路径与 `<run_id>:<call_id>` 引用，短 `call_id` 通过 `_exact_scope_matches` 按 `run_id -> request_id -> task_id` 严格优先匹配当前运行且含 `:` 引用通过对象身份优先匹配带 scope 的 `scoped_call_id` 避免同名无 scope 历史归档或异 `run_id` 同任务记录覆盖，回退 `.json` 包装文件查找时强制 `physical_only=True` 并去重 `open_errors`），并通过 `page_reader.py::read_blob_artifact_page` 基于 `line_offsets` + `os.pread` 执行有界字节窗口切片直读，无需将整份数 MB JSON 反序列化进内存。
- `artifacts/registry_projection_cache.py` 新增按索引四元物理指纹驱动的进程内 LRU+字节上限（`8 MiB`）只读投影缓存，并在 `_retained_tree_bytes` 中递归计入 dataclass `__dict__`、`MappingProxyType` 底层字典与 `latest_records` 视图，确保进程常驻内存严格受控。

## 2026-10-23 工具输出归档 `read_artifact` 单次解析计划与路径系统调用收敛（已实现）

`memory_archive/artifact/reader.py` 新增 `PreparedToolOutputArtifactRead` 与 `prepare_tool_output_artifact_read`：`ReadArtifactTool.execute` 在单次工具调用内最多执行一次 `index.jsonl` 发现与查找，并在调用方显式指定 `max_chars > 0` 或输出预算未开启时跳过冗余 `estimate` 路径。索引行匹配新增本次查询局部的 `_ArtifactPathCache`，对空路径 `path == ""`（`kind="tool_call"` 记录）复用单次解析结果，对规范 POSIX 绝对路径按父目录缓存一次 `realpath + stat` 并以单次叶子 `lstat` 判定普通文件，符号链接、环路（`ELOOP`）、大整数与晚到非法 UTF-8 等异常行继续沿用原错误合同；正文读取通过 `read_text_beneath` 执行锚定无跟随打开，消除双扫与数十万次冗余路径解析系统调用。详见[产物查找耗时设计](../../design/ARTIFACT_LOOKUP_LATENCY.md)。

## 2026-10-21 审计与快照 JSONL 写后尾部有界回读（长期运行性能收敛）

`memory_archive/storage.py::_verify_record_exists` 在 `append_raw_event` 与 `append_snapshot` 写后校验时，优先按文件末尾有界窗口（`max(256 KiB, 2 * len(expected) + 4096)`）读取并在首个物理 LF 后解码 UTF-8 倒序核对刚追加记录；仅在小文件或尾部未命中时回退全量 `read_text`，消除单日大体积 `audit/<date>.jsonl` 每次工具调用都全量读入内存的 $O(N^2)$ 放大，同时保持完整规范化 JSON 相等性校验。

## 2026-10-20 策展发现与执行的时间/配置一致性（独立候选，待真实复验）

策展 consumer 与 owner 发现共用 `memory_store/curator_due.py` 的纯本地时间、日终和间隔判据。C 从原 owner 配置解析实际 interval、daily_finalize_hour 与 timezone；空 timezone 继续使用宿主本地时区，未来日界重新按该日期的时区规则计算，避免当前固定 offset 跨 DST。失败退避基数与 consumer 同为 `max(30,min(300,interval_seconds))`，原特殊失败码一小时不变。mtime 仍只是来源粗筛，不代表真实 batch、排除名单或 turn_threshold 已满足。

引用本身不携带配置授权：`_submit` 入口按当前可信配置重读完整原来源并重新判资格，冻结本次预期；原 T→controller/lease→runtime 短门内部，在实际 queue 写入前再核完整配置及 timezone、source/hash/input_revision 和当前时间资格。单纯时间失格/禁用以私有同步控制流退出本次未写提交，不增加耐久状态；配置/来源变化及未知仍原拒绝。W/Count0 的 `read_source`、原 busy/not_due 结果、pending 重排、P3 和停止合同均不改。

671 的 pending 细分结果未保存，不能把静态 UTC/配置分叉直接认定为那次 drain 失败的确切原因；本片不清旧 pending、不把 not_due 改 handled、不释放 P3。源码隔离真实功能先行，最终统一 strict/full 与一次发行验收；本地 fake/时间矩阵不代实际停止通过。

## 待确认候选与策展派发资格（362，已实现待独审）

待用户确认的候选不再单独唤醒owner或创建interval策展批次；host的auto_eligible来源资格、原候选selector和正式提交确认门保持。真实消息/审计、原pending reason、有效或旧lease恢复、原时间/日终仍按既有链处理；完整source SHA、P3/额度和停止门不变。原conservative_v1仅保留已合法触发批次中的候选处理尝试和结构化回执，manual_only跳过尝试，两者均不授自动正式发布。

语义边界：候选only也不再偶然触发已提交删除墓碑清理；原显式remember/review恢复及真实输入/日终提交后的selector保持，不新增cleanup scheduler或fallback，也不宣称所有自动恢复调度不变。组件与严格门另见TESTS；实际352失败和snapshot内部reason未知保持，不以切片回归代整体/发行验收。

## 2026-10-17 F3 最终写入闭包已整合（待统一验收）

USER/SOUL沿原人格库、配额、版本CAS和恢复顺序，仅C本地写叶消费当前原source.perform；默认False原调用形状与120项默认AST保持。105独立复核发现的两个FD清理出口会盖掉失源错误，108已修且109独立1409项、原同字节两反例及112无源码origin夹具通过；固定close只尝试一次，必要FD释放不授予补偿写入。三个原调用方同bool透传，不保存source或增加异常状态。

最后接通原冷C记忆候选dispatcher：与原factory共用同一command对象，七动作共用原真人seal、source、完整raw CAS和回执；原ExitStack覆盖耐久completed和单次事件发布，退出后才归还同holder借用和global scope。103独立158项及真实HTTP寿命/未知/重放复核通过；Count0、原父未知收口、跨owner和群actor权限不变。冷search/remember、候选确认与人格写叶现已在候选树接齐，不再以旧not-ready回执充数。

固定后台启动入口同时已恢复并支持同包.py/.pyc。当前仍未提交、部署或发包；最终focused、全仓、严格gate、双平台同源包及真实任务结果另记，不能从切片数字外推。公开启动仍保持默认Count0，正count沿原内部验收入口，未新增用户配置或扩大网络/权限。详见[冷记忆闭包](../../design/COORDINATOR_CLIENT_MEMORY_CLOSURE.md)与[人格IO合同](../../design/PERSONA_COORDINATOR_IO.md)。

2026-10-17 根104：原候选服务接口81和实际binder首故障修复99已精确合候选，100独核419项通过；正式分派/Persona/发行入口后继尚未闭合，非端到端或已发布。

2026-10-17 根90：冷C search/remember五路径已按独立复核77合入候选；原默认和部分失败事实保留，候选确认及USER/SOUL闭包仍待后继，不作为完整F3/发行通过。

## 2026-10-17 冷记忆C2与holder借用修复已合候选

根83按精确前像合61组件及76修复：server唯一有界owner holder、原source固定client模式、严格访问snapshot成功后清理。Astra74实证的构造发布/借用登记竞态，由原condition内先预留borrower、同finally归还解决；Astra82独立339项与5项退出边界通过，旧同字节反例1红转1绿。原默认Count0、recent、管理写源不变；holder不保存请求/source/actor，不代其它用户flush。J3装配及候选管理尚未全部接通，search/remember当前仍旧未就绪回执，不宣称F3完成。

根80将J1/J2合当前工作树后403项通过（0F/E/skip、7.542秒，完整输入不变）。1839原组合中的一次审批SQLite锁竞争仍保留定位记录；此处不称最终组合或发布已验收。

## 2026-10-17 冷记忆J2写叶已合候选

Astra78独立311项、原调用方反例1项及额外3项通过后，根80精确合四生产hunk：VectorStore成功后发布、TextVectorCache原乐观增量与诊断、lesson正文→routing/HOT→demote原partial保持；原Promotion唯一ValueError catch在C分支先透传实际来源类。默认138函数AST已独核。实例仅保存IO策略，不持请求/来源。J3和候选管理、人格写叶尚未全部闭合，未提交/部署/打包，不能称端到端已完成。详见[投影写叶合同](../../design/MEMORY_J2_PROJECTION_WRITE_PORTS.md)。

## 2026-10-17 冷记忆J1六写叶已合候选

Astra72独立189项与两项实际SQLite回滚边界通过后，根78按精确前像合候选/quota/policy锁、原候选/operations单次raw写及原账本脱敏六路径；没有重套67或覆盖其它网关变更。默认136函数AST已独核，C只保IO策略bool并消费原当前scope。扫描/序列化/embedding仍在Root外，formal→index→mark→condense与原partial保持，完整raw CAS和O(N)本地事务不称恒短。后继J2、JSONL/builder和HTTP仍待闭合，不能据切片绿称整项可用。详见[写叶合同](../../design/MEMORY_J1_WRITE_PORTS.md)。

根70组合1839项中1838通过、1项审批并发SQLite锁竞争失败，已保留原账和测试结果，正在定位，尚未提交、发布或部署。C2另有独立复核发现的holder借用预留竞态，修复另片进行。

## 片A：显式remember真实服务提取（待评审）

## 2026-10-17 C1 记忆资源纯提取候选（未发布）

解决冷C没有显式真实资源构造入口、而直接创建完整Agent会加载无关运行能力的问题。本片仅把原记忆构造与嵌入解析提成真实config/home/paths/领域对象的纯装配叶；core仍按LocalStore→Candidate→semantic→原startup聊天快照→JsonlMemory→Persona→原AGENTS次序赋值，Curator后半段原地不变。默认模型/共享/凭据/能力解析、唯一日志去重与embedding用量不变，无新增配置开关、请求、缓存或关闭政策。

本地原7文件132项与新增真实构造/引用/显式profile用例已验证，完整source map、XML及规范化AST对照在本片独立工件，实际冻结结果以该REPORT为准。C1只消除依赖障碍，`/memory candidates`和client search/remember的冷入口尚未接通，不能称F3 ready。C2的pending容量进展与C3实际末writer仍待单独裁决，不在本片增加容器、source、HTTP、timeout或typed政策。

建议下一步：由非作者先复核六路径默认等价与构造时点，再按函数集成；其他agent可并行只读准备C2/C3，禁止并改core/profile和真实服务。


ExplicitRememberServices仅携原CandidateService、MemoryPromotionService、JsonlMemory三对象引用；remember_explicit保存原校验/digest/observe→review→promote→formal查询与异常。原RuntimeMixin.remember委托，recall/core/embedding解析/锁/费用/资源寿命不改。新增普通/群真实服务及fake外部embedding用例；结果见片A报告，未接冷client与候选入口。

## 2026-10-16 F3 模型排空与 Curator 未结出口（仓外切片，待组合）

组①在原 model_call_ledger 弱集、记录及 retention 上新增只读 drain 快照，不另建模型账或把逻辑终态当物理退出。Curator 通过原 bounded worker 在发送前复核真实后台 claim；F2 owner/session/预算与 Count0 默认及时返回保持。独立 139 合同通过只覆盖该局部运输，不表示完整排空或 P3/native 终态已验。

本片让 `_run_once`、`_run_leased`、`_extract_for_route` 及原 lease interruption guard 优先原样抛出 canonical 缺端口、运输未结与独立物理退出未确认。不能把它们写成普通失败、换默认模型补跑或退役活租约；pending 代次、游标与原 Call/lease 责任保留。心跳按原流程停止，停止未确认的原异常元数据保护仍在。普通错误与合法解析 fallback 保持，未结不增加正式内容批准或自动晋升能力。

范围仅三函数和精确 lease guard；最终请求 unknown/P3、原句柄 drain 和恢复由 F3 宿主组合根负责。未验 Count2、真实模型/网络、原生进程或完整取消后物理资源释放；后续必须沿原 canonical claim/终态核验，不能凭快照为空或本片通过放行。

## 2026-10-16 正式包同时间片缓存与最新快照修复（待统一验收）

Linux 实际出现五字段 stat 相同而正文已变的消息/审计文件，定位缓存会吞掉原缺 cursor 错误。现复用原两秒信任窗口，近期文件不写、不用 hint；原持久游标、分片错误和 owner 隔离不改。持续写入可能冷扫，稳态读取成绩只适用于真实已稳定文件。旧实现因果负例全红，修后分平台定向通过；完整组合与新包另见根 TESTS，不把历史正式包失败视为通过。

最新上下文快照改用整数纳秒，同目录同时间片优先 latest 发布副本，真实较新归档及较新日期目录仍优先。无新增库、配置或文件结构；日常部署尚未切换。

## 2026-10-16 F4/F5 规则、学习与本人确认（组合候选，未发布）

- AGENTS 是本人规则，来源配置唯一 CAS；模型只读，来源/正文变更由当前用户完整预览确认。宿主学习来源贯穿真实归档，资格只允许候选，unknown 与自动化不升级成批准。
- working_agreement 进入唯一长期记忆候选，正式 Memory/lesson/HOT 自动发布关闭；原人工 review-approved→promote(false) 与证据、配额、冲突及目标版本门保留。普通/群 IM 已补 `/memory candidates` 本人预览确认，不把本机 CLI 管理命令当远端入口。
- 确认绑定候选全记录 SHA、完整显示正文、准确 owner/actor/thread 与目标 CAS；原 receipt 单次 nonce 先耐久 unknown，审核和提交间竞争拒绝，未知不重放。取消不改候选，超限或脱敏不完整不提供确认。
- 删除墓碑及候选/向量/SQLite/正文派生坏库不假成功，保留 REMOVE_CLEANUP_PENDING 和已提交事实，恢复仅清理；原 E11 有界扫描不回退。
- F5 仅读绑定组件元信息与当前权限，正式条数未读取就 null；规则 configuration 不是实时正文，新 Skill 确认/撤回和加载仍沿 main 原链。
- 作者与非作者证据分开见[TESTS](../../../TESTS.md)，当前最终组合 gate、真实资料迁移、供应商/飞书与发布均未完成；详见[规则设计](../../design/PERSONA_AGENTS_OWNERSHIP.md)、[自我目录](../../design/SELF_MAP.md)。

## 2026-10-16 F1 上下文包减负（集成验收中）

- 上下文包在内存定稿后各写一次；完整工具清单按当前 owner 内的实际字节摘要复用。摘要用 v4，已发布内联 v2/v3 显式兼容，完整清单 v3 与 main 权限事实保持。
- 原 retention 同批管理快照和清单，保护 latest、近期、task legal hold 及执行时仍被快照引用的清单。规划后变化和移动失败不会导致误清仍在用的清单，清理不改规范记忆或任务。
- 两臂同输入计量及本轮准确组合验收见根 TESTS 和七块合并设计；来源通过不代替 main 组合 gate。

# 记忆与上下文维护状态

## 压缩请求出站前缀（compactcall，2026-10-08，本地修复待07复核）

- live与归档摘要保留原动态尾项完整字节，新摘要规则独立最后追加；Responses远端降档不再抢到历史user之前。preflight/native共享选中历史及工具/system，窗口内预算与运输同材料；CAS/回滚不变。
- 完整JSON假运输覆盖远端/文字、preflight/live/active_turn、两工具顺序和首尾空白；Anthropic正文块保留而缓存标记/分组允许推进。上游契约及未验证生产收益见DESIGN_LEDGER的compactcall。

## AGENTS 第 6 轮（mc2-agentsr6，2026-10-09；本地实现待复审/集成）

- 字节快照、正常/异常主体的 stop/join 中断原对象保护、completed 旧精确租约收尾对账已实现；只改旧 record 或明确死亡的旧 active_lease，不重做业务，历史留在原记录。
- 真实线程/子进程及 I/O 故障恢复用隔离 HOME 验证；详情与限制见根 TESTS。after_delete 崩溃回执计数可能为 0 已明确记录，未修计数。真实 owner、副本生产清理、Gateway 和全仓未验证，phase1 接口保持暂停。
- 下列第 4/5 轮为历史；当前锁序为 curator 租约→quota admission→policy→人格→线程前缀。未结清 completed 的对账另用规范 state→原 record，不把记录 pending 当执行授权。

## AGENTS 字节快照（mc2-snapbytes，2026-10-09；待复审/集成）

- 复制载荷不再 UTF-8 解码或换行规范化；原锁内私有原子字节写，字节读回、SHA 与长度，清单兼容。
- 隔离公开 apply/续作等 7 项通过；单点文本读取变异 rc=1 且源码字节恢复，详见 TESTS 顶部。真实 owner 副本与 Gateway 未验证；第 6 轮后续见上节，不放行生产清理。

## AGENTS 第 0 期第 4 轮（mc2-agents0r4，2026-10-09，待独立复核）

- 实际建立 policy → AGENTS 外层临界区，quota 先于目标锁；全部目标一次锁内批处理，重判定到完成记录不放锁，续作尾与计划同锁采样。
- curator 提交后保留租约至晋升结束，积压补晋升不绕过租约；心跳及精确 ID 释放不重复累计游标和计数。
- 第 3 轮关于已经一次删除/完整外锁的交接不准确，本轮修正；下面旧节仅保留历史，当前锁序为 curator 租约 → policy → 人格 → 线程前缀。
- 合成数据合同、变异及门禁结果见根 TESTS 和交接；真实 owner、Gateway 与全仓车道未验证。

## AGENTS.md 第 0 期：硬拦与清理迁移（mc3-agents0，2026-10-08，分支 worker/mc3-agents，本地实现待复核）

- 生产 owner 的 AGENTS.md 两天被 curator 自动追加 437 条（派活会话任务书被当用户长期约定）。三件事：promotion 最终提交入口硬拦 agents（自动/积压重试/人工/审核后一律拒绝，候选保持原状态带原因码）、owner 策略学习排除名单（按会话跳过收集 + 既有待定候选持久标记）、CLI 清理迁移（默认 dry-run、`--apply` 才执行；快照+归档+分批 CAS 删除、幂等续作）。
- v2 四个阻断：①硬拦覆盖 `promote()` 主拦、`_automatic_policy_block` 与 `_commit_persona` 最终入口，curator 补晋升与可晋升判定同步排除 agents；②归属判定按「当前条目最后一次有效写入的来源」——用户删除后重加同文字、rollback、手工编辑均歧义保留并列出；③apply 绑定 dry-run 计划摘要，快照含 `persona/backups`、排除名单、归档与事务状态，崩溃幂等续作且不覆盖用户迁移期间的修改；④清理后按内容失效受影响线程的冻结人格快照。
- 测试 21 项（guard 7 + migration 14）；6 项变异全部 rc=1 且备份字节还原；489 条同规模 plan+apply 0.656s。未验证真实 home 迁移（由 3a 部署后空跑→执行）、真实 Gateway 链路。
- 0r 返工（mc4 review29 七阻断 2901–2907）：执行前在人格锁内核对版本账本尾（计划后任何写入都让计划失效）；文件正文与版本历史 SHA 不一致的条目判歧义保留；快照读取/复制失败在删除前中止并按清单读回校验；续作以事务记录的原请求 conversations 为准（冲突拒绝）；清理推进持久失效代次、冻结写入锁内核对；audit 预览按名单跳过且名单不可读 fail-closed；晋升提交前重读名单并核对修订号（`memory_policy.json` 的 `learning_exclusions_revision`）。mc4 18 条探针全绿、回归 634 项、变异 6/6、门禁全过。

## AGENTS.md 第 0 期第 3 轮：单一临界区（mc3-agents0r3，2026-10-09，接着 b731b9b54）

- mc4 review31 判「5 原阻断缺口 + 1 新增回归」；本轮按任务把校验/提交收进单一临界区，不再逐个补并发窗口：迁移执行=curator 租约+人格锁内一次做完（重判定/快照续作逐文件校验/归档/一次性删除/名单/失效代次/事务记录），取消分批；线程前缀锁统一 freeze/expire；policy 锁覆盖 seed 初始化（O_EXCL 不覆盖）与晋升提交复核；`run_if_due` 计入 `skipped_audit_cursor`、名单不可读诊断（`learning_exclusions_unreadable`）进运行审计。
- 固定锁序：curator 租约 → 人格锁 → policy 锁 → 线程前缀锁。
- 8 条红测原样转绿（3101–3107）、review29 18 条保持绿；新增 4 测试（并发 update_persona 真实子进程等待、curator 运行中迁移被拒/释放后成功、freeze/expire 锁一致性静态+并发交错无死锁）；变异 6/6 rc=1 抓到（另记一项探测变体未被测试区分：3103 锁的是「提交内复核存在」而非锁内/锁外）；guards9 12 文件 194 项、persona/curator/promotion/memory 相关回归全绿；size_diff 新增 0；strict_gates 见根 TESTS。
- 未验证：真实 home 迁移演练、Gateway 端到端、全仓 pytest（留给 3a 12 片车道）。

## Compact工具引用按scope流式读取（toolrefs/E11e，2026-10-08，claude/07-c1-toolrefs，本地实现、待07复核）

- Compact、artifact、晋升证据回退、通用控制面及scope carried共用common权威流式读取器；apply两类引用单扫描，E11a原分行/错误口径不变。
- toolrefs-2按07审核意见删除Python词法校验器，逐行C解析后立即丢弃非候选，保留晋升整文件失效、路径异常与晚UTF-8优先；规范POSIX路径避免无关Path对象，无效ref异常支路不保留历史全集。最新回归与门禁见TESTS及任务RESULT。
- 8个同语料入口峰值约222万字节；独立不开tracemalloc的交错三次中位耗时均不超过真基线+10%，详见TESTS。仍全量枚举/读字节，晋升旧峰值本来低；生产RSS/Gateway、Windows及扫描期间symlink并发变更未验证，未推送、合main或部署。

## Curator 恢复/清理/接管同临界区（mc2-e11d-race，2026-10-08，本地实现、待3a复核）

- MC4-2201：原 review22 的 12 个双进程交错先红；修复不改变 mc3 的死亡/过期判据，只把 manifest 锁内重验、restore、严格 cleanup、恢复审计和 acquire 合并到 `recover_and_acquire` 同一 quota/规范文件锁边界。
- 等待者不能复用锁前 state 或 manifest；发现集合变动/取锁竞争返回 busy，下一次重新发现。真实 cleanup/恢复 I/O 异常按恢复失败分类，尚未调用模型就不记模型失败；正常模型调用仍在临界区外。
- 成功提交的清理也留在原锁内；public acquire 和独立恢复入口保留原职责，无新 schema、开关或单进程缓存权威。旧租约死亡/未知判据与 process_identity 的记录边界不扩张。
- 原 review22 20 项 + review18 22 项 + 最终新增 17 项在变异还原后联合复跑 59 passed；memory/curator 与完整 guards9 并集 122 文件、1892 passed，两组均零失败/错误/跳过。三项指定变异各真实 rc1 且备份字节/SHA 还原。门禁与未验证范围见根 TESTS 同名节及交接。
- 未验证真实 Gateway/供应商链路、线上收益、Linux/Windows 实机及全仓车道；下面 mc3 节记录前一轮返工，当前锁边界以本节和 04-structure 为准。

## 网关重启后 Curator 租约接管（mc3-e11d + e11d-fix 返工，2026-10-08，分支 `worker/mc3-e11d`，本地实现，待复核）

- 生产网关 SIGTERM 关闭时 curator 线程池 `shutdown(wait=False, cancel_futures=True)` 不释放租约，`acquire` 只认 `expires_at`，
  新网关要等约 28 分钟才能接手。修复：租约未过期但持有者进程已确定死亡（同主机、非本进程、`os.kill(pid, 0)` 抛 ProcessLookupError）时可提前接管；
  PermissionError/主机不同/信息不全一律等 `expires_at`，宁可多等不误抢。
- 接管写结构化事实 `stale_lease_reclaimed`（原 lease_id/run_id/pid/expires_at + 接管时间），不悄悄覆盖；
  新租约带持有者出生身份 `process_identity`（`daemon_metadata.build_process_identity`），旧租约无该字段按未知、旧状态文件照读且摘要稳定。
- e11d-fix 返工（mc4 review18 两阻断）：运行审计补 `stale_lease_reclaimed` 严格合同（成功/失败记录同一序列化与读回，不再把整轮改写成 `CURATOR_RUN_AUDIT_FAILED`）；
  事务恢复与领取共用 `stale_lease_reclaimable` 死亡判据——持有者已死的旧事务先恢复（`recover_incomplete` 在原锁内核对身份）再换租约（`_run_once` 顺序不变），三个崩溃切点用例转绿。
- 纯修复不加开关。mc4 补测 22 项全绿（返工前 11 红）、原测试 10 条保留、变异 3/3 被抓、120 文件回归全绿；未验证真实网关重启与真实多进程接管（模拟 pid）。详见根 `TESTS.md` 同名节。

## Curator消息/审计只读游标缓存（mc2-e11c，2026-10-08，worker/mc2-e11c，本地实现、待3a复核）

- `MessageStore.after_report/byte_offset_after` 共用有界进程缓存，稳态已追平线程零打开，增长线程校验锚行再读尾；审计跳过指纹未变的旧日分片和游标前缀。
- 新增155项新旧等价/扫描量/隔离/淘汰/换行及空白cursor边界通过；41相关文件含12文件guards9合计1090项通过，四项指定变异各rc1且字节还原。规模800线程约1.21GB消息+119.4MB审计，最终热混合批CPU3.437646→0.186814s，未变和推进后零读取。
- 无配置/schema/写入/游标推进变化，不缓存正文；冷启动和失效完整定位，仍报原缺cursor/坏行错误。详细结构见04-structure、本地验证及未验项见TESTS；真实生产收益未验证。

## 回合中途摘要前缀（midturn，2026-10-08，本地实现、待07复核）

- 应用摘要前置投影接入主发送/容量与缓存安全辅助压缩；归档不重复存应用摘要占位，仅 checkpoint/view 拥有它。
- 两回合密文/文本重放由 input[0] 分叉转为完整13项前缀延伸；193项相关回归通过，主/子/后台共用链不改提交/CAS合同。
- 无新开关；真实供应商顺序接受、渠道入口与缓存收益未验证，详见设计文档 midturn 节及 TESTS。

## 线程人格冻结（personafreeze，2026-10-08，本地实现、待07复核）

- 07跟进：缓存内容损坏按缺失在原锁内重建，只warn线程/原因；截断target凭结构诊断只报新增，完整target删除照旧。两文件101 passed。
- 普通请求与手动/自动Compact的准备明确绑定已提交thread，同源复用唯一安全人格快照；期间行变化进入动态尾巴。
- 压缩准备红测复现重新现读人格导致稳定前缀变化；补齐接线后五文件87 passed，见TESTS。
- 不改晋升、摘要/CAS或历史；配置关闭/无线程保持旧行为，真实渠道和供应商缓存收益未验证。

## 容器估算缓存的指纹内存修复（estcache2，2026-10-05，worker/estcache，本地验证完成，待复审）

- estcache 车道回归（tracemalloc 峰值超半）修复：指纹从嵌套 tuple/frozenset 改为单个 64 位整数摘要（边遍历边折入、不保留嵌套结构），缓存值打包成单 int；1200 条 2KB 消息留存 1.61MB → 173KB（< 整段 JSON 10%）。
- 对拍 3017 + 53 形状 0 不符；43 文件（estimate_tokens 35 ∪ tracemalloc 9）约 3700 条全过；guards9 192 passed；性能 35 万档 131.8→65.7ms、85 万档 348.1→126.4ms。详见根 `TESTS.md` 的 estcache2 节。

## 容器估算合成缓存（estcache，2026-10-05，worker/estcache，本地实现，待复审）

- 模型回合里同一批消息被反复估算（payload 与 messages 各一遍、压缩试算逐次重估）；给 `memory_archive/tokens.py` 的容器估算加"内容指纹 → 直接子项长度"进程内 LRU 缓存与序列/映射逐位合成，估算数值与逐段编码逐位一致（3017 个形状对比 0 不符）。
- 合成只在能证明"逐元素独立编码与整段编码一致"时使用；非 str 键、未知对象、超大元素（未过 512 KiB 有界证明）、环、编码异常一律放弃合成、回退原口径；缓存不落盘、不跨进程、不因状态改变数值。
- 实测（合成历史、假后端、3 次模型调用）：35 万 token 回合估算 583→132ms（-77%）、整轮 1068→698ms；85 万 token 回合 1244→348ms（-72%）、整轮 1722→1061ms。数字只作交接参考，不进仓库。
- 详见本模块 `04-structure.md`"流式估算与消息扫描"节与根目录 `TESTS.md` 的 estcache 节。

## 缓存诊断组合反例返工（cachediag3，2026-10-05，本地定向验证完成，WIP：完整守卫缺文件；待非作者复审与 3a 终审）

- 累计与已计数身份同快照保存、整体恢复；旧内存重发和交错重发不再重复累计，不新增费用账本。
- 长度变化缺可比末点时明确不可比，而非猜测纯追加/截短；已证明的改写和组件变化保留。
- 九文件 273 passed、原十一守卫 187 passed、三变异 KILLED、新增尺寸告警 0；外部清单第十二项不在本树，完整清单未通过，不能写全门禁完成。
- 详细合同见本模块 `04-structure.md` 与 `docs/design/LONG_RUNNING_EXECUTION.md`；实际验证记录见 `TESTS.md` 的 cachediag3 节。

## 回合中归档摘要复用主请求前缀（compactcache，3a，2026-10-05）

- 起因：生产 48 小时压缩类辅助调用命中 0%（DeepSeek 4290 万输入、GPT 约 3300 万，cache_read 已上报且为 0）。离线复现：工具循环 PTL 摘要命中 92%，恢复链的回合中归档摘要命中 4/61234——cachecompact2 让它发空工具 + `none`、只带上一代摘要，恢复宿主还在摘要前卸掉了主请求历史。
- 改法：恢复宿主卸历史前冻结 `ActiveTurnCacheFork`（主请求提示、之前历史、当前回合 IR、同一投影的工具与 system 指令），只给本次摘要用一次；摘要在“来源全在主请求 IR、调用编号不重复”时按主请求前缀发送（截到最后一个被替代调用的结果），否则退回原独立摘要。被替代范围、候选计量与提交都不变。
- 第二个根因：缓存安全单次摘要原受窗口 80% 预算限制，触发点 90% 时永远超限改分段；新增 `compact_cache_surface_budget`（窗口减输出预留）只给带主请求工具的单次请求。机械兜底改读 `fallback_history`（被替代来源），避免前缀被抄进兜底摘要。
- 下面 cachecompact2 节里“active-turn 不承诺前缀命中”的口径由本节取代（结构化条件不满足时仍走那条回退）。验证见 `TESTS.md` 同名节。

## cachecompact/cachecompact2 压缩缓存前缀（2026-10-05，worker/cache-compact；本地实现，待终审）

- 可容纳的单次压缩请求携带与主请求相同的 system/tools/native messages，并将 `tool_choice` 设为 `auto`；压缩目的按 thread 读取相同 thinking/reasoning_effort。结构化工具调用不会执行，只触发一次无工具/`none` 重试。
- Live 原生历史摘要保留可共享前缀并在末尾加摘要指令；carried 文本协议和超窗分段缺少完整主历史/tools或只覆盖局部区间，明确不承诺同样缓存命中。详细设计和实测边界见 DESIGN_LEDGER 与 TESTS.md 的 cachecompact 节。
- Gateway 前台路径现由真实 ask 和 typed overflow 触发原 Compact 恢复链，测试核对辅助请求使用 `conversation_compact_summary`、准确 thread ID，且落在主请求的同一分区和历史前缀。
- active-turn replacement 只合并上一代摘要和本轮选中工具 IR，不包含完整主线程历史；因此不承诺前缀命中，显式传空工具列表与 `ToolChoice.none`，避免工具选择和重试。通用 bounded 路径仅在工具列表非空时覆盖为 `auto`。
- 本地 Compact/cache-prefix 指定回归集与 guards9 已跑过；真实 DeepSeek 服务端缓存、计费与收益仍未验证，详见根目录 `TESTS.md`。

## 私有写整包 9b 终审修复（pbfix，2026-10-04）

- 删掉 `memory_archive/tokens.py` 的普通 mkdir（目录交给私有写的 `ensure_private_dir`）；`memory_store/retention_apply._trash_conversation` 3 处改走 `ensure_private_dir`。
- 记忆侧建目录点（candidates / lessons / daily / jsonl / curator_commit / runtime_fact_source / task_workspace / agent_run_workspace）统一改 `nofollow_fs.ensure_private_dir`：缺失段**逐级** 0700（原 `mkdir(parents=True, mode=0o700)` 只保最后一级）、已存在一律不动。
- 详见 `02-progress.md` 同名节与 `TESTS.md`。

## 私有写只动自己建的东西（pdp，2026-10-03，分支 `worker/private-dirs-policy`，基于集成头 `3a42f457d`，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- `common/json_io._ensure_private_dir` 改口径（3a 裁定，与锁收私 ds8 同口径）：缺失目录按 0700 新建（沿用 no-follow 原语逐段创建、不跟随符号链接），**已存在的目录一律不改权限**；不再对已存在目录 chmod。文件本身仍出生 0600、存量宽权限下次写入收紧。
- 记忆侧建目录点跟着改成 0700 建：`memory_store/candidates`（候选仓库）、`memory_store/lessons`（lesson 目录 + routing 目录 + HOT 目录）、`memory_store/curator_commit`（事务目录与每个 run 的事务子目录）、`memory_store/daily`（daily 分片目录，原为普通 mkdir 靠首次写入收紧）。
- 可配置路径边界：目录被指到已存在的用户目录时，写入只新增 0600 文件、目录与兄弟文件一位不动；存量宿主目录的收紧归 owner 维护负责，不在每次写入时做。
- 用例与变异见 [TESTS](../../../TESTS.md) 的 pdp 节。

## 记忆操作审计账本私有写入（pw2，2026-10-03，分支 `worker/private-writes-batch2`，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- `memory_store/operations` 的 `ops.jsonl`（无正文的形式变更审计与硬删除墓碑）从 `append_jsonl_capped` 改走 `append_private_jsonl_capped`：目录 0700、新文件 0600、已有宽权限文件下次写入收紧；有界保留语义与写入格式逐字节不变。
- 用例与变异见 [TESTS](../../../TESTS.md) 的 pw2 节。

## daily 目录权限复核：本来就已是 0700（ds3l，2026-10-03，分支 `worker/ds3-lock-private`）

- be 2026-10-03 只读评估提到 `memory/daily/` 目录是 0755；复核**不成立**：daily 分片走 `common/json_io.write_private_text_file_atomic_unlocked`，其 `_ensure_private_dir` 会把目录收紧到 0700。已在干净基线 `b6ede99e0` 上构造 `DailyMemoryStore` 并 append 后实测目录为 **0o700**。
- 故本次**不改** daily，也没有对应用例。真正 0755 的是 daily 的 `.lock` 旁目录，已由同批锁权限统一覆盖（见 DESIGN_LEDGER 同名条目）。
- 同批把 `global_index/*.jsonl` 的数据文件与目录收私，只收紧不放松、不做全盘扫描。

## 嵌入用量与召回方式的进程内计数（S7，2026-10-02，分支 `claude/be-embedding-usage-facts`，已实现，待集成）

- 嵌入客户端按用途（写入、召回、重建、工具检索）计请求、条数、失败和供应商回报的 token；scoped 检索计 semantic/keyword/none。不落盘，不进 model call ledger。详见 DESIGN_LEDGER 同名条目。
- 2026-10-03 embo-01 真实对账后补：不带作用域的 `search`（Gateway 与 IM 的 `/memory`、`agent.recall`）也每次记一次召回方式（分支 `claude/be-s7-recall-mode`），原先只计嵌入、不计方式，用户看到“召回 2 次、召回方式 0 次”。
- 2026-10-03 的 9b 两条后续已在 `worker/luna1-s7-followups` 实现，提交 `c8da199c8b35e9d7162797c3bf6da567b68c9960`：空库不带作用域检索记 `none` 且不发查询嵌入；MiniMax 查询用 `type=query`，文档写入/重建用 `type=db`。查询类型不进入 `embedding_identity`，已有向量身份保持。本地桩服务和变异结果见 TESTS.md。

## 决策结果日志七天留存（2026-10-03，luna6，本地已实现，待 3a 复审）

- `conversation/decision_outcome_log` 写入时依 `created_at` 保留最近 7 天；20000 行只作防失控硬上限，超限按时间丢最旧。
- 沿原 JSONL 路径锁和原子替换；公共 `append_jsonl_capped` 不变，写失败仍只记日志、不影响决策。
- Audit 汇总与近期 20 行展示保持原样；`decision_reach_counts` 原有 7 天留存不变。验证见 [TESTS](../../../TESTS.md) 同名节。

## 记忆归档目录收紧权限：memory_archive 写入私有化 + owner 维护收紧已有文件（2026-10-03，分支 `worker/luna3-archive-private`，已实现，待 3a 复审）

- S2 的后续项"`memory_archive/` 那批会话与任务数据的收紧"落地：14 个文件的写入点改走 `common/json_io` 私有原子写（文件 0600、目录 0700）；`storage.tighten_memory_archive_permissions` 递归收紧已有文件（只收紧不放松、不跟随符号链接），挂进 owner 维护（Gateway 每天一次），回执写 `O/data/maintenance.json` 的 `memory_archive_permissions`。详见 DESIGN_LEDGER 同名条目。

## 记忆整理补跑后续：默认补跑推进过的组，下一批会话模型只试 1 次（2026-10-02，分支 `claude/75-curator-fallback-threshold`，已实现，待 be 审）

- 本组最近一次推进游标的是 `:transient` 默认补跑时，下一批阈值从 2 降到 1（`CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT`）；会话模型自己成功推进后回到 2。只读运行账，警告码由 `curator_routing.transient_fallback_warning` 统一生成。详见 DESIGN_LEDGER 同名条目。

## 记忆整理：会话自己的模型连续连不上时让给 owner 默认模型（2026-10-02，分支 `claude/be-curator-transient-fallback`，已实现，待集成）

- 非默认组在同一输入上连续 2 次连接类失败（`CURATOR_MODEL_FAILED`、`CURATOR_MODEL_TIMEOUT`）后，下一次运行直接用 owner 默认模型，记 `curator_thread_model_failed:<码>:transient`；默认模型自己的失败照旧退避或熔断，不按组隔离熔断。详见 DESIGN_LEDGER 同名条目。

## 记忆整理：同一条消息里同主题、不同内容各存一条（用户拍板第 6 条，2026-10-02，分支 `claude/be-curator-content-identity`，已实现，待集成）

- Curator 观察身份加规范化内容指纹：“我对花生过敏，也对芒果过敏”不再塌成一条；规范化后内容相同的照旧合并、不增加出现次数。
- 已知代价：同一来源同一主题换了说法会各存一条（不虚增出现次数，重复候选走正常晋升审核）。详见 DESIGN_LEDGER 同名条目。

## 记忆整理跟着消息来源会话的主代理模型走（用户拍板第 2 条细化，2026-10-02，分支 `claude/be-curator-thread-model`，已实现，待集成）

- `memory_curator_model_profile` 留空时，一批里不同会话、不同模型的消息按会话模型分组，每组一次完整运行；没选或不可用的会话用 owner 默认，并带结构化原因。
- 会话模型输出坏 JSON 时，本次用 owner 默认补跑一次；熔断仍是 owner 级，但只数同一组的失败。指定了档案仍固定用它；某个 owner 在自己的目录（含管理员共享）里解析不到指定档案时，改用该 owner 默认模型并记 `curator_profile_unavailable_fallback:<原因>`。详见 DESIGN_LEDGER 同名条目。
- `CURATOR_SCHEMA_INVALID` 的失败诊断加 `violation_code` 和 `violation_path`（宿主短码 + 只由 schema 字段名拼成的路径，不带正文），能直接看出哪一项不合格。

## 主模型只读长期记忆检索工具 `memory_search`（J9，2026-10-02，分支 `claude/ae-j9-memory-tool`，已实现，待集成）

- 补 P5-A 缺口 2：自动召回漏掉的事实，主模型可以自己按当前 owner 和本轮范围查一次，拿到有界摘录和“这次走语义还是关键词”的事实。
- 只读、只是线索：不写/删/改记忆，不写访问信号，结果不进自动召回；子代理回合与记忆总闸关闭时不可用。
- 开关 `enable_memory_search_tool` 默认关，属于安全边界（模型不能自己打开，用户经 `/settings` 可改）。详见 DESIGN_LEDGER 同名条目。

## 记忆侧四个决策点（recall / pre_recall / curator / curator_relation）登记被丢弃的建议（2026-10-02，分支 `worker/ds1-decision-outcome-category`，已实现，待集成）

- 决策结果日志新增结构化 `result_category`（`selected` / `non_selection:<取值>` / `dropped:<原因码>` / `no_selection_recorded`）。
- 四个记忆点位的采纳前复核出口在丢掉一条已拿到的建议时调 `record_decision_dropped` 登记原因码：期限 `adoption_deadline`、
  运行时身份变化 `runtime_changed`、来源或正式条目变化 `sources_changed`。行为不变（仍保留原材料/原顺序），只多一行补充记录。
- 返工（2026-10-02）：`decision_recall._pre_recall_stale` 的两处复核都比对主模型身份（`_model_identity` 与材料里的 `primary_model`）；
  第二次复核因此比原口径多一条主模型检查，是有意收紧——采用前换过主模型就不按旧模型建议注入。`_StaleStage` 挪到全部 import 之后。
- 详见 DESIGN_LEDGER 同名条目与[决策审计与管控](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#每个点位最近是选中非选择还是被丢弃2026-10-02分支-workerds1-decision-outcome-category)。

## 召回后排序逐条题的候选措辞修正（J12b，2026-10-02，分支 `claude/be-recall-criteria`，已上线 step17a，main de222698b，2026-10-02）

- `decision_recall` 逐条记忆题的候选说明改为写明“这一条”的含义，无关记忆明确指向 `later`；候选键、题目结构和非排序回答的处理不变。
- 质量基准里 recall 从 12/30 升到 30/30，成绩已登记；点位仍默认关闭。详见 DESIGN_LEDGER 同名条目。

## Curator 整批提交不再被单条候选或长警告卡死，同一批反复失败会熔断（J13 根因修复，2026-10-02，分支 `claude/be-curator-identity`，已上线 step17a，main de222698b，2026-10-02）

- 同身份比对和身份计算用同一套规范化：主题键按 `fold_key`，范围按规范键。
- 与已有观察身份冲突的单条候选在提交前剔除，记 `curator_candidate_identity_conflict_dropped:<条数>`；共用合并函数保持严格，冲突改抛 ValueError 子类 `CandidateIdentityConflictError`。
- 模型警告写运行账前先截到 300 字、留一个位置给宿主警告。
- 同一起始游标连续 3 次确定性失败（提交被拒或输出解析失败）即熔断：state 记 `CURATOR_REPLAY_BREAKER_OPEN`，退避一小时，与发现层同源；运行账保留真实失败码。
- 同消息同主题不同内容会被合并（“花生/芒果”例子）：用户 10-02 拍板第 6 条按内容区分，已实施，见本文件顶部同名条目。

## P14 必须修：向量空间身份、同代快照、重建如实计数、实际维度、管理员入口（2026-10-02，分支 `claude/38-p14-embedding-fixes`，基于 `claude/3a-step16z` `f8ae11fe5`，已实现，待集成）

- 来源：sol 只读审查 P14 的 6 个必须修问题，外加“建议修”里的原因码登记、重建文案、状态未知不报 0、回归测试。
- 身份改从实际发请求的嵌入客户端算：档案编号 + 线路协议 + 端点摘要（去掉账号口令后取 sha256）+ 模型名，和建客户端共用一次档案解析。
  同一档案换端点或换协议就是另一个空间；密钥、请求头、端点明文都不进快照。
- 接线：`core._memory_semantic_channel` 在身份建不起来时关掉语义通道，诊断记 `MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE`。
  从此不会出现“有客户端、身份为 None”而跳过检查的情况。
- 向量库改成单文件快照 `my-agent.memory-vectors.v2`，内含身份、实际维度、代次、写入时间和全部条目。
  - 写入：在 `locked_json_path` 里先重读、再裁决、最后整份原子替换。
  - 读取：按文件指纹发现换代就重载；检索在同一份快照上再裁决一次身份和维度。
  - 原来单独写的 `memory_vectors.json.meta.json` 不再读也不再写。
- 重建全有或全无：逐条嵌入，首个失败即停。
  - 失败：返回 `ok=False` + `VECTOR_REBUILD_INCOMPLETE`，附 attempted/embedded/failed/failed_stage/failure，旧库不动。
  - 全部成功才调 `replace_all` 整体替换。
- 维度以实际向量为准：同一批向量必须等长；和快照维度不同时，读写都明确报 `VECTOR_DIMENSION_MISMATCH`。
- `my-agent memory vectors rebuild --confirmed` 先过统一管理员判定 `owner_access.is_complete_local_admin_owner`（与 /settings 同一条）。被拒时不调嵌入、不碰向量文件。
- 状态预览：没有嵌入客户端时也独立读文件，报能确认的数量；读不了报 None，不用 0 冒充。
- 详见 DESIGN_LEDGER 与 TESTS.md 同名节。

## 补充查询片段材料：先预检每个片段能新增的事实（J8，2026-10-02，分支 `claude/be-jev-snippet-facts`，已上线 step17a，main de222698b，2026-10-02）

- 新增 `points.pre_recall.fragment_material`，配置 `memory_decision_pre_recall_fragment_material` 默认 `query_text`，与原做法逐字节相同。
- `with_new_facts` 时：
  - 宿主在问 Jev 前，对每个片段做一次同规则的候选检索：原 scope，去掉基线已有的，按名额和字数截取，不记访问。
  - 只给能补出新事实的片段，请求里带每个片段的新增条数与摘要；全都补不出就不调 Jev（到达原因 `no_new_facts`）。
  - 采用的正是 Jev 看到的那份，最终仍经正式源确认。
- 代价：语义召回下每个片段多一次查询嵌入，最多 4 次。
- 真实 Jev 对照：候选检索写死成模拟语义召回的结果，因为嵌入模型不在授权名单。被覆盖的片段不再给选，没有可补时不调 Jev；选择准确率的提升没有被证明，语义召回下的端到端效果未验证。
- 详见 [召回前审计](../../tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md) 与 TESTS.md 同名节。

## P13+P14：嵌入改引用模型档案、向量库记录生成模型（2026-10-02，ds1，已上线 step17a，main de222698b，2026-10-02；P14 的旁路 meta 方案已被上节取代）

- P13：`embedding_model_profile` 取代 `embedding_model/embedding_api_base/embedding_api_key/embedding_api_key_env` 四个平铺键
  （旧键仅告警，不留别名）。`core._embedding_client` 按档案的服务商凭据与端点构建，空档案 = 不建客户端、只走关键词；
  档案失效按结构化 reason 降级，不回退聊天模型。该键是边界项，模型不能改。
- P14：向量库旁写 `memory_vectors.json.meta.json`（档案编号/服务商/模型名/维度/写入时间）；读取时身份不一致或无元数据，
  已有向量视为不存在、语义召回退回关键词并给原因码（VECTOR_META_MISSING / VECTOR_IDENTITY_MISMATCH），不静默混用两个向量空间；
  写侧身份不一致拒绝混写。
- 重建入口：`memory vectors status`（只读预览数量与身份）+ `memory vectors rebuild --confirmed`（先预览、显式确认才清库重嵌）。
- 相关测试 26 条（档案解析/空值/身份一致/不一致/重建）+ 3 个变异全部被拦截；详见 TESTS.md。

常数整改第三批（P10，分支 `worker/ds2-p10-batch3`，2026-10-02）：agent/memory_store 23 个待整改常数合规——数量上限类补
`_COUNT` 后缀改名（如 `_MAX_LIST_ITEMS→_MAX_LIST_ITEMS_COUNT`、`_MAX_ANNOTATED_ITEMS→_MAX_ANNOTATED_ITEMS_COUNT`、
`_MAX_RELATION_PAIRS→_MAX_RELATION_PAIRS_COUNT`、`_EN_KEYWORD_MAX→_EN_KEYWORD_MAX_COUNT`、`_TIMEOUT_SHRINK_LIMIT→_TIMEOUT_SHRINK_LIMIT_COUNT`、
`CURATOR_MAX_RETRIES→CURATOR_MAX_RETRY_COUNT`），`_EN_KEYWORD_MIN_LEN→_EN_KEYWORD_MIN_LEN_CHARS`、`_TIMEOUT_SHRINK_FLOOR→_TIMEOUT_SHRINK_FLOOR_COUNT`；
字符/字节/秒类补中文说明；BM25 参数 `_BM25_B/_BM25_K1` 无物理单位，只补说明并挪入白名单无单位组。数值一律不变。

2026-10-02（分支 `worker/ds1-p10-batch4`）：常数整改第四批。`cli/memory_archive_commands.py` 与 `cli/memory_commands/` 的常数改名/补说明（如 `_CLI_MEMORY_ARCHIVE_LIMIT`→`_CLI_MEMORY_ARCHIVE_COUNT`、`MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_LIMIT`→`MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT`），数值不变。

## P12：固定提炼模型档案（2026-10-01，sol，已上线 step17a，main de222698b，2026-10-02）

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

Gateway 用户的记忆总闸按各自 home 生效（分支 `claude/9b-owner-path-scope`，2026-09-27）：`owner_resolver.home_paths_with_owner` 原先没有按用户重设 `owner_memory_policy_json`，Gateway 里其它用户经 `owner_policy` 读到的是本机主用户的 `memory_policy.json`，与只按 owner home 读的 `owner_wake_discovery` 不一致；现已按用户重设，新用户按自己 home 里的默认种子（开启）生效。`test_gateway_per_user_scoping.py` 按字段全集守卫所有 owner_* 路径都在该用户 home 内。同分支：召回重排的普通记忆下限改读 `conversation/decision_point_limits.RECALL_MEMORIES_MIN_COUNT`，与诊断大白话共用一处。
参数减量第 2 批（分支 `claude/9a-merge-config`，2026-09-27）：记忆配置三组各只留一个旋钮。`memory_archive_level` 同时决定运行归档与子代理收尾快照的级别（原 `memory_hook_archive_level` 删除，子代理快照改读它）；`memory_rule_routing_mode` 的 `off` 是规则路由唯一开关（原 `memory_rule_routing_enabled` 删除；决策点召回在 `off` 下直接关闭路由，不再报“off 不是合法模式”的 finding）；`memory_resume_auto_context_mode` 默认改为 `off`（等于原默认 enabled=false），单次 `--resume-context` 仍强制 always。旧键写在用户配置里只告警并忽略；回归见 `test_merged_config_knobs.py`。

参数减量第 1 批（分支 `claude/38-delete-dead-config`，2026-09-27）：`memory_hook_retention_days`、`memory_rule_receipt_enabled` 只被 memory doctor 回显，没有清理器或回执逻辑读取，
已从 `MemorySettings`、`AgentConfig`、随包 YAML 和两处 doctor 字段名单删除；`memory_archive.enforce_retention` 仍保留为显式传参的函数，无产品调用方。
同批删除的 `memory_query_default_limit/page_size` 只有归一化器认识。

决策点未触发原因计数（分支 `claude/9b-decision-miss-reasons`，2026-09-27）：记忆侧四个决策点（recall、pre_recall、curator、curator_relation）每次到达都经 `conversation/decision_reach_counts` 记一次结果：没调用记宿主原因码（如 `memory_count`、`no_free_slots`、`nothing_to_label`、`memory_changed`、`point_off`、材料不合格的 `bad_material`），真正调用前记 `called`。只在进程内累加、按 owner 节流合并写盘，召回与整理的结果、警告码和游标都不变。见[决策审计与管控](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#每个点位最近为什么没触发2026-09-27)。

参数中心同名常数收敛（分支 `claude/param-center-dup-constants`，2026-09-27）：记忆诊断两处没有读取方的 `RECENT_ARCHIVE_FILE_LIMIT`
删除（归档文件条数当时改为只读配置 `memory_doctor_recent_archive_file_limit`，参数减量后改为代码常数；e905 统一诊断后当前唯一来源是 `agent/user_space/home_doctor.py::MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT`）；压缩失败熔断阈值与冷却只在 `memory_archive/compact_circuit_breaker`
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

2026-10-07 `remember` 回执让"没记上"一眼可见（3a，分支 `claude/3a-learnpack-prodfix`，learnpack 生产测试发现：候选因缺证据没生效、回执是 `ok: true`，她对用户说"已记下来了"）：
- 回执加 `saved`：每条 promotion 结果的 `reason_code` 都是 PROMOTED 或 ALREADY_PROMOTED 才为 true；`ok` 含义不变（调用跑完了，候选也算副作用）。
- `hint` 按结构化结果改写：没记上的分支一律以"没记上（正式记忆没变）"开头并写"不要对用户说已记下"（缺证据的仍带"不能把缺证据说成等待用户确认"）；一批里有记上有没记上写"部分记上了：有 N 条没记上"；全是 ALREADY_PROMOTED 写"早就在正式记忆里了"；正式记忆更新的三种说法不变。
- 测试：`test_memory_tool.py` 断言缺证据、要人审（REVIEW_REQUIRED）、部分记上、早就记着四种回执的 `saved` 与 `hint`；6 个变异全杀（只认 pytest rc=1）。

2026-10-08 Curator 工具输出引用改批量读取（mc3-e11a，分支 `worker/mc3-e11a`，生产网关内存热修，待复核）：生产观察 curator 批内每个 run_id 各做一次全量控制面查询——每批重复 glob 3993 个索引根（1.66 秒）、整读整解析 836MB 级索引，网关内存 4.1–8.6GB、每分钟跳涨 2.5–4GB。改法：
- `memory_archive/control_plane` 新增 `query_tool_output_refs_for_runs`：一批 run 一次 glob、一次顺序扫描，逐行二进制流式读（物理 LF 切、行尾去 `\r`、严格 UTF-8，与 `jsonl_lines` 同一边界），每 run 只留前 `limit_per_run` 条匹配行；匹配复用 `_record_value`/`_matches_scope`，不复制新规则。
- 可选字节预筛只在全部 run_id 为 JSON 不转义的可见 ASCII 时启用，命中或无法预筛时整行解析；跳过行仍严格解码（非 UTF-8 行为与旧路径一致）。
- `memory_store/curator_inputs.CuratorToolReferenceSource.read` 改批量契约，整批失败时每个 run 复制同类型错误；`core._query_curator_tool_references` 批量转调。通用控制面 `query_memory_control_plane` 本次不改（对超大 owner 同样问题留待后续）。
- 合成约 200MB 索引实测：新路径 tracemalloc 峰值 5.8MB，旧路径 217.9MB；29 个相关测试文件 621 条全过；变异 4/4 被抓。根列表收窄不可由结构化事实证明，本批未收窄，方案写入交接。

2026-10-08 Curator 预筛改正则 + 收尾补测（mc3-e11a-perf，同一分支 `worker/mc3-e11a`，待复核）：
- 预筛从"每行对全部 needle 的 Python 生成器步进"改为每批只编译一次的字节正则（`re.compile(b"|".join(re.escape(n) ...))`，每行一次 C 层 `search`）；字面量交替无回溯风险；空 run_id 不预筛、跳过语义（预筛不中或纯空白）不变。（注：安全条件与预筛机制在 mc3-e11a-fix 里修订——mc4-1301 兜底 + 布尔/数字排除，见下条。）
- 3a bench（1000 根、386MB、40 万行、10 run_id）同一数据两遍取小值：一批 CPU 3.31s → 0.68s、wall 3.47s → 0.73s（目标 ≤1.0s 达标），matched=572 不变。
- 补 CRLF/混合行尾等价用例与正则元字符用例（`.` `+` `(` `[` `*` `?` `|` 精确匹配、与相似 run_id 互不误收）；29 个相关测试文件 629 条全过；变异 6/6 被抓（新增：去掉 `re.escape` → 正则元字符用例 5/7 参数红）。

2026-10-08 Curator 预筛修订（mc3-e11a-fix，mc4 review13 结论"修改后部署"，同一分支 `worker/mc3-e11a`，待复核）：
- MC4-1301（阻断）修复：预筛可证明不漏——安全 run_id 的合法 JSON 写法只有原样/含 `\u00XX`/含 `\/` 三种；预筛 = 字面量正则 + `\u00`/`\/` 转义兜底（独立编译、先按反斜杠闸门）；布尔/数字样式 run_id 排除（`str()` 投影）。
- 性能（与 50b87f934 同场两遍取小）：任务书建议的单正则（字面量|\u|\/）实测 home 2.75s / home_ascii 2.89s，不达标；两段式后 home 0.51 → 0.52s、home_ascii 0.55 → 1.41s；matched=572 不变。
- MC4-1302（3a 接受）：错误合同有意变化——工具引用读取只依赖工具输出索引，无关账本损坏不再连带失败；注释与测试已写明；mc4 两条旧合同红测保持红。
- 测试：新文件 38 条 + v2 56 条全过；29 文件 639 条全过；变异 3/3（去 `\u00`、去 `\/`、去布尔/数字排除，均 rc=1）。

## 2026-10-16 F3 Curator 原请求短事务（组件）

Curator `run/run_if_due` 先沿当前实际 Agent/原 claim 做空短准入，长模型等待继续在 T 门外。StateStore 的公开变更、批次 commit/recover 与失败/隔离/迁移/排除标记沿 common `gateway_short_transaction`，先原 T/P3/P5 再 quota/业务锁；内部 state 与事务材料/规范目标 writer 只核当前真实同线程窗口，不在锁内倒取 T。原 candidate、Daily、run audit、state 仍是唯一批次，state 最后提交；分组、pending 代次、cursor、配额/CAS 和 F4 用户正式确认不变。

租约 renew/retire/release 同样先 T 后 state。原心跳线程用固定来源已验证的 copy_context 运输同一原 source；复制不是新权，每次续租仍核原 execution/lifetime/PID 并进入 fresh T。原 scope 退出使 cell 共同失效，复制活事务窗口到其它线程不能写。common/Bounded 未结对象优先穿过失败处理，不追加普通失败审计、清 lease 或转默认模型。

启动原心跳的 start 也纳入既有 try/finally：ACK 异常保同一 Thread 做 stop/join，确认退出后才能按普通启动失败处理。停止或 join 未确认仍把 `lease_heartbeat_stop_error_class` 写入原异常原生字典，保留 executing lease 和原异常；此字段仅是停止诊断，没有伪造 BoundedCall 身份，也不代表业务未进入。原 Thread 仍经异常 traceback 的 heartbeat 局部对象可达，不是持久 receipt 句柄。该普通 marker 如何经最终真实请求出口保留 P3，仍须平台组合片核验；本组件不声称物理排空或完整多进程可用。

独立准确 X 加 30 个冻结前置：原 BG 六节点 4 绿 2 红变为 6 绿；新增真实续租/撤权/退出 cell/跨线程 writer 五例原全红变绿；ACK 后真实出生与正常/未知 join 两例原红变绿。原外层 186 例保留，最终 16 文件 221 唯一普通通过、零失败/错误/skip。仅假供应商与受控 Python 线程；未运行实际子进程 recovery_race、临时 HTTP、native/Count2、真实模型或服务。

## 2026-10-16 F3 owner 维护原请求边界（候选）

原维护/retention、archive权限收紧、rcb补收口逐个实际写消费同一短回调，T先于业务锁，原RuntimePolicy及完整maintenance状态CAS变化后拒写；长扫描不持T，既有部分效果不重放。独立182普通通过，状态仍唯一owner-maintenance.v1；不替代正式后台来源、Count2或物理排空验收。详见STORAGE_RETENTION与TESTS顶部当前记录。

## 2026-10-17 C正式记忆recent同一只读叶子

`read_memory_records_from_path`不构造会mkdir的JsonlMemory，只共用原唯一事件scanner与版本/删除/过期物化。原`all→self._read_memory_file→self._read_memory_events`虚方法链和坏行容错保持；C调用方另核认证、owner、runtime memory_path及原控制面读前后来源。只读recent不读候选、写访问/向量缓存或触发晋升，同群仍为原群owner共享；search/remember未借此开放。详见[客户端记忆只读](../../design/GATEWAY_CLIENT_MEMORY_READONLY.md)。

## 2026-10-23 有界反向尾行读取器与归档路径共享 helper（Context Optimization）

新增 `memory_archive/tail_lines.py` 从文件尾部按 64KB 块反向读取尾行，供 `agent_run_workspace.py` 与 `task_workspace/payloads.py` 复用并统一 `_safe_relative_path`，避免长日志整文件读入内存；`tool_output_externalizer.py` 精简外置归档提示。
`memory_archive/artifact/reader.py` 的 `_find_index_record` 先按原始引用匹配 `call_id`（支持 `.blob` 后缀 `call_id`），未命中再回退匹配 `.json` sidecar 基名；`artifacts/registry_projection_cache.py` 空投影共享不可变 `_EMPTY_PROXY` 并计入多根 `identity` 元组与容器内存开销。
