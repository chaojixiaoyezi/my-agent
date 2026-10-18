# J2 向量、正文缓存与 lesson/HOT 本地写叶

状态：2026-10-17 独立实施候选，未接 builder/JSONL/bridge/HTTP，尚待非作者评审。输入为冻结 J1 65 全树；除原三文件，另经根批准只补 promotion._commit_target_blocked 的准确 ValueError catch，不能把它伪称原三文件设计已覆盖。

## 内部 API 与唯一来源

VectorStore(path, identity=None, *, coordinator_io=False)、TextVectorCache(path, *, coordinator_io=False)、LessonRepository(lessons_dir, routing_index_path, *, coordinator_io=False)、HotRuleRepository(path, *, coordinator_io=False)。默认 False 保原调用形状、身份、算法和副作用顺序；这是受信组合根的内部 IO 策略，未增加用户配置、授权参数或业务入口。

对象只保存该策略 bool，原路径、RLock、缓存继续属于实际原领域对象，不保存 source/callback。每次真实 borrow 由组合根安装已有 global_write_commit_scope(source.perform)，下层只执行 execute_global_write(require_scope=True)。准确原锁 FD 出生使用 60 的 birth_commit，flock 等待/校验/关闭留在门外；固定原 path 的已编码 raw 写入使用 replace_once=True，保原原子替换权限/顺序。没有新 token、ContextVar、keeper、全局 registry、状态账或缓存替身。

## 原算法与写口

| 原模块/函数 | 门外仍做的事 | 固定门内写叶 |
| --- | --- | --- |
| VectorStore.upsert_many/replace_all/remove、_write_snapshot | 原 RLock/file lock、reload、identity/dim、行过滤/合并、header/代次、JSON编码 | 原 FD 出生与唯一快照 raw writer；成功后原 _header/_items/_readable/_stamp 发布 |
| TextVectorCache._flush | 原 RLock/file lock、两次 keep、完整 load_for_write、增量合并、去重/no-change、JSON编码 | 原 FD 出生、原目录 mkdir、唯一 cache raw writer |
| TextVectorCache.retain_matching、_persist_reclaimed | 原 keep 判据、盘上完整快照、跨实例删除、JSON编码 | 原 FD/目录出生与原回收 raw writer |
| LessonRepository.__init__/promote_with_receipt/rebuild_routing_index | 原候选门槛/证据、幂等读取、collision、list/render | 两原目录出生、目标原 FD、已渲染 lesson/routing raw writer |
| HotRuleRepository.__init__/promote_with_receipt/demote_cold_rules、_demote_hot_excess_unlocked | 原验证/幂等、读取、预算/排序/逐条冷规则让位、render | 原目录/FD、首次 HOT raw writer 与原第二 demote raw writer 分别末核 |

完整原 JSON 快照仍可能很大，实际原子文件写入是随 bytes 增长的本地不可抢占 IO，不声称常数耗时；扫描、编码、锁等待及业务循环不因末门被包入 Root。没有禁 embedding、改 usage/rate、跳 keep、禁 FTS 或新增业务 deadline。

## 缓存、错误和部分提交

text cache 仍保原 _items/read_stamp/last_read_error/last_write_error 算法，put/remove 和 retain 的原内存乐观增量不是耐久成功。来源拒绝先保 last_write_error 原对象，再同对象抛出，不能只记录后返回；原 _read_stamp=None 使后续 get 按原盘代重核。保留同一个真实实例，不用每次重建规避失败，不造第二 pending 账。普通 OSError 仍原 _write_atomic_unlocked False/回收0 与诊断，不把普通 RuntimeError 或相同 error_code 文案升级为 native。

实际 C scope 可达的 canonical 原类是 GlobalWriteCommitUnavailable、GlobalWriteInWorkerError（包括 Executor 子类）、common.owner_write_errors 的 LocalControlCoordinatorUnavailable、OwnerManagementIngressUnavailable。text _flush/retain_matching 与 lesson routing 的旧宽 catch 仅在 C 对这些原类 bare raise；没有反向 Gateway import、码名分类或全表复制。

lesson 正文成功后 routing 来源拒绝保正文事实并原对象抛出；普通 routing IO 仍原 LessonRoutingPendingError。HOT 首次写后第二 demote 拒绝保第一次实际文件，不能报整段原子成功或跳原预算治理，也不换源自动重试。

Astra72发现的额外上层点：promotion._commit_target_blocked 原 except ValueError 会把 OwnerManagementIngressUnavailable 包成 PROMOTION_THRESHOLD_NOT_MET。本片经显式授权只在此函数按 service.coordinator_io 先 bare raise 该实际 ValueError 子类，普通门槛错误与默认 False 保持；其它 promotion 函数 AST 不改，不扩大 persona。

## 验证与后继

新测试以真实领域对象、原 global scope、临时文件为准，fake 仅外层同步门；逐目录/FD/raw 撤权，完整快照发布、跨实例删键不复活、no-change 不新增 raw writer、keep/编码/预算门外、lesson/HOT partial、真实 service→目标→上层 catch、缺源/过期源与普通 IO 诊断均验证。原向量身份/快照一致性/正文cache/HOT/权限/promotion 回归复用；默认规范化 AST 与精确原函数/文件 SHA 单列。早期两个测试属性名错误保留日志，仅改测试正确字段。

后继由 builder/JSONL/bridge 唯一作者串行透传四资源 coordinator_io=True（含无 embedder 删除 fallback 的 VectorStore/TextVectorCache），实际 borrow 安装原 source.perform；沿 JSONL/bridge 真实 catch 同对象穿出后才能继续下一付费阶段。根独占组合，非作者评审通过前不得合根；本片不宣称旧冷 memory 已整体可用。
