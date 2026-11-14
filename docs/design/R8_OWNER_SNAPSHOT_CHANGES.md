# R8-13：owner 快照的可证明温增量

状态：源码本地实现，等待主分支统一 gate/CI；不代表已部署到实际用户 owner。

## 目标与权威

旧 `checkout_delta` 每轮读完整 PG manifest、反复遍历 warm tree、全写 stat-index.json；
默认 full 还恢复整树并替换整表。现在三种配置统一进入租约温缓存主链路。
`full`、`delta_compat` 保留原 PG advisory 锁，`delta` 只用租约；默认仍是 full，
用户不必打开旧 delta 开关。已不用的旧 full checkout 入口删除，显式 restore/commit 仍用于全量操作。

三种模式的对象传输判据一致：20文件、两轮各只改1个对象时，首次必须完整恢复20个文件，
紧接的温轮在权威远端内容未变时应为0下载、仅1次实际变更上传；`full`和`delta_compat`
的advisory锁仍须覆盖实际执行。SLO同时逐文件核对正文与hash，并计量stage、对象HEAD、下载和上传，
不能用漏文件、少校验或恢复重复下载来达标。旧full每轮强制20下载的断言不再描述当前模式合同。

所有模式都从原 owner 目录内已经打开的普通文件生成暂存对象。摘要与上传使用同一份暂存字节，SDK 的分段上传和重试可以重新定位读取。清理时关闭原始句柄。对象键已有内容时，只有完整对象的服务端 SHA256 和长度均符合才直接复用，否则分块读取该对象并核对摘要。校验失败保留原清单。

下载先校验远端内容，在绑定的目标目录中发布文件。完整恢复会在清理现有文件之前验证整批内容并复核原目录。下载期间有人更换父目录、把目录改为链接或修改待清理文件时，本轮停止，保留 DIRTY 供下次按权威清单重新核对。程序分别使用 POSIX 目录句柄和 Windows 原生目录句柄约束实际读写位置。

近期或未来的 mtime、ctime 都会触发实际内容核验。Linux 上可以在同一文件系统时钟刻度里原地改写等长内容，inode、大小和两个时间戳仍完全相同。程序对同一个已打开的文件描述符分块读取两次，各次最多读取原文件大小加一个探测字节，复核摘要、目录和文件身份后才接受本次观察。待上传的内容保存在独立暂存文件。

完整恢复缺少连续变更记录时，会枚举当前 owner 的文件并核对实际字节。匹配权威清单的文件无须再次下载或上传。提交前再核已验证文件的摘要，遇到目录替换或正文变化即保留 DIRTY，以原租约和版本号决定重试。单个文件的多次核验不能提供整个目录在同一时刻的原子快照。

PG `owner_file_manifest` 与对象 hash 仍是唯一文件事实源。新的本地
`<warm_dir>/snapshot-index.sqlite3` 只保存可重建 manifest/stat 投影和
`owner identity / committed version / observer clock / mode / reason`。
CLEAN 只在 PG CAS 已确认、本地投影事务提交后发表；异常、取消、结果未知均保留 DIRTY。
旧 stat-index.json 不再读写，成功迁移后删除；缺失/坏投影在 generation EX 下保全原库及 WAL/SHM，
然后必须完整恢复远端清单、逐对象核 hash 后才能继续，不能拿新空索引当完整视图。

## 变更来源与平台边界

`OwnerChangeJournal` 为一个 worker 启动一个私有 Watchman 子进程，使用私有临时目录中的
sock/state/log/pid，不接系统已有 daemon、不注册全局常驻服务。实际 watch 的控制根为 warm_dir，
query 用 relative_root=home，只返回业务树变化；配置/cookie 不进入 prune 的 home，
不靠跳过任意名字前缀来保护观察器。每个 warm slot 一条 watch；
LRU eviction 先 watch-del 再清缓存。worker_entry 实际包裹 handler startup/close 生命周期，
主线程启动观察器，SIGTERM 排空后的 finally 关闭它，startup/执行异常也回收。独立单线程
Python exec 入口先设置 Linux PDEATHSIG=SIGKILL、复核 getppid，再 exec 官方二进制，
不使用多线程宿主 preexec_fn；父进程硬退出由内核终止私有 observer。硬退出可能遗留私有
临时目录，目录不是恢复权威，正常关闭清理它们。未知 watch backend 不授予
增量覆盖，且仍登记/释放 watch，不能因 unsupported 分支遗留观察树。

仅 Linux `watcher=inotify` 加成功同步 cookie、非 fresh instance、无 warning/canceled 的
结构化结果才是完整增量证明。文件内容修改、in-place、rename、删除、chmod，以及新目录立即
写子文件都来自原生变化，不通过 mtime、prompt、settle 时间或静默 polling 全扫猜测。
观察目录是 worker 本地 warm home；正式部署应使用本地/emptyDir，不支持远程共享 NFS 缓存。

macOS/Windows 等平台明确记录 `observer_ordering_unproven` 并完整校对。官方说明指出
FSEvents 即使 cookie 和 FlushSync 返回后仍可能补送先前事件，因此不能声称这些平台已有
完整温增量。Watchman 重启/fresh、recrawl、取消、配置变化、错误或响应预算溢出也进入显式
完整校对。根被删后会撤销旧 watch、重建新 watch，不能永久卡在失效 root。

完整校对期间先安装观察器，再完整核对文件树，并用 cookie 处理扫描期间产生的变化。
快照使用独立 stage 副本；源 dev/inode/size/mtime/ctime/mode/nlink 在复制时改变即拒绝，
符号链接仍按原安全边界拒绝。提交前再次同步并处理新增变化，连续不能稳定则保留 DIRTY。
不提前推进 clock、不把 partial 当完整、不因工具报告“完成”跳过事实验证。

## 跨 Pod 与滚动旧 writer

只比较租约头 version 不够：旧 full writer 不更新该头。migration 13 在同事务安装：

- `owner_snapshot_head.change_floor`。
- 强制 RLS 的 `owner_file_changes`，每个 `(tenant, owner_kind, owner_id, path)` 仅存最后变更。
- manifest INSERT/UPDATE/DELETE 触发器，同事务推进该 owner 的 version 并登记路径/删除证据。
- 按 owner/version 的增量索引，以及表权限和触发器的 readiness 校验。

因此旧 Pod 的整表替换也不能绕过代次。新 writer 在任何 manifest SQL 前先按 incarnation、
数据库到期时间和 acquire 时的 expected version 做 CAS；清单触发器执行后重新读取最终 version，
同事务提交。其它写者插入的代次不能被旧快照覆盖。

温缓存本地 version 落后时，从 change 投影按 version 查询最新路径，并 JOIN 原 manifest 取值；
删除路径有明确 tombstone，不需要读全部历史清单。去重投影每条源变更一个新代次，每64次变更
最多清128条过期投影，保留最近65536代；head 的 floor 单调推进。低于 floor 的旧游标必须完整
恢复，不能把被清掉的 tombstone 当“从未存在”。清理不删除 manifest 或对象数据。
这避免多年累积的删除路径无限增长，也避免为每个变化保存一整份 manifest。

## 常规复杂度

正常连续观察、可覆盖的远端游标下，枚举、stat/hash/stage 的工作按 D 与 changed_subtree 收费；
排序只针对 D，数据库键查询使用索引路径：
PG 按有索引的变更路径读、SQLite 按主键更新、只 stat/hash/stage 本轮路径。
目录 rename/delete 的子树本身属于实际变更量；普通父目录 modified 事件不展开其全部历史子树。
没有完整 manifest 读取、全树遍历、全量 JSON 索引重写或每文件打开一次 SQLite 的 N+1 模式。
冷启动、DIRTY、丢事件、过期游标和不支持平台明确承担完整恢复成本，投影 metadata 记录原因。
观察器内存按当前 warm slots/文件目录数据增长，watch-del/LRU 切断历史 slot；不按所有历年 owner 常驻。

## 外部依赖与构建验证

选择 Meta 维护、MIT 许可的 Watchman，标准库 JSON socket 客户端，无新增 Python 依赖。
原生 cookie/clock 的接口、丢事件边界与许可来源：
[查询同步](https://facebook.github.io/watchman/docs/cookies.html)、
[query](https://facebook.github.io/watchman/docs/cmd/query)、
[上游与许可](https://github.com/facebook/watchman)。

截至本次核验，最新 tag v2026.10.26.00 无二进制资产，最新可用官方 Linux zip 是
v2026.07.27.00，SHA256 为
`e02227ea30fc07af4131f4c00b851d270dd231c24f86b670d46fa7c9aa95b022`。
不使用官方已警告缺安全/性能修复的旧 4.9 发行包，不混装不同发行版的 apt 包，也不降 Python 基镜。
构建器校验 hash、解包路径和预算，保留上游及 bundled libraries 许可。
该 zip 的 ELF 固定依赖 `/usr/local/lib` 的 gflags/glog/snappy，镜像直接安装原文件至该位置。

默认 Linux amd64、Python 3.12 slim 基镜的观察器阶段以非 root 实际启动并运行 cookie/inplace/
rename/chmod/delete/new-subtree/删根重建探针。此为同源 ABI/原生行为阶段验证，不是全应用镜像部署。
Docker Desktop 的 amd64 仿真不实现 pidfd，因此构建阶段只验 ABI/文件观察；父死亡探针另在真实 Linux 内核通过，不以仿真替代。
该资产需要 GLIBC_2.38；其它架构或可选 bookworm/3.14t 基镜必须显式提供匹配且已校验的官方资产，
最终运行阶段仍执行真实 `watchman --version`，不静默生成无法启动的镜像。

## 回归与部署顺序

纯运行时回归覆盖8/2048文件的温轮无全读/全扫/JSON全写、跨Pod增量、丢事件后恢复、目录rename/
chmod/delete、晚到generation拒绝、游标floor过期、未知backend回收、取消watch重建和坏投影恢复。
真实 Watchman 与实际 checkout 连续三轮联验，第2轮硬禁全 manifest/全树并验证 incremental，
防止“adapter单测 + fake observer store测试”各自通过却没有真正温命中；另实测父 fixture
SIGKILL 后 observer 的 pidfd 退出，以及正常/异常 handler finally 回收。
真 PostgreSQL 在随机私有库及非superuser角色下往返验证旧 writer、RLS、CAS、最终version、
投影清理/floor和缺失触发器的启动拒绝。Linux原生探针只操作隔离临时目录。

建议下一步：先应用 PG migration 13，再滚动升级保留 advisory 的默认 worker；
在精确集成 commit 上跑 owner/scale/schema/PG 与 SQLite recovery focused，再统一严格 gate/CI。
读审可并行，schema/触发器、变更流及 warm cache 状态机只允许单写；不把阶段 ABI 或 fake S3
回归称为真实 S3/Gateway 发布验收。
