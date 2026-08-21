# Subagent Runbook

## 模型可见控制面

父代理、子代理和孙代理共用同一递归关系，每一层只管理自己直接创建的下级：

- `create_subagents`：创建一个或多个直接下级，成功后由宿主立即自动启动。
- `send_guidance`：像用户给主代理插入补充消息一样，只向一个正在运行的直属 child 追加普通上下文。
- `cancel_subagents`：按精确 `run_id/run_ids` 打断或取消直属 child，不提供整树、状态筛选或越层操作。
- `resolve_capability_requests`：批准或拒绝直属 child 的结构化权限申请；它不是催办、推进或验收工具。

模型没有 `inspect_agent_tree`、`wait`、`dispatch_subagents` 或
`schedule_child_subagents`。代码里的 dispatcher、heartbeat、orphan reconciler 和代理树 projection
都是宿主底座：负责启动、租约、恢复、通知、`/status`、TUI 和诊断，不由模型手工推动。

## 状态与通知

创建回执只给本批 run ids、结果读取 refs 和 `await_lifecycle_event`。父级可以继续自己的工作，也可以
结束当前回合；child 的进展、阻塞、权限申请和完成会作为结构化生命周期事件直接唤醒它。不要用 shell
`sleep`、新建“巡检代理”或反复调用其它工具猜状态。

宿主内部 `agent_tree_status_payload` 会展示 run/parent/root、状态、heartbeat、当前步骤、失败原因和
artifact refs，但这是运维状态投影，不是模型工具。用户可以通过 `/status` 和 TUI 看，恢复器也可以读；
普通模型只消费创建回执和送到本层的 lifecycle event。

如果 child 是 `RUNNING`，说明 runner 已启动但尚未写回结果。provider/网络失败、进程崩溃、租约失活或
明确超时由 heartbeat、typed retry 和 orphan reconciler 处理。它们只修执行可靠性，不判断工作质量。

## capability 阻塞与续跑

OPEN 或非法未闭合 capability request 是宿主掌握的结构化阻塞事实，优先于 provider 的普通
`turn_end=completed`：本轮 child 必须保持 `BLOCKED/UNVERIFIED`，不能因为模型结束了这一轮就变成
`DONE`。直属父级 grant 或 deny 后，宿主把同一个 run 重排为 `PENDING`、恢复 conversation link，并由
裁决事件触发 dispatcher 续跑；不得另建 replacement，也不需要父级调用推动工具。

主代理很少出现同类“挂掉”，是因为它通常已经拥有当前工作区，而且没有跨 child runner 的能力申请/
裁决边界。child 过去看似挂掉，实质是“申请已落账—本轮被误关—裁决没有重新排队”三段状态断链。

## 路径与写权限

普通 child 自动继承直接父级的结构化产品写区；孙代理继续逐层继承同一上界，不能扩大到父级之外。
因此项目目录本来就在父级 workspace 内时，父级无需为 child 重复申请或声明权限。

`output_files` 仍应记录用户明确的目标文件/目录，批量创建时由负责写入的 item 分别声明；它负责交付
身份、结果读取顺序和冲突锁，不是普通 child 唯一的写权限来源。goal 或 output_files 都不能把权限扩大到
父级 workspace 外。命名 Audit/exact-scope worker 不继承普通产品写区，只使用其精确结构化授权。

没有用户指定目标时，child 使用系统分配的 task-local work/output 路径，父级从创建回执或生命周期事件
里的 `child_output_read_order`、`primary_artifact_refs`、`expected_outputs` 读取结果。

## 插话、取消与替代

- 补充要求：`send_guidance(target, message)`，target 必须是当前代理的直属 child。
- 停止：`cancel_subagents(run_id|run_ids, reason)`，只停止点名的直属 child。
- 权限：`resolve_capability_requests(run_id, decision, reason, ...)`，只裁决直属 child。
- child 的 child 由 child 自己管理；根代理不能越过中间层直接控制孙代理。
- 已结束且目标仍有缺口时，可以创建职责明确的新 child，并用
  `replacement_for_run_ids` 记录接管关系；不能把“再派一个”当状态查询。

宿主恢复和运维代码可使用内部整树取消/恢复 primitive，但这些参数和入口不得重新进入模型 Schema。

## 汇总

child 的自然最终回复、typed lifecycle event 与真实 artifact refs 是父级汇总输入。推荐顺序：

1. 创建回执或完成事件里的 `child_output_read_order`。
2. 结构化结果里的 `primary_artifact_refs`、`expected_outputs` 和 artifact refs。
3. 只有结构化 refs 缺失或损坏时，才把 run 内部报告当恢复证据。

`work/agents/<run_id>/canonical_state.json`、`checkpoint.json`、`summary.md` 和内部
`final_report.md` 是审计/恢复资料，不是正常状态面或默认汇总入口。普通文件/shell 工具若碰到这些路径，
会返回结构化 child result index，模型应转读其中给出的产物 refs。

父级基于这些事实自然向用户汇报；普通任务没有机器质量验收器，也不要求 `VERIFIED` 才能结束。路径
不存在、工具失败、越权或取消仍按客观事实如实暴露。

## Compact 后

每个代理都在自己的 workspace 使用同一通用 Compact。主代理恢复时读取 thread summary/raw tail、当前
task 状态、未消费 lifecycle event 和 artifact refs；child 从自己的 canonical state/checkpoint 续接。
不存在根任务专用、子代理专用或“查树后再推动”的第二套 Compact/恢复包。
