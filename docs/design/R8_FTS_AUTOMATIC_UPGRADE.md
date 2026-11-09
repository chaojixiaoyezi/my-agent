# R8：旧 FTS 自动渐进对齐

状态：源码候选，仅合成临时库验证；真实库未迁移、未修改。

## 已确认机制

旧版 `records_fts(id UNINDEXED, title, content)` 可能没有
`records_fts_rowid_aligned` 和 `records_fts_rebuild_pending`。它仍可检索，但写入使用
`DELETE ... WHERE id=?`，每次遍历全部历史。不能直接改用 records.rowid：旧写法会真实产生错位。

Python 3.12.13 / SQLite 3.50.4 合成 1k、10k、50k 文档时，旧删除分别为
11k、110k、550k VM 步；重新打开连接后的多轮结果仍线性。已对齐点删低于 1k 步。
这些合成耗时不能代替现场的 5.1 秒，也不是操作系统冷盘基准。

## 正式入口与权威

复用原 owner 维护线程、owner 分页、锁、transition/CAS 写门和 maintenance.json。
该 JSON 只增加 `local_store_fts_upgrade` 发现游标、下次时刻和结构化结果；不复制重建进度。
沿原维护 pending 游标语义，最多保留八条续作路径，与发现页交替；路径只提示查找，不签权限。
SQLite 原 `maintenance_fts_job`、变化捕获器、验证、原子发表及退休回收仍为唯一领域状态。

`records_fts_active_complete` 单独描述active全文覆盖，不能把rowid对齐或pending当作完整性。
缺表且有records、关闭FTS写入时标false；完整旧active在影子pending前保true；发表时同事务置true。
两个正式MATCH读口在同一读事务核该事实，未完整时沿原typed OperationalError/预览LIKE，
避免部分FTS命中压掉旧canonical命中。完整旧active继续MATCH，不能对所有pending盲目全扫。
coverage只加入当拍快照CAS，不改变原持久job.state编码，原在途job不会因新增键永久失配。

普通/协调 LocalStore 构造只恢复结构，不同步调用全量 rebuild。
旧 unicode61 active 不再在构造时 DROP；影子使用当前目标声明，原子发表后才写新 schema version。升级发现最多检查三个固定库
和一个 workspace 原生目录项，持久目录 cookie 随原 maintenance.json 保存。
必要投影维护不受 TTL maintenance_enabled、间隔 0、legal_hold 禁用；删除与 vacuum 继续守原策略。
原 Gateway 提示只合并既有维护时刻，不增加线程或队列。沿原 60 秒节流，
每片128项、8MiB和25ms协作预算；原tick从base owner前计入20秒总deadline，
共享4096片上限，单owner最多1024片/2秒。只在真实进展后续片，busy/space/unsupported/零进展即让出；
每个burst至多一个新目录发现页，后继只轮转已核同owner pending引用，每片先释放原SQLite/业务锁。
资源耗尽不刷新未服务owner的到期时刻，保留原优先FIFO；hint按原周期退避，不能变为热自旋。
Count0的共享预算是进程内原controller tick；C/W单owner原operation同样受1024片/2秒约束，
并发仍归原worker/claim，不伪称新增了跨进程global计数。
连接锁等待在该上下文收紧为零，并沿原 sqlite_connection_scope(False) 使用独立短连接，
不复用或改动父业务缓存连接的 busy_timeout；普通业务连接仍为 30 秒。
原生目录枚举跨冷进程续点；便携枚举沿已有受控实现，本片未证明 Windows 大规模公平性。

原空间检查保留 1.2 倍门槛，估计改用整库页数安全上界，避免 dbstat 遍历全部索引页。
这个上界可能比实际 FTS 更保守，空间不足应延期，不能清空旧索引或盖完成标记。
旧 active 始终保留到原重建的验证及原子换表成功；所有正常写者仍通过原捕获器同步变化。

## 大文档、失败与完成

8MiB 普通片遇到原结构化字节延期时，沿原 64MiB 单文档上限、最多一条文档再推进一次；重试扣同一整拍 deadline，
不循环整库、不把某库的字节预算错用于另一库，也不截断正文。超过原上限明确 unsupported，
保留旧索引及未完成事实。25ms 是协作预算，不是单条 SQLite 语句的可抢占硬时限；
受限大文档可能超过该时间，内存仍受原单文档上限约束。

缺空间、锁忙或坏数据不签完成；原来源撤权按同一异常对象传播。坏 maintenance.json 不作为空游标覆盖。
退休FTS表DROP后还要用三个固定LIMIT1点查原docs/delta/map责任，继续原cleanup直到辅助表也空，不能提前签current。
TTL 与索引各有独立片预算；索引延期不阻止原删除维护继续，反之事件积压也不能耗尽索引预算。
迁移完成后持久对齐证明使正常记录写入走 rowid 点查；不会因重开自动开始下一代重建。
显式管理员全量 rebuild 入口保留。

## 验证边界

回归覆盖普通/协调入口、冷构造、关闭 TTL、间隔 0、legal_hold、低空间后恢复、
大文档尾部完整检索、Event 控制准备后更新/删除、原 job 身份、来源拒绝、配置关闭、
旧 tokenizer 发表前完整保留、双库进行中/缺空间时另一库仍可完成、
坏维护游标和 1k/20k 历史点写工作量。原 schema/rebuild 测试改为构造只恢复后由显式原维护续作，
原正文、检索、事务和 CAS 断言保留。

有限catch-up已实现，避免旧版每分钟只前进一步持续多天。Mac / Python3.12.13 /
SQLite3.50.4真实临时库、每条1KiB完整canonical正文（含文件）验证：

| 规模 | seed DB大小 | 完整闭合累计执行时间 | burst数/片数 | 60秒分钟槽投影 | 进程峰值RSS |
| --- | --- | --- | --- | --- | --- |
| 1万 | 42,954,752B | 7.646s | 5 / 401 | 约5分钟 | 69.5MiB |
| 10万 | 417,619,968B | 78.204s | 45 / 4112 | 约45分钟 | 77.1MiB |

累计执行没有实际等待60秒，首片立即开始时末片分别在约第4/44分钟开始；未计Gateway启动保护期。
seed分别另耗2.167/25.832秒，RSS高水位包含seed，不是Gateway或TUI常驻RSS。
记录/FTS身份完整相等、正文和retired/docs/delta/map清空全部验证后才结束。
最大短正文burst 2.030秒，说明合作deadline并非可抢占硬时延；大文档、SQLite合并/收尾也可能越过单片时间。
旧3/32burst样本只清到旧FTS表而漏辅助表，已被上述完整闭合数据替代，不能继续引用为全部完成。
按相同实际片数、旧每分钟一片仅作工作量折算约6.68/68.53小时，非真实旧调度实测。
这些是温缓存短正文模型，不能从11GB DB文件大小推导真实正文量、行数、冷热I/O或整体迁移时长。

仍须集成者在冻结版本运行组合和跨平台 CI，并沿正式部署观察真实库自动迁移进度及稳态写耗时。
本片不声称已迁移真实资料，不承诺任意超过原单文档上限的索引自动完成。
