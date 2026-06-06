# Subagent Runbook

## 看树

优先使用 `inspect_agent_tree`。它应该展示 run_id、root_id、parent_id、status、channel_status、当前步骤、未完成原因、失败原因、running_seconds、seconds_since_progress、artifact refs 和 work refs。

## 等待

不要高频反复 inspect。使用 `wait` 登记稍后查看。配置下限 60 秒，上限 7200 秒；主代理忙或用户正在交互时顺延。
不要用 shell `sleep`、`timeout /t` 或 `Start-Sleep` 模拟等待；等待是运行时工具语义，
这样 compact、用户插话和后台唤醒都能看懂。

`create_subagents` 开了自动启动时，后台会对本批 run_id 跑一轮精确 dispatch，并启动对应 runner；它不走 watch 循环，避免和 gateway/daemon 的观察锁互相抢占。主代理不需要立刻 `wait` 退出当前任务，下一步应该按工具返回的提示 inspect、继续自己能做的部分，或等后台唤醒再看树。

如果树里看到子代理处于 `RUNNING/channel_OK`，这代表 runner 已启动但还没写回结果；按配置的等待间隔稍后再查。不要把短时间 `RUNNING` 当失败，也不要反复抢占式重派同一个 run_id。

## 取消

子代理卡住时，父代理使用 `cancel_subagents`：

1. 按 run_id/root/status 选择目标。
2. 写 CANCELLED/ABANDONED 和审计。
3. 废弃 active attempt。
4. 尽量 interrupt/terminate 已知 pid/session。
5. 父代理说明原因后接管、重派或汇总。

## 汇总

子代理内部 `final_report.md` 是证据，不是用户最终交付。父代理读 refs 和必要正文后，把最终报告写当前 task `output/` 或用户指定目录，并在 task workspace 记录索引和验收。

父代理汇总优先顺序：

1. `create_subagents` / `inspect_agent_tree` 暴露的 `child_output_read_order`。
2. 子代理结构化结果里的 `primary_artifact_refs`、`expected_outputs`、artifact refs。
3. 只有这些 refs 缺失或损坏时，才读取内部 `work/agents/<run_id>/final_report.md`
   这类审计文件做损坏修复证据；正常主链路不能依赖它。

普通读文件、列目录和 shell 不应把 `work/agents/<run_id>/` 当状态看板；看状态用
`inspect_agent_tree`。

## Compact 后

先读 `continue_packet.json`、`work_state_snapshot.json` 和 `task_rollup.json`。只有缺证据或 refs 损坏时，才深入读取具体子代理目录。
