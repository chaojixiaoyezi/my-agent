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
- task compact 包同时携带根任务的 `task_progress` 紧凑摘要：总览、下一步、状态计数、未完成项和近期
  已完成项。子代理树和主任务进度共用同一 `task_id`，不能只压子代理状态而丢掉主代理正在整合、测试或
  写文档的进度。

## 规则

- 只压 refs 和必要摘要，大正文继续留在 artifact/blob/audit。
- 缺少 acceptance/constraints/latest_tests 时记录 `not_recorded`，不把普通任务变成硬验收任务。
- `rollup_ledger.jsonl` 只在状态签名变化时追加。
- compact 触发的当前上下文压力以 `max(provider_input_tokens, local_prompt_estimate)` 为准；
  provider usage 可能低报或不包含本地拼接后的完整 prompt，不能让它绕过用户配置的触发比例。
- compact 后续接优先读 continue packet 和 work state，再按 refs 精读需要的文件。
- `/goal` 的新 continuation turn 直接注入同一任务的紧凑进度和 compact refs；不要先重新扫描整个
  工作区来猜上轮做到了哪里。派工时按精确 child `run_id` 种下的进度项，在该 child 的 canonical
  状态变成 `DONE` 后按同一个 id 自动闭合；这只是进度投影，不代表根任务验收完成。
- 长文本完整读取的续接不能只靠摘要；continue packet 要从已归档工具输出里恢复
  `offset/total_chars` 或 `start_line/total_lines` 游标，提示下一段从已连续覆盖处继续读。
- 上下文已经到 compact 阈值且下一步工具会产生正文输出时，本轮工具调用必须登记为
  `CONTEXT_COMPACT_DEFERRED`，并写入普通工具记录，表示“这次没有执行，compact/resume
  后继续”。不能静默 break，也不能设置需要模型主动满足的隐藏 checkpoint 门。
- 同一模型轮里已经执行了前几个正文工具、随后才达到 compact 阈值时，剩余未执行的正文
  读取/检索工具也要逐个登记为 `CONTEXT_COMPACT_DEFERRED`。compact 后由
  `pending_deferred_tool_calls` 续跑这些结构化调用，不能只靠一句“还有几个没执行”的提示
  让模型自己重造。
- 即使任务没有 required `full_source_read` 合同，work state 也必须保留已读取的分片范围。
  同一文件的 `read_file`/`read_artifact` 多段读取不能被压成“读过这个文件”一条记录；恢复
  提示要列出已登记范围和下一游标，避免 compact 后从 offset=0 重读或凭摘要猜结论。
- 多文件/多项目任务不能只保留一个“最大进度”的 primary 游标；work state 和 continue
  packet 必须携带 `incomplete_sources`，恢复时优先续接未完成来源，再用完整 `sources`
  做覆盖审计。
- 几 KB 的定位、抽取和统计类工具输出默认留在下一轮上下文；只有大输出才外置到 blob。
  compact 可以引用 blob/ref，但不能让模型必须先读外置 wrapper 才能看见刚抽出的核心事实。
- 当前 run 若已有显式结构化 `target_coverage_contract`，它要进入 runtime fact，再进入
  compact work state 和 handoff；多次 compact 后仍应保留覆盖口径、
  required/enforcement、目标数量和目标预览。如果 required/enforcement 写在
  `target_items[]` 单项里，也必须保留，恢复后不能降级为 advisory。
- 默认 chat/cli/gateway 不因为普通自然语言自动物化覆盖合同。compact/handoff 只保留
  已存在的结构化合同和机器游标，不把“完整读完”这类话术加工成新的硬验收状态。
- `memory-fact-write` 只能补充当前 runtime fact 的验收、约束、测试等人工确认字段；
  它必须保留同一 fact 中已有的 run/task 身份、`delivery_contract`、`desired_outputs`、
  `target_coverage` 和 `run_intent`，不能整文件覆盖成一份更薄的人工状态。
- search/grep/shell 统计可以帮助恢复后定位章节、锚点或候选范围；continue packet 不应
  禁止这些定位动作。但 required full-source coverage 的完成证明只能来自读取窗口、
  artifact refs 或 coverage ledger，不能只靠搜索命中。
- 如果当前任务树里已有子代理，continue packet 要列出这些 run_id/status/progress；活跃时提醒主代理先观察、等待或收集结果，全部完成时提醒主代理直接汇总 recent agents。这不是禁止继续派工的硬门。
