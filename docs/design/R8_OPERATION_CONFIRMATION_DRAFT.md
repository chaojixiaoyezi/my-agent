# 原操作的退出确认义务（设计草案）

状态：**待实现、需独立合同评审**。本片仅补评估与负回归，不启用自动收口，不修改旧 UNKNOWN，不恢复失败父回合。

## 已有证据与边界

初次 c9 的只读结构化样本对应 `cancel_subagents`；原 operation 为 UNKNOWN，原因是 `effect_outcome_unknown:UNKNOWN_ERROR`，父 run/attempt 已 failed。样本没有可认证的 operation→授权目标→canonical stop 绑定；不由诊断摘要推断当时哪一个退出观察失败，也不补查旧 PID 后写结论。

当前源码事实：

- `contracts/recovery.py` 将 `TOOL_OPERATION_OUTCOME_UNKNOWN` 定为 hard_stop/manual_review，禁止自动重复副作用。
- `agent_core/orchestration/tools/cancel.py` 的 `stop-operation.v1` 明确为 `purpose=diagnostic_only`；`CancelSubagentsTool` 没有专用核对实现，沿 BaseTool 保持未知。
- `subagents/cancellation.py` 在真实进程退出前就可将 target 权限和投影关闭，写 `runner_session.status=cancelled/ended_at`。这些字段不证明物理退出。
- `cancellation_hosts.cleanup_runner_stop` 是原停止调用中的观察，不是持久退出凭证；不能将其字典或一般 PID 消失直接升级成新恢复授权。
- 原 coordinator 只在运行调用寿命内运输 dispatch checkpoint；普通返回立即进入成功/失败/UNKNOWN 结算。原 `ok=true` 不是异步受理。
- `operation_reconciliation_authority` 要求准确 owner/root/run/tool/args 和当前活跃 attempt。不能为了旧失败父回合放宽该通用门。
- 原操作账已有 `component_receipts` 与 holder/generation CAS；这是可复用的位置，不另建停止任务 registry 或权威队列。

因此“再查一次进程后把 UNKNOWN 改成功”不成立。新的通用能力应表达**已认证控制效果的确认义务**，取消是首个接入者，不为具体 TUI/任务写专项状态。

## 最小可落地合同

### 1. 副作用前绑定原操作与冻结目标

在原授权通过、creation guard 内重验 canonical 身份后，**任何中断/权限关闭前**，在原 operation 的组件账提交不可变绑定。不能从 handler 返回的诊断反向创建。

绑定至少包括：

- 原 owner/home、root task、request/run/task、attempt 与 attempt generation、operation ID/generation、tool、args hash、授权目标集合摘要；
- 每个 target 的 owner、root/parent/run/task、原 attempt/generation、原 canonical stop/control generation；
- 目标原执行实例的 host/boot identity、launch ID、worker PID/birth、共享宿主资格；
- 同次冻结的后台进程/PTY 等资源 ID 与 lease/generation、完整集合标记。不能只证明 runner 退出而漏掉资源。

绑定落盘失败则本次不能开始停止效果。原 target 的既有 stop receipt 只追加绑定引用/摘要，仍由 canonical 生命周期持有；不建立第二份目标名单权威。必须评审 creation guard→operation checkpoint 的锁序与崩溃点，不能持 DB 写事务等待 OS。

### 2. 明确非终态确认阶段

需要正式的 `awaiting_confirmation` 非终态语义，而不是把 `ok=true` 解释成“已受理”。它属于原 operation，保存上述绑定、已确认成员和未确认成员；不得新建另一条工具执行或释放后允许等价副作用重放。

建议最小接口为 `ToolOperationPendingConfirmation`（绑定摘要、完整冻结成员、原 generation）与 `defer_tool_operation_confirmation`，仅原 holder/generation 可从 RUNNING 进入持久 `CONFIRMING`；原资源锁保留在该操作，不持 SQLite 事务等待。结果 DTO 新增显式 `effect_outcome=pending`，沿现有 `TOOL_OPERATION_IN_FLIGHT` 等待语义承接，不能按失败返回去触发 handler 重放。确认提交用独立的 `confirm_tool_operation_effect`，不复用普通 finish 任意 result 入口。

这些名称是本草案的建议，尚未写入生产：原 coordinator、工具结果、错误目录、资源锁和回合关闭目前都不支持该阶段，不能仅添加一个 JSON 字段假装完成。新状态也影响旧安装回退读取，发布前必须说明兼容边界；租约过期只能转交确认责任或保留未知，不能释放为一次新的副作用执行许可。

只有合法、完整且已落盘的确认绑定可进入此阶段。授权/集合/来源不明或普通 handler 错误仍走原 UNKNOWN。模型及客户端须获得明确“等待原操作确认”的结构化结果，不能收到成功；原 handler 不重新执行。

### 3. 有界、只观察的确证消费者

复用 owner 维护及原操作账：只对未完成确认义务的 partial index 按 keyset 分页，单片沿既有条目/字节/时间预算；不扫历史 UNKNOWN，不为每个操作保留线程，不跨 OS 观察持 SQLite 事务。

每个候选必须重新验证：

1. 原 operation generation、完整目标摘要与组件回执引用一致；
2. 原 target 的 canonical owner/run/task/attempt/stop generation 仍匹配；后续新 attempt 不能被旧义务选中、发信号或取消；
3. 原执行权限确已关闭；cancelled/ended_at 本身不算退出；
4. 每个冻结资源均有原可信宿主提供的 exact-instance 退出/释放事实。拥有者的原 Popen/wait 结果可作原生证据；重启后须有已认证的持久宿主退出回执或严格同 host/boot 的内核确证，不能靠心跳年龄、诊断字段、命令文本或一般 `os.kill` 异常；
5. 共享/缺 birth/缺 launch/权限拒绝/未知 OS 状态/目标证据被删，均保留未确认，不能确认空集合。

消费者**不发停止信号、不重新发现新目标、不执行工具**。仍需停止升级时必须由原控制执行权完成，不能把观察器变成新的执行器。

### 4. 受限确认 CAS 与父回合分责

新增的确认提交只能接受原非终态确认阶段及完整 proof，按 operation generation + binding digest + target/resource generation CAS；不能复用可任意传 result 的 finish 接口绕开活跃 attempt 门。

效果证明确立后，只结算该工具声明的控制效果；不能宣称被停止任务的业务成功、不能反转 failed/cancelled 父/子终态，也不能伪造旧取消事实。仍在等待的活跃父回合可经原唤醒/恢复入口接收原操作结果；已经失败的父回合不因此重开。

普通 UNKNOWN 的跨 attempt 只读核对仍走原活跃授权合同。旧 c9 缺少新绑定，**不迁移、不自动收口、不改原 failure**。若确认写盘失败，原义务与 proof 保留，下一片仅重试 CAS，不重做副作用。

## 必须先完成的运行时验证

- Event 交错：绑定提交/发中断/关闭权限/worker 退出/确认 CAS 的每个崩溃窗口；未提交绑定零副作用。
- target 新 attempt/复活、PID ABA、共享宿主、owner/args/operation 错配、源删除/坏账、EPERM/不可观测：零误确认、零观察器信号。
- 多 target 的部分完成不被集合截断签 complete；有界 keyset 跨拍前进、重启续作、普通 metadata 更新不使进度永久失效。
- 原操作 handler 执行次数严格为一；重复请求只等待/重放原结果，不用新 call ID 绕过。
- 真实自有子进程退出，但只有旧 diagnostic receipt：operation UNKNOWN 和父 failed 字节保持不变。
- 状态机 BFS/replay：原终态不可逆、同 scope 执行体互斥、取消/退出证明不因内存淘汰丢失。涉及 SessionTask/Claim 时再补各自原确定性交错矩阵。

## 本片已做与尚未覆盖

已做：原取消诊断与 managed reconciliation 真实合同测试；新增自有 Popen 确证退出也不能给旧未绑定 diagnostic 赋权的负回归。所有数据库、进程与 HOME 均在隔离测试目录，不碰原 c9 或真实用户账。

未做：上述新阶段、持久绑定、可信宿主退出回执、维护消费者、资源完整证明、回合等待协议和数据格式迁移。不能称 UNKNOWN 已自愈。它们需要一条完整合同切片，不适合用一个 PID 轮询补丁替代。

建议下一步：根先评审新非终态确认阶段及 proof-only CAS 的职责，再按“原操作绑定→真实宿主证明→有界消费者→回合等待”顺序实现；可并行审阅原宿主退出证据和状态机，但同一 operation schema/结算接口应单点修改。保持旧 UNKNOWN 与失败历史不变。

## 增量评估：保留 RUNNING，能否避免新增 SQL 状态

可以作为更小的数据表示方案，但**现有 running/checkpoint 本身不等价于已实现的等待协议**。沿原接口的可执行反例已补在 `test_tool_operation_idempotency`：handler 保存 diagnostic checkpoint 后返回 `TOOL_OPERATION_IN_FLIGHT + effect_outcome=unknown`，原 coordinator 仍合法结算 UNKNOWN；重复请求不再调用 handler。错误码或 `phase=awaiting_exit` 字段不能签发延后权限。

准确阻碍：

1. `_execute_claimed_operation` 对每个返回结果立即调用 finish；没有“已执行、尚未确认、非终态”的返回分支。将 effect 改为 `not_started` 会谎报未执行，改 ok=true 会错误终结并释放锁。
2. `_invoke_with_lease_renewal` 只在原 invoke 寿命内续租，返回后停止。仅留下 running 行而没有已提交的确认责任与消费者，会在后续 holder/lease 判断中变 UNKNOWN。
3. `record_tool_operation_dispatch` 能持久保存组件数据，但 `merge_tool_operation_dispatch` 当前仅固定组件 ID、message_key 和首次时间；任意 receipt 的 target/phase 字段并非不可变授权绑定，不能直接采用为原停止身份。
4. 原回合在已执行且未知时 hard stop；现有通用 reconciliation 又要求活跃 current attempt。仅补取消 reconciler 并不能在父 failed 后获得合法收口权，也不能靠它重新执行。

**推荐的完整最小垂直片**可保留 SQL `RUNNING`，在原 outcome 内引入受严格版本/摘要校验的 `effect_confirmation` 变体；仍须一次性实现以下生产消费者，缺任何一项不得接入：

| 顺序 | 现有拥有者 | 最小必要变化 |
| --- | --- | --- |
| 1 | coordinator/operation store | 原 holder 在副作用前提交不可变 target/resource 绑定；专用 defer CAS 在 handler 返回前交接确认责任，不能用任意 handler envelope 触发 |
| 2 | claim/renew/recovery | 验证该变体后只返回 pending，不重新领取 handler；等待责任由原 owner 维护续作，原资源锁仍归原 operation；租约/重启不授予重执行 |
| 3 | cancel canonical control | 冻结批次中记录原 operation 绑定引用，后续原停止实例的退出回执可对拍；诊断与 canonical 证明分开，不能让新 attempt 填旧绑定 |
| 4 | 原有界 owner 维护 | 只观察原 target/resource，proof-only CAS 确认，不发信号；当前 pending 集部分索引/keyset续作，不扫历史 UNKNOWN |
| 5 | 工具结果/原回合等待 | 显式非终态 DTO 与原 operation 依赖：模型不收到成功，父回合不因合法待确认被改 failed；完成后仅唤醒原仍可续的父回合，取消/失败父回合不重开 |

这种方案不新增权威 registry/队列，也不需新增 SQL status，但仍是一条跨 coordinator、store、控制、维护和回合的状态合同。旧安装不理解新变体时至多回到保守 UNKNOWN，不能假定具备续作能力；必须验证其原 outcome/组件字段保留及降级行为，不能直接承诺无损二进制回退。

本增量只沉淀可独立合并的保护合同：diagnostic checkpoint/IN_FLIGHT 不会成为成功或新的执行许可；failed 与 cancelled 权威在 claim 前、claim 后都不得结算旧 UNKNOWN。没有新增 producer、无人消费的 phase、后台线程或生产状态，因此不与本轮 CPU 补丁发布互相阻塞。
