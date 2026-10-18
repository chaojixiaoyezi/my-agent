# J1 候选、配额、晋升和工具账本本地写叶

状态：2026-10-17 独立候选，尚未接冷 HTTP。基于冻结 C3-L 60，另精确组合 67 三路径异常纯搬迁；不从新 Gateway 整文件覆盖旧基线。

## 资源与来源

CandidateService、OwnerQuotaEnforcer、MemoryPromotionService 增加内部 coordinator_io=False；锁与 operations 函数同名可选参数默认 False。C 只保存 IO 策略，原对象与缓存寿命不变，不保存 source、callback、令牌或新上下文。每次借用由组合根进入原 global_write_commit_scope(source.perform)，固定写叶执行 execute_global_write(require_scope=True)。没有当前来源即原 GlobalWriteCommitUnavailable；Count0 和旧 W 不无条件调用 global guard。

原 quota、candidate、policy 锁都在 Root 外等待与持有，允许原同步 embedding 跨这些领域锁；扫描、候选算法、准入、编码和 JSON 递归准备在 Root 外。只在 60 的 birth_commit 开准确锁 FD 和固定原文件末 replace_once=True 时进入 Root。没有并行候选库、quota 账或第二缓存。后继真实 borrow 互斥区须按本次原 policy 更新既有 quota.max_bytes / policy_available，不以首次缓存值永久替代当前策略。

## 准确写口

| 路径 | 函数/方法 | C 边界与保留顺序 |
| --- | --- | --- |
| memory_store/candidate_commit_lock.py | candidate_commit_lock、candidate_redaction_lock | 仅原锁 FD 出生进门，原 owned lock 及同线程重入保持 |
| user_space/owner_quota.py | OwnerQuotaEnforcer.__init__/admission、_locked_owner_quota | 真实 usage 扫描及原配额判断门外；只锁出生进门，普通 IO/锁竞争仍原领域错误 |
| memory_store/candidates.py | CandidateService.__init__、_locked_candidates、_write_candidates_unlocked 及原调用方 | 目录出生、原候选锁出生与准备完的 raw 末写；原 observe/review/mark/CAS/合并算法不变 |
| memory_store/promotion.py | MemoryPromotionService.__init__/_learning_exclusion_scope/_finalize_promotion、LocalStoreToolEvidenceVerifier.verify | 原 policy 锁出生进门；保 formal→index→mark→condense 和实际 partial，不禁 condense |
| memory_store/operations.py | append_memory_operation_events、append_memory_purge_event、_append_operation_record | 逐条原事件、同 ops path、4096 原上限，读/裁剪/编码在原锁内 Root 外，固定 raw 末写进门 |
| local_storage/ledger_redaction.py | redact_tool_ledger_content、_redact_tool_ledger_coordinator、_truncate_redacted_wal | 原 formal 前隐私链不变；JSON 扫描门外，全 raw CAS→同库 UPDATE/commit→原 checkpoint |

完整函数前后 SHA 和输入输出路径在独立产物 FUNCTION_WHITELIST.json。新增 helper 只提取本地叶子，不把任意业务 callback 当作授权。

## 原错误与部分失败

67 将原 LocalControlCoordinatorUnavailable、OwnerManagementIngressUnavailable 唯一定义搬到 common/owner_write_errors.py，原类 AST/基类/构造/code 保持，Gateway 同名显式导入同一 class，__module__ 自然变化。memory/user_space 不反向导入 Gateway。

本片三处实际宽 catch（quota 锁出生、证据连接读取、晋升后 condense）仅在 C 策略下对实际同链四类 bare raise：GlobalWriteCommitUnavailable、GlobalWriteInWorkerError（含 Executor 子类）、上述两个原类。其它普通 OSError/RuntimeError/ValueError 与原 condense 错误仍保旧语义；不按 error_code 或异常文案升级普通错误。这里没有复制整个错误表，也没有新建 error 类。

formal/index/mark 已完成后 condense 的真实来源拒绝仍原对象向外传播，不能洗成 PROMOTED 成功，也不回滚已经完成的正式事实或另收费用。JSONL/bridge 后继必须在 _try_index_record、_index_vector 和 removal_cleanup_phase 等实际宽 catch 先透传其 callee 的真实 native 类，否则仍未闭合；本片不宣称整条冷 C 可用。

## 账本脱敏事务

原调用因果是 JsonlMemory.apply_batch 删除→_redact_local_tool_ledgers→redact_tool_ledger_content，发生在正式记忆写入前，condense 淘汰旧非 explicit 条目也沿此链。C 首先用 60 的 timeout0 短连接读完整匹配 tool 的 runtime_gate_ledger 与 tool_operations 行，保列名、各值实际类型和稳定主键次序；库 dev/ino 同时记录。原 _redact_json_text 递归与 SQL 参数在门外预备，不增影子表/恢复 schema。

末门用原 secure_delete→BEGIN IMMEDIATE(timeout0)，DELETE/UPDATE 前重读完整同范围 raw 行和库身份，精确 CAS 不符则 BlockingIOError，零 UPDATE、不补账、不碰 formal。匹配 tool 的并发增加/删除/改任意字段均冲突，无关 tool 不冲突；1 与 1.0 不能靠 Python 值相等混过类型校验。CAS 后只执行准备好的原 UPDATE，原 commit 后再原 WAL TRUNCATE checkpoint。SQL 中途失败回滚，checkpoint 撤权则保已提交脱敏事实并抛真实对象。读取准备的原连接即使撤权仍按 60 关闭准确原 FD，不新连/补写。

完整 raw SQL 复读和 UPDATE 数量都是 O(N)；Root 不是恒时短门，普通本地 SQL 单次不可抢占边界明确保留。没有用户总业务 deadline 或新 pragma。定向测试记录 rows、JSON input bytes、门内耗时，只是当前临时库样本，不是容量上界。

## 验证与后继

新增两组普通测试使用实际领域对象、临时 SQLite/文件和原 global scope，fake 只限外层同步门与外部 embedding provider；无真实模型、服务、HTTP 或 native 探针。覆盖逐叶撤权、缺源/过期源、原字节/身份、配额与领域锁跨 embedding、同对象错误、ledger CAS/回滚/partial 和 formal 前删除顺序。136 原函数在显式 False、空参数透传及固定 helper 内联后规范化 AST 等价，不据此代替 C 分支动态验证。

下一片按串行所有权交给 JSONL/builder/bridge 作者：显式传 coordinator_io=True，实际 borrow 安装当前 source.perform，沿真实 catch 闭合。vector_store/text_vector_cache/lessons/HOT 只另列轻量叶子边界后实施；不扩为整域长扫描、付费或锁等待进 Root。本片不启 handler/readiness，不修改用户配置、默认 embedding/usage/rate 算法，也不证明完整 HTTP search/remember 已接通。
