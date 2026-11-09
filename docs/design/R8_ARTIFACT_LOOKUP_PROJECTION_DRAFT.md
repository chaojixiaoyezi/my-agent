# 工具归档引用有界定位草案

状态：仅设计，尚未实现或替换原读合同，需先核对来源成员完整性。配套短片只以不可变身份和全文内容指纹 token 复用同次 prepare 已验证的空候选结果，不保存源行；只减少重复解析，仍非全历史有界查找。复用前仍从当前安全打开的FD流式读取全部字节并核SHA256；只省JSON解析，不省第二遍字节读取。正命中LRU与超大字面单槽也逐次核当前FD全文指纹，原16MiB/32键及单槽边界保持；热读仍O(索引字节)，不是完整有界定位。Linux实证同一时钟刻度内内容可变而dev/ino/size/mtime/ctime均不变，stat不能独立作内容完整性证明。

## 当前事实与边界

scoped ref 是 `scope:call_id`，scope 可以是 run/task/request；它不含日期、工作目录或冻结的 archive_root。run workspace 的目录受创建日、标题和撞名后缀影响；global-run-index 的 run_path 也不等于工具输出冻结根。不能猜目录、仅取首个命中、忽略其他同名或损坏来源。

权威仍为已有 `blobs/tool_outputs/index.jsonl` 与注册正文/完整性字段。projection 只能恢复查找速度，不能授予 owner、路径或 run 权限。失去 projection 必须可重建，不能使历史正文永久不可读。

## 优先复用与尚未满足的证明

现有 `memory_archive/tool_output_retention.py:maintain_owner_tool_outputs_lane` 有 owner 工具输出退休游标；`storage_lanes.lane_cursor_window` 提供分拍续作。应复用预算、游标和安全打开，而不是另建权威 registry。

但当前 owner lane 只覆盖 owner 主根包装文件，模块明确不管理 task 目录；它不是 runs/tasks 全历史 index 成员完整性账。`common/tool_output_paths.py` 仍通过历史目录发现全部来源。不能把该退休游标的 complete 当作 lookup 全源 complete，不能把某次目录分页末尾当成无新增成员证明。

实施前必须将已有来源发现合同补成共享成员代次：原归档 writer 在原 index 追加锁及同一发布协议内登记来源变更，原 retention 对移动/删除也在同一协议下撤销成员。该账应属于现有工具归档来源管理，供 lookup 与 retention 共用，不建立第二套 canonical 注册。滚动旧 writer 或未受协议管理的目录写入无法证明完整时，coverage 必须 unknown，不能假 complete。目录手工删除后进入同源重建；不能用不存在的 projection 签源为空。

## 建议投影及冷迁移

可重建 SQLite projection 保存 source identity、已消费 offset/行号、源代次、全部 ref 候选及原顺序，不保存正文或授权。新 SQLite 必须沿现有 scoped_connection/generation/可重建投影恢复协议；损坏保全后只从 canonical 源重放。

首次旧库迁移分拍发现既有 owner/runs/tasks 来源，并按 bytes/entries/deadline 重放；每拍独立短事务，不长期持读事务钉 WAL。旧 ref 冷读可以推进有限迁移并返回 typed partial/cursor，随后继续；不得用未完成投影返回 not_registered 或 silently empty。显式全历史读取可继续原完整入口，保功能，但普通请求不隐式退回无界扫描。要做到用户一次旧 ref 请求自动完成，可由既有请求生命周期 bounded resume 协调，不能新建无界等待或第二队列。

完成证明需要：来源成员代次在发现轮次内稳定；所有源消费到该代次发布的边界；无来源错误。只有该完整覆盖下，负查询才可签不存在。命中即便来自 partial 集合，也不能忽略尚未扫描来源的同名候选及坏源义务。

## writer、失效及自愈

writer 先追加原 index，再更新/标脏 projection；两者崩溃窗口必须有 durable pending/原发布代次证明，投影落后返回 partial 后重放，不宣称已同步。不得先写 projection 再把它当正文注册。新归档、退休、目录移走、index 替换/截断均改变相关来源代次。普通无关正文读不改变 membership。

projection 被删、schema 不支持或源 token 不匹配时，从原发现 cursor 重新建；不丢历史功能，不加载重型 Agent。跨重启持久 cursor 与源消费边界继续验证；负命中只能在已证明完整且匹配当前代次的投影下复用。

## 权限、竞争与 ABA

候选返回后，仍逐次通过原 authenticated owner root、nofollow 父链/叶子打开、当前 run scope、同名裁决、正文 hash/长度验证。投影不能缓存授权。源 token 至少涵盖 dev/ino/size/mtime_ns/ctime_ns，加原发布代次和按边界验证；禁止以相同 size/mtime 认同一代。索引已删后重建、新 inode 或截断重写都使旧 offset/negative 失效。遇到无法证明的并发替换/截断/源丢失返回 typed partial/error，而不是签空。

## 必须验证

- 老 ref、owner 冻结根、跨 task 承接、重复 ref/同名物理路径，结果与原完整查找一致。
- 百万历史行/大量历史根：冷迁移每拍计数受预算，稳态正负查询工作量不依赖历史；不以缓存通过代替冷进程验证。
- 每个 writer/crash 窗口、projection 删除/损坏、原源追加/替换/截断/退休、跨进程新归档都能失效并恢复。
- partial 不能冒充不存在；任意旧 ref 最终可恢复读取；权限撤销/owner 替换立即拒绝。
- 竞争用 Events 排定；新核心状态若引入需 BFS/replay 不变量测试。

建议下一步：先共同审定既有工具归档来源管理与 retention 的成员代次责任，再实现 projection；lookup 与 FTS 可由不同 agent 并行，来源协议/retention hunk 必须单写者协调。本草案不代表批准新增权威账或完成有界性。
