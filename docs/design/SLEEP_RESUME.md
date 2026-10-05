# 睡眠与长暂停后的执行恢复设计

**状态：设计已由 3a 定稿；SLP-2A 已实现，待审**

**日期：2026-10-05**

**范围：只定义恢复合同与实施顺序；本设计提交不包含产品代码。**

## 1. 背景、证据与边界

本设计承接 slp2 睡眠追踪报告中的“条件风险前五”（原报告基线 `d05a0d075327aab80d196fc32cb1c081d356579a`）。实现入口和现有保护另以只读 `git show` 核对了 `claude/3a-step17k` 已提交 HEAD `43b320bcd`；下文 `文件:行` 均指该提交，不引用该树未提交的工作区内容。sleepdesign 工作树从 `da6e38093` 开出，与只读代码树不是同一个 checkout。

以下都是**触发条件下的设计风险**，不是已观察到的重复执行、重复消息或任务丢失，也不证明 2026-10-03/04 的八个失败回合由睡眠造成。尤其要把“墙钟期限已过”与“执行者已死”“外部副作用未发生”分开；期限只能提示需要恢复核对，不能单独授权重放。

## 2. 统一恢复合同

1. **期限不是死亡证明。** 持久化 `expires_at` 可继续用墙钟，跨进程、重启后仍可比较；睡眠时间计入墙钟期限是预期现象。但过期只表示 lease 需核对，不足以授权另一执行者并发执行。
2. **执行者身份和执行尝试必须结构化。** 记录稳定的 `run_id/task_id`、`execution_attempt_id`、单调递增的 `claim_epoch`、进程身份（PID 与可用的启动时间/boot 身份）及最后心跳。PID 存活只能证明进程存在，不能证明某个线程或操作正在前进；身份不可核验时按“未知”处理，不按“死亡”猜测。
3. **失去 claim 必须形成可观察的栅栏。** 续租失败或 epoch 被替换时，宿主要产生结构化 `claim_lost`，取消旧回合未开始的工作；工具开始、长操作恢复、提交结果等所有副作用边界都核对当前 attempt/epoch。仅拒绝旧 worker 的最终账本写入不够，因为它可能已经调用外部服务。
4. **副作用复用各自既有权威账本。** 注册工具 handler 已经接入 `ToolOperationCoordinator` 与 managed operation store；请求携带 `run_id/task_id/operation_id/idempotency_key/attempt_id`，已完成操作可回放，未知结果可调用 read-only reconciler。恢复设计应补强这条合同，不新建平行工具账本。Adapter 回送是独立的 pending/sent 管线，按现有 delivery store 扩展其 dispatch/receipt 状态，不把它冒充为工具操作账本。证据：工具 `agent_py_agent/agent/tooling/executor.py:545-563,801-823`、`agent_py_agent/agent/tooling/tool_operation_coordinator.py:66-85,98-108,261-298,315-373`。
5. **未知不是失败，也不是成功。** 由现有 `ToolOperationRecord` 状态与 `ToolOperationReconciliation.outcome` 表达，沿用可验证的 `succeeded`、`failed`、`not_started`、`safe_to_retry`、`unknown` 等结构化结果，不另造并行状态枚举。状态机、调度和模型判断不能从错误文案、空日志或工具自述推断副作用是否已提交；`unknown` 要经显式核对/恢复处理，不得默默转成普通 retry。
6. **醒后先核对，后收口/派发。** 恢复屏障可阻止到期扫描、自愈终止、同一 run 再派发在核对前抢跑；它只是协调提示，不是判断执行者或操作结果的权威。对同进程睡眠，可比较前后 wall/monotonic 样本作为“可能暂停”的提示；墙钟校时也可能产生差值，故该提示不能直接触发副作用。冷启动没有前一单调样本时，不依赖该启发式。
7. **时钟用途分开。** 墙钟用于日历计划和持久期限；单调钟用于进程内耗时预算/间隔。macOS 睡眠期间单调钟可能不走，因此醒后需重新核算剩余进程内 deadline；跨重启不保存单调值。若实现需要精确计算含 suspend 的运行时长，可选系统提供的 sleep-inclusive 时钟并绑定 boot 身份，但安全性仍由 attempt/副作用账本保证，不由该时钟单独保证。

## 3. 条件风险前五与实施设计

### 3.1 风险一：会话执行 claim 过期，旧回合与接管者并发，重复有副作用的操作

- **成立条件**：会话回合睡过普通 claim 的墙钟期限；另一领取者在旧回合退出前调用 acquire。旧报告记录的默认 lease 是 90 秒（原报告 `slp2-sleep-trace-luna1.md:15-18`；实施时应以目标分支配置为准）。旧回合若已越过外部效果边界、但结果未提交，接管后重跑可能重复效果。
- **实现证据**：`agent_py_agent/agent/conversation/store_claims.py:231-247` 仅当 `expires_at > current` 时认为 claim active；active 且进程未被明确判死会拒绝接管，但过期后该存活检查不再阻止新 claim。`agent_py_agent/agent/conversation/run_claim.py:110-131` 心跳用 `time.time()` 续约；renew 返回 `None` 或异常后线程退出，本处没有触发回合中断。另一方面，`agent_py_agent/agent/tooling/executor.py:545-563` 将 `operation_id/idempotency_key/attempt_id` 交给现有 coordinator；`agent_py_agent/agent/tooling/tool_operation_coordinator.py:261-298,315-373` 支持已完成操作回放和 unknown 核对。`agent_py_agent/agent/tooling/runtime_contracts.py:127-170,712-714` 的默认 operation_id 由 run、attempt、call 身份推导；恢复若重建为新 attempt/call，默认会成为另一操作，不能自动去重。
- **现有保护**：claim 原子更新、同一 claim_id 才能续租、`recover_same_task_only` 限制特定任务接管；17k 代码对未过期 claim 检查 owner process liveness。工具侧已有 `ToolOperationCoordinator` 的 claim、持久结果回放和 unknown reconcile，且操作账本不可用时可在 handler 前 fail-closed。当前 `ToolOperationExecutionRequest` 带 `attempt_id`，但没有父会话 `claim_epoch`；过期会话 claim 的替换也不由这个工具账本决定。
- **修法与入口**：到期拆成 `expired + owner_dead/live/unverifiable`；live 或 unverifiable 不交给新执行者，先置 `recovery_pending`，并在替换 claim 前栅栏旧 attempt。接管确认后递增 claim_epoch，续租丢失要结构化中断旧回合。重放时复用持久化的原 `ToolCall.operation_id/idempotency_key`，并把父 `claim_epoch` 透传到现有 `ToolOperationExecutionRequest`/operation claim，在 handler 开始前核对；不能用新 attempt 重新生成一个“看起来不同”的 operation_id 后盲目重跑。继续使用 `ToolOperationCoordinator` 与 managed operation store，不建新账本。入口：`agent_py_agent/agent/conversation/store_claims.py`、`agent_py_agent/agent/conversation/run_claim.py`、`agent_py_agent/agent/tooling/executor.py`、`agent_py_agent/agent/tooling/tool_operation_coordinator.py` 及相应 runtime store。
- **故障注入用例**：假时钟将 wall clock 前跳超过 lease、保持旧进程和 attempt 存活，验证第二 claimant 不会启动工具；打乱“旧心跳先续租/新 claim 先到”的顺序，验证 epoch 只有一个有效 owner；旧 attempt 在 handler 前失去 claim 时不得开始副作用；注入 provider 已接受、持久回执未写的崩溃，验证从已保存的同一个 `ToolCall` 恢复时复用原 `operation_id/idempotency_key`，只回放或只读 reconcile、不二次调用 handler；若只能重建新 attempt/call，则不能仅靠同参数哈希假装是原操作；确认新逻辑调用即使参数相同也有新 operation_id。
- **估时**：总计约 **7 agent 小时**，按下方 SLP-1A（3h）和 SLP-1B（4h）拆分；若要给多种外部工具适配幂等 API，另估。

### 3.2 风险二：定时 run claim 过期后重复领取同一计划动作

- **成立条件**：同一 scheduler run 处于 `claimed/running`，墙钟越过 `claim_expires_at`，旧 runner 尚未退出而另一 worker 再次调用 `claim_run`。旧报告记录默认 scheduler lease 为 300 秒（报告 `:20-23`；实施时以目标分支值为准）。这不是 due/misfire 补跑多次：`reserve_due_runs` 为 job 保留活动 run 并按 misfire grace 处理错过的周期（`agent_py_agent/agent/scheduler/repository.py:296-344`），风险是**同一个 run 的 claim 接管**。
- **基线问题证据（812828b98）**：在本切片前，`claim_run` 对 `claimed/running` 只在 expiry 仍未来时拒绝，过期后就创建新 `claim_id` 并写入当前 PID/start time；此入口没有先查 runner PID。`:452-474` 心跳按 claim_id 更新 expiry。`:915-955` 的 crash recovery 则必须 lease 到期并有 PID 死亡证明才转 `unknown`；PID 存活或不可验证都保留原态。`agent_py_agent/agent/scheduler/service.py:63-75,94-105,193-230` 是心跳与 wake-claim 调用入口。
- **切片前已有保护**：每个 run 有稳定 run_id；scheduler wake 有身份核对；heartbeat 仅接受相同 claim_id；中断恢复路径对 PID/start_time 做 fail-closed 检查。但普通 `claim_run` 的过期重领逻辑没有复用该死亡证明；稳定 run_id 也不能阻止同一 run 的业务副作用再执行一次。SLP-2A 修复见下条。
- **修法与入口**：SLP-2A 已在 `scheduler/repository.py` 落地：普通 `claim_run` 与 `recover_interrupted_executions` 共用 `_runner_liveness` 三态判定；lease 过期但 runner live/unverifiable 时保留旧 claim、不返回新执行权；只有 death proof 才先在 owner store 锁内 CAS 为 `queued`，之后 `claim_run` 生成递增的 `claim_epoch` 新 token。heartbeat、release、mark-running 与 finish 按 `claim_id + claim_epoch` 做 CAS。此切片状态：已实现，待审。SLP-2B 的 scheduled action `operation_id` 复用和现有操作账本恢复仍另行接线，不由此切片宣称完成。
- **故障注入用例**：超 lease 保持旧 PID/start_time 存活，普通 `claim_run` 不创建第二个执行者；PID 缺失、权限错误或无法读 start_time 时 fail-closed，不把 run 自动标成死；确认原进程死亡后只产生一个新 epoch；旧 claim 的迟到 heartbeat/finish 被 CAS 拒绝；同一个 run 恢复时 operation_id 不变并读取旧回执，两个不同逻辑动作则使用不同 operation_id；注入睡眠跨过 misfire grace，验证每个 job 仍按现有 misfire 合同最多保留一条应运行的预约。
- **估时**：总计约 **6 agent 小时**，按 SLP-2A（3h）及依赖操作账本的 SLP-2B（3h）拆分。

### 3.3 风险三：Gateway stale processing 被重排/收口，旧请求效果可能重放

- **成立条件**：processing lease 的墙钟年龄跨过阈值，且恢复器看不到该 request/attempt/epoch 的活动 heartbeat；或 startup 路径把缺失的进程身份当作 stale。恢复后可能重排续跑或按计数收口；若旧回合此前已提交外部效果但终态未落盘，重排会有重复风险。
- **实现证据**：`agent_py_agent/agent/gateway_parts/recovery.py:625-657` 先用墙钟 lease age 判阈值，再以 `is_heartbeat_alive_for_request(request_id, execution_attempt_id, lease_epoch)` 豁免同进程活动执行；startup 下确认进程存活会阻止 stale，明确已死或非 dict 身份走 stale。`:660-700` 对候选在回合锁内重读，并比对 attempt、epoch 与 heartbeat 后才变更；`:256-278` 选择重排或收口。`agent_py_agent/agent/gateway_parts/lease_service.py:237-258` 把活动 heartbeat 登记在进程内的 request/attempt/epoch 集合。
- **现有保护**：同一进程活动 heartbeat 豁免；startup 进程身份判定；候选锁内重读与 attempt/epoch/heartbeat 校验；独立重试/恢复计数。工具侧既有 operation store 能回放同一 operation_id 的已完成结果，并对 unknown 调用 handler 的只读 reconciler；但 recovery 是否复用了上次已保存的 ToolCall.operation_id，以及是否在每个旧 attempt 前核查该记录，必须作为集成点补验。不能把新 attempt 默认生成的新 operation_id 当成旧操作已安全去重。
- **修法与入口**：保持现有 Gateway lease/锁 CAS；复用 `ToolOperationCoordinator`/managed operation store，不另建账本。处理 stale request 前，按原 run 与已保存 ToolCall.operation_id 读取操作记录：`succeeded` 回放原回执；`unknown` 只走现有 read-only reconciler；只有结构化 `not_started/safe_to_retry` 才能允许新的 handler dispatch。缺进程身份时进入 `recovery_pending/unknown`，不能仅因缺字段就重放。把 Gateway execution_attempt/lease_epoch 透传到 `ToolOperationExecutionRequest`，在 handler 开始前核对，使旧 attempt 不能再开始新副作用。入口：`agent_py_agent/agent/gateway_parts/recovery.py` 的 `_processing_request_stale`、`_fresh_matching_stale_payload`、`_settle_stale_processing`，以及 `agent_py_agent/agent/tooling/executor.py`、`agent_py_agent/agent/tooling/tool_operation_coordinator.py` 和 managed operation store。
- **故障注入用例**：假墙钟推进超过 processing timeout、同一进程 heartbeat 仍登记时不重排；heartbeat 线程停止后只对相同 attempt/epoch 的候选生效；旧快照与新 attempt 交错时 CAS 不动新状态；startup 缺身份不得直接重放有 `in_flight/unknown` 的副作用；旧 attempt 在副作用已提交但 terminal write 前崩溃时，恢复读取回执而不是再次调用工具。
- **估时**：约 **3–5 agent 小时**；依赖 SLP-1B 的 operation 状态接口。

### 3.4 风险四：长暂停让 stale-waiting 收口过早

- **成立条件**：`waiting_since` 的墙钟年龄因睡眠超过 grace；醒后暂时读不到有效 follow-up，缺失确认又达到条件，任务因此被阻塞并失败收口。它是误终止风险，不等于已有证据证明会发生。
- **实现证据**：`agent_py_agent/agent/scheduler/active_run_closeout.py:133-165` 以 `time.time()` 算 `waiting_since` 年龄；节流后重新读取结构化 follow-up facts；缺失需两次确认；不可读走更长宽限；先 CAS 成 blocked，再写失败终态。
- **现有保护**：多次缺失确认、最短检查间隔、重新读取 follow-up、unreadable 延长宽限、blocked/finish CAS。它们防止瞬时抖动，但睡眠本身可跨过宽限；当前片段未展示执行者存活证明与 wake 后额外宽限。
- **修法与入口**：收口前读取对应 run/attempt 的结构化存活与恢复状态；live 或 unverifiable 时保持 waiting/recovery_pending。若同进程观测到暂停，恢复屏障完成前不消费缺失确认；屏障后重新开始两次确认间隔，而不是把睡眠时长算成“后续事实连续缺失”。保留现有 follow-up 与 CAS 合同。入口：`agent_py_agent/agent/scheduler/active_run_closeout.py:settle_stale_waiting`、调用它的 reconcile/owner tick，以及 follow-up 结构化事实读取器。
- **故障注入用例**：将 wall clock 一次推进超过 grace，仍有 live/unknown follow-up executor 时不得 block/failed；模拟连续两次真实确认缺失并经过最短间隔，才允许按原 CAS 收口；测试 unreadable 宽限、并发 follow-up 到达时的 CAS；测试没有 resume 观测样本的冷启动仍依赖执行者事实，不误用单调时钟旧值。
- **估时**：约 **2–4 agent 小时**。

### 3.5 风险五：Adapter 外部发送成功、回执未持久化，接管后重复发消息

- **成立条件**：外部渠道已接受回复，但进程在 sent receipt 持久化前退出或写回执失败；原 claim 到期后新 worker 看不到 durable sent 标记而重发。若原 worker仍运行，epoch 可以拒绝旧写回，但不能撤销已经发出的消息。
- **实现证据**：`agent_py_agent/agent/adapter/delivery.py:241-288` 通过 owner/递增 epoch/expiry 领取，领取前查 `was_sent`；`:290-313` 对 owner/epoch 精确提交 claim 更新。`:1115-1148` 先调用 `_deliver_response`，返回 sent 后才调用 `_record_sent`；`:1231-1246` 写同 epoch 的 sent 终态，持久化 OSError 时记录“已发送但回执失败”。
- **现有保护**：稳定 pending id、claim owner/epoch/expiry、外发前 sent 检查、旧 epoch 不能覆盖新状态；发送成功后尽快写 terminal receipt。结构化本地 CAS 不等于各外部渠道接受稳定幂等键，不能证明该时间窗无重复消息。
- **修法与入口**：在现有 pending/sent store 中，于 `_deliver_response` 外发前按 `(stable_id, message_sequence, claim_epoch)` 持久记录 dispatch-started；新 claimant 看到 dispatch-started 且无 sent receipt 时，先核验旧 owner/attempt，再按渠道幂等键或只读查询 reconcile。若渠道不能查询/幂等，使用现有 `unknown` terminal disposition 并停止自动重发；本地 epoch CAS 不能撤回已经发出的网络请求。渠道支持时将稳定 message key 传给 provider。progress 与 final reply 使用不同 message operation identity。入口：`agent_py_agent/agent/adapter/delivery.py` 的 `claim`、`_process_record`、`_deliver_response`、`_record_sent`；由各渠道 adapter 实现稳定键或“不可查询则 unknown”的能力声明。
- **故障注入用例**：在 dispatch-started 写入前崩溃，恢复后可发送一次；写入 dispatch-started 后、外部响应前后分别崩溃，重启新 worker 必须先读 marker，旧 owner live/unverifiable 时不接管，dead 后对可幂等渠道使用同一 message key 查询/重试且只出现一条消息，对不可查询渠道收 `unknown` 且不自动重发；旧 epoch 迟到回执不能覆盖新状态；progress 和 final 分别各发一次。
- **估时**：约 **3–5 agent 小时**，依渠道 API 是否支持幂等/查状态调整。

## 4. 前两项可直接派工的实现切片

每片限定 2–4 agent 小时；共同验收口径为：用注入式 wall/monotonic 时钟和 fake process/provider，不等待真实 Mac 睡眠、不连接真实 Gateway 或外部渠道；测试以结构字段和副作用调用计数断言，不解析日志文案。

| 切片 | 估时 | 独占范围与产出 | 验收条件 | 依赖 |
| --- | ---: | --- | --- | --- |
| **SLP-1A 会话 claim 栅栏** | 3h | `agent_py_agent/agent/conversation/store_claims.py`、`agent_py_agent/agent/conversation/run_claim.py` 及其聚焦测试；把 expired+live/unverifiable 与 dead 分流，续租丢失产生结构化取消/失权事实，旧 epoch 不能再开始动作。 | 超租约但 owner 活着/不可验证均不会分配第二个可执行回合；dead proof 后只产生一个新 epoch；旧 claim 的 renew/finish/action 均不能改新状态。 | 无；为 SLP-1B 定义 attempt/epoch 传递字段。
| **SLP-1B 现有操作账本恢复栅栏** | 4h | `agent_py_agent/agent/tooling/executor.py`、`agent_py_agent/agent/tooling/tool_operation_coordinator.py`、`agent_py_agent/agent/runtime_db/managed_operation_store.py` 及聚焦测试；给现有请求/claim 加父 claim_epoch 栅栏，恢复同一持久化 ToolCall 时保留原 operation_id/idempotency_key，不新增账本或状态枚举。 | handler 前校验 claim epoch；同一 operation_id 已 succeeded 时只回放，unknown 只 reconcile、不自动再 dispatch；明确 safe_to_retry 才能按原幂等键续作；同参数的新逻辑调用必须有新 operation_id；操作账本不可用时副作用零启动。 | SLP-1A 确认 attempt/epoch 字段；对不能查询/幂等的 handler 保留现有 unknown 处理。
| **SLP-2A Scheduler claim 栅栏** | 3h | 只改 `agent_py_agent/agent/scheduler/repository.py` 与其聚焦测试；让普通 `claim_run` 与 process-death recovery 共用 structured owner 判定和 CAS epoch。**状态：已实现并挑入 17l；死亡证明后 claimed 转 queued、running 转 unknown（SLP-2B 前不自动重跑）。** | expired+live/unverifiable runner 不会被第二 worker 执行；dead proof 后先 CAS 为明确状态再有且仅有一个新 claim；旧 heartbeat/finish 不改变新 claim。 | 可与 SLP-1A 并行，文件范围不重叠。
| **SLP-2B Scheduler 操作身份接线** | 3h | 只改 `agent_py_agent/agent/scheduler/service.py` 的 scheduled action→existing ToolOperationCoordinator 身份接线及测试；不改 repository claim 逻辑或 SLP-1B 的 coordinator/store。 | 同一持久化 scheduled action 恢复时复用原 operation_id/idempotency_key；同一 run 的不同动作不互相去重；sleep/misfire 不多建 run；原 operation 是 unknown 时不自动重发。 | 依赖 SLP-2A、SLP-1B 完成并冻结接口。

可以先并行 SLP-1A 与 SLP-2A，两片分别只占 conversation 与 scheduler repository。SLP-1A 完成后做 SLP-1B；SLP-2B 等 SLP-2A 与 SLP-1B 的接口都确定后再做，仅改 scheduler service。不同切片不得同时改同一文件；若已存在的 ToolOperationCoordinator/managed operation store 能满足某项，必须复用，不能新建平行账本。

## 5. 跨风险验证矩阵与非目标

- **合成睡眠时钟**：分别推进 wall 与 monotonic；覆盖 wall 前跳、monotonic 不动、普通时间流逝、冷启动无前样本。校验日历 due/misfire 按既有墙钟合同，内部超时醒后重算，而 lease expiry 本身不能触发重放。
- **执行者状态**：fake PID/start time 区分 live、dead、reused、unverifiable；对 thread/attempt 另注入 heartbeat alive/lost。测试不把 PID 活着当作某一具体工具仍运行的充分证据。
- **副作用故障点**：分别在持久化意图前、意图后/dispatch 前、外部接受后/receipt 前、receipt 后崩溃；断言 provider 调用数、operation_id、receipt 和状态转换。未能证明“未发生”的路径必须落 unknown。
- **并发与旧 owner**：两个 waiter 同时领到期对象；尝试以旧 claim_id/epoch续租、启动调用、提交结果、收口状态；只能一个 current owner 生效。
- **自愈和用户可见结果**：验证 stale waiting、Gateway recovery、scheduler recovery 对 `unknown` 不重试、不报告为普通失败；恢复提示区分“已完成 / 明确未发生 / 待核对”，并且状态由宿主字段驱动。
- **非目标**：本设计不改模型网络流式/首包/重试预算（归 luna3）；不改变 scheduler 的日历表达或 misfire 政策；不做真实系统睡眠测试、不启动真实 Gateway、不修改产品配置，也不宣称本报告已找出那八个失败回合的现场根因。
