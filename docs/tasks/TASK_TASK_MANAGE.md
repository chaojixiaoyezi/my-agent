# Task Manage

历史任务说明已过期。当前任务目录规则：

- 默认：`~/.my-agent/owners/<owner>/tasks/<date>/<task-slug>/{output,work}`
- 用户明确指定输出目录时：最终交付可写用户目录，同时 task output/work 记录索引和验收证据。
- 过程草稿、日志、compact、子代理账本写 task work。
- 系统验收报告写当前 task 的 `.agent_delivery/closeout.json`，不是写输入源码仓库。
- `work/run_workspace.json` 保存当前 run 的目录身份，如 request_id/run_id/task_id/owner。
- `work/state.json` 保存任务进度、子代理状态和 rollup 状态；它不能作为目录复用的身份依据。
