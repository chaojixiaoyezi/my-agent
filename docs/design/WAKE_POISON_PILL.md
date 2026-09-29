# 唤醒认领的毒丸处理

状态：**方案已审（2026-09-28，3a），分步实现中**（分支 `claude/75-wake-poison-design`）。
第 1 步（纯函数模块）、第 2 步（尝试账、结案、重放）和 `SkillSnapshotError` 的结构化 `error_code` 先做；
第 3 步起（接线、运维面、真实链路注入）要等 my-agent-3 这轮取消修复合入 main，因为接线会改 `background_claim.py`
和 runtime 的后台认领。第 11 节记录了 3a 对待定点的裁定。

## 1. 背景

今天有两个坏版本，表现都是同一条唤醒被反复领取、每次都失败或取消、永远重试：

- `204f4ddf9`：所有 session_task / session_message 唤醒在领取后被判成 `host_delivery_consumed`，走
  `_finish_nonexecuted_claim`（claim 记为 cancelled），唤醒留在 pending。ae 实测约 30 秒一次，一直不停。
- `7b83c8730`：把会话任务 id 当成回合的 task_id，技能快照 `scope_main_task_references` 抛
  `SkillSnapshotError("SKILL_TASK_BINDING_INVALID")`，被归为 programmer_bug。异常经 Gateway 车道冷却
  （`BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS = 30`）后重试，一直不停。

现有机制停不下来的原因（按 `f7a4cc909` 的代码）：

- 唤醒只有 pending / handled 两种状态。失败和"领到但没执行"都留在 pending。
- 所有重试节流都在进程内：`_wake_retry_after`（30 秒）、Gateway 车道冷却（30 秒）、供应退避（30→900 秒，
  只管瞬时供应错误）。没有按唤醒计的持久失败次数，Gateway 重启就清零。
- 持久失败账只有进度策略有（`_record_policy_failure`，连续 3 次退休）。Goal 续跑和定时任务在第一次
  非瞬时异常时就已结算（blocked / failed 并确认唤醒）。其余 reason（session_task、session_message、
  子代理生命周期、Audit、agent_event 等）没有任何上限。

## 2. 目标与边界

- 同一条唤醒如果**以同一结构化原因**连续失败 N 次，就结案成终态 `failed_permanently`，记下 reason_code
  和次数，不再被领取；同时发一条运维可见的结构化事件。
- 判定只看结构化事实（admission 码、异常类型、`error_code` 属性、报告字段），不读错误文案或模型正文。
- 不做：不改进度策略自己的退休账；不改 Goal / 定时任务首错即结算的行为；无唤醒的 observation 车道循环
  另做；不新增用户配置项。

## 3. 一次领取尝试的结果分类

| 结果 | 结构化来源 | 是否计数 | reason_code |
| --- | --- | --- | --- |
| 领取前跳过（冷却、恢复阻塞、本 tick 已占用、根任务已终态等） | `_skip_pending_wake_signal` | 不计数（没有尝试） | — |
| 没领到租约（车道忙） | `claims.acquire` 返回 None | 不计数 | — |
| 领到但未执行，预期等待类 | admission 为 `turn_interrupted`、`terminal_task_link`、`authority_recovery_required`、`wake_source_not_pending`、`wake_source_changed` | 不计数 | — |
| 领到但未执行，其它任何 admission（含以后新增的码，如 `host_delivery_consumed`） | `_finish_nonexecuted_claim` 的 admission | 计数 | `admission:<码>` |
| 执行中抛异常，瞬时类（见第 4 节） | 异常类型 | 不计数 | — |
| 执行中抛异常，其余 | 异常类型 / `error_code` 属性 | 计数 | 有 `error_code` 用 `error:<码>`，否则 `error:<category>:<异常类名>`（category 取自 `runtime_error_report`） |
| 执行完，报告为 None | `run_once` 返回 None | 计数 | `run:no_report` |
| 执行完，`wake_handled=False` 且没有冻结交付（下次会重跑业务） | 报告 + `cached_owner_delivery` | 计数 | `run:delivery_not_committed` |
| 执行完，`wake_handled=False` 但有冻结交付（下次只重投，不调模型） | 同上 | 不计入失败次数，单独按重投规则计（见第 5 节） | 结案时为 `delivery:channel_unavailable` |
| 上一次尝试时进程中途死亡，没有写下结果 | 尝试账的 in_flight 标记 + 进程身份已死 | 计数 | `attempt:abandoned` |
| 成功确认（`mark_handled`） | — | 清空尝试账 | — |

**同因**：同一 `wake_signal_id` 上相邻两次**计数**结果的 reason_code 完全相同。

- 不计数的结果既不累加，也不打断连续段：瞬时故障夹在中间，不会把程序错误的计数清零。
- 计数结果换了 reason_code，就从 1 重新计。
- 批次执行时，一次尝试对批内每个成员各记一次。

**总上限（3a 裁定保留）**：同一唤醒累计计数达到 M 次也结案，不要求同因。reason_code 取最后一次，
并标 `mixed_causes=true`。这是为了防止两个原因交替出现，逃过"连续同因"的判定。

预期等待类是已知码的优化清单。未知 admission 默认计数，符合"开放世界不封闭枚举"：新码不需要登记就会被兜住，
失败方向是多等几轮后停下，而不是无限重试。

## 4. 瞬时错误的排除（只按类型）

以下不计数：

- `is_provider_transient_error`：网络、超时、5xx，已由供应退避接管；
- `is_provider_quota_exhausted_error`：走额度分路；
- `is_model_configuration_unavailable`：等用户配置模型；
- `RuntimeConflictError` 全族：CAS 或资源占用冲突，包括锁冲突 `RuntimeExecutionBusyError`；
  其中 `RuntimeRecoveryRequiredError` 由恢复闸接管；
- `InterruptedError`、`BackgroundCompactSliceYield`：取消和压缩让出，本来就不算失败；
- 本地 IO 的 `BlockingIOError`、`TimeoutError`；
- `sqlite3.OperationalError` 仅当 `sqlite_errorcode` 是 SQLITE_BUSY 或 SQLITE_LOCKED。
  Python 3.10 没有这个属性，这时按计数处理。

其余一律计数，包括 programmer_bug、`DataCorruptionError`、`ValueError`。

误判的控制：N=5，加上退避一共约 7.5 分钟；瞬时类各有自己的退避；真误判了，一条人工重放命令就能恢复。

## 5. N 与退避（内部常量，不进配置）

- `WAKE_POISON_SAME_CAUSE_LIMIT = 5`；
- `WAKE_POISON_TOTAL_LIMIT = 12`（第 3 节的总上限）；
- `WAKE_POISON_BACKOFF_BASE_SECONDS = 30.0`，`WAKE_POISON_BACKOFF_MAX_SECONDS = 300.0`。

第 k 次计数失败后，下次最早尝试 = 失败时刻 + min(30·2^(k−1), 300) 秒。也就是 30、60、120、240 秒后
各重试一次，第 5 次同因失败即结案，从首次失败到结案约 7.5 分钟。

**只重投路径**（有冻结交付、不调模型）单独计：

- `WAKE_REDELIVERY_BACKOFF_BASE_SECONDS = 30.0`，`WAKE_REDELIVERY_BACKOFF_MAX_SECONDS = 900.0`：
  第 n 次重投失败后等 min(30·2^(n−1), 900) 秒，封顶 15 分钟；
- `WAKE_REDELIVERY_GIVE_UP_SECONDS = 86400.0`：从第一次重投失败起超过 24 小时，按
  `delivery:channel_unavailable` 结案，走同样的结案流程。
- 理由：重投便宜，但渠道长期不可用时无限重投同样不行；15 分钟封顶让恢复后最多再等 15 分钟，
  24 小时足够覆盖一次渠道长时间故障或人工处理。

取值理由：

- 比进度策略的 3 次宽：唤醒路径并发多（车道切换、来源刷新），要给类型没覆盖到的短暂竞态留余量；
- 比现在的"无限"严：`7b83c8730` 那种情况最多领取 5 次，而不是每 30 秒一次直到有人发现；
- 退避时间写进持久尝试账，Gateway 重启不会重置（现在的 `_wake_retry_after` 重启就清零）；
- 不做成配置：它是防死循环的安全兜底，不是用户偏好，和 `PENDING_TURN_INPUT_INVALIDATION_LIMIT` 一样是内部常量。

## 6. 持久化与结案

唤醒信封的内容是冻结的：`_stable_signal` 只放过 status、handled_at 和 owner_delivery，发布层只接受
pending 和 handled 两种状态。所以尝试账不能写进信封。

**尝试账**：`wake_queue/attempts/<wake_signal_id>.json`，是唯一权威。它挂在 WakeStore 的新增子对象上，
不改 `mark_handled` 的签名和行为（my-agent-3 重做第 8 片时可能会改到它附近）。

- 字段：reason_code、same_cause_count、total_count、first_failed_at、last_failed_at、next_attempt_at、
  in_flight（claim_id、owner_process、started_at）、last_error（异常类型、category、截断的诊断消息，只用于诊断）。
- 尝试开始前写 in_flight，结束后写结果并清掉 in_flight。这样进程在尝试中被杀，下一次也能识别成 `attempt:abandoned`。

**结案**为新终态 `failed_permanently`，顺序比照 `mark_handled`：

1. 先写 `wake_queue/quarantine/<id>.json`：原信封内容不变，status 改为 failed_permanently，结案判定
   （reason_code、次数、首末失败时间、last_error）放在顶层的 `quarantine` 键里。`_stable_signal` 只比较
   WakeSignal 的字段，顶层附加键不参与对账，所以发布层的冻结不变量不受影响；
2. 再删 pending 文件；
3. 最后把关联的 observation 标为已处理。这一步必须做：唤醒一离开 pending，
   `_observation_waits_for_linked_wake` 就会让 observation 车道接手。不结掉 observation，同一件事会从另一条路再跑一遍。

**发布层**（`store_wake_publication`）要认识第三个位置和新状态：

- 同一 dedupe_key 再发布时，返回已结案的原信号，不新开一代。同一个事件不能自己复活，只能人工重放；
- `publication_receipt` 返回 `failed_permanently`。

**领域收尾**走一个窄回调，和 `retire_source` 同类：

- session_task 唤醒：会话任务转 `failed`，原因码 `SESSION_TASK_WAKE_QUARANTINED`，并沿 `report_task_result`
  回报发送方。否则发送方会一直卡在 accepted，`7b83c8730` 里的 T1 就是这样；
- session_message 唤醒：对应的一次性回执转成结构化失败状态（`undeliverable` 加原因码），发送方的回执不能永远停在 pending；
- 其它 reason 不改领域状态，只发事件和宿主提示。

**调度侧**：`_skip_pending_wake_signal` 和 `ready_thread_ids` 都要尊重持久的 `next_attempt_at`，
与内存里的 `_wake_retry_after` 取较晚的一个。

## 7. 运维可见

- **日志**：每次计数失败打一行 `[background-wake-poison] {"event":"wake_attempt_failed", ...}`，字段为
  wake_signal_id、thread_id、reason、reason_code、same_cause_count、limit、next_attempt_at。结案时打
  `wake_quarantined`，另带 total_count、首末失败时间、error_type、error_category。
  日志只有结构化字段，不含消息正文或错误文案，和 `[gateway-supply-backoff]` 同一格式族。
- **宿主提示**：结案时给受影响的会话排一条宿主提示（`queue_host_notice`，source=`wake_poison`，
  code=reason_code），显示在下一次前台回复顶部，TUI 和飞书都能看到。
- **状态面**：`gateway_status` 工具和 TUI `/status` 显示本 owner 已结案的唤醒数；TUI `/wakes quarantined`
  列出结构化行（id、thread、reason、reason_code、次数、时间）；飞书提供同名管理员命令。

## 8. 人工重放

入口是 TUI `/wakes replay <wake_signal_id>` 和同名飞书管理员命令。只给管理员，只能操作本 owner，不开放给模型。

语义（在 WakeStore 内、同一把唤醒锁下完成）：

1. 读结案记录，核对 owner 和 thread；
2. 领域已是终态时拒绝，返回 `WAKE_REPLAY_DOMAIN_TERMINAL`。例如会话任务已按第 6 节转成 failed，
   这时应重新派活，而不是重放；
3. 用原 wake_signal_id 和冻结内容写回 pending 队列，发布层的不变量保持成立；
4. 结案记录移到 `quarantine/replayed/<id>-<n>.json` 留档；尝试账清零，replay_count 加 1；
5. 打一条 `wake_replayed` 事件。

重放后如果还是同因失败，会再次在 N 次后结案，不会自动复活。

结案记录不自动删除。在现有 6 小时一次的账本整理周期里，超过 14 天的记录移入归档，只移动、不删除。

## 9. 和现有兜底的关系

| 机制 | 作用范围 | 计数的寿命 | 触发后 |
| --- | --- | --- | --- |
| `PENDING_TURN_INPUT_INVALIDATION_LIMIT = 8` | 一个回合内，因待处理输入而作废回复的次数 | 回合内存 | 结束本回合，turn_end_reason 为 `pending_turn_input_invalidation_limit` |
| 同一失败 15 次熔断（`repeated_failure_halt_threshold`） | 一个回合内，同工具、同参数、同错误码 | 回合内存 | 结束本回合（unfinished，不可自动续跑），回合照常出报告 |
| 进度策略退休（3 次） | 一条 progress policy | 持久（policy metadata） | 停用该 policy |
| 本方案 | 一条持久唤醒，跨回合、跨重启 | 持久（尝试账） | 唤醒结案 `failed_permanently` |

- **分层**：前两个限制的是"一个回合内"的模型和工具调用次数，本方案限制的是"同一条唤醒被领取几次"。
  回合内兜底只能让本回合停下，下一个 tick 又会起新回合，本方案补的就是这一层。
- **不重复计数**：
  - 回合被 15 次熔断结束时，会照常出报告并确认唤醒，本方案不计数；
  - 回合被作废上限结束、唤醒没确认时，本方案记一次 `run:delivery_not_committed`（或 `run:no_report`），
    连续 5 次即结案。
- **不经过唤醒的路径**：进度策略不走 WakeSignal，继续用它自己的 3 次退休账。Goal 续跑和定时任务第一次
  非瞬时失败就已结算，本方案对它们实际不会触发，行为不变。

## 10. 落地顺序与测试

1. **纯函数模块** `conversation/wake_poison.py`：负责结果分类、连续段、退避和上限。
   合同单测覆盖：瞬时类型逐个检查、未知 admission 默认计数、原因交替、中间夹瞬时故障不打断。
2. **WakeStore**：实现尝试账、结案、重放，发布层认识第三个位置。存储单测覆盖：结案和重放前后信封冻结内容不变、
   同键再发布返回已结案的原信号、observation 同步结掉。
3. **接线**：`run_claimed` 把未执行的 admission 结构化地返回给调用方（现在只写进 claim 文件），runtime 在
   批次消费处记账。这一步改 `background_claim.py`，是 my-agent-3 正在改的区域，要等它合入。
4. **运维面**：日志、宿主提示、`/wakes` 两个子命令（TUI 和飞书）、状态计数。
5. **真实链路门**：在 ae 的 `claude/ae-session-real-chain-test` 里加两个注入：
   - 技能快照抛 programmer_bug，复现 `7b83c8730`；
   - admission 返回未知码，复现 `204f4ddf9`。

   断言：5 次后结案、发送方收到失败回报、之后没有新的 claim。
   正向对照：供应瞬时失败连续 20 次不结案；失败 4 次后成功则清账。
   变异点：不计数清单、同因比较、退避写盘、observation 结案、发布层第三位置。

## 11. 待定点的裁定（2026-09-28，3a）

1. 总上限 M=12（不同因也算）：**保留**。
2. 领域收尾：**覆盖 session_task 和 session_message**（见第 6 节），其它 reason 只发事件和宿主提示。
3. 只重投路径：**单独设上限**，指数退避封顶 15 分钟，首次重投失败起超过 24 小时按
   `delivery:channel_unavailable` 结案（见第 5 节）。
4. `SkillSnapshotError` 整族的码**补成结构化 `error_code` 属性**，现有调用方和测试改读该属性；单独一片，
   技能快照模块不在 my-agent-3 的范围内，可以先做。
