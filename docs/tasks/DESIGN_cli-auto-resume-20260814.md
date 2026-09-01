# CLI 自动续跑设计 v2（根因3 正式产品修复，2026-08-14）

> **2026-09-01 已废止，不再实施。** 当前产品与 会话运行时 普通 turn 对齐：普通 CLI/TUI 任务在本轮自然收口，
> 不因 open Todo、工具轮上限或模型正文自动追加隐藏模型轮。跨轮持续执行只由用户显式 `/goal` 建立的
> ThreadGoal 负责；直属 child lifecycle、用户插话、Gateway crash recovery 与 provider transient retry 继续走
> 各自结构化恢复合同。本文以下内容仅保留历史决策背景，不能作为当前运行语义或实现清单。

> v1 审查结论（维护记录 / memory seq 1815）：方向可行、有条件通过、
> 暂不实施。本文档按 7 条审查意见修订。**设计审过、合同单测、通用回归、
> 部署 ledger 核对完成后，才启动 3×3 复刻。**

## 背景

双 CLI 复刻实证：CLI run 表达轮收口（模型回复正文没调工具）后任务未完成
即结束。续跑调度已存在（`_schedule_typed_unfinished_continuation` →
`ensure_ordinary_task_resume` 排 progress_policy，budget=resume_used/
resume_limit 默认 3），但**消费端缺失**——due policy 由 gateway 常驻调度器
扫描（runtime.py:3638），CLI 进程退出后无人消费。

## 审查意见 1：同一 task/run 链路不能靠 resume_context 自然形成

**事实确认**：`run_params_with_request_id`（run_params.py:56）每轮生成新
attempt_id/run_id/task_id（无续跑参数时）；`bind_cli_run_conversation`
（cli_run_conversation.py:42）用 request_id 作 channel_conversation_id——
每轮新 request_id → 新 thread 根。直接重复 agent.run 会每轮新根。

**设计**：新增结构化续跑契约 `CliContinuationContext`，首轮创建后贯穿所有
续跑轮：
- `root_task_id`：首轮 task_id（稳定，续跑轮复用，绝不新建 task 根）
- `root_run_id`：首轮 run_id（续跑轮作为其 continuation，不新建 run 根）
- `thread_id`：首轮 conversation thread（复用，不新建）
- `continuation_seq`：0,1,2...（每轮递增，事件账本用）
- `parent_attempt_id`：上一轮 attempt_id（树形链）
- `request_id`：每轮**显式派生** `{root_request_id}#cont-{seq}`（保证
  ConversationStore append 幂等键唯一，同时可追溯到根）
- `source`：`cli_run`（保留，续跑轮在 runtime_events 标
  `is_continuation=true`，不是新用户请求）

run_params_with_request_id 需要支持显式传入这些 ID（不改其默认生成逻辑，
只允许续跑轮传入全部 ID 覆写）。`bind_cli_run_conversation` 对续跑轮走
续跑分支：同一 thread 追加「机器续跑提示」消息（带 is_continuation 标记），
**不得伪装成普通用户请求**。

## 审查意见 2：统一 gate/预算/claim 路径，防双重计数双重消费

**事实确认**：`ensure_ordinary_task_resume` 排队时已递增 resume_used（默认
预算 3）；CLI loop 若再独立计数 + gateway 并发扫描同一 policy → 双重消费。

**设计**：CLI 续跑**不新建旁路判断**，直接复用现有消费路径：
- `cmd_run` 首轮收口后调用现有 due 扫描的同一 gate 分支：
  `should_resume(result)` = 结构化对照 `_schedule_typed_unfinished_continuation`
  的 gate 条件（reason ∈ {TASK_PROGRESS_OPEN, TOOL_ROUND_LIMIT_REACHED,
  REPEATED_TOOL_FAILURE} 或返工门族），**提取为共享函数**（放
  conversation/runtime.py 导出，gateway 与 CLI 共用，杜绝两份判断漂移）。
- 续跑轮**不递增 resume_used 两次**：CLI 进程内消费时通过
  `ensure_ordinary_task_resume(..., due_now=True)` 的标准路径拿到 policy
  （已含 budget 检查+递增），CLI loop 只读 policy 的 resume_used 做展示，
  不自己计数。
- **防并发双消费**：CLI 消费 policy 时先 `claim_progress_policy`（CAS
  lease，复用 gateway 调度器同款 claim 原语）——gateway 已 claim 则 CLI
  跳过该轮（等 gateway），CLI 已 claim 则 gateway 跳过。同一 lease/
  idempotency 路径，绝不同时两 consumer 跑同一 task。
- `max_rounds`（CLI 进程内续跑次数上限）只作为**进程内防失控预算**（默认
  8），与 resume_limit（policy 级预算，默认 3）双保险；两者任一耗尽即停。
  **注意**：resume_limit 默认 3 意味着默认最多自动续 3 轮——设计 v1 的
  max_rounds=8 是上限不是目标，实际由 budget 决定。

## 审查意见 3：轮终态 vs 任务终态分层 + CLI one-shot failed 兜底冲突

**事实确认**：`_settle_main_agent_run_status` 的 cli_one_shot 分支把
unfinished/blocked 兜底落 failed——续跑轮若仍走该分支，外层 loop 看到的是
已终态化的 run，无法保留任务级非终态。

**设计**：
- **轮终态**（attempt 级）：每轮 attempt 正常终态化（done/failed，已有
  机制，0c6a25ad 保留）。
- **任务终态**（task 级）：由 CLI loop 判定——`should_resume` 为真则任务
  未终态，**不落 task 终态**，保持 task 级可续跑语义（与 gateway 域
  unfinished 不 settle 同一原则）。
- 实现：`_settle_main_agent_run_status` 的 cli_one_shot 分支增加条件——
  若检测到 `CliContinuationContext` 且 `continuation_seq > 0`（续跑轮），
  unfinished/blocked **不落 failed**（保留任务级非终态，由 loop 决定）；
  仅首轮（seq=0）且无后续续跑时兜底 failed。首轮失败账完整保留，续跑
  轮不覆盖。

## 审查意见 4：提示词不得含验收语义

**设计**：`next_resume_prompt(result)` 只引用结构化 continuation_reason +
cursor 摘要（如「上一轮因 {reason} 收口，任务未完成；继续推进」），
**不出现**「代码规模/测试达标」等专项验收词。完成判断只由结构化
task/attempt/effect/artifact/test evidence 决定（本轮不做模型完成度判定，
只依赖收口信号 + budget 耗尽）。

## 审查意见 5：max_rounds=8 只是预算不是成功

**设计**：`run_with_resume` 返回结构化 `CliResumeOutcome`：
- `status`：completed（收口 ok）/ budget_exhausted（resume_limit 或
  max_rounds 耗尽）/ stopped（用户停止）/ unresumable（blocked/协议违规/
  UNKNOWN 等不可续跑）
- budget_exhausted 时**写 runtime_events `continuation_budget_exhausted`**
  （含 budget_before/after、reason、需用户「继续」或人工复核），**绝不输出
  DONE**，保留首轮与每个 attempt。

## 审查意见 6：恢复矩阵（truth table）

| 场景 | 行为 |
|---|---|
| 进程崩溃/被杀（轮中） | attempt 卡 running → 现有 reclaim 机制兜底（171dcb5e 事件可见）；policy 已 claim 未建 attempt → lease 过期后 gateway/下次 CLI 可重 claim |
| policy 已 claim 但 attempt 未建 | claim lease 过期（复用现有 lease 语义）→ 重 claim 重建 attempt |
| attempt 有副作用但 cursor 未落账 | tool_operations 幂等账本兜底（effect_key）；续跑轮用同一 root task 可重放 |
| 双 consumer（CLI+gateway） | claim CAS 互斥，一次只有一个 consumer 跑同一 policy |
| blocked（等用户输入/审批） | **不续跑**（与 gateway 域同一排除先例） |
| 用户停止（InterruptedError/cancelled 族） | **不续跑** |
| 协议违规 break（PROTOCOL_VIOLATION） | **不续跑**（模型格式问题需人工/换模型） |
| UNKNOWN effect（TOOL_OPERATION_OUTCOME_UNKNOWN） | **不续跑**（副作用不确定，防重复） |
| REPEATED_TOOL_FAILURE | 仅结构化可恢复门内续跑（同现有 gate 分支） |
| TASK_PROGRESS_OPEN / TOOL_ROUND_LIMIT_REACHED | 续跑（预算内） |

## 审查意见 7：cli_resume 事件关联全链路

**设计**：每轮续跑写 runtime_events `cli_resume`，payload 含：
`task_id / task_run_id / root_run_id / agent_run_id / attempt_id / turn_id /
continuation_seq / parent_attempt_id / policy_id / idempotency_key /
cursor_before / cursor_after / effect_outcome / terminal_status / reason /
budget_before / budget_after / created_at / version`。
落账顺序：claim → 建 attempt → 写 cursor_before → 执行 → 写 effect →
cursor_after → 轮终态 → cli_resume 事件（同一事务内或可恢复顺序，崩溃后
按 claim/event 重放）。

## 实施切片

1. **续跑契约层**：CliContinuationContext + run_params 显式 ID 覆写 +
   bind_cli_run_conversation 续跑分支
2. **共享 gate**：should_resume 提取为共享函数 + claim/lease 原语复用
3. **轮/任务终态分层**：_settle_main_agent_run_status 续跑轮不落 failed
4. **resume_loop**：cmd_run 接线 + CliResumeOutcome + budget 事件
5. **测试**：合同单测（ID 贯穿/预算/claim 互斥）+ fake tool/LLM 循环 +
   崩溃/重复消费回归 + 全量 gate
6. **部署 + ledger 核对**：1.10 部署，真机验证 cli_resume 事件全链路
7. **3×3 复刻**：审过后按双席验收结构启动

## 明确不做

- 不改 gateway 域续跑（已工作，只共享 gate/claim 原语）
- 不做模型完成度判定（不用行数/文本）
- 不为特定项目/模型加专项分支
- resume_limit 默认值不因本设计改动（3 是预算，3×3 复刻若需更多由
  config ordinary_task_resume_limit 显式调，走配置不是代码）
