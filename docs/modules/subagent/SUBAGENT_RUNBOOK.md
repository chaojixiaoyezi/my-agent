# Subagent Runbook

## 用户模型选择与递归继承

`create_subagents` / `items[]` 可选 `model`：填写当前用户经 `/model` 新增的模型名称或配置编号。
不填继承直接父级当前模型；显式名称重名时用编号。接口/密钥/窗口只从私有配置读取，不能写进派工参数。
因此“主 A、子 B”不需要切换主菜单，子代理及未显式覆盖的孙代理绑定 B；未知模型先请用户新增配置，
不能默默回到 A。部署默认项不是新增配置；显式选用它作为另一模型时先在 `/model` 新增对应配置。
批次中任何一个模型引用无效，整批零创建；已经运行中的其他代理保持原模型与原状态。

用户 `/model` 选择的整组模型字段使用同一显式来源优先级。任务/运行配置中较旧的模型字段不能把
地址、密钥或窗口拆开覆盖；无关配置叠加保留宿主 profile ID。child 和 grandchild 创建/恢复都沿此
引用读取本 owner 的配置，不受后来主菜单选择影响。普通角色/权限/工作区 overlay 继续原优先级规则。

## 模型可见控制面

主代理、子代理和孙代理共用同一递归关系，每一层只管理自己直接创建的下级。根主代理与结构化
`can_spawn_children=true` 的 coordinator 可以看到下面五个入口：

- `create_subagents`：创建一个或多个直接下级，成功后由宿主立即自动启动。
- `list_agents`：按需只读查看当前可见代理树；不推进、不等待、不重试、不取消，正常等待时不要轮询。
- `send_guidance`：像用户给主代理插入补充消息一样，只向一个正在运行的直属 child 追加普通上下文。
- `cancel_subagents`：按精确 `run_id/run_ids` 打断或取消直属 child，不提供整树、状态筛选或越层操作。
- `resolve_capability_requests`：批准或拒绝直属 child 的结构化权限申请；它不是催办、推进或验收工具。

worker、researcher、tester、writer、bug-finder 等普通 leaf 不创建下级，所以五个直属控制入口全部从其
工具快照移除；leaf 只保留执行工具和给自己申请权限的 `capability_request`。某个 child 确实需要继续拆分
时，应在创建时明确选择 coordinator，不能靠 goal、展示名或历史 grant 临时扩权。

模型没有旧 `inspect_agent_tree`、`wait`、`dispatch_subagents` 或
`schedule_child_subagents`，也没有自报进展的 `raise_event`。代码里的 dispatcher、heartbeat、
orphan reconciler、observation/wake 和代理树 projection
都是宿主底座：负责启动、租约、恢复、通知、`/status`、TUI 和诊断，不由模型手工推动。`list_agents`
只是同一 projection 的 会话运行时 式只读适配，不恢复旧巡检/推动语义。

默认同一 root 可保留 8 个未结束 child，终态会释放槽位；历史累计数量可以超过 8。批量
`create_subagents` 默认不再另设“单次最多 4 个”，整批只按当前 root 可用槽位原子接受或拒绝。
`subagent_hierarchy_max_children_per_tool_call` 仍可由部署方显式设置更小批次，`0` 表示不额外收紧；
默认 `runner_auto_concurrency=8`，所以八个已创建 child 可以真正并行启动。
这份容量不是根代理专属：child/coordinator 创建 grandchild 时也在同一 owner-local 创建事务里复用同一个
session/owner/task/per-call 计算，任意一层超限都整批 `not_started`，不会截断成部分创建。runner 并发上限与
创建容量仍是两件事，和 会话运行时 的 session registry / execution limiter 分工相同。

`items` 里的所有 child 都会立即并发启动，因此只允许放彼此独立、无需等待兄弟未来结果的工作。把 goal
写成“先修复、再测试”不会形成串行顺序；若测试必须读取本轮修复后的代码，应先只创建修复 child，等其
生命周期完成事件自动唤醒父级后，再创建测试 child。该约束对齐 会话运行时 的独立 sidecar 派工纪律，只是
模型可见的执行合同：宿主不解析 goal/role 猜依赖，也不恢复机器验收或另建依赖调度器。

## 状态与通知

provider 达到输出长度限制时，宿主的 `turn_end_reason=max-tokens` 必须随 canonical final 保存。
child 详情页将已收到的正文与技术提示分开，空正文也能显示原因；提示本身不决定重派、取消或完成。
它与主代理/后台历史共用同一协议，不能通过回复中是否包含“完成”或“截断”来判断。

创建回执给本批 run ids、真实 `execution_cwd`、结果读取 refs 和 `continue_independent_work`。
主、子、孙代理可继续当前模型/工具循环，不因创建强制结束工作片。模型确实没有独立工作而自然让出时，
才把等待的直属 run ids 写入 canonical state；孤儿恢复器不会把正常等待误当挂死重启。
child 的阻塞、权限申请和完成会作为结构化生命周期事件唤醒直属父级。不要用 shell
`sleep`、新建“巡检代理”或反复调用其它工具猜状态。

成功 child 按已有短合批窗口投递，不等最慢兄弟；递归等待也可由任一未读结果解除。
父级忙时在原安全点接收，空闲时沿原调度车道唤醒；已消费事件不重复确认。
失败、缺失 canonical state 或 capability 阻塞会及时交回。父级新工作片的 `direct_children` 包直接给出
status、turn end、failure type、有界最终回复、正式 artifact/声明输出 refs 和待裁决请求；过长最终回复可按
`final_report_ref` 继续读取。内部 runner result/output/response 文件不进入正常父模型 prompt。

宿主 `agent_tree_status_payload` 会展示 run/parent/root、状态、heartbeat、当前步骤、失败原因和 artifact
refs。用户可以通过 `/status` 和 TUI 看，恢复器可以读，模型也能在用户询问或需要核对明确 run 时通过
`list_agents` 读同一有界投影；正常工作仍只消费创建回执和送到本层的 lifecycle event，不靠轮询续跑。

如果 child 是 `RUNNING`，说明 runner 已启动但尚未写回结果。provider/网络失败、进程崩溃、租约失活或
明确超时由 heartbeat、typed retry 和 orphan reconciler 处理。它们只修执行可靠性，不判断工作质量。
状态面还会显示宿主观测到的短活动，例如“模型响应中”“正在使用工具：write_file”或“工具失败”；这些
内容来自 typed 阶段和工具名，不是模型自报，也不会公开 prompt、response、工具输出或隐式思考正文。

## capability 阻塞与续跑

OPEN 或非法未闭合 capability request 是宿主掌握的结构化阻塞事实，优先于 provider 的普通
`turn_end=completed`：本轮 child 必须保持 `BLOCKED/UNVERIFIED`，不能因为模型结束了这一轮就变成
`DONE`。直属父级 grant 或 deny 后，宿主把同一个 run 重排为 `PENDING`、恢复 conversation link，并由
裁决事件触发 dispatcher 续跑；不得另建 replacement，也不需要父级调用推动工具。

grant/deny 是直属父级在自己既有 authority 内的编排裁决，运行层按 mutating 控制执行，不再为 grant
额外要求普通用户理解内部 run/path/tool 后再确认。可授权范围仍只能来自 direct-parent、owner wall、父级
workspace/write roots 和当前 Skill/Tool 快照；任何越界、跨 owner 或父级本身没有的能力都必须结构化拒绝或
上抛，模型正文里的“批准”没有提权效力。它与下面一次具体危险 ToolCall 的 exact approval 仍是两份账。

`coordinator` 的 `create_subagents` 不是 capability_request：builtin 结构化角色模板声明
`can_spawn_children=true` 后，直接在父级继承的 depth/capacity/owner/workspace 上限内暴露 edge-local 管理工具，
由创建 handler 再守上限。标准 wheel 必须携带 builtin role JSON；缺少模板时应由 distribution gate 阻断发布，
不能在运行时静默降成 leaf 后再期待模型自行申请同一份固有角色能力。

主代理很少出现同类“挂掉”，是因为它通常已经拥有当前工作区，而且没有跨 child runner 的能力申请/
裁决边界。child 过去看似挂掉主要有两条状态断链：一是“申请已落账—本轮被误关—裁决没有重新排队”；
二是“创建孙代理后父级 PENDING—孤儿器立即误复活—只能反复轮询，孙代理结果又越级发给根会话”。
当前两条都由结构化状态和直属事件链修复，不需要增加催办工具。

## 具体工具审批

capability grant 与一次具体副作用批准是两件事。grant 只缩小 child 能使用的工具、命令、路径和网络范围；
即使 grant 完整匹配，ToolExecutor 对 `ask` 动作仍必须生成 exact `ToolApprovalRequest`，不能直接执行 handler。

交互式 TUI/Web 存在时，child 的 `BackgroundTranscriptSink` 将 request 发布到所属 owner
ConversationStore 的 `subagent_tool_approval.v1` 记录，并阻塞原 ToolCall。所属 TUI 通过
`/client/notices` 显式声明 `tool_approval` 能力、续短租约并领取请求；用户决定经
`/client/agent-permission`、owner/thread/root/current-attempt 门和完整 request 比对后写回。批准只让现有
ToolExecutor 对原 ToolCall 原地重试，不新造工具参数；拒绝/取消进入当前模型的结构化工具结果。

主代理与多个 child 同时请求时，TUI 只显示一个全局 FIFO 队首，标题会标明 child 名称。切入 child 详情不会
隐藏 root 审批面板。没有交互客户端、客户端断线、租约过期、child 终态、请求损坏或写回失败时，系统不会
自动批准；等待者返回 unavailable/cancelled，模型可继续改方案或向用户如实说明。详细身份、租约和故障语义
见 `docs/design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md`。

## 路径与写权限

普通 child 自动继承直接父级的结构化产品写区；孙代理继续逐层继承同一上界，不能扩大到父级之外。
因此项目目录本来就在父级 workspace 内时，父级无需为 child 重复申请或声明权限。

`output_files` 可记录用户明确的目标文件/目录，批量创建时可由负责写入的 item 分别声明；它负责交付
身份和结果读取顺序，不是普通 child 的完整写集或写权限来源。可以省略；同批可以声明共享项目目录，
不因相同或祖先/子目录路径自动合并、拒绝或生成独占锁。具体文件的并行分工由父级安排。提供的
路径也不能扩大到父级 workspace 外。命名 Audit/exact-scope worker 不继承普通产品写区，只使用其精确结构化授权。

root 继续对用户完整目标负责；普通 child 只把直接父级给自己的当前 `goal` 当作本轮完整工作边界，必须
完整完成该 goal，但不得因为根目标更大而替兄弟计划项扩做。宿主不解析 goal 或扫描 diff 做机器验收。

并行 worker 共享同一实时 workspace，但每名 worker 只修改父级明确分配的文件/模块范围。看到兄弟改动时
不得回滚、覆盖或“整理掉”；整合必须碰兄弟范围时，先向直接父级报告并重新分工。修改已有文本的一处或
少数片段优先 `edit_file`，关联多文件修改使用 `apply_patch`；补丁上下文未命中先读工具回显的期望行和
最小最新片段再修正，不能用整文件 `write_file`、`sed/head/mv` 或 heredoc 绕过局部冲突。它是 会话运行时 式
软执行纪律，机器权限仍只认结构化工具快照与写边界。

`covers` 只在 child 确实原样承接一个 open Todo 时提供。省略时 child 用真实 run id 显示自己的进度，不
关闭现有 Todo；提供的未知、关闭或重复 id 会在创建前拒绝。若已完成项因路径、测试等问题需要返工，先
用 `task_progress` 对原 id 传 `status=in_progress, correction=true` 显式重开，再绑定原 id；不能为了通过
创建门拿下一个无关 open id 顶替。

路径解析与父级 cwd 一致：`abc/index.html`、`output/report.md`、`work/notes.md` 都是相对当前可信 cwd
的普通路径；没有内部 work/output 魔法重定向。绝对路径保留原目标，继续
由结构化写边界允许或拒绝，不能静默搬到内部 output 后冒充成功。

allow 与 forbidden 同时命中时按最具体路径条目决定，同层由 forbidden 胜出。例如 `/root` 仍可作为宽泛
保护，但 `/root/.my-agent/.../output/abc` 的更窄明确授权必须可写；反过来，同一路径被禁止时不能绕过。

没有用户指定目标时，child 继承父级可信 cwd 与 owner home 上界，按软整理约定在用户家中安排目录；
内部 run 工作区只保存运行记录，不强制作为业务产物目录。父级从创建回执或生命周期事件
里的 `child_output_read_order`、`primary_artifact_refs`、`expected_outputs` 读取结果。
输出声明与文件工具共用可信 cwd；权限不足时由工具明确拒绝，不把目标静默移到内部 output。
收口不自动复制业务文件；`run_workspace.output_dir` 不覆盖业务 cwd，输出声明不能新增写权限。

## 插话、取消与替代

- 补充要求：`send_guidance(target, message)`，target 必须是当前代理的直属 child。
- 用户从 TUI/Web 发给运行 child 的普通输入也走同一消息箱，但 Gateway 必须在写账前把消息绑定到
  canonical 当前 `AgentAttempt`。只有 `pending/running` attempt 可接收；执行片切换期间明确拒绝并保留
  用户输入，不能落一条没有 `expected_turn_id` 的消息，也不能因校验失败杀掉 child。
- 停止：`cancel_subagents(run_id|run_ids, reason)`，只停止点名的直属 child。
- 权限：`resolve_capability_requests(run_id, decision, reason, ...)`，只裁决直属 child。
- 权限裁决只读宿主提供的 exact `capability_request` 与 `parent_tool_authority`。根 child 的上限来自创建当轮
  不可变 Tool Gateway 快照，孙代理来自直属父 run 当前 execution context；模型不能用“我好像没有这个工具”
  或 child 自报字段替代。申请位于上限内时父级自主 grant，同一 run 续跑；超出时结构化 deny/fail closed。
- capability grant 不是具体危险动作的批准。获批工具执行时仍按 ToolCall effect/policy 和 owner 当前审批模式裁决，
  仅 `ask` 请求交用户确认；自主/管理员 Full Access 不再为范围内每个子代理动作逐次弹窗；
  排障必须分别找 capability request/grant、runner session、tool approval 和 tool result 四份账。
- MCP 短名只在直属父快照中存在唯一 exact 末段命中时由宿主规范成完整 `mcp__server__tool`；重名或未知
  不猜。若父级有全部 exact 工具而 request 已变 GAP，说明旧语义路由抢跑，不能让父级重复 deny 或新建替身。
- child 的 child 由 child 自己管理；根代理不能越过中间层直接控制孙代理。
- 已结束且目标仍有缺口时，可以创建职责明确的新 child，并用
  `replacement_for_run_ids` 记录接管关系；不能把“再派一个”当状态查询。

宿主恢复和运维代码可使用内部整树取消/恢复 primitive，但这些参数和入口不得重新进入模型 Schema。

## 汇总

child 的自然最终回复、typed lifecycle event 与真实 artifact refs 是父级汇总输入。推荐顺序：

1. 完成事件或 `direct_children.items[].completion_message` 里的 child 最终回复。
2. 同一交接包里的 artifact refs、`declared_output_refs` 与 `final_report_ref`。
3. 只有这些结构化交接内容缺失或损坏时，才由宿主把 run 内部结果文件当恢复证据。

自然回复不需要附加 JSON 才能交回文件：收口读取 exact run 的工具产物 registry，把最新 ready 记录
投影进 canonical artifact refs 和结果信封。工具输出归档、其它 run、已删除或缺失记录不算交付物；
账本读取错误单独留诊断。模型报告的完成与项目质量不由这些引用自动判定。

执行 child 只写 `required_file_refs` 或父级明确声明的业务产物。内部 final report、runner result 和 closeout
记录由宿主在 child 自然 final 后自动生成，不会出现在 child 的 output contract、task packet 或 workspace
refs；不要为“完成交差”主动创建这些运行时文件。若用户明确要求的业务文件恰好叫 `final_report.md`，仍按
精确 `required_file_refs` 写入，不能仅凭文件名把它隐藏。

attempt 续跑时只按模型可见的 checkpoint、summary、task 与明确 handoff 接续；不要读取本 run 的
output/runner-result/final-response 来反推工作。完整 execution-context 和完整 write boundary 属于宿主审计与
工具执行面，runner prompt 只收到安全投影和 context bundle。

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

读取当前 Todo 时使用 `task_progress(action=read)`，不要把界面或恢复包里的当前 task id 当成另一份
`run_id`。底座会把误传的当前 typed task id 归一到同一 task-path 账本；只有确实要查看另一个历史 run
时才显式传不同 id。该别名裁决只认当前运行参数和 ConversationStore 链接，不解析任务正文。

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

用户通过 `/model` 选择自定义模型时，新 child 的 `host_model_profile.v1` 由父级真实配置来源生成，
忽略模型参数里的同名伪造属性。子代理在 owner/dispatch 授权后按该私有引用恢复接口、密钥和显式窗口；
菜单后续切换不热改当前 child。未使用自定义模型的旧任务保持原部署配置继承，不做批量迁移。

main/child/grandchild 现在各自从独立 ConversationThread 的 summary/raw tail 恢复，并全部走同一
Conversation Compact。task-local 只保留子代理权限、Memory 隔离和运行工作区，不再生成旧 compact apply/
continue。排障时以 `task.agent_thread_id -> ConversationThread.compact_generation/checkpoint_id` 为正式次数与
恢复事实；允许持久化的活动回合 native IR reduction 必须以 `source_kind=live_tool_ir` 进入同一 checkpoint/
generation，实时事件只投影提交结果。presentation/no-save 临时 reduction 不计数。

provider overflow 可能让同一 request/run 在 Compact 前后各收口一次。模型调用内存账是累计快照，会话用量
JSONL 是逐次增量：事件 id 必须包含 `physical_model_attempt_count` cursor，Store 在唯一锁内减去同 scope
既有增量，并保存原快照 digest。排障时若第二次模型回复已成功却 child 反向失败，先查
`conversations/model_usage/<thread>.jsonl` 是否出现 scope-only event id 异值复用；不得通过清账、忽略冲突或把
两份累计快照直接相加来绕过。
