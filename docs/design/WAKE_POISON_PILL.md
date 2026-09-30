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
| 领到但未执行，预期等待类 | admission 为 `turn_interrupted`、`terminal_task_link`、`authority_recovery_required`、`wake_source_not_pending`、`wake_source_changed` | 不计数 | `admission:<码>`（只用于长时间不计数提醒） |
| 领到但未执行，其它任何 admission（含以后新增的码，如 `host_delivery_consumed`） | `_finish_nonexecuted_claim` 的 admission | 计数 | `admission:<码>` |
| 执行中抛异常，瞬时类或环境级故障（见第 4 节） | 异常类型 / HTTP 状态 | 不计数 | 与下一行同一规则（只用于提醒） |
| 执行中抛异常，其余 | 异常类型 / `error_code` 属性 | 计数 | 有形状合规的 `error_code` 用 `error:<码>`，否则 `error:<category>:<异常类名>`（category 取自 `runtime_error_report`）；有结构化 HTTP 状态时再加 `:http_<状态>` |
| 一次尝试执行了多条唤醒（批大小 > 1），结果是上面任何一种计数失败 | 批大小 | 不计数，记为批次失败，下一次逐条单独执行；同时推进连续不计数段 | 同上（`batch_failures` 加 1） |
| 执行完，报告为 None | `run_once` 返回 None | 计数 | `run:no_report` |
| 执行完，`wake_handled=False` 且没有冻结交付（下次会重跑业务） | 报告 + `cached_owner_delivery` | 计数 | `run:delivery_not_committed` |
| 执行完，`wake_handled=False` 但有冻结交付（下次只重投，不调模型） | 同上 | 不计入失败次数，单独按重投规则计（见第 5 节） | 结案时为 `delivery:channel_unavailable` |
| 上一次尝试时进程中途死亡，没有写下结果 | 尝试账的 in_flight 标记 + 进程身份已死 | 计数 | `attempt:abandoned` |
| 成功确认（`mark_handled`） | — | 清空尝试账 | — |

**同因**：同一 `wake_signal_id` 上相邻两次**计数**结果的 reason_code 完全相同。

- 不计数的结果既不累加，也不打断连续段：瞬时故障夹在中间，不会把程序错误的计数清零。它们组成"连续不计数段"，
  满 24 小时发提醒（见第 5 节末尾），但永不结案。
- **批次隔离**（2026-09-29 裁定，消息队列隔离毒消息的常规做法）：批次失败只让下一次逐条单独执行
  （`needs_isolation`），只对批大小为 1 的失败计数，避免一条有毒的内容拖着同批健康成员一起结案；
  直到成功为止都保持逐条执行。批次中途进程死亡同样按批次失败记。
- 计数结果换了 reason_code，就从 1 重新计。
- 批次执行时，一次尝试对批内每个成员各记一次（计数失败改记为批次失败）。

**总上限（3a 裁定保留）**：同一唤醒累计计数达到 M 次也结案，不要求同因。reason_code 取最后一次，
并标 `mixed_causes=true`。这是为了防止两个原因交替出现，逃过"连续同因"的判定。

预期等待类是已知码的优化清单。未知 admission 默认计数，符合"开放世界不封闭枚举"：新码不需要登记就会被兜住，
失败方向是多等几轮后停下，而不是无限重试。

## 4. 瞬时错误的排除（只按类型）

以下不计数：

- `is_provider_transient_error`：网络、5xx、限流（含 `ProviderUsageLimitError`），已由供应退避接管；
- `ProviderTimeoutError`：供应超时，属于网络类；
- `compact_guard.is_provider_quota_failure`：额度用完，含大线程压缩调用撞额度的 `ConversationCompactError` 包装，走额度分路；
- `is_model_configuration_unavailable`：等用户配置模型；
- `RuntimeConflictError` 全族：CAS 或资源占用冲突，包括锁冲突 `RuntimeExecutionBusyError`；
  其中 `RuntimeRecoveryRequiredError` 由恢复闸接管；
- `InterruptedError`、`BackgroundCompactSliceYield`：取消和压缩让出，本来就不算失败；
- 本地 IO 的 `BlockingIOError`、`TimeoutError`；
- `sqlite3.OperationalError` 仅当 `sqlite_errorcode` 是 SQLITE_BUSY 或 SQLITE_LOCKED。
  Python 3.10 没有这个属性，这时按计数处理。

**环境级故障也不计数**（2026-09-29 裁定，二次复审补 402、404）：HTTP 401、402、403、404、407（按 `provider_error_http_status`
的整数属性判断），以及 `ProviderConfigurationError` 基类（含 `ProviderConnectionError`、`ModelNotConfiguredError`）。
密钥过期、账户欠费、端点或模型配错、代理鉴权失败是环境坏了，不是某条唤醒有毒；否则环境坏 7.5 分钟就会把健康的
session_task 不可逆地判成 failed。`ProviderRequestRejectedError` 在 400、413、422 这类请求本身有问题时照样计数。
判定的唯一权威是 `backends/errors.is_provider_environment_fault`，毒丸和 Gateway 车道共用。
**按车道暂停**（第 3 步第 4 点，C5 已接线，分支 `claude/9b-lane-env-pause`）：这些故障在 `cli/gateway_lane_retry.py`
里不再按 30 秒冷却重试，而是按车道暂停到两件事之一发生：会话的模型指纹变了（`thread_model_fingerprint`：
model_profile_id、model_selection_revision 与所选模型连接字段的进程盐摘要，只读、不构造后端、不联网），立即放行；
或者到了探测时刻（60 秒起、每次探测再失败翻倍、封顶 900 秒，内部常量），放行一次真实尝试，成功由车道成功清掉暂停；
取指纹出错按未知处理，只等探测时刻，暂停和翻倍保持。
`ModelNotConfiguredError` 与模型引用失效仍先走"等模型配置"分支。额度用完在车道这层也显式并入暂停（3a 裁定；共享判定本身不含额度，
毒丸侧额度按瞬时类不计数）。车道、毒丸、持久策略失败账和唤醒额度分路判额度都只读 `compact_guard.is_provider_quota_failure`，
压缩调用撞额度的包装也算。暂停只在进程内，重启即清；暂停和恢复各打一行
`[gateway-lane-retry]`。对应的唤醒尝试照旧记不计数，满 24 小时由连续不计数提醒兜底。

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
- `WAKE_REDELIVERY_GIVE_UP_SECONDS = 86400.0`：从第一次重投失败起满 24 小时（>=），按
  `delivery:channel_unavailable` 结案，走同样的结案流程。
- 理由：重投便宜，但渠道长期不可用时无限重投同样不行；15 分钟封顶让恢复后最多再等 15 分钟，
  24 小时足够覆盖一次渠道长时间故障或人工处理。

**批次失败**后 30 秒（基础间隔）再试，下一次逐条单独执行。

**连续不计数的远端提醒**（2026-09-29 裁定）：`WAKE_UNCOUNTED_STALL_SECONDS = 86400.0`，与只重投路径同一个窗口。
同一条唤醒连续不计数满 24 小时（>=）时发一次运维事件 `wake_uncounted_stalled`（带最近一次不计数的 reason_code、
次数和起点），并给受影响的会话排一条宿主提示；之后每满 24 小时再提醒一次。**不自动结案**：这些结果按定义属于
预期等待、瞬时或环境故障，长时间故障里自动结案会误伤健康的工作。任何一次计数失败都结束当前不计数段（连同提醒记录），
成功清空一切；只重投失败不影响它；批次失败同样不计数，也推进这一段（一直批次失败满 24 小时照样提醒）。领取前就被跳过的时段不算尝试，也不参与这个计时。

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

- 字段：reason_code、same_cause_count、total_count、first_failed_at、last_failed_at、batch_failures、
  uncounted_reason_code、uncounted_count、uncounted_since、stall_alerts、last_stall_alert_at、只重投三项、next_attempt_at，
  in_flight（claim_id、batch_size、turn_id、owner_process、started_at；turn_id 为这一片的精确回合号，缺省为空串）、last_error（异常类型、category、error_code、截断的诊断消息，
  只用于诊断）。读回时严格校验：未知键、NaN/inf、计数与时间互相矛盾都按数据损坏处理。
- `record` 在同一把锁里判定并记下长时间不计数提醒，随结果返回，调用方负责发事件与宿主提示，同一窗口只提醒一次。
- 时间只往前走：本机时钟回拨时，失败、只重投和提醒的"最近一次"时间取 max(当前时间, 已记的时间)，首次时间不会晚于
  最近一次，账始终读得回来（二次复审：回拨 60 秒曾让下一次读账报数据损坏）。
- 计数失败、批次失败、只重投失败必须带非空原因码，空原因抛 ValueError；`begin` 与 `record` 写账前都按读回同一口径
  校验（`ensure_writable_state`），状态不自洽时抛 ValueError、原账不动，不会写出一份读不回来的账。
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

**领域收尾**（第 3 步 C4 已实现，`conversation/wake_domain_closeout.py`，与人工重放的"领域已是终态"判定同一处）：

- session_task 唤醒：会话任务转 `failed`，`failure_code` 为 `SESSION_TASK_WAKE_QUARANTINED`，并沿 `report_task_result`
  回报发送方（metadata 带 `session_task_failure_code`）。否则发送方会一直卡在 accepted，`7b83c8730` 里的 T1 就是这样；
- session_message 唤醒：**回执不动**，只留宿主提示，`details` 带 `message_dedupe_key` 与回执状态注明仍待投递。
  这一条原先设想为"回执转 `undeliverable` 加原因码"，之后的裁定 b 改为"只转 rejected、不带原因"，**2026-09-29 3a 以做法 2 取代裁定 b**：
  毒丸隔离的是唤醒这条执行路径，不代表消息本身有毒；方案 A 与后台片收尾之后，被隔离唤醒的消息是 pending，仍能交给目标的下一次
  回合，消息的去留只由回执层上限（`SESSION_MESSAGE_RELEASE_LIMIT_REACHED`）决定；
- 其它 reason 不改领域状态，只发事件和宿主提示；
- 信封读不出时，用选批时冻结的副本收尾（裁定 d）；
- 宿主提示来源 `wake_poison`、code 为原因码，同一会话同一原因码只留最新一条；长时间不计数提醒同样留提示；
- 派活正文达到释放上限被放弃时，领取后准入判 `session_task_body_abandoned`，先收任务（failed，`failure_code` 为
  `SESSION_MESSAGE_RELEASE_LIMIT_REACHED`）并回报，再结案唤醒。

**调度侧**：`_skip_pending_wake_signal` 和 `ready_thread_ids` 都要尊重持久的 `next_attempt_at`，
与内存里的 `_wake_retry_after` 取较晚的一个。

## 7. 运维可见

- **日志**：每次计数失败打一行 `[background-wake-poison] {"event":"wake_attempt_failed", ...}`，字段为
  wake_signal_id、thread_id、reason、reason_code、same_cause_count、limit、next_attempt_at。结案时打
  `wake_quarantined`，另带 total_count、首末失败时间、error_type、error_category。
  日志只有结构化字段，不含消息正文或错误文案，和 `[gateway-supply-backoff]` 同一格式族。
- **长时间不计数**：`wake_uncounted_stalled` 事件（reason_code、uncounted_count、uncounted_since、alert_number），同时给
  受影响的会话排一条宿主提示；每满 24 小时重复一次，不结案。
- **宿主提示**：结案时给受影响的会话排一条宿主提示（`queue_host_notice`，source=`wake_poison`，
  code=reason_code），显示在下一次前台回复顶部，TUI 和飞书都能看到。
- **状态面**：`gateway_status` 工具和 TUI `/status` 显示本 owner 已结案的唤醒数；TUI `/wakes quarantined`
  列出结构化行（id、thread、reason、reason_code、次数、时间）；飞书提供同名管理员命令。
  第 4 步已落地（分支 `claude/be-wake-ops`，待复审）：
  - `/status` 多一行「已结案的后台唤醒：N 条」，为 0 时不显示；`gateway_status` 返回 `quarantined_wakes`。
    两处都只数结案目录里的文件（`attempts.quarantined_count()`），不读内容，不含已归档的。
  - `/wakes` 与 `/wakes quarantined` 列出结构化行（id、会话、reason、原因码、同因与总次数、结案时间、已重放次数），
    按结案时间倒序最多 30 条；读不出的只报条数和留档位置。
  - 原因码是开放集合：已知码与 `admission:`、`error:` 前缀带中文短说明，认不出的只显示原码。
  - TUI 与飞书共用 Gateway 控制入口（`gateway_parts/wake_ops_control.py`），仅管理员，不开放给模型。

## 8. 人工重放

入口是 TUI `/wakes replay <wake_signal_id>` 和同名飞书管理员命令。只给管理员，只能操作本 owner，不开放给模型。

语义（在 WakeStore 内、同一把唤醒锁下完成）：

1. 读结案记录，核对 owner 和 thread；
2. 领域已是终态时拒绝，返回 `WAKE_REPLAY_DOMAIN_TERMINAL`。例如会话任务已按第 6 节转成 failed，
   这时应重新派活，而不是重放；
3. 用原 wake_signal_id 和冻结内容写回 pending 队列，发布层的不变量保持成立；
4. 结案记录移到 `quarantine/replayed/<id>/<n>.json` 留档（每条唤醒一个子目录，避免 ID 前缀互相匹配；
   留档个数就是重放次数的唯一权威），尝试账清零；
5. 打一条 `wake_replayed` 事件。

重放后如果还是同因失败，会再次在 N 次后结案，不会自动复活。

命令面（第 4 步）：`/wakes replay <ID>` 只读预览，`/wakes replay <ID> confirm` 才重放。预览和确认前做同一组核对：
- 来源状态只读 `attempts.replay_source`：已归档返回 `WAKE_REPLAY_ARCHIVED`，信封或结案记录读不出返回 `WAKE_REPLAY_SOURCE_UNREADABLE`，
  都没有返回 `WAKE_REPLAY_NOT_FOUND`；
- 待处理队列已有同 ID 唤醒返回 `WAKE_REPLAY_PENDING_CONFLICT`；
- 领域已结束返回 `WAKE_REPLAY_DOMAIN_TERMINAL`：会话任务唤醒看 `metadata.session_task_id` 对应任务是否已是终态，
  会话消息唤醒看 `metadata.message_dedupe_key` 对应回执是否 consumed/rejected（没带键的旧唤醒按未结束处理），其它 reason 不检查。
  这是本地版本，第 3 步 C4 的 `wake_domain_closeout.wake_domain_terminal` 落地后改成导入它，只留一个权威。
确认时 store 在结案锁里重新核对一遍。`wake_replayed` 事件带 wake_signal_id、thread_id、reason、replay_count。
拒绝码与 `WAKE_OPS_ADMIN_ONLY` 都登记在 `ERROR_CONTRACTS`。

结案记录不自动删除。在现有 6 小时一次的账本整理周期（`ConversationStore.gc_stale_ledger_records`）里，超过 14 天的记录移入归档，
只移动、不删除（`conversation/store_wake_quarantine_archive.py`）：
- 顶层结案记录按 `quarantine.quarantined_at` 判断（读不出或缺字段的按文件时间），移到 `quarantine/archive/<id>.json`；
  它的坏账留档 `ledger/<id>.json` 随记录一起移到 `archive/ledger/`，有记录对应的坏账不会单独先走。
- 读不出的信封 `unreadable/<id>.json` 和没有记录对应的孤儿坏账按 max(mtime, ctime) 判断：结案时是 os.replace 移过去的，
  mtime 还是原文件最后写入的时间，会比结案早很多。
- `replayed/` 不动，它是重放次数的唯一权威。归档位置已有同名文件时不覆盖；文件名不是合法唤醒 ID 的杂项文件不碰。
- 归档只换物理位置、不改发布语义：`archive/<id>.json` 是发布层的第四个只读安装位置（`_installed_signal`、
  `_read_existing_identity`），同键再发布仍返回原结案信号，回执仍是 failed_permanently，不会抛「published wake signal is missing」。
  重放对已归档的记录拒绝（`WAKE_REPLAY_ARCHIVED`）。「14 天后同键可以重新发布」没有采用：那要连去重回执和观察配对一起动，
  还会让 `thread-goal:<goal_id>` 这类复用键自己复活，和上面「只能人工重放」冲突；要放开需另立设计项。
- 列表和计数不含已归档的。会话删除一并清理 `archive/` 顶层带 thread_id 的记录；`archive/ledger/`、`archive/unreadable/`
  与原来的 `ledger/`、`unreadable/` 一样无法归属会话，也没有保留上限，由运维清理。
- **运维删除归档记录的正确做法**：不能单独删 `archive/<id>.json`。它是发布层的只读安装位置，单删之后只要 `wake_queue/dedupe`
  里同键的回执还在，同键再发布和查回执（`raise_signal`、`delivery_receipt`）都会抛 `DataCorruptionError("published wake signal is missing")`。
  要清掉一条，必须和它同键的 `wake_queue/dedupe` 回执一起删；两者都删掉之后，同键再发布才会生成新一代信号。

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
- **不经过唤醒的路径**：进度策略不走 WakeSignal，继续用它自己的 3 次退休账；按同一原则，环境级故障与额度用完不记这本账、
  不因此退休（3a 2026-09-29 裁定，`background_claim._counts_as_policy_failure`）。Goal 续跑和定时任务第一次
  非瞬时失败就已结算，本方案对它们实际不会触发，行为不变。

## 10. 落地顺序与测试

1. **纯函数模块** `conversation/wake_poison.py`（已完成）：负责结果分类、连续段、退避和上限。
   合同单测覆盖：瞬时类型逐个检查、未知 admission 默认计数、原因交替、中间夹瞬时故障不打断。
2. **WakeStore**（已完成，`store_wake_attempts.py` 挂在 `store.wakes.attempts`）：实现尝试账、结案、重放，发布层认识第三个位置。存储单测覆盖：结案和重放前后信封冻结内容不变、
   同键再发布返回已结案的原信号、observation 同步结掉。
2b. **第 3 步 C1（已完成，`claude/38-wake-poison-c1`）**：`wake_poison` 加六个新原因常量、`WakeAttemptFacts` 与 `verdict_for_attempt`（判定总表：异常 → 额度分路 → 只重投 → 领到没执行 → 取消/让出 → 无报告 → 已处理仍 pending → 报告规则）、`ledger_corrupt_decision`；`store_wake_attempts` 加 `preflight`（死进程在途尝试：带 stopping_at 记不计数的 gateway_stopped，否则 abandoned）、`has_ledger`、`mark_stopping`、`discard`，`quarantine` 处理读不出的信封（原字节移到 quarantine/unreadable/）与坏账（原字节留到 quarantine/ledger/），`quarantined()` 列出留档，`replay` 对读不出来源返回 WAKE_REPLAY_SOURCE_UNREADABLE；WAKE_REPLAY_* 四个码登记进 ERROR_CONTRACTS。
3. **接线**（C2–C4 与 C6 已完成，2026-09-30 随 step16m 上线）：
   - C2 `background_claim.py`：`BackgroundClaimDependencies.attempt`（`BackgroundClaimAttempt` 协议：`begin(claim_id) -> str`、
     `settled(status, admission)`）。领到 claim 后先 `begin`，返回非空码时本片不执行、按该码结算 claim；片结束时回调
     结束方式（`WAKE_CLAIM_FINISHED/CANCELLED/YIELDED/NOT_EXECUTED`）与领取后的准入码。
   - C3 新模块 `conversation/wake_attempt_tracking.py`：`track_wake_attempt` 包住批次消费（含执行后的确认与兄弟唤醒结案），
     退出时逐条记账：成功清账、唤醒已离开 pending 删账（`discard`，与停机线程的 `mark_stopping` 同一把账锁）、
     begin 被拦下的成员结案、其余按 `verdict_for_batch(verdict_for_attempt(...))` 记账，到上限按 `failed_permanently` 结案，
     连续不计数满窗口记 `wake_uncounted_stalled`。记账时刻 = 这一拍的 `now` + 单调时钟经过的时长，与跳过阶段同一时间基准。
   - C3 调度侧：跳过阶段先 `has_ledger` 再 `preflight`（死进程的在途尝试在这里补记；尝试账读不出按 `attempt:ledger_corrupt`
     结案、坏账保留），`next_attempt_at` 未到就跳过；就绪扫描用只读的同一判据，两边不会一个放行一个跳过。
     批次里有成员带尝试账时逐条执行（`isolate_wake_batch`）。
   - C3 在途记录持久化这一片的精确回合号（`WakeAttemptStart.turn_id`，与 `_run_params` 注入补充消息时同源：定时 run、派活、
     会话消息唤醒），`inflight_attempts()` 的条目也带上。进程中途死亡时 C6 在 preflight 里按它收尾那一片认领的补充消息
     （`reject_pending(turn_id, reject_reserved=True)`），见 DESIGN_LEDGER 会话消息方案 A 条目。
   - 记账失败不影响执行结果：写账 `OSError` 只打日志；执行路径的原异常原样上抛。日志为 `[background-wake-poison]` 前缀的
     一行 JSON：`wake_attempt_failed`、`wake_quarantined`、`wake_uncounted_stalled`、`wake_attempt_facts_invalid`、
     `wake_attempt_ledger_unwritable`。
   - C4 `wake_domain_closeout.py`：结案后的领域收尾与宿主提示、派活正文已放弃的收尾、长时间不计数提醒的提示、
     `wake_domain_status`（领域终态状态，没结束为空串）与 `wake_domain_terminal`（第 6 节）。`background_claim` 加可选依赖 `close_out_source`，来源已处理完或已放弃时先收领域再结案。
   - C6（已完成，step16m，3a 接手 38 的范围）：
     - **优雅停机**：后台 supervisor 的 `shutdown()` 关执行池之前调 `mark_inflight_attempts_stopping`，对 `inflight_attempts()`
       逐条 `mark_stopping`；写失败逐条吞掉、只打 `wake_attempt_ledger_unwritable`（stage=mark_stopping），绝不让停机失败。
       下次 preflight 看到标记按不计数的 `attempt:gateway_stopped` 补记。
     - **死进程回合收尾**：preflight/begin 识别出上一片随进程消失时，`WakeAttemptOutcome.abandoned_turn_id` 带出在途记录里持久化
       的回合号，`wake_domain_closeout.settle_abandoned_turn` 按它 `reject_pending(turn_id, reject_reserved=True,
       release_task_body=<任务没被取消>)`，与后台片异常结束同一套收尾：会话消息退回、steer 拒绝、已提交不动。
       进程死亡没有异常对象，这次释放不计次；反复死亡由本步按 `attempt:abandoned` 计数结案兜底。旧账没有回合号时不收尾。
     - **结案顺序收窄**：写结案记录 → 删 pending → 移坏账 → 删尝试账。任何一步之后崩溃都不会留下"pending 在、账已不在"
       而从零重新计数，最多留下一份孤儿坏账。
4. **运维面**（第 4 步，be，已完成，2026-09-30 随 step16m 上线）：`/wakes` 两个子命令（TUI 和飞书）、状态计数、归档、`wake_replayed`。日志与宿主提示已随第 3 步落地。
5. **真实链路门**（待做；现在只有 `test_wake_poison.py` 用纯函数重放这两次事故）：在 ae 的 `claude/ae-session-real-chain-test` 里加两个注入：
   - 技能快照抛 programmer_bug，复现 `7b83c8730`；
   - admission 返回未知码，复现 `204f4ddf9`。

   断言：5 次后结案、发送方收到失败回报、之后没有新的 claim。
   正向对照：供应瞬时失败连续 20 次不结案；失败 4 次后成功则清账。
   变异点：不计数清单、同因比较、退避写盘、observation 结案、发布层第三位置。

**第 3 步接线时必须处理的四点**（2026-09-29 复审；第 4 点为二次复审补充）：

1. **额度耗尽的回退报告**要单独识别（`_quota_wake_report_after_error` 产出的报告），不能记成
   `run:delivery_not_committed`；它走额度分路，按不计数处理。
2. **优雅停机**（C6 已接线，见第 3 步清单）：Gateway 正常停止时给在途尝试打停机标记，下次 preflight 记不计数的
   `attempt:gateway_stopped`，部署不再给在途唤醒记 `attempt:abandoned`。只有进程被强杀、来不及收尾时才出现 abandoned。
3. **信封读不出来时的结案**：现在 `quarantine` 遇到读不出的 pending 文件抛数据损坏、原文件不动。接线时要决定：
   建议把原始字节原样移到 `quarantine/unreadable/<id>.json`，发 `wake_quarantined`（reason_code
   `admission:wake_source_unreadable`），列表把它放进 load_errors；不在读不出的内容上补字段。
4. **环境级故障按车道暂停**（C5 已接线，见第 4 节）：401/402/403/404/407、连接与配置错误不再按车道冷却每 30 秒重试，
   改为按车道暂停，模型指纹变化或探测成功后再放行，不再对每条唤醒反复尝试。

另外三条接线约定（二次复审）：

- 尝试账本身读不出（`DataCorruptionError`）时，这条唤醒按 `attempt:ledger_corrupt` 结案，坏账原样保留供排查，
  不清零重来（清零会让毒丸重新获得无限次机会）；
- `record()` 放在 `finally` 里，执行路径抛出任何异常都要记下这次尝试的结果并清掉 in_flight；
- 宿主提示按会话和 reason_code 合并：同一会话里同一原因的结案或提醒只留最新一条，不刷屏。

## 11. 待定点的裁定（2026-09-28，3a；2026-09-29 复审补充）

1. 总上限 M=12（不同因也算）：**保留**。
2. 领域收尾：**覆盖 session_task 和 session_message**（见第 6 节），其它 reason 只发事件和宿主提示。
3. 只重投路径：**单独设上限**，指数退避封顶 15 分钟，首次重投失败起满 24 小时（>=）按
   `delivery:channel_unavailable` 结案（见第 5 节）。
4. `SkillSnapshotError` 整族的码**补成结构化 `error_code` 属性**，现有调用方和测试改读该属性；单独一片，
   技能快照模块不在 my-agent-3 的范围内，可以先做。
5. **环境级故障不计数**（9a 复审，3a 裁定必须改；二次复审补 402、404）：401/402/403/404/407 与配置错误基类不计数；按车道暂停已由第 3 步 C5 接线（第 4 节）。
6. **批次隔离**（采纳）：批次失败后逐条单独执行，只对批大小为 1 的失败计数（第 3 节）。
7. **不计数的远端上限**（采纳，但不自动结案）：连续不计数满 24 小时发 `wake_uncounted_stalled` 并每 24 小时
   提醒一次（第 5 节）。
