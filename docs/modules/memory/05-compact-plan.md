# Compact Plan

## 当前目标

compact 只做一件事：在上下文压力或显式请求下，把当前 run/task/agent 的事实压成可继续工作的 refs 和摘要。它不验收任务，不制造新硬门，不从旧目录猜状态。

## 输入

- 当前 run 的 runtime facts。
- 当前 task workspace 的 `work/state.json`、`work/timeline.jsonl`、`work/compact/`。
- 当前 task workspace 下的子代理 `tasks/<date>/<task>/work/agents/<run_id>/canonical_state.json`、events、artifact refs。
- owner memory、daily memory、tool output refs 和 audit refs。
- 当前 guidance inbox 中点名给本 run 的提示。

## 输出

- `compact_context.md`
- `handoff_summary.md`
- `work_state_snapshot.json`
- `continue_packet.json`
- `refs.json`
- `metadata.json`
- task rollup：`work/compact/task_rollup.json`

## 规则

- 只压 refs 和必要摘要，大正文继续留在 artifact/blob/audit。
- 缺少 acceptance/constraints/latest_tests 时记录 `not_recorded`，不把普通任务变成硬验收任务。
- `rollup_ledger.jsonl` 只在状态签名变化时追加。
- compact 触发的当前上下文压力以 `max(provider_input_tokens, local_prompt_estimate)` 为准；
  provider usage 可能低报或不包含本地拼接后的完整 prompt，不能让它绕过用户配置的触发比例。
- compact 后续接优先读 continue packet 和 work state，再按 refs 精读需要的文件。
- 长文本完整读取的续接不能只靠摘要；continue packet 要从已归档工具输出里恢复
  `offset/total_chars` 或 `start_line/total_lines` 游标，提示下一段从已连续覆盖处继续读。
- 如果当前任务树里已有子代理，continue packet 要列出这些 run_id/status/progress；活跃时提醒主代理先观察、等待或收集结果，全部完成时提醒主代理直接汇总 recent agents。这不是禁止继续派工的硬门。
