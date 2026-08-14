# CLI 自动续跑设计（根因3 正式产品修复，2026-08-14）

> 背景：双 CLI 复刻实证——CLI run 表达轮收口（模型回复正文没调工具）后任务
> 未完成即结束。外部 drive-resume 脚本能串接多次 CLI run，但不改变「产品无
> 自动续跑」结论（维护记录 定性），且双席要求禁外部脚本接管完成判断
> （seq 1806/1807）。本设计让产品自身在同一 task/run 链路内自动续跑。

## 现状（代码实证）

1. **续跑调度已存在**：CLI run 收口时 `_schedule_typed_unfinished_continuation`
   （_finalization_service.py:579）对 cli_run 前台形态调用
   `ensure_ordinary_task_resume`（conversation/runtime.py:5006）——排入
   progress_policy 队列（kind=ordinary_task_resume，resume_used/resume_limit
   预算上限，due_now=foreground 时 expedite 到立即 due）。
2. **消费端缺失**：due policy 由 gateway 常驻调度器扫描
   （runtime.py:3638 `_consume_due_policies`，background_main_agent 每 tick）。
   CLI 进程退出后**没有常驻进程扫描该队列** → 续跑 policy 永远不被消费 →
   任务停在表达轮收口。
3. **终态已修**：0c6a25ad 让 CLI one-shot 收口兜底落 failed 终态（不悬挂
   attempt）——但这与续跑是两件事：现在 run 正常终态化但任务不继续。

## 修复设计（最小治本）

**CLI run 进程内续跑**：`cmd_run`（cli/local_commands.py）在单次 `agent.run()`
返回后，若结果是非终态收口（runtime_status ∈ {unfinished, blocked} 且非
用户停止），在**同一进程内**检查并消费该 task 的续跑 policy——循环
`agent.run()` 直到：①任务完成（runtime_status=ok）②续跑预算耗尽
（resume_used ≥ resume_limit，收口提示用户回复「继续」）③达到进程内续跑
次数上限（防失控，默认如 8 次）。

### 具体接线

- 新增 `agent_py_agent/cli/resume_loop.py`：
  - `should_resume(result) -> bool`：结构化判定（runtime_status ∈
    {unfinished, blocked} 且 runtime_reason ∈ {TASK_PROGRESS_OPEN,
    TOOL_ROUND_LIMIT_REACHED, REPEATED_TOOL_FAILURE, REQUIRED_ACTION_* 返工门族}
    ——与 _schedule_typed_unfinished_continuation 同一 gate 分支），禁
    NL 匹配。
  - `next_resume_prompt(result) -> str`：续跑提示词（「继续推进任务：沿用
    当前进度继续完成，直到代码规模和测试都达标」+ 前一轮收口摘要），普通
    用户话术，非专项。
  - `run_with_resume(agent, args, max_rounds=8) -> result`：循环调用
    `agent.run(prompt=next_resume_prompt, resume_context=True, ...)`，
    每次收口后结构化检查；预算耗尽或完成即停。
- `cmd_run` 改为调用 `run_with_resume`（保留单次 run 语义作为 max_rounds=1
  的退化，gateway/chat 路径不动）。
- **同一 task/run 链路**：续跑 run 复用同一 conversation thread（resume
  context），runtime.db 的 task/agent_run 树继续增长（不新建 task 根），
  attempt 逐轮新建并正常终态化——与外部 drive-resume 的「多次独立 run」
  本质区别：产品自身在同一链路内接续。
- **续跑轮次记账**：每轮续跑在 runtime_events 写 `cli_resume` 事件
  （round 序号 + 收口原因 + 预算余量），首轮失败与续跑轮分开可审计。

### 安全边界（防失控）

- resume_limit 预算：复用现有 ensure_ordinary_task_resume 的
  resume_used/resume_limit（默认预算耗尽即停，提示用户回复「继续」）。
- 进程内续跑次数上限（max_rounds，默认 8）：与预算双保险。
- 用户停止（InterruptedError/cancelled 族）绝不续跑。
- blocked（等用户输入/审批）不续跑（与 gateway 域同一排除先例）。
- 协议违规 break（PROTOCOL_VIOLATION）不自动续跑（模型格式问题需人工/
  换模型，续跑会烧钱空转）。

### 验收标准（对照双席结构）

1. 3 项目 × 3 CLI × 普通中文 prompt，产品自身完成（无外部驱动）。
2. 每轮续跑有 runtime_events 账（cli_resume 事件含 round/reason/预算）。
3. task/run/attempt/turn/tool-operation 全链路 ID 可核对、无悬挂。
4. 首轮失败与续跑轮分开记账（不合并成一次成功）。
5. 任一协议违规/UNKNOWN 误归/账本断链 → 停该复刻先修底座。

## 明确不做

- 不改 gateway 域续跑（已工作）。
- 不做「模型完成度判定」（用结构化收口信号 + 预算，不用行数/文本）。
- 不为特定项目/模型加专项分支。
