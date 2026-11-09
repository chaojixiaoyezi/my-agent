# Claim 活动投影的有界续扫

此投影仅定位 canonical claim，不能授予或修改状态。每次最多读取 128 条目录项及 claim，25ms 到期即保留续点；未完成、坏行、目录原子换代均不能签发空闲证明。

原 4-store 内存 LRU 会使 6 个长历史目录交错轮询永久无法完成。现将目录 cookie、代号、完成状态、活跃 thread 去重集合存入进程私有临时 SQLite；Python 不持有全量 busy 集合。SQLite 页缓存 256KiB、禁用 mmap，索引定位单个 store/thread。签名失效以新代号立刻隔离旧结果，每拍最多删除 128 条旧代记录。按 last_access 索引每拍淘汰最多 4 个一小时未访问 store，不因 store 数量淘汰进行中游标。退出后临时目录清理，重启重新核对。

POSIX 复用公共 directory_page 原生目录 cookie，每步关闭目录 FD。Windows 使用最多 8 个 DirectoryCursor 流，SQLite 按固定代号排队，进行中流不会被容量淘汰；完成后释放槽。失联流 60 秒后释放且清掉其未完成投影，下次重新核对。等待者每拍仍受原扫描预算，不能将等待当空。进程丢失不是持久化权威丢失，只会重新 unknown 至核对完成。

运行时回归覆盖原实现 6×300 交错永久 unknown 的红测、原生 6-store 和便携 12-store 收敛、Event 控制原子换叶、坏行/目录删除重建，以及固定页缓存/FD 上限。未启动真实 Gateway 或操作真实 owner 数据。
