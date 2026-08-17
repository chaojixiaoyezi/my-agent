# wake_intent 调度实现规格（#233）

> 状态：设计定稿（群复核 seq2353-2411 收敛，含维护记录 2406/2409 + 大橘 2411 全部规格点），待实施。
> 根因：1.10 主 owner 525 条 gateway 对话任务（8/10-8/16 累积）完成/中断后未终态化，
> 卡 `status=active` 被唤醒轮无差别唤醒 → 重复 provider 调用。
> 治本：**移除「active 扫描 = 唤醒资格」，改为「单一权威 wake_intent 状态机 + 意图驱动调度」**。

## 1. 数据模型（wake_intents 表）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `intent_id` | TEXT PK | ✅ | 全局唯一（规范 ID 生成器） |
| `dedup_key` | TEXT UNIQUE | ✅ | 见 §3——同一来源同一 due window 只允许一条 intent |
| `task_id` | TEXT | ✅ | 目标任务（conversation task / run task） |
| `run_id` | TEXT | 可空 | 关联 run（未绑定 run 的初始化 intent 可空） |
| `parent_run_id` | TEXT | 可空 | 重试/恢复链的父 run |
| `root_run_id` | TEXT | 可空 | 整条执行链根 run |
| `attempt_id` | TEXT | 可空 | 当前/最近 attempt 的**受控 ref**（dispatcher claim 后落，**不是 dedup 输入**） |
| `owner_id` | TEXT | ✅ | 归属 owner（隔离/配额） |
| `execution_mode` | TEXT | ✅ | `interactive` / `async` |
| `source` | TEXT | ✅ | 受控枚举：`inbound` / `cron` / `heartbeat` / `sleep` / `subagent_completed` / `provider_recovery` / `interrupted_recovery` |
| `wake_reason` | TEXT | ✅ | 受控枚举（§5），解释「为什么醒」 |
| `source_event_id` | TEXT | 可空 | 触发来源事件 ID（幂等/审计） |
| `retry_event_id` | TEXT | 可空 | **producer 生成的重试来源事件 ID**——重试必须新来源事件 → 新 dedup_key（§3） |
| `retry_generation` | INTEGER | 可空 | 重试代次（同源链上的第几次重试） |
| `provenance_ref` | TEXT | 可空 | 来源产物/证据引用（**只存 ref，不存正文/payload**） |
| `provider_scope_ref` | TEXT | ✅ | provider route/key 的受控引用（从 policy/run ledger 解析，纳入 circuit 判定与冻结） |
| `continuation_policy` | TEXT | ✅ | `interactive` / `async`——授权证明，不能从 active 推导 |
| `policy_generation` | INTEGER | ✅ | 策略代次；**有效 = 匹配当前 policy ledger 且未撤销**（非仅存在） |
| `payload_schema_version` | TEXT | ✅ | intent 载荷 schema 版本 |
| `priority` | INTEGER | ✅ | 调度优先级（默认 0） |
| `not_before` | REAL | 可空 | **原因/审计字段**：由写入方折叠进 `next_wake_at`，不作为第二时钟 |
| `next_wake_at` | REAL | ✅ | 到期时间（Unix ts）——**due 的唯一权威事实**（写入方把 not_before/retry_after 折叠为 max(not_before, now+retry_delay)） |
| `due_window` | TEXT | ✅ | 到期窗口标签（§3），参与 dedup |
| `retry_after` | REAL | 可空 | **原因/审计字段**：记录 429/quota 恢复来源，折叠进 `next_wake_at`，不作为独立调度判据 |
| `expires_at` | REAL | 可空 | intent 硬过期（超时未 claim → expired） |
| `status` | TEXT | ✅ | `pending` / `claimed` / `handed_off` / `cancelled` / `expired`（§2） |
| `claim_generation` | INTEGER | ✅ | 租约/claim 代次（claim 递增；旧租约回收后防误认领） |
| `claim_token` | TEXT | 可空 | 原子 claim 令牌（CAS 用） |
| `lease_owner` | TEXT | 可空 | 持有 lease 的 gateway/执行席 |
| `lease_until` | REAL | 可空 | lease 过期时间 |
| `claimed_at` | REAL | 可空 | claim 时间 |
| `finished_at` | REAL | 可空 | intent 生命周期结束时间 |
| `handoff_id` / `dispatch_id` | TEXT | 可空 | 交给执行席的 dispatch/handoff 标识（落 attempt ledger） |
| `idempotency_key` | TEXT | ✅ | 投递幂等键（provider 副作用去重） |
| `attempt_count` | INTEGER | ✅ | 该 intent 已创建的 attempt 数 |
| `last_error_ref` | TEXT | 可空 | 最近失败原因（受控 ref，非自由文本） |
| `cancelled_reason` | TEXT | 可空 | 撤销原因（终态/用户取消/policy 失效） |
| `created_at` | REAL | ✅ | |
| `updated_at` | REAL | ✅ | 每次 CAS 更新 |

**索引**：`(status, next_wake_at)`（due 查询）、`dedup_key` UNIQUE、`(owner_id, status)`（配额/隔离）。
**约束**：intent 只存受控 ref——不存 prompt、聊天正文、密钥、大 payload。

## 2. 状态机

**due 权威**：`due = pending AND next_wake_at <= now AND now < expires_at AND policy 有效（continuation_policy + policy_generation 存在）`（持久索引 `(status, next_wake_at)` 派生），`next_wake_at` 是唯一权威事实（`not_before`/`retry_after` 只作原因/审计字段，由写入方折叠进 next_wake_at，**不存在双重时钟**）。claim 用 CAS（`status: pending → claimed` + `claim_generation` 递增）。

**canonical status token**：`status` 只存 `handed_off`（可靠交接）；`handoff_id` + `handed_off_at` 表达「已交给执行席」，`finished_at` 不得被解释成模型完成——执行结果另存 dispatch/attempt ledger。

### 可执行 CAS 状态转移表

| 转移 | 前置（全部满足才 CAS） | 副作用 |
|---|---|---|
| `pending → claimed` | status=pending、next_wake_at<=now、now<expires_at、continuation_policy+policy_generation 存在、配额/circuit 可用 | claim_generation+1，写 claim_token/lease_owner/lease_until |
| `claimed → handed_off` | status=claimed、dispatch/handoff 已在权威 ledger 持久化 + 执行席接收 | 写 handoff_id/handed_off_at |
| `claimed → pending`（lease 过期，无已知副作用） | status=claimed、lease_until<=now、**已确认无已知副作用** | claim_generation+1（防旧租约误认领），清 claim_token/lease_owner/lease_until |
| `claimed → reconciliation_required`（lease 过期，未知副作用） | status=claimed、lease_until<=now、副作用未知 | 标 last_error_ref，等待 reconciliation/人工，**不隐式重放** |
| `pending/claimed → cancelled` | 用户取消/任务终态/policy 失效；**仅未 handed_off 可取消** | 写 cancelled_reason |
| `pending → expired` | next_wake_at+宽限期超时仍未被 claim | 终态，进 reconciler 审计 |
| `handed_off → 任何` | **禁止回退** | — |

- **`handed_off` CAS 条件**：dispatch/handoff 已在权威 ledger 持久化 + 被执行席接收，才允许 `claimed → handed_off`。
- **叫醒失败**：不丢 intent——保持 `claimed` 直到 lease 回收，或标 `last_error_ref` 进 reconciler 补投递。
- **调度状态 vs 任务状态分离**：intent 状态只表达调度生命周期；任务进度/终态在 task/attempt ledger。需能表达 `deferred`（not_before 未到）、`expired`、`orphaned`（无 run 绑定）、`blocked`（配额冻结）。

## 3. dedup_key 组成

```
dedup_key = sha256(f"{owner_id}:{task_id}:{run_id or ''}:"
                   f"{policy_generation}:{source}:{wake_reason}:{source_event_id or ''}:{due_window}")
```

- **不依赖 attempt_id**（producer 写 intent 时 attempt 尚未创建）。
- 重试 / 新的明确唤醒 → 必须生成**新**的 dedup_key（新的 source_event_id / due_window / policy_generation）。
- UNIQUE 约束：同一来源同一 due window 只允许一条 intent，防多 gateway/重启重复拉起。
- 重试 lineage（`parent_attempt_id` / `retry_of`）与 provider `idempotency_key` 放在 **attempt ledger**，不参与 intent dedup。

## 4. 四层职责 + 授权不变量

| 层 | 职责 | 禁止 |
|---|---|---|
| **intent producer** | 来源/策略/schema/去重校验，写 intent/outbox | 调用模型、创建 attempt |
| **durable wake queue/outbox** | 持久化、索引、去重、投递 | 调用模型、创建 attempt、做策略裁决 |
| **dispatcher/executor** | **唯一** due 选择、CAS claim、lease、创建一次活跃 attempt、持久化 handoff/result | — |
| **reconciler** | 低频按结构化事实补写缺失 intent / 标记待核对 | 调用模型、创建 attempt；补 intent 必须过同一 dedup/授权校验 |

**硬不变量**：
1. 只有 dispatcher 在成功 claim 后才能创建 attempt。
2. producer / queue / reconciler 都不能调用模型或创建 attempt。
3. lease 过期不隐式重放：先确认无已知副作用；未知副作用 → `RECONCILIATION_REQUIRED` / 人工确认。
4. **同一 intent 同时最多一个 active attempt**；重试必须是有父子关系（parent_run_id + attempt ledger 中 `parent_attempt_id`/`retry_of` lineage）、显式 retry policy、独立 idempotency_key 的新 attempt，未知 effect 先 reconciliation，禁止自动 replay。
5. 配额/429 时**冻结该 provider 的 intent 集合**（含启动探针/隐式重试，探测必须是显式授权的 quota probe），不逐任务各自重试；恢复后按显式策略最多一次新 attempt。
6. `无 policy / 无 run / legacy_unbound` → fail-closed（`ORPHANED/RECONCILIATION_REQUIRED`），525 条旧 active 先隔离并保留 provenance，不按 active 扫描执行。
7. **`continuation_policy + policy_generation` 缺失 → producer / reconciler / dispatcher 一律拒绝调度**——授权不能从 active 推导，fail-closed。
8. **`claim_generation` 每次 claim/reclaim 递增**，并与 `claim_token + lease_owner + lease_until` 绑定——旧租约回收后不得凭旧 generation 认领。

## 5. wake_reason 受控枚举

`user_message` / `user_continue` / `cron_due` / `heartbeat_due` / `model_sleep_due` / `subagent_completed` / `provider_quota_recovered` / `interrupted_run_recovery` / `retry_after_due`。

## 6. 实施顺序（含前置止血）

- **第 0 步（前置）**：止血 + 定契约
  - 关闭旧 `all-active→模型` 路径（唤醒轮冻结，不重启原逻辑），**建立 525 迁移前零调用基线**（provider 请求计数快照）。
  - 冻结/隔离 525 条遗留任务：迁移到 `PAUSED/ORPHANED/RECONCILIATION_REQUIRED`（带 reason/last_seen/来源/provenance，不批量删不标完成），迁移前后计数落账。
  - 定义状态转移、CAS 条件、唯一键、attempt/lease 关系（本规格）。
- **第 1 步**：建 `wake_intents` 表 + repository（字段/CAS/dedup/索引）。
- **第 2 步**：dispatcher 只消费「到期且有授权」的 intent（`status=pending AND next_wake_at<=now AND continuation_policy=async`），原子 claim 后创建 attempt；旧路径只 shadow/dual-write，**不 dual-execute**。
- **第 3 步**：接入事件投递（入站/cron/heartbeat/sleep/subagent_completed/provider_recovery 写 intent）+ 精确定时器（持久 due 索引 + 最小堆，睡到最近到期点；重启后从索引重建）。
- **第 4 步**：sleep/progress/wait 归一到同一挂起记录（sleep 不占 worker/LLM 槽）；sleep 产生的 `sleep_until` wake intent 与 progress/wait 的 suspension fact 在 source adapter 契约中明确。
- **第 5 步**：reconciler（低频安全网 10-30 分钟，仅审计+补 intent）+ 525 迁移验证 + 双 gateway/重启/429/终态竞态/重复副作用验收（见 §7）。
- **第 6 步**：旧路径双写 shadow/计数比对验证无重复/漏写后，删除旧执行路径，参数化 reconciler 频率（迁移期 10 分钟，稳定后 30 分钟 + jitter，信号丢失临时缩短）。

## 7. 证据化验收矩阵（可证伪）

| 验收项 | 证据来源（不只看 scheduler 日志） |
|---|---|
| 525 遗留 + 无 policy active 零模型调用/零调度 | 迁移前后快照 + 明确时间窗 + `task/run→intent→attempt→provider-request` ledger 关联 + 请求计数 |
| 同一 intent 同时最多一个 active attempt（含 crash-after-claim / lease 过期 / 未知副作用） | claim ledger + attempt ledger + **同一 provider 幂等 key（idempotency_key）重复副作用 = 0**；重试为带 parent_run_id 的显式新 attempt |
| reconciler 零直接执行 | dispatcher/模型调用入口结构化调用计数 + 来源断言 |
| 429 停止后窗口内零新 provider request（含探针/隐式重试） | provider 侧请求记录 + circuit 状态 + retry_after 时间窗；探针仅显式授权的 quota probe |
| 重启/双 gateway 不追赶 | 固定窗口请求数 + claim 记录 + attempt ledger 对账；覆盖同一 due 并发 claim、claim 后进程死、lease 过期、outbox 重放、429、terminal race |
| subagent completion → 叫醒 | `event_id → intent persisted → claim → handoff` 端到端延迟：**测量窗口 ≥ 200 条样本**，P95 < 5s、P99 < 10s（允许投递延迟），重复率 = 0，丢失率 = 0 |
| 终态后零新 attempt/handoff | attempt ledger + runtime_events |

## 8. 明确不做（本棒边界）

- 不改造收口（delivery_verify 三支已定，另行闭环）。
- 本棒不落 `sleep` 用户态接口完整实现（归一到挂起记录在 step 4，接口细节后续）。
- 不批量删除/伪造完成 525 条；只迁移隔离 + 保留证据。
- intent 不存 prompt/正文/密钥/大 payload（只存受控 ref）。

## 9. 与既有机制关系

- resume_loop / finalize 续跑调度 → 改为写 intent，不再直接扫 active。
- wake 链 / self-wake → 统一收敛到 intent + claim。
- 429 配额耗尽 → 冻结 provider intent 集 + `retry_after`，恢复按显式策略一次新 attempt。
- 中断恢复 → 只认近期 `interrupted_run` 证据 + 新鲜度 + 重试上限（≤3）+ lease 窗口。
- 对话任务只允许四类来源：inbound、显式 cron、opt-in heartbeat、受 policy 约束的 interrupted recovery。
