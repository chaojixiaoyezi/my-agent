# R8：Compact 历史节点与冷读定位

状态：本地实现，等待主分支集成与统一 CI；不代表已修改真实用户数据。

## 解决问题

旧可达性维护把每个历史 checkpoint 的六类 carried root 全部展开，累计节点超过
65536 后每轮都从头失败。任意旧 task 可按创建边界选中旧 thread checkpoint，
后端可读性回退又允许同 scope 从旧 base 分叉，因此不能截断历史根，也不能用
`generation <= current` 的过滤替代完整版本语义。

新实现保留原 HAMT 内容、hash、root seal、scope/base、checkpoint ID 和冷证据格式，
只把节点的物理存储与生命周期改成事务内容库。`ImmutableJsonIndex` 的默认文件后端
保持不变；Compact 写者显式使用 `durable_nodes=True`。读取自动识别同命名空间的内容库，
缺库节点时仍可精确读取尚未搬迁的原文件；坏数据库或坏节点不会被解释为空覆盖。

## 唯一内容与提交事实

每个 `evidence/<thread>/{indexes,timeline}/.nodes.sqlite3` 保存：

- `nodes`：原内容地址、原字节、入边数和最后采用时间；每次读取仍核长度与 SHA256。
- `edges`：不可变节点的父子关系，只跟随 `children`，不把领域 `entries` 当边。
- `pins`：checkpoint ID、generation、根节点与 committed 事实。
- `objects/files`：原冷副本与载荷的引用数和零引用候选队列。
- `state`：已核实的提交链前缀及一次性迁移的精确续点。

原 thread CAS 仍是唯一提交权威。候选先耐久保存节点、冷副本和 root pin，再追加原账本。
`ConversationThreadStore.update_compact_state` 成功 CAS 后登记准确获胜 checkpoint。
登记异常只留下 `compact_index_maintenance.status=pending`，不能倒称已成功的 thread CAS
失败。维护从规范 thread head 向已核实前缀逐条读取原 checkpoint，完成两个命名空间登记
前不放行 GC。历史 pending 元数据保留为故障记录；仓库的 `sealed_head` 表达已恢复前缀。

同一次逻辑根构造使用显式节点批事务，跨实例访问同一数据库复用原事务，避免每个 CoW 小节点独立 FULL fsync。
批事务携带 PID、创建线程和 active 租约；嵌套不提前提交，失败整体回滚，复制 Context 不能复活已退出租约。

根 pin 与父子入边在 SQLite 事务内计数。创建父节点、登记根时同事务证明节点仍在库中，
或仍有 hash 匹配的旧文件；暂停超过宽限期后到达的发表不能把已收走的节点重新声明为完整。
引用计数非负，已引用节点禁止删除，已有不可变正文禁止覆盖。

## 回收与必要历史增长

GC 只读取有索引的零入边队列。解除候选 pin 必须同时满足：规范提交链已登记，且候选
`generation <= thread.compact_generation`、不在已提交链。该候选已不可能通过原 generation CAS。
尚可能胜出的更高代候选不能只因超过七天而解除 pin。

零入边节点还须经过原七天宽限期；删除父节点同事务减少子节点入边数，后续小片继续回收。
每片最多处理128个候选/节点，每节点子边最多16条，单节点最多1MiB；不展开全部历史根。
所有物理连接共用 `scoped_connection` 的 generation 租约和既有恢复协议。
工厂在 PRAGMA/schema 初始化失败时先物理关闭原连接再重抛首因；关闭本身失败则附带
`sqlite_cleanup_error` 与真实 `sqlite_unclosed_factory_connection`，由通用 guard 保留原 SH，
直到创建者确认物理关闭，不能因异常 traceback 仍持有 FD 而提前允许 EX 恢复。

保留所有旧视图必然需要保留其信息；不承诺历史存储绝对常数。这里的上界是：
节点数量为 `O(64 * D + R)`，其中 D 是累计真实键变更数，R 是历史根数，64为固定哈希深度。
每个 checkpoint 的数据库记录为常数数量；两个节点命名空间的数据库/锁 inode 数与历史
checkpoint 数无关。原 checkpoint 冷证据仍按原内容格式保留，不借迁移丢失恢复证据。
删除节点后 SQLite 空闲页可复用；不在热维护中执行全库 VACUUM。

## 旧格式迁移与崩溃

首次新写入或维护发现旧 carried 格式时进入同一条迁移路径：

1. 每片至多登记128个原提交链 checkpoint，保存 next ID/generation，不把 partial 当完整。
2. 每片直接从旧命名空间取至多128个剩余节点；先验证原字节并写库，再读回核对，最后移除原文件。
   已成功搬迁的文件消失，下片只看剩余集，不做历史排序窗口的重复全扫。
3. 必须导入所有旧节点的出边，再允许零引用删除。不能只搬当前已知根后就删其它文件，
   因为尚未读到的父节点可能引用字典序更早的子节点。
4. 缺失 placeholder、坏引用、坏 hash、坏提交链均拒绝 GC；中断后库与旧文件可以共存，
   同字节重复导入不重复增加边计数。

旧版本未登记、且不在任何提交 checkpoint 中的冷载荷/冷副本保持保守保留；新写者从产物
出生开始登记零引用队列。未登记的外来文件不会被仅凭名字授予删除权。

退休线程的数据库删除先取得原 generation EX 租约；稳定 `.generation.lock` 以及恢复锁锚
不移走、不换 inode。退休后可能只剩锁锚及其父目录，不能把这类残留误报为仍有历史正文。

## 持久 checkpoint 地址投影

`.<ledger>.offsets.sqlite3` 是可重建读投影，不是 checkpoint 或提交权威。
首次构建完整验证固定源水位，单行仍受8MiB预算；事务保存物理 offset、长度、hash，
每个 ID 按 offset 降序查询，保留 duplicate last-wins 和无 LF 尾行重读规则。

重启后通过源 dev/inode/size/mtime/ctime、原头尾锚及 validator 实现身份恢复校验水位；
同 inode 合法追加只扫新尾，不把进程缓存淘汰当成历史缺失。命中地址仍沿同一源 FD
核对原字节 hash、长度和 checkpoint ID。固定快照不包含后来追加的记录。

同水位的越界地址必须显式报错，不能被 SQL 范围过滤隐藏成缺失。更换源 inode、截短、
等长改写、头尾锚不符、validator 改变都重新严格扫描。头尾锚只证明追加合同下的快速复用，
不宣称能证明任意中段未被外部改写；选中记录仍单独核 hash，与原暖缓存合同一致。

坏投影不能产生 partial 完整水位；结构损坏时先完整校验源，正常退出后在 EX 租约下保全
坏库及 WAL/SHM，保留稳定锁锚，再由下一次严格扫描重建。源校验失败时不退休坏投影。
投影不可写时可按原严格扫描读取，不能因此改写原账本或创建提交事实。

## 本地行为验证

新增回归覆盖：10000个历史 checkpoint root 的真实 SQLite 存储、常数 inode、旧根与分叉读取、
共享子树与失败候选回收、尚可 CAS 的候选保留、库写入后迁移中断/重入、旧 task 时间边界、
CAS 后 pin 登记失败恢复、收走的未发表节点禁止晚到发表、冷进程重启定位、窗口淘汰、
坏 offset 显式错误、坏投影保全重建和新尾 validator 失败。

建议下一步：先在集成提交上跑 Compact/scope/索引/线程 CAS 与 SQLite 恢复 focused 组合，
再跑远端严格 gate 和 CI；读审可并行，节点库及其迁移协议不适合多个 agent 同时修改。
真实 owner 数据迁移、在线模型与 Gateway 长期运行尚未作为本轮验收来源。


## 连接成本作用域

实际 prepare_conversation_context 操作共用原 SQLite 请求作用域，carried builder 不另建内层缓存。
各调用归还前完成事务并复核文件代次；摘要等待只保留空闲、无事务连接，scope退出沿原guard关闭。
首次无旧checkpoint压缩由原local.db一条连接、indexes/timeline各独立ro/rw两条，
以及原追加写门内发表offset投影的一条连接组成，总计6条；
不能再把新增持久库误计为原local.db批次的一条预算。成本探针保留local<=1，节点库逐路径<=2且仅两个路径，offset发表独立<=1；
独立1/240/10000历史回归证明连接数不随根或重复读取增长，权限/代次租约不合并。

维护lane也以一片为连接复用/关闭边界，避免零引用预筛和事务仓读取反复开关同库耗尽25ms。
退休完成后只允许保留每库精确的空generation/recovery-intent锁锚；它们不代表业务节点仍存在，
直接unlink会破坏并发出生/恢复互斥。原载荷、checkpoint和节点库内容仍必须清除。


## 持久地址发表与只读来源边界

普通checkpoint查询默认只消费已有、同源有效的只读投影；缺失或需要重校验时仅在内存完整验证，
不出生generation锁/SQLite、不执行DELETE或退休坏投影。持久投影的建立、增量发表和损坏退休
只允许显式publish_projection写路径，生产沿原checkpoint追加锁与已有宿主写门调用。
单条回查仍核源行hash及identity；未知/partial不被当成完整。原只读SQLite可能维护其非权威WAL/SHM，
它们不能计作canonical正文或借此授权生产写入。

只读投影头的便携打开使用可选O_NOFOLLOW，并以打开前lstat、实际fstat和读后路径/FD完整身份
共同拒绝symlink、硬链接和同窗口替换；Windows缺该flag不代表可以取消身份核验。

## 线程任务上下文的稳态读边界

`TaskStore.context_report` 只选最近最多 128 个历史位置、全部活动任务、当前
`workspace_task_id` 和调用方提供的结构化 `required_task_ids`，对每个选中的链接重新核验
线程归属。`ConversationStore.context_bundle_report` 使用该读口；后台任务在读取前固定本次
谱系身份，确保窗口外的当前任务及其谱系不会因历史裁窗消失。同次范围裁决复用这组身份，
不重复枚举谱系。显式 `list_report`、`list` 和 `iter_task_ids` 仍保留完整历史语义。

`task_coverage` 明示 `status=complete/window/unknown` 和 `history_complete`，窗口不代表
完整历史；缺失、未完成或未知版本的历史索引保持原读取错误，不在上下文读取时创建或修复。
外线程链接一律阻断而非静默装载。后台范围投影移除全线程历史数量，只报告该范围的选择覆盖。
普通旧线程的首次写入仍可能执行一次完整历史初始化；已完成迁移后的上下文读取成本不随终态
任务总量增长，全部活动集和显式必需引用的成本仍按其真实数量增长。

必需引用、当前工作区和活动任务排在近期终态之前，避免展示预算优先保留无关历史。
后台普通与窄审计渲染均传递 `task_coverage`；预算收缩不会把窗口事实裁成“完整”。
模型正文的既有列表预算仍以 `truncated/omitted_items` 明示裁剪，和底层历史选择覆盖是两层事实。
命名任务绑定的重名互斥只读取活动索引，再核原 kind/name/status 谓词，不再为拒绝活跃重名遍历终态历史。
