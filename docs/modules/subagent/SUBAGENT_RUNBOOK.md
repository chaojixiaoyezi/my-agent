# Subagent Runbook

## 模型可见控制面

主代理、子代理和孙代理共用同一递归关系，每一层只管理自己直接创建的下级。根主代理与结构化
`can_spawn_children=true` 的 coordinator 可以看到下面四个入口：

- `create_subagents`：创建一个或多个直接下级，成功后由宿主立即自动启动。
- `send_guidance`：像用户给主代理插入补充消息一样，只向一个正在运行的直属 child 追加普通上下文。
- `cancel_subagents`：按精确 `run_id/run_ids` 打断或取消直属 child，不提供整树、状态筛选或越层操作。
- `resolve_capability_requests`：批准或拒绝直属 child 的结构化权限申请；它不是催办、推进或验收工具。

worker、researcher、tester、writer、bug-finder 等普通 leaf 不创建下级，所以四个直属控制入口全部从其
工具快照移除；leaf 只保留执行工具和给自己申请权限的 `capability_request`。某个 child 确实需要继续拆分
时，应在创建时明确选择 coordinator，不能靠 goal、展示名或历史 grant 临时扩权。

模型没有 `inspect_agent_tree`、`wait`、`dispatch_subagents` 或
`schedule_child_subagents`，也没有自报进展的 `raise_event`。代码里的 dispatcher、heartbeat、
orphan reconciler、observation/wake 和代理树 projection
都是宿主底座：负责启动、租约、恢复、通知、`/status`、TUI 和诊断，不由模型手工推动。

默认同一 root 可保留 8 个未结束 child，终态会释放槽位；历史累计数量可以超过 8。批量
`create_subagents` 默认不再另设“单次最多 4 个”，整批只按当前 root 可用槽位原子接受或拒绝。
`subagent_hierarchy_max_children_per_tool_call` 仍可由部署方显式设置更小批次，`0` 表示不额外收紧；
默认 `runner_auto_concurrency=8`，所以八个已创建 child 可以真正并行启动。

## 状态与通知

创建回执只给本批 run ids、结果读取 refs 和 `await_lifecycle_event`。根主代理会先给用户一条短回执；
任意 task-local 父代理则立即以 `interrupted/SUBAGENTS_ACTIVE` 结束当前工作片。宿主把正在等待的
直属 run ids 写入 canonical state，孤儿恢复器不会把这种正常等待误当挂死重启。child 的进展、
阻塞、权限申请和完成会作为结构化生命周期事件唤醒直属父级。不要用 shell
`sleep`、新建“巡检代理”或反复调用其它工具猜状态。

成功同批 child 会等到收齐后只唤醒一次，避免每个 child 都让父级重读整份上下文。失败、
缺失 canonical state 或 capability 阻塞会立即唤醒。父级新工作片的 `direct_children` 包直接给出
status、turn end、failure type、有界最终回复、正式 artifact/声明输出 refs 和待裁决请求；过长最终回复可按
`final_report_ref` 继续读取。内部 runner result/output/response 文件不进入正常父模型 prompt。

宿主内部 `agent_tree_status_payload` 会展示 run/parent/root、状态、heartbeat、当前步骤、失败原因和
artifact refs，但这是运维状态投影，不是模型工具。用户可以通过 `/status` 和 TUI 看，恢复器也可以读；
普通模型只消费创建回执和送到本层的 lifecycle event。

如果 child 是 `RUNNING`，说明 runner 已启动但尚未写回结果。provider/网络失败、进程崩溃、租约失活或
明确超时由 heartbeat、typed retry 和 orphan reconciler 处理。它们只修执行可靠性，不判断工作质量。
状态面还会显示宿主观测到的短活动，例如“模型响应中”“正在使用工具：write_file”或“工具失败”；这些
内容来自 typed 阶段和工具名，不是模型自报，也不会公开 prompt、response、工具输出或隐式思考正文。

## capability 阻塞与续跑

OPEN 或非法未闭合 capability request 是宿主掌握的结构化阻塞事实，优先于 provider 的普通
`turn_end=completed`：本轮 child 必须保持 `BLOCKED/UNVERIFIED`，不能因为模型结束了这一轮就变成
`DONE`。直属父级 grant 或 deny 后，宿主把同一个 run 重排为 `PENDING`、恢复 conversation link，并由
裁决事件触发 dispatcher 续跑；不得另建 replacement，也不需要父级调用推动工具。

主代理很少出现同类“挂掉”，是因为它通常已经拥有当前工作区，而且没有跨 child runner 的能力申请/
裁决边界。child 过去看似挂掉主要有两条状态断链：一是“申请已落账—本轮被误关—裁决没有重新排队”；
二是“创建孙代理后父级 PENDING—孤儿器立即误复活—只能反复轮询，孙代理结果又越级发给根会话”。
当前两条都由结构化状态和直属事件链修复，不需要增加催办工具。

## 路径与写权限

普通 child 自动继承直接父级的结构化产品写区；孙代理继续逐层继承同一上界，不能扩大到父级之外。
因此项目目录本来就在父级 workspace 内时，父级无需为 child 重复申请或声明权限。

`output_files` 可记录用户明确的目标文件/目录，批量创建时可由负责写入的 item 分别声明；它负责交付
身份、结果读取顺序和冲突提示，不是普通 child 的完整写集、创建前置条件或写权限来源。提供的路径不能
扩大到父级 workspace 外。命名 Audit/exact-scope worker 不继承普通产品写区，只使用其精确结构化授权。

root 继续对用户完整目标负责；普通 child 只把直接父级给自己的当前 `goal` 当作本轮完整工作边界，必须
完整完成该 goal，但不得因为根目标更大而替兄弟计划项扩做。宿主不解析 goal 或扫描 diff 做机器验收。

`covers` 只在 child 确实原样承接一个 open Todo 时提供。省略时 child 用真实 run id 显示自己的进度，不
关闭现有 Todo；提供的未知、关闭或重复 id 会在创建前拒绝。若已完成项因路径、测试等问题需要返工，先
用 `task_progress` 对原 id 传 `status=in_progress, correction=true` 显式重开，再绑定原 id；不能为了通过
创建门拿下一个无关 open id 顶替。

路径解析与父级 cwd 一致：裸 `abc/index.html` 表示当前可信 workspace 下的 `abc/index.html`；只有显式
`output/report.md` 和 `work/notes.md` 才分别表示当前 task 内部的 output/work。绝对路径保留原目标，继续
由结构化写边界允许或拒绝，不能静默搬到内部 output 后冒充成功。

allow 与 forbidden 同时命中时按最具体路径条目决定，同层由 forbidden 胜出。例如 `/root` 仍可作为宽泛
保护，但 `/root/.my-agent/.../output/abc` 的更窄明确授权必须可写；反过来，同一路径被禁止时不能绕过。

没有用户指定目标时，child 使用系统分配的 task-local work/output 路径，父级从创建回执或生命周期事件
里的 `child_output_read_order`、`primary_artifact_refs`、`expected_outputs` 读取结果。

## 插话、取消与替代

- 补充要求：`send_guidance(target, message)`，target 必须是当前代理的直属 child。
- 用户从 TUI/Web 发给运行 child 的普通输入也走同一消息箱，但 Gateway 必须在写账前把消息绑定到
  canonical 当前 `AgentAttempt`。只有 `pending/running` attempt 可接收；执行片切换期间明确拒绝并保留
  用户输入，不能落一条没有 `expected_turn_id` 的消息，也不能因校验失败杀掉 child。
- 停止：`cancel_subagents(run_id|run_ids, reason)`，只停止点名的直属 child。
- 权限：`resolve_capability_requests(run_id, decision, reason, ...)`，只裁决直属 child。
- child 的 child 由 child 自己管理；根代理不能越过中间层直接控制孙代理。
- 已结束且目标仍有缺口时，可以创建职责明确的新 child，并用
  `replacement_for_run_ids` 记录接管关系；不能把“再派一个”当状态查询。

宿主恢复和运维代码可使用内部整树取消/恢复 primitive，但这些参数和入口不得重新进入模型 Schema。

## 汇总

child 的自然最终回复、typed lifecycle event 与真实 artifact refs 是父级汇总输入。推荐顺序：

1. 完成事件或 `direct_children.items[].completion_message` 里的 child 最终回复。
2. 同一交接包里的 artifact refs、`declared_output_refs` 与 `final_report_ref`。
3. 只有这些结构化交接内容缺失或损坏时，才由宿主把 run 内部结果文件当恢复证据。

后台 lifecycle wake 与用户在同一 TUI 里追加的普通前台消息必须读取同一份完成信封。普通追加轮会换
task id，但继续复用原 canonical task workspace；前台续轮会按 same-thread、非 detached task link 的 exact
task path 形成 workspace lineage，再从 ConversationStore observation 中选择 root 属于 lineage、且
`parent_agent_id == root_task_id` 的直属 child，按 task id 去重后注入最新结果。模型应直接用这些
`completion_message` 与精确 refs 汇总；不要猜 `child_outputs`、遍历 `work/agents`，也不要读取
`runner_result_json/output_json`。若有界视图显示 `omitted_count > 0`，改用正式子代理树/结果索引补读。

child 完成信封还必须携带创建它的 exact `conversation_request_id`。后台续片用 durable task id 找工作区，
但只按这个 active-turn id 恢复工具历史；新工具索引显式保存该字段，旧索引只在 row `request_id` 与之完全
一致时兼容。两者不得互相替代，也不能扫描同一长期任务的其它追加轮补齐。

`work/agents/<run_id>/canonical_state.json`、`checkpoint.json`、`summary.md`、`progress/` 是内部审计/恢复
状态面，不是正常汇总入口。普通文件/shell 工具若碰到这些路径，会返回结构化 child result index，模型
应转读其中给出的产物 refs。系统生成的精确 `final_report.md` 是例外：它是完成信封显式给直接父级的
有界交接投影，只含 task/run/status 与 child 最终回复，可由 `read_file` 直接读取；它不参与状态裁决，
目录枚举和 shell 也不能借此进入整个 agent 工作区。若模型把 exact run id 与过期 task 目录组合，
`read_file` 只可从同 owner 的 canonical agent projection 找回这一精确叶子，并对解析后的路径重做读取权限
检查；任何 sibling 状态文件都不跟随跳转。

这类拒绝发生在 shell 进程启动前，运行时记录为 `WRONG_STATUS_SURFACE/not_started`，把可操作原因返回
当前模型继续换正式读取入口；它不是副作用未知，也不能据此把 child 整轮挂起。该分类不允许模型绕过
状态面，已启动命令的真实 unknown 仍保持禁止自动重做。

父级基于这些事实自然向用户汇报；普通任务没有机器质量验收器，也不要求 `VERIFIED` 才能结束。路径
不存在、工具失败、越权或取消仍按客观事实如实暴露。

复杂任务只维护一份稳定 `task_progress` 清单。创建 child 时优先把每项负责的普通 Todo 或 coverage
exact id 放入 `covers`；child 进入 canonical `DONE` 后宿主只按该 id 原位打钩，不解析 title/goal 猜对应
关系。后续补派仍沿用原 id，不重建 `p1/s1` 之类同义计划。open 项只会在工具回执里给模型一条软续做
提示：已知缺口且仍有工具或 child 容量时继续协调；它不会让宿主自动再调用一次模型，也不会充当质量验收。

系统生成的 child 显示名在同一 exact parent 下跨单个、批量和递归创建连续编号，方便 TUI 与未来 Web
区分后续补派。显式自定义名称保持原样；控制、权限、状态和产物归属始终只认结构化 run_id/lineage，
不能用显示名代替机器身份。

普通可恢复工具失败只作为 ToolResult 返回当前 child/grandchild 的模型继续修正，宿主不会按同类失败
次数替它结束 turn。达到提示阈值时只要求换参数、换工具或拆小步骤；精确同参机械重试可拒绝该次动作。
只有取消、明确安全边界、副作用真实 unknown 或部署者显式启用的 typed hard policy 才能硬收口。同一批
后到的同工具成功必须覆盖更早失败留下的 active halt，不能让并行返回顺序把已经恢复的 child 挂回
`PENDING`。

父级给 child 的共享读取预览只来自创建发生时当前 tool loop 已完成的 read/search 记录。进程内不会缓存
上一任务的阅读包给下一任务复用；需要跨轮保留的事实必须进入正式 transcript、Compact 或结构化 refs。

## Compact 后

main/child/grandchild 现在各自从独立 ConversationThread 的 summary/raw tail 恢复，并全部走同一
Conversation Compact。task-local 只保留子代理权限、Memory 隔离和运行工作区，不再生成旧 compact apply/
continue。排障时以 `task.agent_thread_id -> ConversationThread.compact_generation/checkpoint_id` 为正式次数与
恢复事实；允许持久化的活动回合 native IR reduction 必须以 `source_kind=live_tool_ir` 进入同一 checkpoint/
generation，实时事件只投影提交结果。presentation/no-save 临时 reduction 不计数。
