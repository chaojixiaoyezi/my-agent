# 收口状态机设计（owner 四改之 2，状态：设计中未实施）

> 配套决策背景：`docs/stability/ISSUES.md` EXEC-30/33/35/36/38/39。
> 参考实现：会话运行时（模型自然停即收口，无系统侧收口机；goal 扩展只在目标层
> 驱动轮次）、deepseek-harness `/goal`（goal-round-driver idle 驱动 +
> 轮数预算）、长期助手（batch-runner resume）、通道运行时（cron tick 续跑）。

## 1. 问题

当前工具循环停下后，"下一步怎么办"散落在多处，各写各的判断：

- `_tool_loop_service.py`：`_final_response_after_tool_limit` /
  `_final_response_after_repeated_failure` / `_final_response_after_unknown_
  outcome_halt` / `_final_response_after_no_action_gate` 各自拼提示词 + 各自
  读续跑预算；
- `response_decision.py`：break 时分散定 runtime_status（ok/unfinished/
  blocked/TOOL_CALL_UNCLOSED 降级分支）；
- `conversation/runtime.py`：`should_continue_task`（可续跑白名单 gate）；
- `resume_loop.py`：`run_with_resume` / `run_manual_resume` 各自重复
  budget/收敛/收口判定；gateway 侧 `_run_handoff_continuation` 又一套。

后果：同一事实（还能不能续、续了会不会兑现）在 4+ 处重复推理，历史上出过
"提示词承诺自动续跑但普通任务不续"（2026-08-07）与"EXEC-39 前无 goal 也
自动续跑"两类不一致。owner 拍板四改之 2 就是把这部分收拢成一个小状态机。

## 2. 目标

一个权威决策点 `decide_closeout(...)`，回答：本轮停下后，下一步是哪一种。
CLI resume_loop 与 gateway 调度器共用它，停止原因采集方（`_final_response_
after_*`）只负责报原因，不再各自决定命运、不再各自拼"会不会继续"的承诺文案。

不改变的三件事（owner 已拍板）：
1. 正常不自动续跑：无 active goal 时收口即停（EXEC-39，会话运行时/轻量运行时 语义）。
2. resume 能力保留：goal 模式与 cron 模式要用（owner 原话）。
3. 自然收口是主路径：模型无工具停 = done，系统不搞产出门/质量门拦它
   （EXEC-38，对齐 会话运行时）。

## 3. 状态与迁移

一轮 run 代数（generation）的终态：

```text
                 ┌─────────────────────────────────────────────┐
  工具循环停止 ──►│ 停止原因采集（各 _final_response_after_* 只报因） │
                 └──────────────┬──────────────────────────────┘
                                ▼
                      should_continue_task（白名单 gate，保持不变）
                    ┌───────────┴───────────┐
                不可续跑族               可续跑族
                    │                       │
                    ▼                       ▼
             ┌──────────────┐      _auto_resume_authorized（EXEC-39: 仅 active goal）
             │ blocked/违规 │      ┌─────────┴─────────┐
             │ unknown 副作用│    无 goal               有 goal + 预算内
             │ user_stop    │      │                    │
             └──────┬───────┘      ▼                    ▼
                    │        wait_handoff         resume_round（下一代数）
                    │        （写移交单；          （同因≥3 → EXEC-30 收敛
                    │         用户 run --resume      → 降级 wait_handoff）
                    │         或 gateway/cron 接管）
                    ▼
              wait_human（等用户显式继续；不自动跑）
```

终态集合（结构化，一字不多）：

| 终态 | runtime 记录 | 下一步 | 谁触发下一步 |
|---|---|---|---|
| `done` | ok | 无 | — |
| `cancelled` | cancelled/user_stop | 无 | — |
| `wait_human` | blocked / PROTOCOL_VIOLATION / UNKNOWN 副作用 | 无（人工闸） | 用户 `run --resume`（结构化重激活 link） |
| `wait_handoff` | unfinished（可续跑族 reason） | 写 continuation handoff | 用户 `run --resume` / gateway / cron |
| `resume_round` | unfinished（可续跑族 reason） | 进程内自动续下一轮 | goal-round-driver（EXEC-39 授权） |

收敛护栏全部保留并收进机器输入：resume_limit 预算（policy 单一权威）、
max_rounds（进程内护栏）、EXEC-30 同因 3 次收敛。`resume_round` 只在
预算内才发，预算耗尽即降级 `wait_handoff`。

## 4. 接口草案

```python
@dataclass(frozen=True)
class CloseoutFacts:
    runtime_status: str        # ok/unfinished/blocked/cancelled
    runtime_reason: str
    runtime_source: str
    active_goal: bool          # EXEC-39 授权事实（load_goal 结果）
    resume_budget_left: int    # policy resume_used/resume_limit 差（0=无预算）
    same_reason_streak: int    # EXEC-30 连续同因计数
    rounds: int                # 已跑代数

@dataclass(frozen=True)
class CloseoutOutcome:
    state: str                 # done/cancelled/wait_human/wait_handoff/resume_round
    reason: str
    guidance_key: str          # 唯一文案选择键（见 §5）
    handoff: bool              # 是否写移交单
```

`decide_closeout(facts) -> CloseoutOutcome` 纯函数（可穷举单测）。
CLI `run_with_resume`/`run_manual_resume` 与 gateway `_run_handoff_continuation`
都调它；停止原因采集方不直接拼命运文案。

## 5. 承诺文案单一权威

`_final_response_after_tool_limit` 现按 `_ordinary_task_resume_available`
读 policy 自行决定"会自动继续"还是"请回复继续"——这必须改成读
`CloseoutOutcome.state`：只有 `resume_round` 才写"会自动继续"；
`wait_handoff`/`wait_human` 一律写"已暂停，回复『继续』（run --resume）
我会接着做"。承诺与机器行为一一对应，不允许文案先行于状态。

## 6. 实施切法（建议，每步可独立提交）

1. 纯函数 `decide_closeout` + 穷举单测（fake facts 全组合 truth table）。
2. `run_with_resume` 换用机器（行为等价：EXEC-30/35/39 语义不变）。
3. `run_manual_resume` 与 gateway `_run_handoff_continuation` 换用。
4. `_final_response_after_*` 的文案选择换用 guidance_key（停止原因采集
   职责收敛为"报因"）。
5. 收尾：删除各处的并行预算推理（`_ordinary_task_resume_available` 等），
   确认 CONVERSATION/CONTINUABLE_REASONS 白名单仍是机器唯一原因源。

## 7. 验收

- 穷举测试：facts 组合 → state 映射 truth table 全覆盖；
- 行为回归：resume 合同 45 项 + manual resume 22 项 + gateway resume 场景
  测试全过；
- 真机：goal 模式下自动续跑轮在预算内推进、预算尽后移交单出现且不再自跑；
  普通 run 收口即停（EXEC-39 语义不变）。
