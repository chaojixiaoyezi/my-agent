# LocalStore 协调者本地 IO（C3-L 候选，未接入口）

本片为冷记忆资源补原 LocalStore 最深本地写者，生产仅 store/schema/records/maintenance/events、common global_write_commit/json_io 七文件。它没有接入 cold client search/remember 或候选管理，不能称 F3 ready。

## 来源与实例寿命

`LocalStore(..., coordinator_io=False)` 默认原行为。内部 C 实例仅保存更严格 IO 策略，不保存 source、callback、原 HTTP work、权限、事务连接或新的上下文。每次真实 owner borrow 必须在原 `global_write_commit_scope(source.perform)` 中使用同一资源；`execute_global_write(..., require_scope=True)` 缺源抛原 GlobalWriteCommitUnavailable，不借工具来源或默认直接写。scope退出/跨线程仍沿原核验。旧 W/Count0 LocalStore 不无条件进入 global guard。

C1 factory透传与 C2 holder/source 为后继接线，本片不改它们。业务身份、正式 JSONL/Candidate 权威、quota/policy/formal 锁以及 embedding/费用均不在 LocalStore 中另造。

## 固定本地末写

C目录与连接出生、原WAL/foreign_keys、完整原schema guard/迁移/stamp、正文replace、完整SQL事务、unlink/checkpoint逐固定叶消费原scope。C使用独立短连接 timeout0/busy_timeout0，不借 F1 scope、旧 batch 或长期连接；C batch仅保上下文形状，各操作独立。默认仍原30秒与原缓存行为。

正文仍 upsert先写body再SQL；delete先擦body，再原secure_delete/FTS/records/event/commit，后unlink/checkpoint。SQL失败不新增正文回滚，原来可能存在的body/事件partial如实保留。原事件只有一套构造：SQL INSERT→同 events_path JSONL→DB commit。events原锁在Root外先持有，事务内调用原编码/权限的unlocked append，禁止重复自取锁。

common lock birth_commit只包原FD open/create/chmod，每次原最多五轮重开重新核源；flock等待、身份校验及close在门外。原默认None不变。raw正文C显式replace_once=True仅限制PermissionError第一次返回；默认仍8轮重试，不编造业务deadline。

## FTS 重建与并发

Root外从同库按id读取records全列、原schema/FTS状态，并按原 `_read_content` 准备正文。记录实际正文的dev/ino/size/mtime_ns/ctime_ns及实际读取结果摘要，缺失/断链沿原preview fallback，有明确事实。快照只活于本次栈，非新cache/恢复表。

Root中独立短连接 BEGIN IMMEDIATE只try；任何DELETE前重新精确比较全列和值类型、原schema/FTS状态、库身份及正文代次。变化抛BlockingIOError，零DELETE/event/pendingclear；无自动无限重试。匹配才原DELETE→每行原FTS写→原fts_rebuilt事件→原pendingfalse→commit；成功才内存needs_rebuild=False。原旧库迁移、FTS不可用与pending恢复保持，不永久unsupported或禁用FTS。

完整CAS SELECT和FTS单事务随N增长，是O(N)本地SQL与元数据工作，不承诺恒定短门；Root没有全文文件读取、网络、embedding或锁等待。单次本地IO不可抢占。测试记录实际records/正文UTF8字节/门持时，不用5秒期限伪造终态。正文代次检查守原受控atomic replace边界，不声称约束任意外部恶意原地修改。

## 清理、错误和后继

写事务同门内完成commit/rollback/close；门外读取准备连接在finally回收同一个FD，撤权不泄漏、不新连接/补账，cleanup不覆盖正在传播的主异常。必要close可能有WAL物理IO，此为已有FD回收，不新增业务DML许可。

没有新错误类。原 source/runtime错误同对象穿过本地叶，普通sqlite/OSError/BlockingIOError保原资源语义。后继 `_jsonl_indexing` 的真实catch尚须在下一付费embedding前透传实际GlobalWriteCommitUnavailable/LocalControlCoordinatorUnavailable/GlobalWriteInWorkerError等真实callee类；本片不改bridge或声称完整promotion/index/mark已经闭合。

建议下一步：非作者复核七路径与默认等价、逐writer撤权/FTS交错，再接C1策略透传及C2/C3-J真实主链。source/jsonl单作者，其他agent只读复核可并行。
