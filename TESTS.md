# TESTS

当前测试文档只保留常用入口。完整文件清单以 `agent_py_agent/tests/` 为准，不再手工维护旧表格。

默认按改动范围运行 focused tests，不重复运行全仓 pytest。只有本轮生产代码和测试代码新增、删除累计
约 10,000 行以上，或用户明确要求时，才追加一次 `python3 -m pytest -q --tb=short`。静态检查、文档同步、
严格代码尺寸、diff 和 clean-package 守卫仍按发布风险执行，不用全仓 pytest 代替。

测试机上的所有真机用例只使用一个真实 Gateway。需要并行时启动多个独立 TUI/会话并统一连接该实例，
不得为 input、render、lifecycle、isolation、replica 或 steer 等用例另开端口和 Gateway。每轮真机测试前先
确认只有一个 Gateway 进程和一个配置端口；故障恢复用例顺序重启或中断这个实例。合同单测中的 fake
Gateway 可以并行，但不能作为“单 Gateway 多客户端”验收的替代证据。

2026-08-22 起，任何真实 TUI 启动、切换或输入之前，测试者必须先在用户可见消息中报告测试对象、测试机、
唯一 tmux session 名称和完整观看命令，例如
`ssh root@192.0.2.7 -t 'tmux attach -t <session>'`；报告之前不得先操作该 TUI。正式功能验收固定使用
`MiniMax-M2.7`，并依次输入下面四个原样任务，不得用玩具 prompt 替代：

1. `你在底下创建一个abc目录，然后在abc目录里写一个web页面的植物大战僵尸游戏，不准自己写，必须多个子代理，要求游戏真实，带20个关卡，关卡设置合理，你只能盯着，并且要求最后是0.0.0.0局域网都能访问`
2. `在自己的任务目录底下创建一个bbb的目录，在里面写一个超级玛丽的游戏，要求不准自己做，必须创建多个子代理，需要复刻原始超级玛丽的所有玩法和前三个关卡，直接网页版可玩`
3. `深度调研轻量运行时,工具运行时,会话运行时,终端交互,deepseek-harness,代理运行时,通道运行时,长期助手架构和完整功能，做代码级别的学习和了解，不准自己去做，只能派子代理去做调研，完整列出每个功能目录如何做的，技术实现，架构方案，优缺点等等。最后你整合进行横向对比。`
4. 先在该 TUI 对应目录下载一个 GitHub stars 超过 10,000、功能代码超过 20,000 行的项目，再输入：
   `换一种编程语言，完整复刻这个项目，要求实现所有的功能，所有测试，最后保证你复刻的这个项目能够和原项目一样，毫无缺陷的运行，并且所有功能和原项目一样可用和完善。你自己不允许进行复刻，只能创建子代理进行复刻和代码编写工作，你只能负责最后的测试功能和整合工作，你不允许写功能代码。`

这四轮都只把用户 prompt 输入一次；测试者只观察 TUI、typed 账本、日志、产物和真实服务，不替被测对象
补代码、发技术推动指令或修改产物。单测、fake、renderer snapshot 和静态 gate 只作上线前护栏，不能替代
上述真实 TUI 验收。

`931ee20` 在 `.7` 唯一 Gateway 上以 tmux `dsh-p3-research-931ee20-verify` 执行第 3 条原样任务，prompt
只输入一次。4 个 child 的短职责、实时 context 总 token、一次 attempt 自然 DONE 和 Todo 打标均正确；
但 main 只收到不含最终正文/报告 ref 的生命周期通知，猜测 `research_reports/` 后反问用户，未继续第二批，
因此该轮判定失败。canonical 证据显示四份 `task.result` 和每个
`work/agents/<child>/final_report.md` 均已落盘，问题属于完成交接，不属于模型未产出。

对应回归必须同时证明：每个 `subagent-completion.v1` 携带 typed status、最多 1000 估算 token 的最终回复
预览、精确 `final_report_ref` 和 declared/artifact refs；完成正文不得改变 typed status；同根 4 路成功
wake 在一次模型轮的 `metadata.events` 中全部可见；背景总上下文压到 2200 token 时仍保留四名 child 身份
和四个报告 ref；只确认实际进入本轮的 wake，新到事件继续 pending；失败与 Audit worker 不参加成功批。
修复后必须用全新 tmux/cwd 重跑同一 Prompt 3，仍只输入一次且不得给 main 发送“去哪个目录找”的提示。

`fd7d2b9` 部署后的第二轮使用 tmux `dsh-p3-research-fd7d2b9-verify2`，首次 4 名 child 的完成信封全部被
main 消费并触发第二批，第二批 代理运行时/长期助手 的两份信封也被消费；但最终只创建 6/8 名 child，漏掉
轻量运行时/通道运行时，横向汇总仍结束。canonical 文件证明 8 项原计划保存在 task-path 账本且仍 open，而后台
Task Runtime State 旧代码读取 request-id 账本。对应回归必须在相同 owner 下同时建立两份不同摘要的账本，
断言后台只注入 task-path 真账并同步其 child seed，且普通 open item 不触发机器验收或普通任务自动续跑。
部署后仍须使用全新 tmux/cwd 进行第三轮原样 Prompt 3，单测不能替代。

单 Gateway 多目录回归必须另行覆盖：两个不同 cwd 的 lightweight client 得到同一 owner-level
`gateway_workspace`，但请求分别携带自己的绝对 `workspace.cwd/roots`；thread 在后续未覆盖的请求中保留
该范围；模型前 workspace gate 拒绝相对/不存在目录；Tool Registry 的 path gate、resource lock 与 handler
使用同一 effective cwd；任务 workspace 提示和 child 相对 output ref 也落在该 cwd。runner future 抛异常
必须保存 `BLOCKED/UNVERIFIED/runner_worker_error`，不得被异常处理分支的二次错误掩盖。

`7c052f2` 在 `.7` 唯一 Gateway 上的第 1 条原样任务使用 tmux
`dsh-p1-pvz-7c052f2`，测试者只输入一次 prompt，六个 child 全部自然 DONE。最终
`/root/abc` 共 9 个文件，`0.0.0.0:8080` 监听，loopback HTTP 200；该轮同时抓到三个真实
回归点：首条 Todo 在 task 晋升前写入 request-id 账本，派工后却使用 task-path 账本；显式
`covers=[Todo id]` 未进入 child 展示投影，所以 8 项全未勾选；一次 main 模型轮返回被误显示为
“整理最终回复”，且 main 被错放在输入框下方。当前候选已增加“先晋升再选账本”、
`covers -> progress_item_ids`、大输出 result-envelope Todo 快照、`waiting` main 轮状态，并按 终端交互
`SpinnerWithVerb` 把main `Working` 放到正文末尾/Context 前，输入框下只保留 child。相关 6 文件
focused 组合 147 项已通过，但候选未部署前不计真机通过。

2026-08-21 子代理自然收口与 TUI 可观察性回归分三层执行：第一层覆盖 `turn_end`、普通自然完成、
递归 `create_subagents` 自动启动、模型工具表不含 dispatch/schedule/wait/inspect、父级直属
guidance/cancel/capability、cancel schema/回执不含 dry-run 查树旁路、普通 child 工作区继承，以及
OPEN request → BLOCKED → grant/deny 后同 run
PENDING 续跑；
第二层覆盖 TUI Working 动画、直属 child 固定职责行、实时上下文 token、thinking 灰色增量、条件式 follow-tail 与 Compact typed progress；第三层只在
`192.0.2.7` 的单 Gateway/真实 TUI 输入一次目标 prompt，测试者只观察产物、日志、child 树和 8080
监听，不旁路补代码。当前 backend focused 144 项、context/protocol focused 75 项已通过。由于本轮累计
增删超过 10,000 行，发布前追加一次且仅一次全仓 pytest；若发现失败，修复后只重跑失败项和相关 focused。

`714c0c8` 的直属活动区真机 smoke 使用 `.7` 唯一 Gateway 和 `my-agent-panel-smoke` TUI，会话一次创建
两个 child，分别在 46 秒、52 秒一次 attempt `DONE`，`a.txt=苹果`、`b.txt=月亮`。活动区从等待启动、
模型响应、工具活动推进到 `0 进行中 · 2 完成`，测试者没有插话推动。第二条完成 wake 的持久文件、ready
发现和 root active 状态都正常，但旧进程未取得 claim；优雅重启后同一 wake 立即运行并约 30 秒汇总。
后续回归必须把“durable source ready，但 process-local lane 长时间 queued/running 且无有效推进”作为独立
故障注入，证明不靠人工消息或 Gateway 重启也能有界自愈。

本轮唯一一次有效全仓测试使用仓库 `.venv` 运行到 100%，暴露 39 个失败：真实缺陷是窄终端闭合思考行
全角括号宽度漏算和 finalize 轻量参数缺少 `prompt` 时的防御读取，其余主要是测试仍断言已删除的
`SUBAGENT_RESULT`、手动 dispatch/schedule 与机器验收。修复后，对这些失败来源收集到的 468 项 focused
组合只剩 2 个测试期望/导入问题，二者精确复测 2/2 通过；动作协议/CLI 121 项、状态投影 36 项另行通过。
按用户约定不再重复全仓 pytest。

同日追加的 cwd/直属控制回归必须证明：`allowed_write_roots` 不选择工作目录，只有宿主
`execution_cwd` 可覆盖项目 cwd；Gateway 根任务和递归 child 的裸 `abc/...` 都落用户 cwd，隐藏 task
root 只接受显式 `work/...`/`output/...`；每个 child prompt 不含 `sibling_roster` 或兄弟完整 goal；
retryable child 仍可被直属父级显式打断。`/stop` 在前台请求已让出且没有 live claim/process 时仍选择当前
thread 的 ordinary root；TUI Working 只数 `status=active` link，interrupted sticky link 必须显示 0。

`.7` 的 `26563ac` 原样 TUI 进一步要求覆盖“任务晋升后的主代理写权”：主代理在 foreground 创建
`/root/abc` 后，background continuation 写同一目录不能因只剩 task `work/output` 而
`WRITE_FORBIDDEN`，也不能绕到 `additional_write_roots` 后把旧目录冒充当前交付。合同测试固定验证本地
Gateway 主会话的 `execution_cwd` 和 `allowed_write_roots` 同时包含 ToolRegistry 项目根；远程 owner、
task-local child 与 transient Audit 的窄权限测试必须继续通过。

子代理完成触发的后台 `run-*` 回合也必须从已加载 thread 快照恢复同一
`cwd/runtime_workspace_roots`：模型可见 Workspace Context、工具 `execution_cwd`、相对 `output_files`
以及下一批 child 的项目根必须继续指向启动该 TUI 的目录，不能退回单 Gateway daemon 的 `/root`，也不能
出现 `None` 拼接路径。focused 回归至少覆盖后台 `RunParams`、write boundary 和 child output 根三处。

严格 code-size 初次被当前提交 `31f30fe` 自身的 25 个未登记 hard finding 阻断。为避免把存量债务冒充本轮
回归，先从 `git archive HEAD` 纯净快照生成 baseline，再修掉本轮唯一新增的 `_progress_payload` 深嵌套；
当前 strict gate 通过，原始报告中的 4 个 hard 均能在纯净基线复现，本轮新增 hard 为 0。baseline 不取
当前脏工作树，因此没有把本轮新增问题写成豁免。递归创建的同配置每次上限与新建后代空验收字段又以
44 项 hierarchy/orchestration focused 复测通过。

`.7` 真实 TUI 首轮按原样植物大战僵尸 prompt 请求 3 个 child 时，结构化回执显示
`owner_active=12/available=0`，证明旧实现把其它会话的历史未终态 run 算进当前会话容量；同时该确定性
整批拒绝被误包成 `TOOL_OPERATION_OUTCOME_UNKNOWN`。回归应锁定：当前 root 没有 child 时，即使 owner
另有 12 个可恢复 run，仍可按单次上限获得 4 个槽；容量不可读/超限均返回
`effect_outcome=not_started`，不进入副作用未知收口。

容量修复部署后，同一 TUI 成功自动启动 3 个 child，三者约 3 分钟内均为 `DONE`；但一个后台主代理
回合从部分完成快照开始，模型只看到 `2/3`，其采样期间最后一个 child 结束，旧最终化读取新状态并把
根任务错误标成 `completed`，8080 未启动。新增回归在模型采样回调里把最后一个 child 改成 `DONE`：
首个旧快照回合必须保持根任务 `active` 且不投递完成；下一次从 `subagents_terminal` 快照开始的回合才
允许自然收口。该回归与 background runtime、gateway conversation、active-turn guidance focused 同跑。

新鲜度补丁后的真实 TUI 继续暴露三项底座问题：后台主代理用 thread 级 run id 命中旧任务，导致新任务
attempt/工具权威链错挂；同 thread 后台 notice 复用稳定 block id，reducer 丢掉后续消息；child goal 中的
旧版 child 没继承父级 `/root` workspace，且 `/root/abc` 没进入 `output_files`，所以当时安全边界拒绝写入。
当前修复改为普通 child 继承父级结构化上界，`output_files` 只补交付身份与锁。定向回归现覆盖 task-bound `_run_params`、
旧 run 跨 task 复用拒绝、notice 首次消费/独立 block、`items[].output_files` 嵌套 schema，以及
`send_guidance(target,message)` 的单 child/直接父子授权。创建后自动 `dispatch_supervision_auto` policy 和
旧 wait helper 已删除，历史 policy 只按结构化 tool 标记退休；不再用周期模型调用换取子代理可靠性。

`596118f` 首轮原样 TUI 任务又形成 capability 断链样本：child 工具账已存在 OPEN request，runner 却被
通用 completed 关成 DONE，父级 grant 后没有同 run continuation，主代理遂开始轮询并自行写产品。定向
回归必须锁住四件事：OPEN 优先投影 BLOCKED；grant 与 deny 都重排同 run；取消/接管终态不能复活；根、
子、孙只能操作直属下级。模型工具表和历史 wake policy 同时不得出现 inspect/dispatch/schedule。

`e321483` 的真实前台运行还要求检查“首次模型调用的工具面”，不能只检查进程注册表。
回归必须确认默认 `model_visible_specs()` 直接包含 create/guidance/cancel/resolve，仍不含已删除的
inspect/dispatch/schedule/raise_event；`tool_search` 继续只加载 goal/web/vision/MCP 等延迟能力。真机必须以
`create_subagents` 真实 tool call 和 2 个以上 child state 作为证据，不认模型文字里“我要派子代理”。

`c2c0235` 真机路径失败新增两组回归。写边界必须证明“宽 allow + 宽 forbidden + 更窄 task output allow”
允许写窄目录，而 allow/forbidden 同层仍由 deny 胜出；runner 活动必须用真实 `ToolResult.tool_name`，并在
模型请求、工具开始/结束时把有界短状态写进 canonical child state，不能保存 prompt、response、工具输出
或思考正文。发布前 focused 至少包含 `test_write_boundary.py`、`test_subagent_kernel.py`、
`test_subagent_debug_trace.py`、编排工具常量/注册和文件工具边界测试。
编排工具常量回归还会扫描所有活跃内置 `SKILL.md`，防止工具已退休后 Skill 仍教模型调用
`inspect_agent_tree` / `dispatch_subagents` / `schedule_child_subagents` / `raise_event`。

`d257dfb` 真机复验的 3 个 child 和 8 个文件已证明写边界/活动状态修复生效；最终 wake
被同 owner 另一条长 policy 回合饿死，增加后台会话车道回归。必须同时覆盖：
实际 `ready_thread_ids` 保留两条 durable thread identity；fake scheduler 的长 thread 不阻塞同 owner
的 sibling；真实 `BackgroundMainAgentScheduler` 在第一个模型调用挂起时，第二个同 owner
会话必须在 1 秒内进入模型。同 thread 仍由持久 run claim 单飞，不能为过测试放宽成双执行。
真机复验继续只用一个 Gateway，故意保留或创建一条其它 TUI 长后台回合，再确认当前
植物大战僵尸会话能自动消费 child 完成 wake、整合、启动受管服务并从独立请求核验 8080。

`bea6fed` 的四 child DONE/HTTP 200 样本还要锁住递归等待成本：task-local 父级创建孩子后必须返回
`interrupted/SUBAGENTS_ACTIVE` 并留下 exact direct-child wait；dispatcher/orphan 恢复不能在孩子活跃时
重新采样父级；嵌套 child 不发根会话 wake；同批成功收齐只释放一次，失败/缺状态/capability 阻塞立即
释放；崩溃巡检能从耐久标记补偿丢失事件。恢复后的 runner context 必须含有界 `direct_children`
status/result/artifact refs。`task_progress` 回归同时证明 open 项只作软账本、普通 final 零自动续跑，
写后 canonical child DONE 不能被模型传入的 pending 覆盖。

`e4cd58b` 部署后的原样 TUI 证明输出冲突还需区分两类：同批 item 重用一个路径时
`existing_run_ids` 必须为空，回执要求 `revise_proposed_output_scopes_and_retry`；真实未结束
sibling 占用时才返回 `await_existing_run_lifecycle_event` 和其 run id。两种失败都是整批
`not_started`，必须带 `preserve_user_constraints=true`；前者不得诱导模型等待不存在的 run。

`be531a8` 真机的 4 child 样本要锁住工作区继承：local child 的结构化
`workspace_root` 同时存在于 `allowed_write_roots` 时，只移除完全同路径的默认 home deny；
`.ssh`/Downloads 等窄 deny 仍在。非 workspace 的同路径 allow/deny 仍 deny 胜出，远程 owner 也不做
该调和。TUI 回归同时覆盖：foreground `running=true` 但 id 未绑定时 `/stop` 不发；foreground
已让出时 `/stop` 以空 expected turn id 进持久 outbox，由 Gateway 只选当前 conversation 唯一 live task。

`db41bb0` 部署后的原样 TUI 复验已有四名 child 全部 `DONE` 和完整页面文件，但 8080 最终未监听。证据
显示模型在前台 `run_command` 内使用 `nohup ... &`，同一条命令里的 curl 得到 200 后，foreground shell
结束时未受管 child 被清理。新增回归覆盖：独立 `&` 在任何模式都以 `not_started` 拒绝；`2>&1` 和引号内
`&` 不误伤；真正的 `run_in_background=true` 在工具返回后仍能由 `process_status` 看到并由 registry 终止。
子代理侧同时覆盖单次 phase 快照、采样期间终态回复不公开、晚于采样时刻的 sibling wake 保持 pending，
以及全部 child 已终态时不依赖 active root task status 放行自然回复。定向 runtime/shell/error taxonomy
组合通过；按用户约定不重跑全仓 pytest。

`f1a7746` 部署重启后，旧任务的主 run/current attempt 已由崩溃调和置为 `unknown`，遗留 observation 每个
Gateway tick 仍进入自动挂载并触发 `RuntimeConflictError`。新增 focused 回归把 observation 上限设为 1：
旧任务事件必须保持未处理且零模型调用，同线程后到的新任务仍可运行；人工恢复后旧事件才被消费。仓储层
另验证 run/attempt 两层恢复投影、run `unknown -> created`、精确锁释放和新 attempt 挂载。真机部署后需以
Gateway 新日志偏移确认只出现一次结构化 recovery block、没有重复 `gateway-loop-error`。

2026-08-20 TUI 灰色层级与 tmux 复制修复在本地、`192.0.2.7` 各运行 renderer/view/input/ANSI/PTY/chat
6 文件 focused 组合，均为 105 项通过。测试机仅有一个 Gateway（8420），10 个 TUI 共享；真实中文请求
约 3.09 秒出现回答，ANSI capture 证明思考为 246 灰、助手正文为 231，`tmux load-buffer -w` 中文探针
完整。外层系统剪贴板必须在用户 attach 的终端执行一次真实粘贴才可标最终通过。全仓 Ruff 20 项、strict
code-size 29 项均为修复前基线已有且本提交未新增；用户在获知后明确授权推送和测试部署，因此不能把本轮
记录写成“严格发布 gate 全绿”。

同日用户确认外层仍不能通过右键复制后，本地新增正文/输入框“已有选区时右键按下直接复制”回归：右键
事件必须在原控件前被拦截，中文宽字符全文只复制一次，配对 release 不得重复写入或清除高亮；lost right
release 仍可由无按键 motion/下一次其它 press 解锁。上述六文件 focused 组合现为 107 项通过；测试机部署
和用户本机系统剪贴板粘贴在完成前仍不得标成通过。

2026-08-18 活动输入/控制回执提交候选先运行 12 个原失败文件的 focused 组合，再运行一次修复后的完整
`pytest -q --tb=short`，两者均到 100% 且退出 0。changed-file Ruff、doc sync 和 diff check 通过；全仓
Ruff 的 112 项属于当前 HEAD 存量扫描结果，本轮变更文件为 0。strict code-size 仍有 25 个 hard finding，
因此本轮只允许部署到 `.13` 测试机，不得据此推远端分支或宣称严格发布 gate 全绿。clean-package 必须在
新增源码/测试先进入 Git index 后重跑；未跟踪正式源码被它拦住属于预期行为，不能用排除规则绕过。

2026-08-18 活动回合普通输入与上下文可观察性追补使用以下 focused 组合：

```bash
python3 -m pytest -q --tb=short \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py
```

必须分别证明：薄客户端 `/ask` payload 带精确 `message_id/expected_turn_id` 和完整执行选项；运行中 Enter
在真实 Gateway ID 已知后才进入活动回合，提交窗口的本地 `chat-*` 不得冒充 turn；pending
receipt 在真实注入事件前不进入稳定历史；外部 ID 不得移除本地 pending；Gateway 拒绝只撤销同一 receipt
并保留 follow-up queue；pending/queue 位于 fixed input-status pane 而非 transcript；rich/non-rich 客户端的
确认事件边界不扩大；active→queued 必须保留 inject/files/save/resume/client capabilities 且 chat-style 只
出现一次；guidance receipt 必须重算 embedded entry digest；队列文件名与正文 ID 冲突必须按文件名失败
归档且 provider 零调用；context usage 与 active-turn compaction 事件
不携带正文。`.13` 还必须用真实长回合
覆盖“连续两条补充、滚离尾部、注入确认、结束竞态”四步，单元测试不能替代真机通过。

Gateway chunk JSONL 的两个本机 reader 必须共享 byte-offset 合同：只消费换行已完整落盘的 UTF-8 行，末尾
半行保留原 offset，下一次补齐后恰好交付一次；暂时读取失败不得把 offset 清零造成重复。focused 使用
`test_gateway_client.py + test_gateway_streaming.py` 同时覆盖富 TUI 与普通 CLI。

## Focused Commands

```bash
python3 -m pytest agent_py_agent/tests/test_tui_reference_fixture_server.py agent_py_agent/tests/test_tui_events.py agent_py_agent/tests/test_tui_view_model.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_markdown.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_view.py agent_py_agent/tests/test_tui_input.py agent_py_agent/tests/test_tui_interaction.py agent_py_agent/tests/test_tui_paste.py agent_py_agent/tests/test_tui_preflight.py agent_py_agent/tests/test_tui_terminal.py agent_py_agent/tests/test_tui_transcript.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_chat_prompt_queue.py agent_py_agent/tests/test_cli_chat.py agent_py_agent/tests/test_chat_parts.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_tool_loop_recovery_scope.py agent_py_agent/tests/test_tool_round_execution.py -q
python3 -m pytest agent_py_agent/tests/test_owner_resolver.py agent_py_agent/tests/test_config_normalize.py -q
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q
python3 -m pytest agent_py_agent/tests/test_lease.py agent_py_agent/tests/test_gateway_heartbeat.py -q
python3 -m pytest agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_agent/test_dispatch_capability_followup.py -q
python3 -m pytest agent_py_agent/tests/test_code_size_script.py agent_py_agent/tests/test_architecture_guardrails.py -q
python3 -m pytest agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_attempt_sandbox.py agent_py_agent/tests/test_sandbox.py agent_py_agent/tests/test_tooling_shell.py agent_py_agent/tests/test_owner_scoped_pip_env.py agent_py_agent/tests/test_path_access_owner_scope.py agent_py_agent/tests/test_graceful_shutdown.py -q
python3 -m pytest agent_py_agent/tests/test_shell_orphan_kill.py agent_py_agent/tests/test_process_registry_tools.py agent_py_agent/tests/test_shell_bg_log_cap.py -q
python3 -m pytest agent_py_agent/tests/test_container_install.py agent_py_agent/tests/test_check_clean_package.py -q
python3 -m pytest agent_py_agent/tests/test_mcp_registration.py agent_py_agent/tests/test_offline_contract_matrix_gate.py -q
python3 -m pytest agent_py_agent/tests/test_tool_input_completion_provenance.py agent_py_agent/tests/test_tool_input_schema.py agent_py_agent/tests/test_tool_call_policy_contract.py agent_py_agent/tests/test_tool_call_parameter_gate_wiring.py agent_py_agent/tests/test_runtime_gate_integration.py agent_py_agent/tests/test_backends_tool_schema.py agent_py_agent/tests/test_backends_tool_schema_precise.py agent_py_agent/tests/test_mcp_registration.py -q
python3 -m pytest agent_py_agent/tests/test_owner_object_store.py agent_py_agent/tests/test_scale_runtime.py agent_py_agent/tests/test_runtime_schema.py agent_py_agent/tests/test_deploy_manifests.py agent_py_agent/tests/test_continuous_monitor.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_adapter_feishu.py agent_py_agent/tests/test_asgi_ingress.py agent_py_agent/tests/test_scale_downstream.py agent_py_agent/tests/test_session_lock.py agent_py_agent/tests/test_persona_write_guard.py -q
python3 -m pytest agent_py_agent/tests/test_delivery_service.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_background_main_wake_recall.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_main_agent_delivery_closeout.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_session_search_tool.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_identity_trust.py agent_py_agent/tests/test_gateway_http.py agent_py_agent/tests/test_gateway_http_runtime_errors.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_thread_interrupt.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_tool_model_generation.py -q
python3 -m pytest agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_orchestration_tool_specs.py agent_py_agent/tests/test_wait_tool_self_wake.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_goal_tools.py agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_audit_activation.py agent_py_agent/tests/test_watch_audit_guarantee.py -q
python3 -m pytest agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_final_exit_contract.py agent_py_agent/tests/test_path_access_owner_scope.py -q
python3 -m pytest agent_py_agent/tests/test_ingestion_harvester.py agent_py_agent/tests/test_ingestion_puller_cursor.py agent_py_agent/tests/test_watch_spool_takeover.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_structured_output.py agent_py_agent/tests/test_live_lab_model_preflight.py -q
python3 -m pytest agent_py_agent/tests/test_tool_operation_idempotency.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tool_unresolved_runtime_issue_guard.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_runtime_gate_ledger.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_tool_context_reducer.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_artifact_read.py agent_py_agent/tests/test_tooling_filesystem.py agent_py_agent/tests/test_mcp_client.py agent_py_agent/tests/test_mcp_registration.py -q
python3 -m pytest agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py -q
python3 -m pytest agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_subagent_hierarchy_scheduler.py agent_py_agent/tests/test_subagent_hierarchy_scheduler_tool_roles.py agent_py_agent/tests/test_subagent_hierarchy_write_policy.py agent_py_agent/tests/test_subagent_capability_request_tool.py agent_py_agent/tests/test_subagent_natural_language_e2e.py agent_py_agent/tests/test_local_collaboration_subagent_integration.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_tools/test_tool_loop.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_gateway_orphan_reconciler.py -q
python3 -m pytest agent_py_agent/tests/test_cli_run_conversation.py agent_py_agent/tests/test_cli_run_provider_timeout.py agent_py_agent/tests/test_runtime_mixin.py agent_py_agent/tests/test_run_task_workspace_writer.py agent_py_agent/tests/test_memory_tool.py::test_remember_user_explicit_goes_through_candidate_and_promotes agent_py_agent/tests/test_memory_tool.py::test_remember_single_add_tool_verified_authorized_auto_but_evidence_gate_blocks agent_py_agent/tests/test_gateway_chat_conversation_context.py::test_gateway_returns_answer_and_repairs_assistant_transcript_on_next_turn -q
python3 -m pytest -q --tb=short agent_py_agent/tests/test_verification_runtime.py agent_py_agent/tests/test_verification_project_facts.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_model_call_ledger.py
```

终端交互 TUI parity 的 focused 命令覆盖 typed event/reducer、Markdown/diff、spinner、权限续跑、输入、
history/search/paste/completion/queue/stash、follow/unseen、session-history、真实 Gateway readiness、title、
鼠标选择/OSC52、PTY recorder 和 ANSI replay。
鼠标回归还必须覆盖：prompt_toolkit 传入的是源字符索引，中文宽字符不得再次按显示列换算或只复制一半；窗口外丢失 mouse-up 后，首个
`MouseButton.NONE` motion 或下一次 fresh press 只结束旧拖动，后续 hover 不再扩展；一次 settled selection
只自动复制一次，并同时保留 prompt_toolkit、OSC52 与 `tmux load-buffer -w` 外层剪贴板路径；iTerm2
也不得退化成只写 tmux 内部 buffer。输入回归还要覆盖鼠标松手自动
复制、Ctrl-C/右键复制且保留输入高亮、右键 press/release 只复制一次、Ctrl-V/终端 bracketed paste 替换选区，以及 marker 普通空格不会触发
`nbsp` 下划线。运行中普通输入还要证明下一次
真实模型调用能看到该输入；若 exact turn 已结束，TUI 只能挂接 Gateway 返回的 canonical queued request，
不得再次提交正文。
富 transcript 追补还必须覆盖：未声明能力的 Gateway 不公开 thinking/display 且继续按 verbose 裁剪；TUI
声明能力后逐轮 commentary、provider 明示 thinking、edit/overwrite/patch diff、write preview、命令
stdout/stderr/exit code 均走结构化事件；思考 Markdown 与 `Ctrl+O` 折叠提示的每个可见 fragment 都必须
以最终 muted/thinking role 覆盖正文前景色，不能只断言行前缀是灰色。失败后最后一次 workspace mutation 的软续跑只触发一次，简单写入
和已有后续检查不触发。供应商网络回归还要用真实 `ConnectionRefusedError/ECONNREFUSED` 证明传输层
`2/5/15` 秒三次退避、模型回合层 `10/25/45/100/180` 秒五次恢复和富 TUI typed retry 提示；普通
`"connection refused"` 字符串、DNS 与无效地址不得取得重试权。常用定向命令为：

```bash
python3 -m pytest -q --tb=short agent_py_agent/tests/test_provider_connection_error.py agent_py_agent/tests/test_provider_transient_auto_resume.py agent_py_agent/tests/test_runtime_error_reports.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tools/test_edit_file_tool.py agent_py_agent/tests/test_tooling_filesystem_write.py agent_py_agent/tests/test_tools/test_shell_tool.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_view.py
```
`test_tui_pty.py` 必须保存固定 TERM/locale、
终端尺寸和 reference/target fixture 身份；golden 只允许脱敏后的 ANSI/结构化片段。矩阵项目只有在
对应 test 与测试机 evidence run 同时存在时才能标为 `VERIFIED`。外部 终端交互 provider 健康不属于
UI 验收前提，参考客户端使用 loopback deterministic Anthropic fixture；MiniMax-M2.7 只用于 my-agent
真实链路，不得把 key 写入 pytest output、录屏或 fixture。

真实代码任务的交付验收必须把“命令退出 0”和“有测试覆盖”分开：`go test ./...` 出现 `[no test files]`
只能证明 package 可装载，不能证明行为测试通过。还要检查 output 总大小、隐藏目录和 cache/debug 文件；
process sandbox 回归需证明 canonical `task_work_dir/.sandbox-tmp` 承载 `/tmp`，项目 cwd 不出现
`.sandbox-tmp`，owner-scoped 环境的 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`。

写后验证新鲜度回归必须覆盖“verify 成功→workspace mutation→read/search→plain final”仍产生一次软核对；
同一 verification event 不循环提醒，执行新的真实 verify 后才能形成新周期。验证 envelope 必须从
`metadata.handler_details` 读回。模型调用账本还必须证明明细超过 `max_records` 后 request/run 的 logical、
physical、provider attempt/retry 和 status 累计数不截断；旧任务 response 的 128 不能再当精确总数。

完成收口与 operation 审计要分层回归：`succeeded mutation -> not_started tail` 仍保留 partial 审计，
但 no-effect 尾部不能触发整项 `OPERATION_INCOMPLETE`。末尾 `failed/not_started` 与 plain final 冲突时，
必须先把 `completion_conflict.v1` 和被拒绝草稿送回同一 active turn，原工具 schema 仍可调用；至少覆盖
“第一轮返工调用修复工具并形成 succeeded”“连续两次只口头完成后才安全收口”“新的失败 call id 不重置
全 turn 返工预算”。`unknown/cancelled/incomplete/unverified` 不得进入通用带工具返工，显式 required action
仍使用自己的结构化 gate，不能借该路径绕过。owner-scoped shell 环境还必须证明 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`、
`NPM_CONFIG_CACHE=/tmp/.cache/npm`，同时 `HOME` 只读边界和凭据擦洗不变。

终端交互 命令/输入追补至少覆盖：`/context` 使用自动 compact 同一估算而不写状态；手动 `/compact` 取得
同一 run lane、写 checkpoint 并推进 generation，live turn 时拒绝；可选摘要要求不能覆盖 operation evidence；
`/effort` 在 backend 没有结构化能力时查询成功但设置失败且不改参数；Up/Down 先走 ASCII/CJK/恰好满行的
软折视觉行；`N new messages ↓` 左键释放后恢复 follow-tail。对应 focused 文件为
`test_conversation_control_commands.py`、`test_chat_control_runtime.py`、`test_gateway_conversation_compact.py`、
`test_gateway_conversation_control.py`、`test_tui_input.py` 和 `test_tui_view.py`。

外部消息工具还要覆盖两个对称面：无 proactive owner route 时，`send_message` 实现仍注册但必须出现在
`runtime_snapshot.unavailable_tools`，且不进入 specs/retrieval；有真实 provider、target、owner root 与
proactive capability 时仍进入可见/可执行快照。对应 `test_channel_message_tool.py`，真机再用同一句普通中文
问候比较修复前后的工具块数量。

2026-08-18 命令/输入追补最终本地 89 项 focused tests、`.13` 精确 23 项到 100%；changed-file Ruff、
py_compile、doc-sync、strict code-size 与 diff check 通过。改动远小于 10,000 行，未重复全仓 pytest。
远端输出、TUI capture、进程与部署 hash 保存在
`/root/tui-parity-evidence/context-controls-20260818T1630CST/`。

2026-08-18 本轮只运行相关 focused 文件：本地与 `.13` 均到 100%（保留预期 xfail），changed-file
Ruff 与 py_compile 通过。改动远低于 10,000 行，按用户约定没有重复运行全仓 pytest。Tornado 真机任务
只证明核心 10 项测试曾通过；examples 在其后修改却未复测，所以整体结果明确记为未通过。后续
aiohttp→Go 在最终修改后重新 build、通过 29 项测试和 HTTP E2E，作为 EXEC-44 的真实闭环证据；其末尾
no-effect 清理又形成 EXEC-45 的独立反例，二者不能混写成同一结果。

测试机 evidence 分轮保存在 `/root/tui-parity-evidence/`：reference、mainchain、input-state、transcript、
interrupt、permission 和 final run。最终 `long-session-10k-optimized.json` 在 120×29 下建立 10k 回合/
20k stable block、39,999 rendered lines；idle animation tick 复用同一 frame，平均 6.4ms，真实可见状态
变化重绘平均 45.8ms。该压测与 `test_idle_animation_tick_reuses_static_long_transcript_frame` 一起证明“不在
空闲时全量重建”，不能只引用首帧时间。

本轮 TUI focused suite 327 项运行到 100%。代码与测试变更超过 10,000 行，因此额外执行一次全仓 pytest：
缓存记录收集 24,663 项，运行到 100% 且退出 0；此后不重复执行。最终 `.13` 部署后的 reducer/view/
renderer/runtime 回归 49 项和 changed-file Ruff 均通过。全仓 Ruff 尚有 119 项历史问题，而独立 clean
`main@2d5a964f` 为 122 项；本任务没有新增 lint debt。doc sync、strict code-size、diff 与 staged
clean-package 守卫通过。

`CODE_SIZE_BASELINE.json` 在本轮只登记 clean `main@2d5a964f` 已存在的 10 个严格 size finding；登记前在
独立 HEAD archive 上复跑并得到同一身份集合。新 TUI/审批/中断代码不得借该 baseline 隐藏新增 blocker，
每轮仍执行 `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json`。

CLI Memory 入口回归必须证明：user 原文在首个模型调用前进入 ConversationStore，`remember(user_explicit)`
取得唯一 user `source_message_ref` 并按统一 Promotion 晋升；相同 request/role 重放不重复，内容或
run/task lineage 漂移 fail-closed；assistant 尾部落账失败只产生 typed degradation。one-shot thread 不得
预填或遗留伪造/active task link：无 task-promoting tool 时 links 为空；真实工具晋升时只允许留下
`completed` link，且 active ids/links 为空。standalone workspace 必须进入终态，Gateway 的既有会话 lifecycle 不变。
真实 testbox 仍需另外保留 ConversationStore、candidate/formal memory、workspace、runtime event 与重放证据；
定向单测不能替代真实 provider 运行。

2026-08-12 的隔离 B5R3 已完成上述真实 provider 验证：`anthropic_compatible/deepseek-v4-flash`、CLI
RC=0、ConversationStore user/assistant=2、Candidate/formal=1/1、唯一 user message ref、workspace
`DONE`、`status_conflict=0`；同 request 纯存储重放前后全部语义计数和 ID 不变。真实工具晋升产生的
唯一 link 为 `completed`，active ids/links 为空。脱敏原始证据位于 testbox
`/root/memory-evidence/会话运行时-MEM-20260812-B5R3/`；该结果不替代 Goal 中尚未闭合的 Gateway/Curator、
第二模型、重启恢复与长文本验收。

`test_tools/test_tool_loop.py` 覆盖普通任务不会被旧进度清单劫持、显式 goal 的 open-plan 生命周期、工具轮数上限和
后台 continuation；`test_turn_end.py`、`test_subagent_finalize_helpers.py`、`test_subagent_protocol_contracts.py`
覆盖六类结束原因、自然结果保存与递归控制面；`test_conversation_goal_tools.py` 覆盖一会话一个未完成 goal、
精确创建/更新/完成边界。

主代理完成表达回归必须覆盖：普通 `task_progress` 即使仍有 open item，也只是一份可恢复的进度笔记，
不能拦截模型本轮回复、追加隐藏提醒、自动唤醒后台执行或要求下一轮先选择/关闭旧任务。只有显式持久
`/goal` 的 open plan 才保持 `unfinished` 并由既有 continuation 续跑。该行为不得解析“完成”等自然语言、
扫描任务目录、执行验证命令或给普通 task 增加完成硬门。终态普通 task 续作必须保留旧终态和 cwd、
创建新执行 task id；只有精确持久 `/goal` 可以原 id 恢复。子代理普通工具必须是父 run 快照的严格子集；
coordinator 可通过统一 `create_subagents` 继续递归创建，所有 leaf 都不得获得
create/guidance/cancel/resolve 四个直属下级控制工具，但仍可用 `capability_request` 为自己申请权限。
直接创建和层级调度都要覆盖这条规则。模型调用账本必须区分 logical turn、
物理 model attempt 和 provider HTTP attempt，并覆盖并发首次请求、重试、失败、超时和迟到 finish。
task-local child 即使携带父 conversation id，也必须证明可在自己的 runner lane 正常写入授权产物。
sticky workspace 回归还必须覆盖：新 execution 复用旧 task path 时，四份当前执行投影同步换成新
request/run/task，旧 output 与 artifact 列表保留，timeline 只追加一次；相同 task 恢复时保留既有
progress/evidence 并回到 RUNNING。损坏投影不得在激活时被静默洗掉，旧 task link 也不得覆盖新投影。

Gateway 会话控制回归还必须覆盖：已有 linked live turn 时 `/btw` 只写 guidance、不发布
第二个 wake；`/stop` 在线程阻塞于模型 JSON/SSE 读取时主动关闭响应，并以用户中断结束，
不误判为 provider 网络故障或等完整超时。测试必须使用生产同样的 wall-timeout guard 子线程，
不能只证明在任务登记线程内直接调 provider 的简化情况。本地 CLI 还必须用 worker 与界面共享的
精确 request id 测试，不能读取 thread-local `agent._current_run_params` 假装跨线程可见。所有
支持的 `/XXXX` 必须证明命令词不进入 transcript/guidance/模型；未知命令必须 fail-closed。

Gateway compact 回归必须覆盖：候选只有在包含 summary、近期 raw tail、已压缩与近期工具事实及当前
用户输入的完整投影低于精确配置阈值时才能提交；checkpoint 写失败、CAS 冲突和过大候选都不得推进
summary/cursor/generation。近期尾部只能按 user/assistant role 选择完整回合，不能分析正文；旧 v4 thread
安全加载为空的 v5 guard 字段；连续三次失败进入冷却，冷却后成功半开并清零；连续两代 checkpoint
能按 previous pointer 串联，raw transcript 始终不删。

原生工具长链还必须覆盖完整 provider-visible 计量：prompt、工具 Schema、ToolCall 参数、
ToolResult、UserTurn 与尚未转发的运行引导都要进入同一 token 估算。大 `write_file/edit_file`
参数不能因工具结果很短而漏算；达到配置阈值后只能整对移除最旧调用/结果，保留最新往返与全部
运行中用户输入，并按既有 recent-tail 预算一次取得余量。被回收旧段必须由同一 IR 中最多一条
非权威语义 summary 承接；下一次跨阈值要把前代 summary 作为输入再原位替换。摘要调用失败必须回退
机械 handoff；摘要、handoff marker 与近期尾部都要纳入同一预算。连续两次再次跨阈值时不能堆叠
summary/窗口标记、遗失最新用户纠正、留下 tool-use/tool-result 孤儿或退回 Gateway 同 turn 重启。
展示回归还必须覆盖 presentation/no-save 回合：公开 `compact_trigger_tokens` 仍等于统一
配置压缩点（例如 128k 窗口的 115.2k），不得因当轮禁止持久 apply 而变成 128k/100%。
同时要用超过 90% 但未达完整窗口的 no-save 输入证明它没有获得落盘 Compact 权限，
避免为了修 UI 暗改执行边界。

Gateway/IM 投递回归还必须覆盖：同一进度批次重试使用稳定 provider 幂等键，不同 progress cursor 与
最终回复使用不同键。身份只取可信 message ID、request ID、phase 和 cursor，不能从回复正文猜测；
否则平台可能把同一请求后续的真实进度或最终回复当作重复消息吞掉。
终态权威回归必须另外制造合法、截断和陈旧的孤立 `responses/<id>.json` 以及
`done/failed` 投影，证明它们不会让 provider 跳过执行、让 stale 请求消失、让活动输入误判
终态或让 CLI/TUI/plain 提前结束；只有 schema 和内外层 request ID 都匹配的
`requests/terminal/<id>.json` 可以返回最终答复。
必须再覆盖 provider 已成功、紧接着 `/stop` 先把同 attempt 写成 closing/cancel 的顺序：ACK 仍应把
已进入模型的 guidance 收成 consumed，不得永久卡在 submitted；相反 stop 在 provider admission 之前
先赢时必须零次调用 provider。audit clear、linked task stop 和 window stop 均要通过同一 T 锁路径。
同 ID 恢复回归还要在 canonical 已存在后人为放回一份 owner/prompt/options 不同的 hot
processing 文件，即使两者最终文案相同也必须保留 hot 并报冲突；只有重算后的
`gateway_request_fingerprint.v1` 一致才能幂等退役热文件。
同时在 inbox 文件已移入 processing、attempt/lease/fingerprint 栅栏尚未成功的精确写入点注入
`OSError`，验证 claim 返回失败、原请求回到 inbox，且 processing/canonical/response 都不留半成品。

Adapter durable ingress 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_adapter_ingress.py agent_py_agent/tests/test_adapter_manager.py -q --tb=short`。
必须证明 route callback 在媒体和 POST 前已经落盘可信身份与 canonical digest；same-id/same-body 幂等，
diff-body 隔离；媒体瞬时失败停在 prepared 且 POST 尚未发生；Gateway 响应丢失按原 body 重试，
submission 落盘后崩溃不重 POST；唯一 worker 继续推进
input-status/result/control-status/placeholder，IO callback 不持 store lock；ingress/reply 两类持久 row 必须覆盖
A/B store 同时读取、租约未过期拒绝 B、过期后 B 以更大 epoch 接管、A 的旧 epoch CAS 失败。还要断言
POST 直接发送持久 `gateway_payload`，progress handle 可变，429/5xx 保持 WAIT，auth/config 隔离且不发送
伪终态正文，input `terminal_unknown` 清占位并留下 durable unknown receipt。`/btw` unknown 必须保存
`operation_id/receipt_id/control_state`，以 operation ID 而非目标 `request_id` 建 `control_receipt` watcher。
同一 target turn 的两条 `/btw` 必须生成两个 pending 文件和两份独立终态；目标初始为空时，首次 GET 可在
同 epoch CAS 绑定，后续不同 target 必须 quarantine。stop rejected 必须有明确回复，stop
`terminal_unknown` 必须清占位并留下 durable unknown receipt，二者都不能静默完成。control pending 还要
冻结 channel/user/conversation/chat type/chat id，逐项路由漂移均 fail-closed；`/control-status` GET 必须
发送这五项结构化身份头，不能把可猜的 operation ID 当鉴权能力。

Slash 控制操作回执的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_gateway_control_operation.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_gateway_http.py -q --tb=short`。
必须覆盖副作用前 prepared、执行前 executing、结果落盘 completed 和崩溃 terminal_unknown 四个切点；同一
message ID/正文只执行一次，同 ID/异正文零次新增副作用并返回冲突。`/control-status/<operation_id>` 只可
读取 authenticated owner 的回执；`/btw` 可以从已有 guidance receipt 推进 accepted/rejected，其他控制和
没有 guidance 证据的 unknown 均不能因 GET、TUI 重连或 adapter 重启而再次执行。若 wrapper 已越过
executing、但 guidance 尚未落盘就崩溃，只能在 exact turn 终态证据下收为 rejected；active、recoverable、
corrupt、absent 四种非终态证明都必须保持 terminal_unknown。群聊回执必须冻结
`channel_chat_type/channel_chat_id`；同 message ID 改群身份应在副作用前冲突，GET 对账仍进入原 group owner。
回执还必须冻结首次解析的 canonical owner ref；在 `prepared` 落盘后切换 per-owner 配置再重试，effect、steer、
stop、task link 和子代理取消仍只能命中原 owner，不能按新配置重算。
TUI 控制 outbox 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_tui_control_delivery.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_tui_input.py -q --tb=short`。
必须证明 persist-before-POST、两次传输请求复用同一 message ID、收到 operation ID 后永久 GET-only、
terminal_unknown/conflict 不换 ID 重做、重启恢复原行，以及未绑定 exact turn 时 `/btw`/`/stop` 零次发送。
长控制正在执行时，重复 POST 与 GET 还必须在有界短时间内返回同一 `executing` 回执，effect 调用次数仍为
一；原 worker 释放 C 后才能出现 completed 或确证中断后的 terminal_unknown。Gateway IO focused 还要模拟
无 fcntl 的 Windows 分支，证明 `msvcrt` lock/unlock 成对发生，而不是仅发告警后无锁运行。
还要证明明确 lock contention 才返回未领取，坏句柄等错误会上抛；两种跨进程原语都缺失时必须 fail-closed。

停止后续接回归必须覆盖：新 gateway request 的 `task_id` 与原持久 task 不同时，`/btw` 仍从
`task_attributes.conversation_task_id` 消费一次；`/stop` 只中断当前真实 live turn，没有运行内容时
不得修改旧 task/goal；停止后的下一条普通消息无需 select/start/close 命令即可聊天或在 sticky cwd
继续工作。若命中一个已终态工作目录，首个 `promotes_task` 工具自动创建本轮执行身份，旧终态保持不变。

容器节点真验收不能只看单测：最终镜像必须运行
`python -m agent_py_agent.agent.tooling.sandbox --quiet` 并退出 0。工作树检查使用
`python3 scripts/check_clean_package.py --mode worktree .`；wheel/tar 发布前再以
`--mode artifact <制品>` 检查实际成员和大小预算。

## Full Command

```bash
python3 -m pip install -e ".[dev,secrets,scale]"
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts
```

默认测试集覆盖 secrets 加密与 scale 存储/队列，因此 CI 和全新开发环境必须显式安装三套正式
extras；不能依赖宿主机碰巧已有 cryptography/SQLAlchemy，也不能用 skip 把缺依赖伪装成通过。
生产 wheel 使用 PEP 517 默认隔离构建，让 `[build-system].requires` 独立决定构建后端。

真实主代理/子代理链路通过后，再提交和推送。

多个外部写的专项回归必须覆盖：同轮保持模型原顺序；每项结果独立留痕；权威 operation 终态写入失败
时把表面成功降级为 unknown 且同操作不再执行；archive、runtime event、机械 compact 续跑和语义 compact
都保留 operation/effect 事实。该回归不要求通用 Saga，也不能用自然语言猜依赖或补偿动作。

路径极端回归还必须覆盖：显式未授权绝对路径保持原目标身份并返回 `WRITE_FORBIDDEN`，原目标与任务
`output/` 下的替代路径都不得生成；失败前后的独立合法写仍能按顺序完成。裸相对路径必须继承当前可信
cwd/workspace；相对 `output/...`、`work/...` 的结构化任务落位继续生效，不能把裸项目路径误投到 task
output，也不能用绝对路径静默搬运兼容层冒充成功。

后台可观察性 focused 必须覆盖：Gateway 成功快照返回 canonical active-root count 和有界
direct-child 行；只选当前 thread 活跃 root 的直属 child，不展开 grandchild/历史 root，不泄露 goal、工具输出、
路径或权限。TUI 用一个可移除的固定 Working 区域显示 main；输入框下每个 child 只占一行，显示名称、
状态、职责短标题、耗时、当前模型可见上下文 token、Compact 和真实重试。短标题按剩余终端列截断，
不得回退“模型响应中/模型已生成回复”或累计计费 token。相同快照不重复追加，数值变化原位更新，root
归零整体删除；HTTP/解析失败不把上次真实活动误清零。
main 的后台 context 也必须来自同一次 `model_visible_context_usage.v1` provider preflight；每轮模型调用时
实时更新，不能永远停在首次 8.7k。Todo 必须从当前 active task 的 canonical `task_progress.v1`
投影；后台更新原位替换，active link 关闭前后的最终 notice 还要携带最后一份 `id/title/status` 快照，
确保模型最终回复出现时已完成项仍打勾。以上两类字段只用于显示，不得成为结束、恢复或验收事实源。
Todo 与 child panel 必须按结构化身份去重：`item.id` 精确等于当前直属 `child.run_id` 的自动 seed 项只在
TUI 隐藏，canonical task_progress 账本不删除；普通 Todo 和显式 `covers/progress_item_ids` 仍显示并打标。
禁止匹配“子代理”标题或 goal 文本来决定隐藏。
Todo 超过四项时，默认投影必须恰好保留四条任务行：最近完成、typed `in_progress` 和下一条
`pending/blocked` 按状态优先；多个运行项优先占位，运行图标与 Working 共用动画时钟。`Ctrl+T` 展开后
显示全部 canonical 顺序，再按一次收起；该按键不得编辑或提交输入、不得写 task_progress。常驻 Context
显示总量、窗口占比、ConversationThread `compact_generation` 与同一 usage 预算算出的明确“压缩点”；
`compact N`、`压缩点 90%` 和临时 Compact 操作进度不能混为一类。prompt/messages/tools 的详细构成只由
`/context` 命令展示。child 发生 `model_visible_context_compaction.v1` 时，exact run 必须按
attempt+generation 幂等累计纯数字投影；TUI 过渡期只相加 durable apply 行与该真实 reduction，不从 token
下降或正文猜测。未来统一 ConversationThread 后，回归必须改为只读 child thread generation 并删除过渡链。
内部 child 状态路径的 shell 拒绝还必须覆盖两层：ShellTool 返回
`WRONG_STATUS_SURFACE + effect_outcome=not_started`；经过 canonical authorized dispatch 后即使
`handler_executed=true`，operation 仍归确定性 `failed` 而非 `unknown`。模型应收到原拒绝原因后换用
直属生命周期事件或正式 result refs；测试不得为了通过而放开内部路径，真实未知副作用也不得降级。
父级共享上下文还必须证明当前轮无 read archive 时不会复用 agent 上一次任务的缓存内容。
同一 exact parent 分多批创建默认命名 child 时，第二批编号必须从既有最高序号继续，不能重新出现
`worker-1/worker-2`；显式 run id 仍是身份权威。用户明确“主代理只能协调/不准自己写”时，真实 TUI
必须证明容量满、创建参数失败和 child 完成均不会让 main 自行编写被禁止的功能代码；main 只能按用户
允许范围协调、读取、整合和测试。这项只检验用户指令优先级，产品代码禁止按具体游戏名或 prompt 关键词
硬编码机器裁决。

2026-08-22 的正式 Prompt 2 基线证据：机器 `192.0.2.7`、tmux
`dsh-p2-mario-993ce4f`、cwd `/root/dsh-tui-p2-993ce4f`、模型 `MiniMax-M2.7`，只发送了本文件原样
Prompt 2。首次 ask、后台 main、6 个 child 和全部命令 working_dir 均保持该 cwd；child 职责行分别为
“玩家控制/物理引擎/敌人AI/道具系统/关卡设计/游戏HTML”，6 个 child 一次 attempt DONE，`bbb/`
产物齐全，`0.0.0.0:8082` HTTP 200。该轮同时固定了四个待复测失败样本：main context 停在 8.7k、
canonical Todo 5/5 而 TUI 未更新、第二批 child 名称重号、main 在纯委派失败后自行补写功能代码。
候选部署后的复测仍必须用同一原样 Prompt 2、新 cwd、新 tmux、唯一 Gateway；测试者不得追加“继续”、
技术提示或旁路修复。

`f5dc695` 的新鲜 Prompt 2 复验使用 tmux `dsh-p2-mario-f5dc695-verify`、cwd
`/root/dsh-tui-p2-f5dc695-verify`，证明 main context、跨批编号和 8 个 child 一次 attempt DONE 已生效；
wake durable 文件创建到后台 claim 约 7.56 秒。下一候选必须重点确认：批量每个 item 的职责短标题互不
复制且只概括该 child 工作；main/child 任意长文本严格单行；最终 notice 不让 exact child seed Todo
复现；用户要求纯委派时 main 不再写 child 的功能代码。仍只发送一个原样用户 prompt，不允许测试者追加
技术指导。

本切片 focused 命令：

```bash
python3 -m pytest agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_conversation_agent_activity.py agent_py_agent/tests/test_background_notice_display.py agent_py_agent/tests/test_timeout_gate1_accounting.py -q --tb=short
```

真实本地模型回归示例：

```bash
python3 scripts/live_agent_lab.py --suite main-artifact --real-llm --timeout 900
python3 scripts/live_agent_lab.py --suite tool-recovery --real-llm --timeout 900
```

真实模式会先发起一次模型调用；key 仅存在但不可用、响应为空或 endpoint 失败都会直接终止，不会把专项
harness 的离线结果冒充真实模型结果。
