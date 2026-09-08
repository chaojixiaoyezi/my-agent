# Gateway Structure

## R204 后台原文定位

`_goal_runtime_context` 的生命周期分支使用 `_background_request_objective`，按 wake 的请求编号在
canonical transcript 分页定位 user 输入；`history_page_report` 只提供无副作用读取，临时游标不写入状态。
稳定 task link 继续提供路径与旧事件目标，原文缺失抛出 DataCorruptionError，不能回落错误旧目标。

## R203 Todo 动画活动源

`TuiViewSnapshot.has_active_work` 是显示与刷新共用的只读活动谓词；`_render_fixed_todo` 只增加本地
`active_execution` 投影。renderer 缓存键纳入它，空闲时不纳入 spinner；runtime 不再由 Todo 状态单独驱动刷新。
该字段不进入 Gateway、模型、计划账或持久化，不修改任何任务状态。

## R201 客户端详细正文来源

`TuiFrameProvider.set_state_store` 是导航换显示仓库的唯一入口；详细模式的冻结 snapshot 通过
`TuiTranscriptModeState.rebind_view_snapshot` 同步重绑。界面来源不改变 Gateway 控制或模型会话。

## R199 显示历史双向游标

- `ConversationMessageStore.history_page_report` 使用 `history_page.py` 在 canonical JSONL 中向前读取，
  与 display 投影共用结构化工作片身份；目标行数边界不拆最早连续工作片。
- `GatewayClientHistoryResult` 的 `before_message_cursor/has_older` 只供更早显示页，`message_cursor`
  仍是实际已读末行的实时位置。`handle_client_history` 拒绝非法游标；路径和 owner 不由游标决定。
- 薄客户端和本地显示恢复共用分页合同；TUI 旧页不写入 conversation_history、任务队列或模型请求。

## R198 恢复执行归属

- `_GatewayTaskBindingWriter.bind_conversation_claim` 在请求原子锁内保存 request/thread/claim-task，
  不允许恢复改绑会话；`ConversationRunLaneRequest.acquire_transition` 将领取与请求终态串行。
- `ConversationClaimStore` 的 `recover_same_task_only` 只约束未结束的宿主专属 claim，普通 claim 不变。
  Gateway 车道在模型返回后不提前释放；普通后台继续在 finally 按 claim ID 结束。
- 正常终态和 `_recover_committed_terminal_processing` 均调用 `_finish_gateway_conversation_claim`，
  由 owner-scoped store 在一个原子更新内核对 thread/task/恢复标记/运行态；不会删除后来请求的 claim。
  释放写入失败保留完整 sealed 请求等待补交，执行身份与 UNKNOWN 规则仍独立核对。


## R196 UNKNOWN 恢复的安全公开错误

- `_recover_gateway_active_turn_authority` 仍核对真实 runtime binding 和工具终态，仅按返回的结构化
  reason 选择 `ActiveTurnOutcomeUncertainError`；不解析异常正文，不更改旧工具账本或释放 UNKNOWN。
- `request_errors.gateway_client_error_message` 将该错误投影成“结果未确认，停止自动恢复，避免重做”；
  通用 `ConversationPersistenceError` 说明会话记录读写不可用，provider 错误继续保持各自含义。
- shell/ToolOperation 的确定失败依赖共享进程终止回执，Gateway 不另行猜测命令内容或补造退出事实。

## R194 展示与执行双字段、单权威

- `runtime_mixin::_bind_main_agent_authority` 取得 RuntimeDB 真身份，调用现有 binding callback 的
  `bind_runtime_authority`；异常沿原 exact-attempt 收口入口处理，尚未进入任何模型或工具。
- `_GatewayTaskBindingWriter` 在原 active-turn transition 与 JSON 原子更新内保存 `runtime_authority`
  v1，不覆盖 heartbeat、停止位或 `conversation_runtime` 展示链接。
- `_gateway_runtime_authority` 核对 request 与 current/dead transport 代次、完整结构字段；恢复与
  `_gateway_run_params` 共用。坏凭据报错，不回退猜另一个 task/run。
- `RuntimeRepository.recover_recorded_active_turn_attempt` 核对 exact task+run 和预期 agent/attempt；
  `_mark_dead_runner_attempt_unknown` 与启动扫描共用 PID/start+CAS，再应用原工具/资源完整性证明。

## R193 唯一恢复计数来源

- `recovery.py::_gateway_processing_failure_count` 读取 processing 请求的非负整数；缺字段是未记录过失败，
  从 0 开始，不把旧 attempts 或 last_error 拆解为历史失败。非法类型不回退。
- `_recover_one_processing_request` 仅非 startup 的真实租约失效消耗 max_attempts；重排在回合锁内重读
  exact attempt/epoch/heartbeat 再保存计数，active_turn_recovery.cause 为宿主枚举，不解析模型或错误正文。
- 总 attempts 保留用于诊断，lease epoch/attempt ID 仍独立承担执行 fence；terminal_response 及其终态投影
  同步保存 processing_failure_count，重启/停止/未知副作用不会被新的计数入口绕开。

## R191 状态来源与纯请求投影

- `PromptCacheLayout.volatile_sections` 是宿主提供的唯一分段正文；`volatile_suffix` 仅由其渲染，拒绝同时
  提供两份正文或重复来源。未分段的直接调用仍作为单段输入，不分析 Markdown。
- `RuntimeFactsTurn.source` 是开放来源字符串，不获得权限。`record_runtime_facts_turn_ir` 只比较同来源
  最后一个保留项，不维护 ever-seen 集合；旧消息不重排，变化返回旧值也追加。
- `project_native_prompt_history` 在浅副本上投影本次变化和会话状态；context pressure 只读该副本，模型
  生成才提交同一 IR。Compact 仍走原整对回收入口，但保留每个来源最新项；回滚恢复 IR 即恢复基线。

## R190 摘要覆盖范围与状态快照

`tool_ir_history.drop_tool_call_pairs` 的完整摘要参数同时授权回收旧 assistant 轮和连续退休前缀内的旧运行
快照。`_summarized_runtime_fact_indexes` 从 typed 调用/结果顺序找边界，不读正文；遇到任何保留调用即停止，
并始终保留最新快照。`_native_compact_floor_tokens` 与实际压缩共用此规则，取消/CAS 失败恢复原列表。
原始 transcript/工具账不参与删除，普通窗口化与孤儿清理不传摘要授权。

## R189 易失过程与持久消息的两种游标

- `background_transcript.read_background_transcript_events` 在 owner Agent 锁内分配进程实例 stream ID，
  同流按序读取；流不同则从当前保留事件接续。LRU 清理仍沿用该实例的全局单调序号。
- `/client/notices` 增加成对的 `event_stream_id` 请求/响应字段；薄客户端与本地模式共享换代消费合同。
  `cursor` 仍是 canonical 消息字节位置；两者不互相重置。仅在发布成功后确认新事件身份和序号。
- `conversation_agent_view` 把 canonical child `thread_id/final_response_message_id` 与 request ID 同时投影；
  request 用于合并 typed final，message ID 用于历史块，正文不充当任何身份。

## R185 后台正文与恢复快照交接

- `ConversationStore.message_page_after_offset_report` 从完整 JSONL 行后的字节位置顺序读一页，不回扫旧前缀；
  `message_stream.read_background_response_page` 只投影同 thread 已提交且公开的后台 final，不写新存储。
- `/client/history.message_cursor` 是实际读到的最后一条消息之后的位置，不能取稍后的文件大小或当前时间。
  `/client/notices.after/cursor` 使用该整数位置；过程事件仍有独立 `event_after/event_cursor`，不能混用。
- TUI 在历史恢复后接续读取，前后台 final 共用 `history:thread_id:message_id:assistant`，不靠正文或时间去重。
  未发布成功不确认游标；游标倒退/非完整行/损坏显式失败并沿用既有读取退避，不生成空历史假成功。
- 旧 notices 只保留为历史现场，不再接入新的消息主链；匹配版本 Gateway/TUI 一起升级。页面之外的历史读取和
  原生过程完整归档仍为独立 P1，不能宣称这些旧数据已经恢复。

## R184 执行身份与用户目录分离

Gateway 解析并校验会话 cwd，但不再把 `workspace_task.task_path` 覆盖为工具 cwd。任务晋升仅登记运行身份，
不按写目标切换 task、不递归改写工具参数。新运行归档位于 `owner_runs_dir/日期/身份哈希`，旧 tasks 链接保持
原处可恢复；wake/索引识别两种已知归档，不迁移用户文件。普通文件范围仍由 canonical owner 决定。


## 会话恢复的显示与模型预览

- 显示转换只投影调用方已经限定的 raw rows，不再按 `max_turns` 对 request/message 分组二次裁剪；后台
  commentary 可能没有独立 request id，不能被误算为一个用户回合。真正分页属于存储/读取层，不属于渲染器。
- `client_service.read_gateway_client_history` 在 authenticated scope 内解析唯一 owner/thread；同一批 canonical
  rows 分别产生 `turns` 问答预览与 `display_events` 公开类型正文。客户端不能选择内部文件或其它 owner。
- `conversation/history_display.py` 消费已保存 native envelope 与可见消息，不改变 provider replay 或 Compact；
  内部 user 注入和 thinking signature 不外发。恢复事件只接受终态显示 kind，不支持权限、控制或活动恢复。
- CLI history snapshot 将两者分别传给 `TuiRunParams`；TUI 事件进入现有 reducer，预览才进入原本的本地上下文
  helper。该显示修复不会产生额外模型调用、工具执行或历史写入。raw 之外已裁掉的 native 原文不得伪造。

## Gateway 历史任务引用投影

- `requests/{state}/<request_id>.json` 内的 `conversation_runtime` 是 request/thread/task/path 的宿主权威；
  response 和 LocalStore 都不能独立发明或修改它。completion 写同一 `gateway_request` 索引行时必须从原
  request 携带该字段，不能因最终 response 本身没有路径就覆盖丢失。
- LocalStore metadata 只保存上述字段的 owner-local 搜索投影。`session_search` 仅在记录来源确为
  `gateway_request`、状态是 `done/failed/interrupted`、task_id 非空且 task_path 为绝对路径时返回
  `task_ref`；普通聊天、Memory、当前占位请求、标题、摘要和
  模型回复都不能生成该引用。discover/scroll/browse 使用同一投影函数，避免切换检索模式丢路径。
- `task_ref` 只帮助模型定位历史工作，不是写授权。R184 已删除按业务目录重新绑定任务；工具实际参数保留，
  文件读写只认 canonical owner 边界和结构化安全策略，不因属于另一个 task 拦截。
- 历史任务定位有两层有界入口：Gateway 会把同 owner/thread 最近四个 completed canonical task-path 作为
  动态尾部候选，帮助模型直接识别最近 A/B；更旧或候选不足时仍走按需工具发现。候选按 path 去重，排除
  当前 request/task、detached、缺失目录和 owner 外路径，只携带 exact id/path/status 与短目标预览；它既不
  设置 workspace selection，也不取得读写授权。
- `session_search` 的
  `ToolModelHints` 覆盖“刚才/先前/回到/原项目”等自然续作说法，并提供限定 `gateway_request` 的查询示例；
  hints 只影响模型侧软推荐排序，不参与路径、owner、thread 或终态裁决。
- discovery 对宽检索在本地最多超采样 20 条，将带合法 typed `task_ref` 的结果稳定提到普通文本前，
  然后再按用户 limit 截断。这只是搜索结果展示优先级；两个分组内保留 FTS 原顺序，不产生第二次模型
  请求，也不把历史任务常驻注入 prompt。
- 模型可把已返回的绝对 task_path 复用为 owner-relative `tasks/...`。该形式只在工具参数入口从
  宿主 `canonical_owner_home_root` 还原，不从进程 cwd 或模型文本猜 owner；旧内部入口只为兼容回退
  `effective_owner_scope_root`。两个字段分别表示“地址基准”和“实际安全墙”，Full Access 下前者存在而
  后者可以为空。地址还原后仍顺序经过 owner 墙、工作区读写根、
  同 thread 历史 link 与 exact mutation rebind；任一层不匹配就拒绝，所以规范化本身没有权限语义。
- 首轮任务晋升可以把启动 cwd 的普通绝对路径重定向到正式 task root，但不得改写已经位于 canonical
  `<owner>/tasks/...` 下的显式历史地址。该保护仅在 target 的结构化形状证明 source 是 owner home 时启用；
  后续 task-to-task 重定向不套用这条例外。路径是否可读、可写或可续接仍由后续统一工具门裁决。
- 真 TUI 中，历史地址命中后仍按既有两阶段生命周期工作：本轮占位 task 转 `superseded`，原历史目录内创建
  同 request id 的 continuation successor。业务文件只写 successor 的 canonical task root；successor、sticky
  selection、request binding 和 Todo 迁移均成功后，底座只删除 identity 精确匹配且整棵树仍为原始脚手架的
  占位物理目录。任意用户文件、陌生目录、symlink 或身份冲突均不删除；superseded link 继续保留完整审计事实。
- 如果本轮已在占位 task-path 建立 `task_progress`，上述 successor 切换还必须迁移同一 request 的
  `display_plan.generation_id`。迁移只使用 task-path 指纹、request generation 和 exact item ids：先合并并验证
  目标 canonical `progress.json`，再移除源文件。不同 generation、缺失源或同路径不迁移；final、pytest 文本和
  Todo 标题都不参与。

## ConversationTaskLink 与 TaskRun 收口

- `ConversationTaskLink` 表示用户会话任务是否仍有执行意图；`TaskRun/AgentRun` 表示这一轮真实执行树是否已经
  结束。TaskRun 只有在 link 进入结构化不可复活终态、并且 exact TaskRun 下所有 AgentRun 都终态且只有一个
  root 时才可关闭。模型回复里的“完成/失败”、一轮内临时属性和任务质量都没有终态权限。
- root、child 的普通与异常收口边都调用同一幂等 CAS，因此“link 先终态”和“最后一个 child 后终态”两种顺序
  都能闭环。`owner_wake_discovery` 在 Gateway 启动/周期发现时只扫描仍开放 TaskRun，并以唯一、无冲突的
  canonical link 状态重放该 CAS；缺失、活跃、未知或互相冲突的 link 状态一律保持开放。
- TaskRun 的最终 status 来自唯一 root AgentRun；一次成功关闭只追加一条 `task_run.closed` 事件。该投影不修改
  ToolOperation，UNKNOWN 工具副作用仍由原有恢复/人工审计协议处理。

## Compact generation 与动画 operation

- `compact_generation` 是 ConversationThread 成功提交历史替换的持久代次；`operation_id` 是一次摘要候选/动画
  的身份。同一待提交 generation 可以先出现 live-tool 候选，再由 transcript fallback 接管，二者不能共享
  百分比、block id 或终态。
- TUI 只用 generation 拒绝旧代事件；同一 operation 内百分比取单调高水位。旧 operation 已 failed 或
  superseded 后，新 operation 从自身 started 百分比开始，随后到达的旧事件不再更新当前块。只有 canonical
  checkpoint/CAS 后的 completion/boundary 推进界面 Compact 次数。
- 每个进度事件还必须携带成对的来源和提交权：
  `conversation_transcript/conversation_thread`、
  `active_turn_tool_archive/conversation_thread` 或 `turn_local_tool_ir/turn_local`。三类生产者、Gateway、
  后台 transcript 与 TUI 只调用 `conversation/compact_progress.py` 这一份白名单；不得从 operation id 前缀、
  阶段或中文文案猜。双字段都缺失的历史事件只投影成 `legacy/legacy`，不能据此推进 generation。
- transcript、active-turn archive 与 native IR 共享同一个 typed interruption callback。慢摘要、内存候选、
  checkpoint 和 generation CAS 各自在副作用前后复核；CAS 前的停止只把 operation 标成
  `superseded/candidate_discarded`，恢复未提交 IR/tool-context，不推进 generation/cursor/failure circuit。
  checkpoint 已写而 CAS 未赢的候选没有 live authority；CAS 赢后不回滚，避免持久 thread 与 TUI 分叉。

## ThreadGoal 与 workspace 物化边界

- `ThreadGoal` 是 thread 上的持久控制 overlay；创建 Goal 不等于已经进行文件工作，也不要求立即创建 task
  workspace。其 exact task link 可以保留空 `task_path`，下一 foreground turn 继续从 thread 的可信 cwd 运行。
- `_gateway_workspace_task` 必须区分“从未配置工作区”和“配置过非空工作区但目录已消失”。前者返回
  `workspace_task=None` 且保留 Goal；后者写入 `gateway.conversation.workspace_task` load error，让请求按
  `CONVERSATION_PERSISTENCE_UNAVAILABLE` fail closed。该分流只读结构化路径字段，不从 Goal 正文推断。

## TUI transcript channel 与后台回复提交

- `conversation/channels.py` 的 transcript-capable 集合表示“该 channel 的 authenticated thread 由
  ConversationStore 承载模型历史”，不是“存在外部发送 adapter”。本地 `tui` 属于前者；未知外部 channel
  仍不自动取得该语义。
- background main 经统一 transcript sink 生成 commentary/final 后，先用 exact wake id 与
  `assistant_part_id` 幂等写入 thread，再独立发布 notice/delivery projection。即使 TUI 没有 adapter、发送
  结果为 `not_applicable`，canonical history 也必须存在；重试只得到同一行，不能重复 final。
- 下一前台轮只从 ConversationStore/Compact checkpoint 恢复这些正文，不从 notice、footer 或屏幕回放猜
  模型看过什么。该边界对齐 会话运行时 的 response item 先记录、UI event 后投影顺序。

## provider context observation

- `conversation_thread.v8` 在 exact thread 保存一份 provider 数值观察。观察附带稳定 backend/model/protocol/
  system/prompt/tools 指纹与 Compact generation；写入使用同 generation CAS，且不推进 thread `updated_at`。
- context pressure 只在指纹、代次与数值都可信时用它校准 reconstructed durable slice；动态 messages、guidance
  不进稳定指纹。缺失或不匹配时回退原始估算，成本继续由 ModelCallLedger 负责，不能混成第二份 token 总账。

## pending conversation 的模型 cwd 投影

- 普通 conversation 在没有 `run_workspace` 时仍不创建 task；owner home 是既有资料与权限边界，不是新产物
  默认落点。模型短提示不得把该绝对目录标成“当前工具工作目录”，也不得通过 context bundle 摘要再次泄露成
  placement hint。
- 首个 `ToolRuntimePolicy.promotes_task=true` 的调用继续在唯一执行缝隙建立 task link 和 run workspace；
  完成后 `conversation_execution_cwd/run_workspace/write_boundary` 同步到本轮工具快照，后续 main、child、
  approval 与 sandbox 复用同一 canonical root。pending 阶段只告诉模型使用相对路径，不能扫描或改写 goal
  来补救路径。

## 工具目录与停止入口的单一事实

- `ToolRuntimeSnapshot` 在 request/run 边界完成 owner、显式 allowlist、availability 和 exposure 收敛。
  `tool_manifest_payload` 只读取其中 `model_visible` runtimes；它不得回读全局 registry，也不得拿主体
  `owner_type` 去匹配代理角色。provider specs、`tool_search`、`list_tools`、ActionPolicy 和 Executor 因而看到
  同一集合，执行时的 owner/path/effect/approval 门保持不变。
- agent-stop HTTP 入口只负责同步授权、终态短路、幂等 admission 和启动唯一后台工作；
  `conversation.agent_control` 的 canonical cancel 仍是写终态的唯一入口。响应必须携带 accepted/terminal
  typed 状态，客户端不能把 transport timeout 当业务拒绝，也不能把 accepted 当 terminal。
- exact child runner 的 session、snapshot 保存和 heartbeat 在每次写入前读取 canonical state；发现更新的
  终态立即退出。显式 recovery 的 reopen 标志只在结构化 user-stop 恢复路径传入，普通保存默认 false。
- runtime.db 的 `AgentRun/Attempt` 与 `SubAgentTask` 是同一执行的权威生命周期和文件投影，不是两套可互相
  猜测的状态。Gateway 重启发现 task 仍 RUNNING 时，exact `agent_run.completed` 若带宿主 `runtime_status`，
  必须复用普通 runner-result finalizer 补投影和父级通知，不得 abandon/requeue；orphan reclaim 产生且没有
  `runtime_status` 的 terminal event 只表示旧执行轮已封存，仍可把同一逻辑 run 放回 PENDING。

## 完成回合 canonical native envelope

- `gateway_parts/request_execution.py` 在同一次最终化中写入用户可见 transcript、canonical native envelope 和
  artifact refs。`conversation/native_history.py` 只接收结构化 `UserTurn/AssistantTurn/ToolCall/ToolResult`
  事实，序列化时保持 provider 顺序与 call id；它不是第二份会话，也不从模型正文猜工具语义。
- 下一轮先由 ConversationStore 选择同一批有界 rows，再从每个 assistant transcript metadata 恢复 envelope；
  旧行没有 envelope 时只保留原展示文本。当前 user 与 runtime facts 由本轮 IR 单独追加，因此已完成前缀保持
  append-only，供应商 prompt cache 可以跨普通追问复用。
- `conversation/compact.py` 是历史替换的唯一入口：成功 checkpoint/CAS 后才用新 summary 代替旧前缀；普通
  final、后台 wake、自然语言插话和 TUI 重连不得暗中重写既有 envelope。

## 后台工作片的统一发布边界

- child wake、observation 与 due progress policy 可以用不同触发源运行，但结果统一进入
  `conversation.runtime._append_background_report`。它先保留 scheduler report，再走唯一 notice projector；
  ConversationStore 仍是正文权威，notice 只是附着 TUI/渠道读取的新消息投影。
- exact background task slice 会设置 `conversation_transcript_authoritative=true`。它的 `save=False` 只关闭
  `Agent.run` 的旧式回复/记忆保存，不能关闭该 ConversationThread 的 live-tool Compact；presentation-only
  或没有 exact thread 的 no-save 调用仍无 Compact 写权。
- background main 与 foreground Gateway、child runner 共用“当前 run 返回 context_overflow，外层 Compact
  唯一 ConversationThread，再携带 typed archive/active-turn input 原地续跑”的控制语义。scheduler wake
  只负责真正的新事件，不充当 Compact 重试器；一个公平 slice 最多推进 8 代。达到 8 代且每代都有进展时
  发内部 typed yield：当前 claim 正常结束、来源保持未消费，下一 slice 从 canonical checkpoint 续跑；没有
  generation 变化或普通异常仍失败，不能被 yield 吞掉。
- native live Compact 的完整 replacement summary 覆盖的是旧 assistant/tool/result 工具轮；只有该摘要存在
  时才可连同被删工具对所属的 assistant 正文整轮回收。无摘要窗口不能删除 thinking-only/assistant 正文，
  UserTurn 与 carried handoff 继续独立保留。
- transcript 与 live-tool Compact 都只向客户端发送 `conversation_compaction_progress.v1` 的阶段、百分比、
  generation、token 计数和失败时可选的 typed `error_code`。live-tool 在慢摘要前创建块，在真实 checkpoint
  后才进入 committing；TUI 不读日志文案猜进度。完成块收起后保留原有 content-free Compact 结果行；未提交
  但已恢复 IR 的候选发送 `superseded/candidate_discarded` 并静默收起，只有真实异常发送红色 `failed`。
  Gateway 和 TUI 都只允许有界字母数字错误码，异常 message、摘要和 prompt 不能穿过该边界。
- 同 thread 前台与后台模型工作片共用 durable run claim，一次只执行一个真实 slice。用户普通输入命中 live
  foreground 时作为 typed steer 在最近 provider 安全点进入；父代理已经让出等待 child 时，新 foreground
  slice 不等待所有 child，最多等待正在执行的单个后台片释放 lane。

## 会话历史与 provider 缓存前缀

- `request_execution._gateway_conversation_context` 先通过 Conversation Compact 取得唯一 committed summary
  与完整消息边界收缩后的 raw tail；`_gateway_conversation_history_seed` 再把这份结果冻结为 immutable
  `ConversationHistorySeed`。Gateway 不把 transcript 重复渲染进 `runtime_injections`，runtime 也不重新读取
  ConversationStore。
- native runtime 的固定时间顺序是 committed summary、已结束 user/assistant、当前 user、本轮
  ToolCall/ToolResult 与插话；text runtime 从同一个 seed 只渲染一次。`CacheStructuredPrompt` 的当前 user
  字符串只服务诊断/归档，provider adapter 在 native messages 已携带该 turn 时必须省略副本。
- 记忆召回、推荐工具、工作区、wake/runtime injection 和执行事实属于本次请求的动态尾部，不得移动到旧
  messages 之前。普通回合只追加消息，真正 Compact 才能以 committed summary 一次替换旧前缀并推进
  generation；TUI Context、模型正文或字符数不能充当 cache-read 证据。
- transcript Compact 也必须使用普通轮同一 tool snapshot、stable PromptBuilder 前缀、provider system 和
  canonical native messages，摘要指令是最后一条 synthetic user 输入。候选分区共用一次冻结的缓存面；
  摘要调用没有工具循环，返回 ToolCall 时改用机械摘要。provider overflow 后尚未消费的 `tool_search` Schema
  只由 carried archive 的 typed envelope 恢复，成功轮已经消费的临时工具不得跨轮复活。
- child/grandchild 通过各自 ConversationThread 生成相同 seed，保持 thread、Compact、workspace 与权限独立。
  presentation-only 辅助调用默认不携带长期历史，避免一次任务上下文被复制到第二个模型表达轮。

## 模型流式存活边界

- `backends/gateway_helpers.py` 为每个请求分别保存短 connect、first-event 和 rolling idle。响应头与首个有效
  SSE data 使用按本轮输入量估算的首事件窗口；第一条 data 后把 socket 与 watchdog 一起切回普通 idle。
- 有效 `data:` 才刷新 liveness；SSE 注释、空行和半行不能续命。流式路径没有隐式 total wall，健康慢流
  可以跨小时；非流式调用仍由模型线程有界墙钟守卫。
- `ProviderRequestOptions.first_event_timeout_seconds` 是 request-local，不修改共享 `HttpBackend`。主代理、
  child 和孙代理共用同一条 typed 请求合同，超时按 `first_event/stream_idle/wall_clock` 落唯一调用账本。
- `/stop` 仍通过 provider interrupt callback 关闭 connect/header/body/退避中的当前 attempt；超时分相不得
  变成关闭取消能力或无限重试。

## provider 工具参数生成进度

- `backends/stream_parsers.py` 继续独占 Anthropic `partial_json` 累积与 stop 后解析；同一 StreamEvent 只附带
  `provider_tool_input_progress.v1` 的阶段、stream index、tool name 和累计字符数。`usage_metadata.py` 在
  provider 层按首条、1 秒/8,192 字符、ready 合批，observer 异常 fail-open。
- `conversation/tool_input_progress.py` 是公开 schema 与 transient block identity 的唯一合同。
  `BackgroundTranscriptSink`、后台 main 和直接 TUI 复用 projector；Gateway 前台只传同一白名单后的 rich
  chunk。任何调用方都拿不到 partial JSON、文件内容、命令、路径或凭据。
- `tui_view_model.py` 的 `tool_input` role 只存在于 active blocks；`tool_input_completed` 直接删除，不冻结历史。
  `tui_block_renderer.py` 将其画成灰色动画计数行并暂时隐藏笼统 Thinking。真实 `tool_started` 与 turn terminal
  另有兜底清理，因此丢一条易失 ready 也不会留下永久 Working。
- 这套投影不属于 tool protocol：只有完整 `content_block_stop` 后形成的既有 tool_use block 才能进入
  ToolCall、权限和 handler；字符数/ready 不参与任务状态、完成裁决、恢复、Compact、缓存或 timeout。

## Owner WorkspaceOnly 与管理员 Full Access

- owner-scoped Shell 的结构化 cwd 先由 path policy 校验，命令正文仍交给 OS 沙箱，不解析重定向、管道或
  任意字符串中的路径。另一 owner/未授权宿主路径不会被挂载，因此进程内 `ENOENT`/`EACCES` 只证明当前
  scope 不可访问。Shell 结果无论成功失败都向模型投影 `owner_workspace_only`、
  `external_host_paths_hidden=true`、`host_path_absence_proven=false`；该投影解释可见性，不参与授权、
  operation 终态或副作用裁决。
- `run_command` 的唯一机器退出事实仍是 shell 最终 return code。供应商若在失败动作后追加恒成功命令，底座
  不从 stdout 或自然语言猜中间动作；model spec 明确要求直接使用工具自带退出码，避免 `; echo $?` 遮蔽。

## 会话发现与恢复边界

- `/sessions` 从当前 owner 的 `SessionManager` 读取 canonical 会话记录，倒序、有界展示并生成精确
  `my-agent resume <session_id>`；坏记录只计数，不中断其余列表。命令不写 transcript、不调用模型、不触碰
  其他 owner。
- 当前 TUI 不支持只替换 session id 的原地切换。完整 picker 需要像 会话运行时/终端交互 一样同时重建
  transcript、active turn、Compact、task link、权限和视图滚动状态；在该合同落地前，退出再 resume 是唯一
  正确恢复路径。

- 每个 Gateway 请求先冻结结构化 owner 身份和 owner home。WorkspaceOnly 的文件、Shell、PTY 与 LSP 都以
  owner home 为硬墙，进程启动 cwd 不是权限来源；这道文件墙不关闭外网，网络仍由工具自己的网络合同裁决。
- owner 磁盘发现每个 controller tick 只处理一页，避免大用户量启动时阻塞；只要该页返回 continuation
  cursor，下一 tick 立即继续相邻页，完整一轮结束后才进入配置的稳态重扫间隔。否则后页 owner 的恢复延迟
  会错误放大成 `页数 × 60/120 秒`。runner 心跳仍是默认判活事实；仅当 session 明确记录进程形态且 OS 已
  证明 exact PID 死亡时，最后一拍新鲜心跳不阻止重启回收。
- 通知轮询不得为 cold owner 构造完整 Agent，也不得因此丢掉已提交 final。被动读先用 exact
  owner identity 定位 owner home，只查固定深度的已存在 conversation roots 与 exact channel binding；
  只读 store 禁止 `mkdir`，volatile activity/approval 保持空，直到 owner 因真实任务或控制请求被加载。
- 运行 child 的用户插话使用 exact attempt/message id；provider 真正接受后才同时提交 child
  ConversationThread 与 durable display event。当前 TUI 从 pending map 提升，新 TUI/Gateway 从持久事件
  重建相同 user block；普通文本不得越过 provider boundary 提前写成已消费。
- `full-access` 只有 `local/main` 管理员配置可以生效。远程 owner 即使复制配置或在正文里声称管理员也会降为
  WorkspaceOnly；Full 主代理创建 child/grandchild 时重新加 owner 墙，并只保留结构化 task/product 写根。
- Full 模式下的外部路径意图属于 prompt 软约束：用户明确指定外部目录或系统排障时才离开 owner home；涉及
  其他 owner 必须明确点名，默认先只读、尽量少改。自然语言永远不负责授权，真正裁决只读 owner/config/boundary。
- owner home 内用户文件可正常使用，但 `permissions/quota/retention/policy`、运行账本、Compact 与审计目录作为
  宿主控制面保持只读。每次工具调用复制 request-local handler/path policy，单 Gateway 多 TUI 不共享可变权限。

## 跨回合工具终态折叠

- `request_execution._persist_gateway_assistant_result` 从本轮 `archive_tool_calls` 构造唯一
  `conversation_terminal_tool_fold.v2`，与公开 assistant 正文同一次写入 ConversationStore metadata；repair
  队列携带同一份值。用户 transcript、channel delivery 和最终正文不拼接该投影。
- V2 metadata 同时固定保存有界 hot-tail、cold-fold 与 typed deadline。`_gateway_conversation_context` 读取 raw
  tail 时，默认 300 秒热期内附加 hot-tail，过期后附加 cold-fold；它不重写 metadata、不调用模型，也不推进
  Compact。旧 V1 只按 cold-fold 读取。完整输出继续由 owner archive 掌权，
  operation/artifact refs 继续是副作用与产物事实源。
- `conversation/compact.py` 计量并摘要正文加 fold，但普通 fold 不推进 generation。当前尾部 fold 次数与
  `compact_source_tool_pairs` 分栏，后者仍只表示运行中 native IR 真压掉的完整工具对。
- 普通续轮 history 只在末尾追加新 user/assistant/fold；冷热期内部的旧前缀不得按新预算重写。唯一一次
  hot→cold 变化只允许发生在配置的 cache-cold deadline，真正 Compact 才原子替换旧 history 为 checkpoint
  summary；替换后的新前缀继续保持稳定。
- 手动 `/compact` 的 Gateway 控制结果把 canonical generation 放在 typed `task_status`，operation receipt
  原样保存，CLI adapter 只恢复结构化字段。TUI 收到成功代数后发布 `compact_boundary`、撤下失效的压缩前
  Context snapshot，并显示“下次模型调用刷新”；下一次真实模型 preflight 才写入新用量，不为 UI 单独发模型请求。
- `/client/notices.agent_activity.compact_count` 是 TUI 启动/恢复的 typed 水合入口。即使 active task 为 0、
  没有 Working block，completed display frame 也要更新 status；controller 与 reducer 都按 max 合并代数，
  只防迟到帧回退，不把客户端投影升级成新的 Compact 权威。

## 普通续轮的直属子代理完成输入

- `request_execution._gateway_conversation_context` 在加载 sticky workspace 后，从同一 thread 的
  ConversationStore observation 构造 `conversation-subagent-completions.v1`。普通追加轮会换 task id，
  所以 `_gateway_workspace_lineage_task_ids` 以同 thread、非 detached task link 的 exact canonical
  `task_path` 等值形成持续 workspace lineage；cwd、goal 和用户正文都不参与。
- 只有 event `root_task_id` 属于该 lineage，且 exact `parent_agent_id == root_task_id` 的
  `subagent_runner_finished` 才能进入。每项保留自己的 root id，便于审计多个分批 follow-up root。
- 同 child 的多条终态按 observation 时间取最新，详细项最多 12 条并显式报告 total/omitted_count。
  Gateway 只投影 `subagent-completion.v1` 的 status、turn-end、最终回复和 refs，永不投影
  `runner_result_json/output_json`。普通前台 prompt 在历史后注入这批整合输入；named Audit prepare 不注入。
- 这是 会话运行时 child completion message 在现有文件协议上的适配：后台 wake、前台续轮和递归父级共用一个
  completion schema，Gateway 不复制 child lifecycle、权限或验收状态。

## 后台过程事件传输

- `conversation/background_transcript.py` 是 Gateway 进程内的有界公开事件环：每个 thread 最多 1024 条，
  序号单调，新任务清旧正文但不回退序号。它只保存已脱敏显式 thinking、真实工具边界前的过程段、公开
  tool/display、重试计数和数值 Compact；不保存 hidden reasoning、权限、任务状态或最终回复权威。
- Anthropic thinking 由 parser 的原 `content_block_stop` 形成 typed terminal，严格早于后续 text delta；完整
  response 只为没有流内 completion capability 的 backend 兜底。TUI 对旧 Gateway 在正文后到达的完整
  thinking 只消费一次，不创建新块；这层兼容只读事件相位，不比较模型正文。
- `/client/notices` 在原 `after/cursor` 最终通知流旁增加 `event_after/event_cursor` 过程流。客户端只能按
  已鉴权 conversation 读取，不能指定内部 thread/task。慢客户端丢 start 后可由携带完整内容的 terminal
  event 恢复稳定块；Gateway 重启允许丢中间过程，持久 `background_notice.v2` final 不受影响。
- `cli/chat_parts/tui_threading.py` 同步推进两个游标，`TuiRuntime` 只接受冻结 schema 和 `bg-main:` 命名空间，
  再交给原 sequencer/reducer/renderer；因此没有第二套 Working、完成状态或业务 transcript。

## 后台会话车道与公平调度

- `conversation.runtime.BackgroundMainAgentScheduler.prepare_tick()` 只做无模型的维护与到期入队；直接调用者
  默认附带 inline orphan supervision 作为无 Gateway 时的恢复兜底。单 Gateway 已由独立
  `_GatewayOrphanReconciler` 按 owner 有界恢复，supervisor 因而必须传
  `include_orphan_supervision=False`；否则同步重复扫描会在历史账或磁盘变慢时先堵住所有 ready lane。
  `ready_thread_ids()` 只投影持久 wake/observation/policy 中的 thread identity，不领取或消费来源。
- `tick_thread(thread_id)` 只能消费一个 durable thread 的来源。同 thread 的前台、后台唤醒、
  progress policy 和 scheduled continuation 继续由 `conversation/run_claim.py` 的唯一执行 lane 串行。
- `cli/gateway_loops.py::_BackgroundMainSupervisor` 用 `(owner_key, thread_id)` 作进程内 in-flight 键，
  按 owner 轮询提交。`background_owner_workers` 是全局后台会话池，
  `background_threads_per_owner` 限制单 owner 并发；超出者保留在持久队列等后续 tick。
- scheduler 的 policy 诊断是 worker thread-local，不同会话不会互相污染运行事实。

Gateway 的 active-turn 工具恢复从 `user_space.runtime_paths.runtime_owner_root` 读取唯一 owner 根；该 helper
位于 Gateway 与 agent-core 都能依赖的下层。旧 `agent_core.runtime.owner_roots` 只兼容 re-export，同一目录
选择不能在 Gateway 内复制，也不能形成 gateway-parts 反向 import agent-core 的分层环。

## TUI 后台活动投影

2026-08-22 起，activity endpoint 公开 `conversation_agent_activity.v5`：Gateway 后台主代理把最近一次
thinking、tool、provider retry 或 finalizing 阶段和同一次 provider preflight 的数字 context usage 写入
进程内有界 display sink；直属 child 的职责短标题与当前上下文 token 从 exact canonical run 只读，
Compact 次数则精确加载其 `agent_thread_id` 对应 ConversationThread generation。持久 native IR reduction
先提交同一 thread generation，display sink 只投影结果；presentation/no-save 临时事件不计数。当前 token
快照也不是累计计费用量。当前 active task 的 Todo 则从
canonical `task_progress.v1` 账本只读投影为 `id/title/status`；
主代理累计 Compact 次数只读 ConversationThread 的 `compact_generation`。客户端首次立即查询，正常时
每秒轮询并只接收白名单字段；传输或合同失败按 0.5/1/2/4/8 秒退避，下一份有效快照会重置周期。main 快照可易失；child
数值与 Todo 仍由各自 canonical ledger 持有，全部展示字段都不参与任务结束、恢复或授权，真实 task
link/run/turn_end 仍是唯一生命周期事实。

main 行的 task identity 与耗时不由易失 sink 或客户端面板年龄决定。activity projector 精确选择
`ConversationThread.workspace_task_id` 对应的 active root，并用该 `ThreadTaskLink.created_at` 作为当前普通
回合起点；sink 只补 thinking/tool/context 阶段。Gateway 重启使 sink 为空时仍从这个 root 恢复 waiting 行，
而缺少结构化 root 起点的旧数据只显示 `0:00`。较晚 child link 不得抢占主时钟，该投影也不参与续跑或收口。

`gateway_parts/bounded_http_server.py::GatewayBoundedHTTPServer` 是这些客户端共享的唯一 HTTP 并发入口；
accept backlog 与在途请求上限均为 128，16 个 daemon worker 在进程生命周期内复用。容量满时入口在进入
产品 handler 前返回 `503 GATEWAY_HTTP_BUSY + Retry-After: 1`，让客户端沿现有失败退避重连；它不能改变
handler 内的 owner/thread 鉴权、turn 串行、operation 幂等或状态权威。停机时未开始的排队连接会被取消
并关闭，正在执行的 daemon handler 不得拖住 Gateway 退出。JSON 与 metrics 响应统一声明
`Connection: close`，防止空闲 keep-alive 永久占用固定 worker；后台 notice 请求 2 秒无响应仍由客户端转入
退避，避免一个失联窗口长期占住连接。JSON/metrics 的 header/body 写回共用唯一 transport boundary：只将
EPIPE、ECONNRESET、ECONNABORTED 解释为客户端已离开并结束该 socket；已经进入 handler 的持久业务照常
收尾。序列化失败和其它 OSError 继续进入 server error path，不能用断连收口掩盖产品错误。

多用户公平分层处理：未鉴权 socket 只受全局 16/128 transport 上限约束，不能相信 header 猜 owner；handler
鉴权以后，模型任务沿 `GatewayAdmission` 的每用户/全局/同会话三层准入进入持久队列，后台会话再按 owner
轮询。对照中 通道运行时 更适合多通道入口（pre-auth IP budget、auth scope 限流、device/IP 控制面限流），
长期助手 更适合会话一致性（resolved session lease、profile 独立 DB）。未来接 Web/飞书仍共用一个 Gateway，
只在各自层适配这两类合同，不按用户启动 Gateway，也不把共享 IP 当成 owner 身份。

累计成本另走 `ModelCallLedger`：按 request/run 记录 provider input、output、cache read、
cache creation 和真实/估算调用数，再投影到 runtime fact、`AgentRunResult` 与 Gateway result。一次 request
可能在 overflow→Compact 前后多次 finalization；每次都以 `physical_model_attempt_count` 作为累计 snapshot
cursor，`ConversationModelUsageStore` 在唯一追加锁内减去同 scope 既有增量，把 delta 幂等写入 exact
owner/thread 的 `model_usage/<thread_id>.jsonl`。事件保存原 snapshot digest；重放同 cursor 幂等、异值复用
fail closed。后台 main 即使 `do_save=false` 也只跳过普通档案，不丢真实模型用量。供应商真值和本地估算分栏
汇总，损坏账本不能降成零成本。它不会反向改动 Context 行，也不会参与 Compact、任务完成或权限触发。

薄 TUI 的 audit hook 与 workspace 传递相互独立：客户端无需构造第二个完整 Agent，仍必须把当前绝对
cwd/roots 放进首次 ask。服务路径继续固定到 owner 唯一 Gateway；workspace 只属于 thread v6，不能因
audit Agent 为空而回退 daemon cwd。

后台轮只有在 delivery contract 已形成可交付正文时才追加 `background_notice.v2`，TUI 将
`display_kind=assistant_response` 投影为普通 assistant transcript；内部 suppressed 轮和空正文不生成消息。
旧 v1 notice 继续只作为灰色系统显示兼容，不取得新的语义。

- `gateway_parts/http_handlers.py::read_gateway_client_notices` 在可信 owner/session 解析出的同一
  `ConversationThread` 上读取 active task link，再通过 `conversation/agent_activity.py` 从 canonical run
  账本投影这些 root 的 depth=1/parent 精确直属 child，与 after-cursor 之后的 notice rows 一次返回。
  客户端不能指定 thread/root id，也不能从通知正文推断活跃状态。
- notice 是高频被动投影，不是 owner Agent 的加载入口。已驻留 owner 通过
  `OwnerScopedAgentPool.peek()` 查询，冷 owner 返回 `ok=true/owner_state=cold` 的空活动投影；该路径
  不构造 Agent、不提升 hard residency、不触碰 LRU。历史、Memory、消息、控制和工具继续走
  `resolve_gateway_scope_agent()` 的完整身份/权限装配，不能为了省内存改成 cold lookup。
- `cli/chat_parts/tui_threading.py` 只有在 `ok=true` 时消费该快照；传输或解析失败保持当前投影，避免把
  “暂时查不到”伪造成任务结束。直连本地模式调用同一个只读 activity projector。
- `TuiRuntime`、reducer 和 renderer 只维护一个 session-scoped `background` 活动块。前台 thinking 优先，
  前台让出后在 composer 附近显示灰色闪动 Working 标题、实时 main context、Todo 与直属 child 行；真实
  root 计数归零时删除 Working/child。最终后台 notice 会先携带最后一份 Todo 快照再显示 assistant final，
  因而完成勾选不会随 active link 收口一起丢失。Todo 快照采用 replace-all：字段缺失保留旧投影，明确
  空列表收起旧清单；后者只清 UI，不删除账本。整个区域不进入 transcript，也没有调度、重试、停止或
  完成裁决权。
- Todo 默认是四条状态窗口：最近完成、全部当前运行项和下一待办按优先级占位，超出部分由 `Ctrl+T`
  展开。运行项复用 Working 的全局动画帧；该本地展开状态不写回账本。常驻 Context 只显示总量、窗口占比、
  已提交 Compact 次数和明确命名的自动压缩点，prompt/messages/tools 的协议构成留在 `/context`。若下方
  coordinator panel 仍有 typed active child、但它没有 explicit progress id 映射到可见 Todo，标题单独显示
  `子代理运行中 N`；该数字不进入 Todo 完成数、不写回 ledger，也不从展示文案猜关联。
- transcript 的 `follow` 仍是每个 main/child viewport 的 process-local 展示状态：被动事件仅在原本位于尾部
  时自动推进，用户上翻后保留阅读位置。一次通过输入长度与 child 只读检查的真实提交则复用唯一 `end()`
  显式 return-to-live；空输入且当前 viewport 已离尾时，`Down` 同样先复用该 `end()`，已经贴底时才进入
  child selection/history。按键层只读 typed `follow`，不解析未读提示文案；无效输入不能移动 viewport，
  命令/消息也不创建第二个滚动事实源。

## 回合终态与 Compact 进度投影

- `gateway_parts/request_execution.py::_update_response_from_result` 只把运行时已经确定的
  `turn_end_reason` 写入最终 response；Gateway 不拥有第二套完成判定器。
- `BufferedChunkStreamWriter.write_conversation_compact_progress` 是 rich 客户端唯一的持久会话 Compact
  进度出口。`_public_conversation_compact_progress_payload` 对 schema、phase、stage、数值字段和可选 typed
  `error_code` 做白名单投影，再写入 `conversation_compaction_progress` chunk；其中
  `superseded/candidate_discarded` 是未提交候选的中性终态，客户端只撤下活动块，不生成失败历史或 Compact
  代次。真正 `failed` 会冻结“原上下文已保留 + error_code”，但不推进 generation。
- `_gateway_compact_progress_callback` 只在当前 chunk writer 明确实现上述 typed 方法时向
  `conversation/compact.py` 传回调；普通 callable、CLI 和 IM 客户端不会收到展示事件。
- ConversationStore 的 summary/checkpoint/generation 仍是权威事实，百分比只是同一操作的展示投影，
  不参与完成、恢复或 CAS 裁决。

## TUI 活动回合输入确认与 rich context 事件

- `cli/chat_client_context.py::GatewayChatClientAgent.request_active_turn_input` 是薄 TUI 的活动输入 HTTP
  入口；它只调用本机 `/control`，携带 owner conversation 与 opaque `message_id`，返回值只表示 steer 是否
  被 Gateway 接收，不能创建 fallback request。
- `agent_core/runtime/guidance.py` 从持久 guidance 的结构化 `channel_message_id` 提取本批关联 ID；正文和
  FIFO 位置均不参与身份。`begin_active_turn_input(ids)` 只切分新的用户回复段；只有
  ConversationStore 在 provider 成功后把 exact receipt 推进为 consumed，才调用 sink 的
  `complete_active_turn_input(ids)` 写 `active_turn_input_consumed`。普通客户端不扩大公开面。
- `cli/chat_parts/tui_runtime.py` 保存 session-local pending receipt，Gateway event 只提升本 TUI 已登记的
  精确 ID；未知外部 ID 被消费但不创建本地消息。`tui_view_model.py` 分开保存 `pending_steers` 与
  `queued_inputs`，`tui_block_renderer.py` 把两者固定在 composer 上方，不放进滚动 transcript。
- `model_visible_context_usage.v1` 与 `model_visible_context_compaction.v1` 都只允许数字白名单穿过 rich
  chunk。usage 是最新模型调用前的展示快照；compaction 是 active-turn native IR 的事实，只在当前 sink
  存活期间展示，不再写 child run。child 常驻次数只来自 ConversationThread；两种 rich 事件均不获得
  ConversationStore compact 权威。
- Gateway transcript、message repair 与 delegated agent attempt 都写 canonical `conversation_request_id`；
  Compact 只排除尾部尚无 assistant checkpoint 的当前 user 输入；一旦同 request 已经产生并持久化 assistant
  checkpoint，该完成前缀就是合法 Compact 来源，不能因 request id 相同而把整段历史隐藏。
  `gateway_request_id` 只保留为旧 Gateway 行的显式迁移读取与请求账本
  join，不是第二种 Compact identity。
- usage 的 `compact_trigger_tokens` 是会话统一策略，不是单次请求的权限投影；no-save 仅让
  preflight 使用完整 context window 作当轮硬限，不把 TUI 的 90% 压缩点改成 100%。

## 最终效果裁决与 owner-scoped 命令环境

- Gateway 最终 presentation 同时保留两类结构化事实：完整 `operation_verification` 供审计；最近
  effect-bearing mutation 供“是否必须改写 final”裁决。handler 前的 `not_started` 尾部属于前者，不能
  覆盖此前 succeeded effect；unknown/failed/cancelled/incomplete 仍拥有阻断权。required action 使用
  独立 gate，任何层都不能从模型正文判断。
- owner-scoped `run_command` 继续由 sandbox 提供只读 home 与 task `/tmp` 映射。子进程环境在 secret scrub
  后设置 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`，并为标准 npm 增加
  `NPM_CONFIG_CACHE=/tmp/.cache/npm`。admin/无 owner scope 不改环境，`HOME` 也不被伪造；Gateway 不按
  项目名、目标语言或命令文本扩展写权限。

## TUI 工具审批、Compact 边界和传输中断

- `cli/chat_parts/tui_preflight.py` 只负责真实 Gateway readiness 到 typed connection block 的启动门；它不创建
  request、不读取 key，也不在 ready 前启动 worker。`cmd_chat` 的同步 wait 只供 plain/non-TUI 路径。
- `agent/contracts/tool_approval.py` 定义共享 `ToolApprovalRequest/ToolApprovalDecision`。授权只认
  `tool_name/run_id/operation_id/idempotency_key/args_hash` 组成的 binding 和有限 decision 枚举；标题、
  option label、feedback 都只用于展示或后续模型上下文。
- `run_command` 的 command parser 与结构化 effect mapping 由同一 `EffectResolverPolicy` 取最高风险。
  sandbox 包含性另由 canonical `SandboxPolicy.uncontained_by_parameter` 显式声明：
  `run_in_background=true` 和 `terminal_session.action=start` 都越过单次 handler 存活，且当前
  bwrap 共享主机网络，因此不能用 `sandbox=required` 免掉 exact approval。这条门只读
  typed 工具参数和 binding，不解析 prompt 或命令的业务含义。PTY 已批准 start 后的
  `write/read/close` 只操作原 session，不重复弹窗。
- child capability grant 与 `approved_actions` 分账：前者只投影允许的工具、命令、路径和网络 scope，
  后者才按 exact `tool_name/run_id/operation_id/idempotency_key/args_hash` 批准一次具体副作用。
  `controlled_exec` 当前经 `subprocess.Popen` 执行且没有 OS sandbox，因此声明 `sandbox=none`；即使 grant
  完整匹配，`apply=true` 仍必须进入同一 ToolExecutor/permission bridge，未批准时 handler 不运行。
- `agent/conversation/agent_tool_approval.py` 是后台 child 到 owner UI 的唯一 pending/decision 账本。记录位于
  owner ConversationStore 的 root/run 哈希路径，发布、决定和清理共用 exact transition lock；
  `.consumer.json` 只表示显式交互客户端的短租约，不是批准。无租约、取消、终态、损坏和 identity mismatch
  全部关闭式失败。
- `/client/notices.agent_permission_requests` 只向同 conversation 的有界 root 投影返回；客户端必须显式声明
  `client_capabilities.tool_approval=true` 才续租。`/client/agent-permission` 经过统一 agent control owner/
  thread/root/current-attempt 门，并再次比较完整 request。HTTP 错误不能回退成普通聊天或批准。
- `cli/chat_parts/tui_permission_queue.py` 将前台和多个 child 控制器排入 root runtime 的同一 FIFO。只有队首
  发布 overlay/接收按键；child 名称只装饰标题，writer 捕获服务端原始 request。详情页只切正文 store，
  root overlay provider 不切换，因此导航不能隐藏安全确认。
- `agent/gateway_parts/permission_bridge.py` 是跨进程决定桥。目标固定为 processing chunk 同级的
  `.approvals/<sha256(request_id)[:24]>/<sha256(permission_id)[:24]>.json`；路径不接受外部 id 拼接，
  原子文件在 schema、request id、permission id、完整 binding 全部匹配后才消费。
- `request_execution.BufferedChunkStreamWriter` 是 Gateway typed 事件唯一出口：模型 delta、工具 progress、
  `permission_requested/resolved`、`conversation_compacted` 共用同一 chunk cursor。writer 是否等待审批只读
  请求的显式 `client_capabilities.tool_approval`；没有该能力时不得按 source、终端在线或文案猜测。
- `cli/chat_parts/gateway_client.py` 把 chunk object 交给 TUI typed consumer；consumer 成功时不再走 legacy
  文本投影，最终 response file 仍是请求终态事实。坏行有界跳过，cursor 继续前进，不能重放已消费事件。
- Gateway server 是模型历史注入的唯一位置；TUI resume 的 `load_gateway_chat_history()` 只做显示投影，按
  `channel=cli_chat/source=cli_chat/gateway_request_id` 成对恢复 foreground user/assistant，不把这些行再次
  发给 Gateway worker，也不接受 background/半回合/其它 channel 混入。
- `agent/backends/gateway_helpers.py` 在真实 HTTP 读边界惰性注册 interrupt callback，header 等待和 SSE
  阻塞都关闭同一 response/socket；任务结束注销回调。该传输清理只使当前调用退出，不替代 conversation
  `/stop`、task 状态或 Gateway response 终态。

## Memory Curator 后台接线

- `cli/gateway_loops.py::_run_memory_curator_if_due` 使用当前 owner scheduler 已持有的 scoped agent，不能
  重新创建 Agent、ToolRegistry、owner identity 或独立线程系统。
- `gateway_parts/control_service.py` 把已认证 session close/reset 转成 typed lifecycle request；
  `owner_wake_discovery.py` 负责重启后重新发现 durable pending work。二者都不直接生成 Daily/Candidate。
- Curator 的 provider/model、strict schema、lease、cursor、批提交和运行审计由 Memory 模块拥有；Gateway
  只提供生命周期触发和现有 owner maintenance 执行位置，因此聊天、飞书、本地入口不会形成不同记忆语义。

Gateway 负责把外部请求落成可审计队列，并由 worker 调用 SimpleAgent。它不负责模型业务决策。

2026-07-09 P0 维护仅清理 gateway 文件的 import/type lint，不新增入口或结构层。

## 2026-07-29 模型工作节奏与 standalone 状态边界

- 工具循环只把真实工具请求、结果、错误恢复和 Compact 事实带给模型，不再维护按读取轮数触发的
  `exploration_fuse` 或分段读取后的自动 checkpoint prompt。计划、草稿与阶段记录是模型可按任务
  自主调用的普通能力，不是 read 工具的隐式后置动作。
- `runtime_mixin._run_with_params` 是 standalone 顶层运行的终态缝隙。所有 Compact 自动续接结束后，
  它把 typed `AgentRunResult` 交给 `run_task_workspace_writer.finish_run_task_workspace_if_needed`；
  后者只允许 owner-scoped 当前工作区，并排除 conversation、control-plane 和 task-local 生命周期。
- `user_space.run_workspace.finish_run_workspace` 是该投影的唯一写入口：精确核对
  request/run/task identity，在 `state.json` 锁内幂等写终态，再追加 `run_workspace_finished`
  timeline。自然语言回复、目录内容和旧索引都没有状态变更权。

## 2026-07-28 工作目录、task lifecycle 与 model attempt（2026-09-02 R155 更新）

- `request_execution._gateway_task_attributes` 把 thread 最近工作区投影与本轮 live
  `conversation_task_id` 分栏。只有 exact request binding、未结束 Goal 或 active task 才能在模型首采样前
  投影 `conversation_execution_cwd/runtime_workspace_roots`；普通 terminal link 不再取得新回合 cwd 权威。
- `conversation.task_promotion` 为没有精确绑定的新工作懒建本轮目录。若写工具携带同 thread 旧项目内的
  显式结构化目标，统一 tool runtime 在 effect 前按 canonical `task_path` 回绑并建立 successor：同根多代
  terminal execution 只算一个项目，多个不同根或同根多个 active executor 时不猜。`/goal` 的精确持久记录
  仍是唯一允许原 id resume 的例外。`run_task_workspace_writer` 只在本轮工作工具已设置 active 标志后归档
  task workspace；task-local child 携带父 conversation id 只作 lineage。
- 新会话没有 sticky workspace 时仍保持惰性晋升，但首个 `promotes_task` 工具必须在冻结该调用的
  write boundary、审批 cwd、沙箱根和 handler cwd 之前完成晋升。materialize 后由 conversation seam 原子同步
  `run_workspace`、`conversation_execution_cwd`、runtime roots 和旧 cwd rebase source，再重建当前 call；
  不能只让第二个工具看到新目录。该顺序与 会话运行时 在工具前固定 `TurnContext.cwd` 的边界一致，普通纯聊天
  不因此创建任务目录。
- `user_space.run_workspace.activate_run_workspace` 是新建与复用 task 目录的同一激活入口。它原子更新
  当前执行在 `task.yaml`、`run_workspace.json`、`state.json` 和 artifact manifest 的投影，保留
  `output/`、既有 artifact 条目与 append-only timeline；相同 request/run/task 重入幂等。旧
  conversation task link 只能在身份仍与当前 `state.json` 一致时同步终态，不能让旧执行覆盖新执行。
- `conversation.runtime` 在 background claim 后复核终态，关闭 lost-race wake/policy；它保留同一 thread
  的完整 summary/raw tail，同时把 task link、observation 与 progress 等运行投影收敛到精确 current task
  及其持久 child lineage，不从正文分类任务，也不向模型重建历史任务菜单。
- `_finalization_service` 把 `ModelCallLedger` 的 logical turn、物理 model attempt 与 provider HTTP
  attempt 计数写入内部 run result 和 runtime facts；这些字段只观测，不参与任务完成裁决。ledger 的
  `records()` 只保留最近 128 条诊断明细，按 request/run 的同源 aggregate 单独累计完整计数，不能再用
  retained detail 长度冒充总量；aggregate 不保存 prompt、response、请求体或 key。
- 普通代码任务的验证新鲜度只读工具结果 `metadata.handler_details` 内的 typed verification envelope：
  成功 workspace mutation 使最近验证 stale，read/search 不改变该事实。completion soft followup 以
  root + durable verification event ID/status 去重；它不执行测试、不把普通任务改成硬门，也不解析正文。

## 2026-07-27 工具轮窗口与持久会话 Compact 的边界

- `agent_core._tool_loop_service.build_tool_loop_prompt` 是主代理、子代理和 Gateway conversation
  共用的每轮模型输入入口；文字协议使用 `conversation.tool_context_window` 的字符近似，原生协议使用
  `model_visible_context_tokens` 的完整模型输入 token 估算。两者读取同一
  `RuntimeCompactPolicy`，但不再把字符数误当成原生 IR 的预算。
- 原生入口达到精确配置阈值时，`tool_ir_compact.compact_native_ir_to_token_budget` 只从最旧
  ToolCall/ToolResult 整对回收，保留最新一对和所有运行中 `UserTurn`。回收前复用
  `memory_archive.compact_semantic_summary` 读取同一 native messages，并用最多一条
  `CompactionSummary` 替换旧段；后续压缩原位替换旧摘要。`conversation.tool_context_window` 仍只写一条
  有界 archive handoff。summary、handoff marker 和近期尾部共同进入同一完整 token 预算，
  都不是第二个 Compact ledger。
- Gateway 的 `conversation/compact.py` 当前只管理 owner/thread 持久 transcript 的
  summary + raw tail + checkpoint。每轮工具历史窗口仍是同一模型请求的内部阶段；迁移期间 exact child
  attributes 只保存其纯数字展示计数，不形成第二份摘要、transcript 或恢复状态。
- Gateway 请求在 provider/tool 回合中途崩溃时，`gateway_active_turn_recovery.v1` 只证明“同一 request 被
  reconciler 重排”。恢复器从当前 owner 的 canonical 工具索引按 exact `conversation_request_id` 重建
  `carried_archive_tool_calls`，再复用上面的同一 IR/Compact 续接链；不把未提交 assistant 正文当历史，也不
  扫描 child 工作区。工具索引缺失表示尚无已归档调用；索引存在但不可读则 fail closed，不能退回原 prompt
  自动重做副作用。
- 跨工作片恢复时，`carried_archive_tool_calls` 始终是预算、去重、已执行工具、未知副作用和审计的完整权威；
  `conversation.active_turn_compact` 只从模型可见重建中排除当前 thread 已提交 checkpoint 链里的 exact
  `source_tool_call_ids`。provider overflow 且 transcript Compact 无来源时，它复用 live-tool checkpoint/CAS
  提交一代完整替代摘要与有界近期记录；未提交候选、损坏链和其它 thread 不能改变模型视图。
- provider overflow 的既有最终保险仍可成对回收最旧 native IR；任何路径都不得留下孤立
  tool-use 或 tool-result。

## 核心文件

- `agent/gateway_parts/io.py`、`agent/conversation/store.py`：文件锁与线程锁组合的
  `locked_file_transition` / `task_transition_guard` / `goal_transition_guard` 是单任务或单目标状态迁移临界区；`/btw`、`/stop`、`/goal` 与完成关闭共用，不各自维护竞态规则。
- `agent/gateway_parts/control_service.py`：当前 processing turn 已绑定 task 时，`/status`、`/btw`、`/stop`
  以该精确绑定为 expected-task guard；没有活跃 turn 时才按 owner/thread 解析控制候选。普通 active 根直接
  进入候选；task projection 已是 `completed` 但同 thread 的 exact claim/enabled policy 仍证明执行中的
  交接窗口也可进入。每次解析只取一份 thread execution snapshot；`interrupted`、过期或不可读状态不复活。
  失去当前任务竞态的 guidance 当场退休，既不进入旧任务也不污染新任务。linked live turn 存在时
  `/btw` 只写持久 guidance，由该 turn 在下一安全点消费；只有没有 live execution 的空闲根任务才
  发布 wake，不允许引导启动第二个主执行器。
- `agent/gateway_parts/http_handlers.py`、`agent/adapter/manager.py`：普通 `/ask` 先按可信 owner/channel/thread
  查询 live processing turn；命中时复用上方同一 guidance 主链并返回 typed `status=steered`，adapter 不再
  新建或覆盖第二份待回送记录。只有 durable task、没有 live turn 时保持普通入队，使这条消息拥有正常回复。
  该决定只看结构化身份和运行状态，不检查消息文字，也没有 Feishu 专用分支。
- `agent/adapter/feishu.py`、`agent/adapter/feishu_unlock_queue.py`：私聊闲置锁在 adapter 入站边界保存
  被拦消息的原 `IncomingMessage`，按 user + conversation + `message_id` 去重并进入有界 FIFO。正确
  密码卡回调只取得一次 drain 所有权，再沿 adapter 原 `_dispatch` 回调续送；错误密码、身份不匹配、
  重复回调和平台迟到重投均不创建第二次执行。队列只做进程内短期接力，不拥有 Gateway thread/task
  状态；跨进程恢复尚未承诺。
- `agent/concurrency/interrupt.py`：线程级 typed interrupt 除了供工具安全点轮询，还允许
  正在阻塞的传输注册短命、幂等的关闭回调。回调在共享锁外执行，执行线程退出时连同中断旗
  一起清理，避免线程复用携带旧任务状态。对齐 会话运行时 的中断边界：状态立即立旗，协作式传输清理
  最多阻塞控制调用 `100 ms`；慢关闭在 daemon thread 继续，不能把 `/stop` 拖到 provider 超时。
- `agent/backends/gateway_helpers.py`：模型 JSON/SSE 响应读取在真正发请求时惰性挂接中断回调；
  `/stop` 会关闭正在读取的响应并报为 `InterruptedError`，不得包装成可重试的 provider 网络故障。
  系统级 `ConnectionRefusedError/ECONNREFUSED` 与异常链中的 typed `socket.gaierror` 属于可恢复供应故障，
  先按 `2/5/15` 秒执行三次物理 HTTP 退避；三次 DNS 仍失败时返回 transient 供模型轮恢复。畸形 URL、
  认证/额度和代理配置错误仍快速失败。每次物理 attempt 把 retry 序号和等待值写入同一 model-call ledger，
  不从异常文本决定是否重试。
  流式 `request_timeout` 按 会话运行时 语义是有效 SSE `data:` 事件之间的 idle timeout（空闲超时），不是整轮
  总墙钟上限；注释、空行、半行和静默不能续期。与 concurrency 的依赖保持请求时惰性解析，避免
  backend/runtime 初始化环。
- `agent/agent_core/tool_model_generation.py`：外层 Gateway/background worker 持有任务中断身份，
  真正的 provider 请求运行在可中断 guard 子线程。流式 HTTP backend 由传输层拥有 idle timeout，外层
  不得把同值重新解释成整轮总时长；非流式或不拥有 idle timeout 的 backend 仍由外层执行总时长保护。
  模型调用边界必须注册一次中断转发，
  将外层 `/stop` 精确传给该子线程，并有界等待其收回；不得只在 provider helper 的子线程
  登记回调，否则任务名中断无法到达真正连接。
- `agent/agent_core/runtime/guidance.py`：`/btw` UserTurn 与精确匹配 durable task 的子代理生命周期 wake
  共用 active-turn 安全点。当前 tool-loop state 保证每条输入只注入一次；provider 生成前后检查新输入，
  丢弃过期响应。用户输入和运行事件都只在模型成功读取其 prompt 后确认，provider 失败时保持可重试；
  只负责模型自然回执的 auxiliary round 显式禁止消费 active-turn input，新输入会先淘汰旧回执草稿，再由
  真实任务轮读取。前台 request 已结束但输入尚未确认时，后台轮按精确 durable task id
  继续读取原 request inbox；确认后的输入已经写入同一 thread transcript，不再作为“新输入”重复注入。启动当前后台轮的 wake id
  留给 scheduler 确认，避免双消费。
- `agent/gateway_parts/request_execution.py`：live turn 注入新的真实用户输入时，只重新开放一次模型自然
  commentary 段；普通工具轮仍不会反复把碎碎念送给用户。旧模型片段先丢弃，新的 commentary/final 继续
  由同一个 request 的原回复投递链送出。显式 rich writer 还把 transport/model-turn retry 的结构化
  层级、序号和等待值投影成即时 system block；不公开原始异常、endpoint 或 key，也不混入模型 commentary。
- `agent/agent_core/tool_loop/natural_user_reply.py`：非阻塞 `wait` 等需要中途回复时，短回复仍由模型按
  结构化事实自然撰写；它不建立第二个执行上下文，也不改变 thread/task 状态。
  presentation-only 参数明确把 `allowed_tools` 和 native tool IR 置空；部分兼容模型仍可能违规返回
  结构化 tool call，因此第一轮仍拒绝并重试。有界重试后只丢弃这份未授权机器调用，保留通过统一协议
  边界的自然正文；没有安全正文则返回 `user_reply_unavailable`，不能伪装成正常空回复。
- `agent/agent_core/runtime/task_identity.py`：区分一次 request/run 与持久 conversation task，为 guidance、
  进度账本、派工 seed、wait 和监督提醒提供唯一的结构化任务/账本键解析。Gateway 普通对话使用的
  `context_scope=conversation` 仍属于 main-agent turn，必须采用已经结构化选择的
  `conversation_task_id`；task-local child 和 control scope 继续只认自己的 run，不能继承父任务身份。
  task-path 指纹算法只在这里定义，Task Runtime State、Goal continuation、dispatch child 终态同步和
  TUI activity 必须复用同一 ledger id，不能按 durable request id 另读一本空账。
- `agent/agent_core/parameters.py`、`tool_call_runtime.py`、`runtime/loop_support.py`：一次性编排工具同时使用
  exact payload key 和结构化 child intent key 去重；同一 assistant turn 的 batch + overlapping singles
  只执行首份副作用，compact continuation 重建相同 key 集合。
- `agent/local_storage/tool_operations.py`、`agent/tooling/tool_operation_coordinator.py`：所有模型可调用的
  mutating/dangerous 工具在真实实现前共用 owner/run/operation 原子 claim；成功或失败结果可精确重放，
  活跃副本不并发执行，持有者死亡或终态不明时 fail-closed 为 unknown。Gateway audit ledger 只观测，
  不再承担执行授权；裸 Registry 默认没有权威 store 就拒绝副作用。提供方返回后只有权威 operation
  store 成功保存终态，`ok=true` 才能继续交给模型；终态保存失败必须降级为
  `TOOL_OPERATION_OUTCOME_UNKNOWN`，原提供方结果只作为脱敏旁证且不能恢复自动重试权。
- `agent/agent_core/tool_call_archive_record.py`、`tool_runtime_ledger.py`、
  `runtime/loop_support.py`、`agent/memory_archive/compact_semantic_summary.py`：operation status/action、
  replay 与 effect outcome/source ref 共用一份 typed 投影进入归档、观测事件和 compact 续跑。语义摘要
  可以折叠普通中段过程，但必须额外保留中段非成功副作用的精确事实块；它不是第二份执行账本，也不能
  改写 operation store 的结论。
- `agent/agent_core/tool_call_runtime.py`、`agent/tooling/write_boundary.py`：写文件、编辑和补丁调用若声明的
  所有绝对变更路径只落在同一 thread 的一个旧 task 内，可在 effect 执行前结构化选回该 task；读操作、
  相对路径、多个候选或 lifecycle 阻塞均不自动选择，也不解析自然语言。
- `agent/tooling/registry_invoke.py`、`agent/tooling/write_boundary.py`：相对 `output/...`、`work/...`
  只按当前结构化任务目录解析；显式绝对路径不改写，直接以原目标进入 owner/allowed roots/危险目录
  硬门。旧 escape-relocate 静默搬运链已经删除，因此一次拒绝不会伪装成写到另一位置的成功。
- `agent/agent_core/_finalization_service.py` 与子代理 typed result/event 主链：普通任务最终回复直接来自模型；
  子代理自然结果和 artifact refs 只作为当前 request/run/task 的结构化事实交给主代理汇总，不再生成完成
  marker、目标覆盖判官或独立验收报告。后台轮按真实 `params.task_id` 认领子代理，任务目录的可读标题只作
  旧数据兼容。
- `agent/conversation/user_visible_text.py`：所有用户出口共用的内部协议净化器，覆盖 bracket tool block、
  XML function/tool envelope、模型以工具名直接降级成 XML 标签以及截断尾块；不得由各 IM adapter 另建
  deny list。
- `agent/gateway_parts/request_execution.py`：执行单个 request，并读取/写回同一 conversation 的
  累计消息历史；复用 runtime compact policy/token estimator/backend 在 owner+thread 内自动 compact，
  raw transcript 保留，thread summary/message+byte cursor/generation/checkpoint pointer 是唯一 live compact
  状态；首次 compact
  后从 byte cursor 读取新增尾部，不重复扫描旧前缀。尾部尚无 assistant checkpoint 的当前 user message
  在持久写入后仍作为 active turn 单独传入，不进入本次历史摘要；同 request 已完成的 user/assistant checkpoint
  可进入后续 Compact。provider 明确返回 `context_overflow` 时，Gateway 会强制推进同一 thread
  的 compact generation 后重试同一 user turn，generation 没有前进或八次后仍溢出则 fail closed。
  Compact 先尝试保留最多四个近期完整回合，近期尾部受统一 token 上限约束；若完整下一轮候选仍会越过
  精确阈值，则退回压缩全部旧段。候选先验证、写完整 checkpoint，再用一次 CAS 提交；失败不推进
  summary/cursor/generation。当前消息始终是独立 root
  prompt。thread 持久保存 `workspace_task_id` 作为最近状态/导航投影；后续 turn 只有 exact request、未结束
  Goal 或 active task 才继承原 cwd。普通 terminal task 后的新工作从 owner home 起步，第一个文件、执行、
  派工或 wait 等 `promotes_task` 工具建立本轮 task id/目录；旧 link 始终保持终态。只有精确持久 `/goal`
  可以原 id 恢复。普通模型上下文不注入活跃/已完成任务菜单；
  `task_progress` 只保留 `read/update`，不承担会话、目录或任务生命周期控制。若写工具携带同 thread
  既有目录中的精确结构化路径，统一执行入口可无歧义绑定该目录；正文不参与身份判断。根 task workspace 不再保存 recovery compact
  指针、continue packet 或第二份任务对话恢复包；主 thread 的 summary + raw tail 是唯一主会话 compact，
  `conversation_thread.v7` 还在同一 compact CAS 中保存 `compact_operation_evidence`、checkpoint pointer、
  经 Gateway 校验的客户端 `cwd/runtime_workspace_roots`
  和连续失败状态，只作为摘要旁边
  的程序事实 metadata，不形成第二份会话。当前 task-local child 的旧持久 compact 数据仍写入各自
  agent run workspace；目标是为每个 child 建独立 ConversationThread 并复用同一引擎，接线完成后删除旧
  apply/continuation，而不是长期双写。thread 创建与
  compact 准备由独立 loader 报告各自错误，避免
  主组装函数吞掉边界。assistant 写回前将用户正文和近期产物 metadata 分栏；公开
  response 使用同一用户投影且不暴露服务器 path。typed tool progress、真实 model delta 与 runtime notice
  分栏写 chunk；工具事件以 `phase` 做机器判断、`status` 只做本地化展示。第一次工具开始前已有的模型
  正文只投影一条 `assistant_commentary`，不改变请求终态，
  provider/runtime notice 不得进入。执行轮
  本轮第一个工作工具激活 sticky task，或模型结构化 `select`/新建 task 时，会把 `thread_id/task_id/task_path` 原子写入
  当前 processing record；多用户 Gateway 无法保存该绑定时阻断工作工具，不能继续产生一个控制不到的任务。
- `agent/conversation/task_promotion.py`、`agent/conversation/store.py`：`ConversationThread.workspace_task_id`
  是耐久状态/导航投影，store 在写入前核验同一 thread 的精确 task link。普通聊天没有本轮 task-active
  标志，因而不会重开生命周期或获得任务归档；第一个工作工具才在 exact/Goal/active task 上继续，或为
  普通新工作创建新目录。写工具精确命中旧 canonical root 时才回绑并建立 successor。模型用 `select`
  切换其他旧 workspace/task 候选时只切换结构化
  lineage；本轮用户消息早已属于同一权威 thread transcript，
  不复制到 task guidance ledger，正文不参与任务身份判断。
  conversation task link 是生命周期权威，`work/state.json`
  只投影同一 task path 的状态；状态文件最终解析目标必须仍在该 task 根内，符号链接越界直接拒绝。
  完成、停止或重新打开后同步更新。workspace 懒建通过共享
  `durable_task_id` 读取已选择任务，不能把新的 gateway request id 写成另一个任务身份。
- `agent/gateway_parts/request_worker.py`：worker loop、认领、完成、失败写回；准入按同会话单飞、
  每用户上限、全局上限三层记账。owner 只从 adapter 的结构化 channel identity 构造：
  `p2p/private -> provider_user(user_id)`，群聊 -> `provider_group(chat_id)`；远程 owner 建立失败终态
  fail-closed，不从 conversation 字符串或首个发言人猜归属。
- `agent/conversation/control_commands.py`：CLI/IM 共用的 `/status`、`/btw`、`/stop`、`/goal`、`/audit`
  唯一 typed command parser、状态 DTO 与确定性用户文本；自然语言不参与硬控制判断。命名 Audit/Goal 的
  `kind/name/duration/cancellation_scope` 在入口一次解析，后续不再从任务正文反推；旧的未命名 Audit
  语法和重复正则入口已删除。`/status` 的
  task/recent-progress 在这一共享渲染边界
  对确定性文字和结构化 DTO 都统一脱敏宿主绝对路径，内部 task authority 仍保留完整路径。
- `agent/gateway_parts/control_service.py`：按可信 user/channel/conversation 解析同一 thread；有 processing
  record 时优先读取其精确 task binding，避免更新但无关的旧 task link 抢走控制权；没有绑定时才选择
  上述 control-active 根 task，尚未晋升则回落 processing request。`/stop` 按所选 link 的真实旧状态做 CAS，
  同时持久中断根 task 和当前 turn，并向两种 interrupt id 发信号；`/status` 只在 processing record、活跃
  子代理或 claim/progress policy 证明有执行器时显示 running 与时长/进度。仅剩可续接 durable link 时保留
  当前任务但显示 idle；同时显示子代理与唯一 thread compact generation，不把 task recovery package 当作
  第二种上下文。
- `agent/gateway_parts/goal_control_service.py`、`agent/conversation/named_work.py`：按已解析的
  owner/thread 执行持续目标的查看、创建、修改、暂停、恢复和清除。旧未命名 Goal 保持单目标兼容；
  显式命名 Goal 可并存，并以独立 goal/task ID 续跑。命名 Audit/Goal 的停止共用 exact
  owner/thread/kind/name 选择器，最终中断、task 状态、子代理和进度策略仍按 task ID 落账。
- `agent/gateway_parts/audit_control_service.py`、`agent/conversation/audit_tools.py`、
  `agent/conversation/workspace_paths.py`：`help/status/clear` 走确定控制面；prepare turn 临时绑定精确
  `audit_id` 与 owner 的 `audits/<audit_id>/`，普通下一轮不继承。`pending_prompt` 与生效要求分离，
  只有当前 prepare scope 中的 `publish_audit_update` 能携带 owner-local 证据发布；名称、task id 和
  工作区均来自可信上下文，不由模型参数选择。
- `agent/agent_core/tool_runtime_ledger.py`、`agent/tooling/write_boundary.py`：远程普通 owner 在已有结构化
  task workspace 时，把文件工具、shell、PTY、LSP 的可写域统一收窄到当前 `task_root`；同 owner 旧任务
  可读不可写，子代理窄授权不放大，畸形根或空解析结果 fail-closed。local 与显式 admin bypass 不自动
  收窄；任务身份只读 runtime facts，不读自然语言。
- `agent/tooling/registry_invoke.py`、`agent/tooling/shell.py`：`run_command` 和 PTY start 没有显式
  `working_dir` 时使用结构化 `task_root` 作为进程 cwd；显式目录优先，ShellTool 仍按注入的 workspace roots
  验证。LSP 和已存在 PTY 的后续动作不被重写，命令正文不参与判断。
- `agent/conversation/goal_tools.py`：持续目标轮的 `get_goal` / `create_goal` / `update_goal`；工具只能读写
  当前结构化 thread+task 绑定，模型只能通过 update 写 `complete` 或 `blocked` 终态。
- `agent/conversation/runtime.py`：每个后台续接 turn 都读取同一 thread 的 compact summary 与完整 raw tail；
  task id 只约束 wake、progress、workspace 和子代理树等运行事实，不能过滤消息或建立 task-scoped history。
  持续目标轮携带精确 goal id，未进入 complete/blocked/paused/cleared 才发布一个去重续跑 wake。scheduler
  只有在没有 linked live turn 时才能启动续接；定时或生命周期 wake 在精确 task 已终态时直接退休。
- `agent/conversation/run_claim.py`：foreground Gateway turn 与 background scheduler turn 共用的唯一
  per-thread 持久执行 lane。它复用 `ConversationStore` 的 claim 文件、租约、进程身份接管与 heartbeat；
  不按 IM、提示词或任务类型分流。Gateway 可等待当前 lane，scheduler 拿不到 lane 则跳过并由既有 due/wake
  事实重试。claim 终态只说明本次执行权已释放，不替代 task/thread 生命周期。
- `agent/scheduler/repository.py`、`agent/scheduler/due_index.py`：前者的 owner-local `store.json`/history
  是 job/run 唯一权威；后者的全局 SQLite 只投影 owner 身份、最早到期时间和短租约。repository 在返回
  create/update 成功前同步投影；Gateway claim 投影后仍必须回到 owner 账本 reserve，不能从投影读取 prompt、
  Persona、Memory 或直接执行任务。旧账本修复遇到不可读文件时不写完成标志，下次重启重试，且不跟随
  provider/owner symlink。`reserve_due_runs` 的轮询只有真正预留 run 或推进 misfire 时才写 owner 账本；
  没有状态变化时保持只读，不刷新 `updated_at`、不重建 due 投影，也不触发 owner logical-byte 配额扫描。
  这与 通道运行时 `cron/service/jobs.ts::nextWakeAtMs`、`cron/service/timer.ts::armTimer` 只按最早到期事实
  唤醒并对 past-due tight loop 设置最小重触发间隔的边界一致；my-agent 仍保留自己的 owner JSON 权威和
  SQLite wake 投影，没有复制另一套调度事实源。
- `cli/gateway_loops.py::_GatewaySchedulerDueController`：按最早到期时间有界 claim owner，只把结构化
  owner identity 写回现有 active-owner registry；不实例化 scoped Agent，不扫描全部 owner prompt，也不新建
  scheduler 执行队列。原 owner discovery 仍处理其他 wake/维护事实，Scheduler 快速到期不再依赖其分页周期。
- `agent/capability/channel_message_tool.py`、`agent/agent_core/_finalization_service.py`、
  `agent/conversation/runtime.py`：消息工具成功后在内部 archive 提交 receipt、实际用户投影和附件引用；只有
  `scheduled_job_due` 的 source reply 会消费该证据并按 receipt 幂等镜像到同一 transcript，随后跳过自动
  DeliveryService 兜底。没有成功证据时仍走原单一自动出口；普通任务中途主动消息不会抑制最终完成答复。
- `agent/agent_core/runtime/guidance.py`、`agent/agent_core/runtime/active_turn_input.py`、
  `agent/backends/tool_ir.py`、
  `agent/backends/message_adapter.py`：`/btw` 在安全点按 typed guidance id 进入当前执行 turn；内容以
  `UserTurn` 留在 provider-neutral 历史的真实时间位置，并映射为 provider `role=user`。模型成功接收后，
  guidance id 幂等追加到同一 thread transcript；它不伪装成 runtime injection。compact 创建新工具循环时
  通过 typed active-turn packet 续接，不解析自然语言、不建立任务专属历史。
- `agent/common/audit_activation.py`、`agent/gateway_parts/request_execution.py`、`agent/ingestion/watch_tool.py`：
  `/audit` 只在请求前缀显式激活，并把 guarantee/window/objective 及 name/duration 写入 task attributes；
  命名项在模型和工具产生副作用前先登记，重名活跃项 fail-closed。后台轮只从自己的 exact task link
  恢复原 root task id、任务正文和 Audit 属性；同 owner 的普通任务不会因另一条 Audit 正在运行而继承
  保证档。准备后启动复用稳定 `audit_id`，已发布要求优先于启动文字。watch 不再从 prompt、goal 或
  summary 重新猜测；同 URL 的不同 Audit 使用独立 watch id。harvester 在领取新批次时才读取新要求，
  已领取批次及其 redelivery 保持旧要求，全部结论落账后下一批才安全切换。
  运行时不指定必须由主代理或哪一个子代理消费，也不固定来源和子代理的映射；模型根据任务、积压、
  可用工具和当前协作状态自主决定。
- `agent/ingestion/harvester.py`、`agent/ingestion/watch_payloads.py`、
  `agent/common/structured_output.py`：保证档先把完整事件写 owner-scoped spool，再按时间、条数或累计体积
  组成有界批次。每条记录带稳定 `ack_id/source_ref/hash`；只有合法 verdict 原子写入结论账后才签收。
  常规模型视图不截断，极端超窗单条只在临时模型视图中保留明确标注的头尾，完整原文仍可由
  `source_ref` 和 inspect 动作查回。程序校验结构和引用，不替模型判断业务结论或选择后续处理路线。
- `agent/agent_core/runner/context.py`：前台聊天与后台任务共用 Agent 时，当前 prompt/run/task/tool-loop 按线程与 agent 弱引用身份隔离；对象销毁即清理，禁止 Python object id 复用把旧工作区带给新 Agent。
- `agent/adapter/manager.py`：把 `channel_chat_type/channel_chat_id` 与 user/message/conversation identity
  一起写入 gateway ask metadata；provider 专有字段在 adapter 边界归一，request worker 不依赖 Feishu
  payload 细节。控制命令在 `/ask` 前走 `/control`，不进入普通单飞队列。
- `agent/gateway_parts/queue_service.py`：request/response/history/index 文件队列。
- `agent/gateway_parts/request_worker.py`：认领、执行和终态归档。归档请求的 `status` 以最终 response 为
  权威，不能让 processing lease 的旧状态覆盖 `done/interrupted/failed`；lease 只保留运行期计数与心跳。
- `agent/gateway_parts/lease_service.py`：processing lease 和 heartbeat。
- `agent/gateway_parts/adapter.py`：文件 adapter 到 gateway ask 的转换，直接调用 `request_worker`。
- `agent/gateway_parts/channel_health.py`：把 adapter daemon 的 PID、heartbeat 与逐通道 JSON 状态投影为
  registry health；状态缺失、损坏、进程死亡或心跳过期均 fail-closed，不读取日志正文。
- `agent/core.py`、`agent/owner_scoped_pool.py`、`agent/gateway_parts/request_worker.py`：adapter health 是
  基础 Gateway 进程的只读事实，由 composition root 显式传给 scoped owner；各 owner 仍持有独立 registry、
  凭据配置和 conversation binding，禁止从 owner 私有 gateway 目录重新推导共享进程是否运行。
- `agent/gateway_parts/recovery.py`：processing 恢复，直接读取 `lease_service` 判断 heartbeat。
- `agent/gateway_parts/http_handlers.py`：HTTP 入口；`POST /ask` 在普通 active-turn steer 或入队前
  调用唯一 typed slash dispatcher，剥离 `/audit` 命令词并拒绝未知 `/XXXX`；调用方传入的
  `system_task` 没有权限，只有入口生成的白名单载荷可进入请求。`/result/<request_id>` 的 USER 权限始终从请求记录
  读取 owner，排队/执行态查 pending/processing，完成态查 done/failed，禁止把 response 正文当身份源；
  `/progress/<request_id>?since=` 复用同一 owner 权限并只返回 thread 已启用的 typed progress；
  `POST /control` 是用户会话任务的即时控制入口，管理员 `POST /stop` 仍只停止 Gateway 服务。
- `agent/gateway_parts/control_service.py`：按可信 owner/channel/conversation 解析精确 live request。
  会话 `/stop` 先触发命名中断和 transport abort，再持久 cancel/task/subagent 清理；`/verbose`
  直接读写当前 thread 设置，二者都不调用模型或写 transcript。
- `agent/gateway_parts/request_worker.py`、`request_execution.py`：CLI 文件协议只接受已规范化 task
  command；worker 执行前再次拒绝任何 slash 命令，防止旧队列或旁路把系统命令送进模型。
- `agent/gateway_parts/response_renderer.py`：响应渲染、响应文件结构化读取、客户端轮询状态去重。
- `agent/delivery/registry.py`：channel adapter、懒工厂、capabilities、配置状态、运行健康、当前结构化
  binding 和 target validator 的唯一注册表；新增 IM 通过注册扩展，不修改投递服务或能力工具。
- `agent/delivery/service.py`：普通最终回复、后台主动消息和显式发送的统一出口；组合可信
  `DeliveryContext` 与无收件人的 `ReplyEnvelope`，净化正文后走原生 text/reply/image/file API，
  返回 `DeliveryReceipt` 并对相同失败做有界去重。
- `agent/user_space/owner_quota.py`：管理员显式非零磁盘上限的跨进程准入锁；`max_disk_mb=0` 表示不限制并
  直接退出热路径，不扫描 owner 全树。启用时在锁内按完整 multi-file mutation 的最终字节判断，
  policy/usage/lock 不可读时 fail-closed。
- `agent/user_space/home_retention.py`：只按 typed retention policy、terminal authority 和时间生成/执行
  plan；task/scratch 使用执行前状态复验、trash tombstone、legal hold 和 audit。
- `agent/user_space/owner_maintenance.py`：记录 owner 上次维护尝试/成功和结果；损坏 policy 不执行删除。
- `cli/gateway_loops.py::_GatewayOwnerMaintenanceController`：用 owner discovery cursor 有界轮询，不创建
  scoped Agent；全局扫描频率与每 owner policy 的实际维护间隔分离。
- `agent/conversation/channels.py`：通道 typed context/envelope/attachment 与统一 user-facing reply projection。
  内部运行协议在此从外部正文中移除；宿主绝对路径只在真实通道出口显示 basename，内部 transcript
  保留原路径供后续工作续接。
- `agent/agent_core/tool_loop/natural_user_reply.py`：派工与 wait 的辅助自然回复出口；不携带旧 tool
  context/native IR/runtime injection，只按 typed runtime status、结构化工具调用、空正文和内部协议
  做机器形态校验；不再用自然语言正则猜完成、ETA 或大小。普通任务最终回复不经过第二次验收或摘要
  重写，直接使用主模型自然正文。Gateway 对 `user_reply_unavailable + 空正文` fail-closed，不写空
  assistant 修复队列，也不把请求标成成功；通道运行时 的空 final/streamed text 回退与 长期助手 的
  commentary/final-delivery 分离只作为边界对照，my-agent 仍由同一个模型回复投影负责正文安全。
- `agent/conversation/runtime.py`：后台唤醒继续使用内部协议做运行裁决，但在写普通 assistant transcript
  和返回后台 report 前必须经过同一 user-facing projection；原始内部协议只交投递服务做抑制判定，
  不得进入 compact 或 owner-local 会话搜索。主/子代理续接只消费 typed child lifecycle wake、active-turn
  input 和真实工具进展，不再运行一套无人接线的全树进度指纹判官。后台根任务轮按 durable task id 注册
  协作中断，发送前抑制 cancelled/abandoned/superseded 任务的迟到正文。
- `agent/capability/channel_message_tool.py`：主代理唯一 `send_message` 工具。收件人由 scoped owner
  决定，附件必须通过 task registry、owner 边界、ready 状态与 hash 校验；执行前占位和结果重放由
  通用 tool operation 账本负责，不再维护消息工具自己的第二份回执。
- `agent/adapter/delivery.py`：交互消息提交后的持久化异步回送；pending/sent receipt 支持重启恢复，
  只轮询既有 request_id，不重新运行 Agent；同一 pending 记录保存 progress cursor，进度和最终答复均
  通过统一 DeliveryService 回送。commentary/工具进度失败时仍前移 cursor，防止重复刷屏或阻塞最终答复；
  最终答复继续由独立耐久 receipt 保证。
- `agent/conversation/compact.py`、`compact_guard.py`、`compact_checkpoint.py`、`history_index.py`、
  `control_commands.py`：分别承载 owner/thread 唯一自动 compact、结构化近期尾部与失败熔断、
  完整恢复点、owner-local 旧聊天检索投影，以及 CLI/IM 共用 typed slash/task command；
  `/context` 只读自动压缩同一估算，`/compact` 复用同一 run lane/checkpoint/CAS，`/effort` 只投影真实后端
  参数能力；`/verbose off|on|full` 的持久状态仍在唯一 thread schema 中，不从自然语言推断 owner 或 compact 成败。
- `agent/capability/channel_message_tool.py`：`send_message` 的模型可见性与执行前检共用 owner provider/target、
  owner root 和 registry proactive capability；无外部通道的本地 transcript 只隐藏当前快照，不删除唯一工具实现。
- `agent/conversation/authority.py`、`task_promotion.py`：普通 transcript 唯一权威标记，以及任务候选的
  结构化选择、已完成或已中断任务重开、误建占位任务 supersede、提升和完成关闭。
- `agent/gateway_parts/supervisor.py`：gateway supervisor 的启动、停止、重启、heartbeat 健康判断和
  runtime status 写入；不拆成 facade/operation 影子文件。
- 旧 `chunk_service.py` / `context_tokens.py` facade 已删除；请求正文压缩、上下文显示和响应渲染走当前 request execution / renderer 主链路。
- `cli/gateway_loops.py`：gateway request worker 池、后台主代理 tick、heartbeat loop、owner maintenance 与
  Scheduler due-owner 有界唤醒；各 controller 只负责编排，不保存业务事实源。
- `cli/gateway_process.py`、`cli/gateway_client.py`：
  启动、停止、状态和客户端命令；`gateway_process.py` 直接承载公开 gateway 命令实现，不再转发到 `_gateway_commands.py`。
  watch 返回必须分类为计划 stop、signal shutdown、有限轮完成或意外返回；SIGTERM/SIGINT 先落 typed
  stop request/forensics 再走同一 drain，意外返回非零退出，cleanup 另记 drain 结果。
- `cli/chat_parts/control_runtime.py`：终端 Gateway 模式调用同一 `/control`；本地直跑模式使用同一 typed
  command/状态渲染，并读取 plain/TUI worker 写入的锁保护 request id 控制本进程当前窗口；不得跨线程
  读取 thread-local `RunParams`。
- `cli/gateway_service.py`：systemd/launchd service unit 生成和安装/卸载入口；服务 cwd 固定为
  `<MY_AGENT_HOME>/service-cwd` 中性目录，不能继承安装命令所在源码 checkout，也不把该目录当 owner workspace；
  不再拆成私有 facade helper。
- `agent/gateway_parts/process_control.py`：进程存活、终止和等待退出的唯一进程控制模块。
  `daemon_control.py` 只处理 PID record、后台化、锁和 shutdown request，不再作为进程控制转口。
- `agent/tooling/process_registry.py`、`agent/tooling/process_sessions.py`、`agent/tooling/shell.py`：模型命令
  进程的独立生命周期权威。前台超时、
  用户中断、后台 kill 和日志上限都复用 registry 的完整后代树终止；POSIX 会快照后代及进程出生标识，覆盖
  bwrap `--new-session` 建出的嵌套 session。shell 只负责 2 秒有界 pipe drain，不能用无界
  `communicate()` 等待可能被孙进程继承的 stdout/stderr。后台启动把 host 注入的 owner/TUI session scope
  冻结进记录；模型只通过统一 `process_session` list/status/wait/network_status/stop 续接，不能用可猜 session id 跨用户读
  日志或停止，也不再用 shell sleep 轮询。子代理最终工具快照把它作为 `run_command` 的依赖闭包，覆盖动态
  capability grant 与旧任务恢复；owner 显式禁用列表仍做最后收窄。
- `agent/gateway_parts/scoped_locks.py`：机器级【进程单例】锁（pid+进程启动时间判归属，
  长期助手 风格）。用于"同一台机器同一 scope+identity 只有一个活进程持有"的网关身份独占；
  持有进程重复 acquire = 刷新心跳，死进程残留锁自动接管。`daemon_control.py` 是它的
  公共 API 转口。
- `agent/gateway_parts/daemon_metadata.py`：统一提供 process-domain（machine/hostname + PID namespace）、
  PID 和 start_time 身份。后台会话 claim 与 gateway PID record 共用，不复制一套存活判定。

## 路径

gateway 服务运行态写当前 owner 的固定 service runtime，不再按 TUI cwd 哈希：

```text
owner_home/workspace/runtime/services/gateway/
|-- requests/
|-- responses/
|-- history/
|-- workers/
|-- leases/
`-- index/
```

不同 TUI/CLI 的项目范围随每个 ask 的结构化 `workspace={cwd, roots}` 进入请求；Gateway 只允许本地 owner
设置存在的绝对目录，并把结果持久化到 `conversation_thread.v7`。后续前台、后台 main、工具与子代理从
同一 thread 字段恢复，不读取守护进程 cwd，也不从 prompt 猜目录。Gateway/adapter 的显式相对配置以
owner workspace 解析，防止另一个项目目录派生出第二套 pid、队列或监听端口。

流式响应 chunk 写入被认领请求所在的 `requests/processing/<request-id>.chunks.jsonl`；即使执行者是
per-owner Agent，也必须跟随基础 Gateway 的权威队列记录，不能改从 owner Agent root 推导。请求结束时
随 request 归档到 `requests/done/` 或 `requests/failed/`，最终 response 会记录 `chunk_stream_path`。
移动前必须把最终 response 的 `done/interrupted/failed` 写回 request JSON，确保目录、请求记录、
`/result` 与 `/status` 不会一边终态、一边仍显示 processing。
客户端补读 chunk 时按 processing -> done -> failed 的结构化候选路径查找，不靠日志文本猜测。

## 规则

- `scoped_locks.py` 只提供进程对进程互斥，【不提供】进程内线程互斥：同进程任意线程
  acquire 同一把锁都是持有者重入（刷新心跳），任意线程 release 都按进程维度删锁。
  单进程多线程的临界区（如 request worker 池内共享状态）禁止复用这把锁，应使用
  `threading.Lock`（参考 `agent/io/jsonl.py` 的"线程锁 + flock"双层模式；对照组
  长期助手 ProcessRegistry 同样将进程身份锁与线程互斥锁语义分离）。钉子：
  `tests/test_real_io_concurrency.py::test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`。
- request/response/history 损坏要显式报告 load_error，不能渲染成“没有记录”。
- USER 读取完成响应必须先用 pending/processing/done/failed 中的请求记录校验 owner；同 request id
  出现多份记录时必须全部归于同一 owner。归档缺失、损坏或任一身份不匹配时 fail-closed；只有具备
  all-user 权限的可信管理员可读取无请求归档的孤立 response。普通 USER 的完成 response 必须再经
  顶层字段白名单投影，新增内部字段默认不公开；损坏请求记录不得把 load report 路径返回给 USER。
- status/doctor 要能看到当前 processing request 的结构化租约事实，包括 request id、
  lease owner、attempts、lease/heartbeat/update age 和 chunk stream 路径；这些只用于观察，
  不作为调度或验收硬门。
- worker 秒退、参数错、import 错要立即标记失败状态，不能伪装成 processing/planning。
- 无限 gateway watch 没有 stop request 却返回时必须写 `GATEWAY_WATCH_UNEXPECTED_RETURN` 并返回非零；
- 后台主代理 claim 默认 TTL 90 秒，`background_claim_heartbeat_interval_seconds=0` 表示按 TTL 自动取安全
  间隔（默认 30 秒），不是 50ms 热写。同一进程域旧 owner 已死可立即接管；跨 Pod/旧 claim 无法证明时
  等待 TTL，保证 RWX 事实源上不会双执行。
  三个后台线程任一未在 drain deadline 内结束时写 `GATEWAY_DRAIN_INCOMPLETE`。计划停止与有限轮完成
  保持 exit 0，但 termination kind/reason 必须持久化。
- CLI `gateway ask` 和 HTTP `/ask` 都必须写 `conversation` 结构化字段；本地 CLI 默认使用
  `gateway-cli/default`，HTTP 使用请求体里的 `conversation_id` / `session_id` /
  `thread_id`，缺省为 `default`。Feishu 必须传真实 `chat_id`，话题再叠加 `thread/root`，不得退化成
  user id 或“该用户最近 thread”。
- ordinary channel input 始终走同一 thread：是否调用文件、派工或定时工具由模型决定，不预先根据
  文本分“聊天/任务”，也不接受外部 lane/task selector。`/audit`、`/goal` 只是同一 thread 上的显式
  overlay；多个命名项不建立第二份 transcript/compact。普通前台 turn 不猜多个 Goal 中谁是当前目标，
  后台续跑通过 exact `thread_goal_id + task_id` 只读取自己的目标。
- 普通 `task_progress` 是当前 workspace 的可选恢复笔记；open item 不拦截模型最终回复、不追加隐藏
  completion 提醒，也不安排后台 continuation。只有显式 `thread_goal_id` 的 `/goal` 使用 open-plan
  lifecycle、暂停恢复和 durable continuation。
- 持久提醒由 `agent/scheduler/` 的 owner job/run 事实源和 `schedule` action tool 管理。
  到期时以 typed wake metadata 回到创建时的同一 thread，不读取用户文本推断身份或会话；
  `wait` 只负责 active task 内让出，两者不共享第二份 transcript/compact。
- `SchedulerRepository` 仍是唯一公开 facade；内部按 job CRUD、run claim/lifecycle、store/quota 三个职责
  mixin 组合，避免一个超大类同时承担全部状态转换，但不会产生第二份 store 或替代入口。
- 普通 turn 不预建 task workspace；只有注册表 `promotes_task` 或结构化任务动作能惰性晋升。派出子代理
  本身不自动结束当前 turn；模型可继续协调，只有显式非阻塞 `wait` 或正常最终回复结束本轮。用户正文由
  LLM 自然表达，子代理执行和命令记录留在 TaskRun，不直接写普通 transcript。
- conversation context 只包含同 thread 已完成的 user/assistant raw tail 与该 thread 的 compact summary，
  并明确是历史参考；当前 `# User Task` 优先。固定 `conversation_history_max_turns` 只决定 compact 后
  优先保留多少近期 turn，不得在 compact 前截断累计历史。工具执行产生后台任务时用结构化 task link，
  不把旧 goal 拼进普通消息。
- assistant 历史正文不得保存或重放 `MAIN_AGENT/RUN/SUBAGENT` 内部协议；完成轮次的产物引用写入
  message metadata。后续“发我”使用 `Recent Artifact Refs.path` 调 `send_message`，不得重做旧任务。
- transcript 持久化对 user 消息 fail-closed；assistant 消息失败走持久 repair。conversation-backed
  run 禁止再自动写 owner-global dialogue memory，稳定偏好继续由 USER/preference authority 提供。
- 入站请求只从 exact request binding、未结束 Goal 或 active task 继承 cwd；thread 的
  `workspace_task_id` 本身只保留最近状态/导航投影。旧 v1/v2 thread 只在持续目标的精确 task id 或仅有一个
  合法 active 根任务时无歧义迁移；多候选时不猜。普通聊天不会改变 task lifecycle；新工作由首个工作工具
  懒建目录，精确旧项目写路径才允许回绑。`subagent-*` 和 `bg-main-*` 内部链接不能替代根工作目录。所有
  `promotes_task` 工具共享
  同一个执行冲突门；只有另一个真实 live executor 会阻止第二执行器，历史 task/open checklist 不会阻止
  当前消息开始工作。精确路径绑定会同步 run workspace；该决策不解析用户自然语言。
- 同一 `canonical_user_id + channel + channel_conversation_id` 同时最多执行一条前台 request；此外同一
  durable `thread_id` 的 foreground、scheduled progress、scheduled job 和 wake continuation 必须再共用
  `conversation/run_claim.py` 的执行 lane。Gateway 在 lane 内重新读取 compact/history/task state，避免
  排队期间形成旧快照；完成、失败、中断都必须成对释放并记录结构化终态。不同 thread 不共用此 lane。
- 子代理完成 wake 在消费前校验结构化 root task link；已 completed/superseded 的根只归档迟到信号，
  不再启动后台主代理或写普通会话。
- 成功完成 wake 可短暂按 thread 合并，但失败/阻塞必须立即处理；后台主代理的内部整合回复和用户通知是
  两个不同结果面。部分成功只更新内部任务事实，全部结束/异常/需决策才写普通 transcript 和外呼 IM。
- 每个后台整合轮在模型调用前只冻结一次子树阶段。只有采样时已存在的同 root DONE 通知可以随该轮合并
  确认；采样后新建的通知必须保留并触发下一轮。新鲜轮看到全部直接/递归下级终态后可直接发送模型自然
  回复，不能再等待 root task link 的 completed 状态作为机器验收。
- observation + wake 的生产顺序必须由 store 统一封装为 wake-first 发布；消费者不得依赖两个独立文件
  “通常会挨着写完”。内部 continuation policy 只有在 root 不再存在运行中子任务时才允许进入公开结果面。
- 后台主代理准入在 wake、observation、policy 和 claim 后最终边界都读取 RuntimeRepository 的同一份
  `main-agent-recovery-block.v1`。run/current attempt 为 `unknown` 或权威不可读时，来源保持 pending、模型
  与工具不启动，状态不变时不重复打日志；恢复后下一 tick 自然消费。observation 按
  `thread_id + root_task_id` 分批，blocked 项不计入每轮 runnable limit，避免旧任务堵住同线程新任务。
- 后台 task record 必须从 `ThreadTaskLink` 恢复权威 goal 与 workspace；任何 background prompt、wait reason
  或继承父 conversation task id 的子代理 prompt 都无权覆盖这两个持久字段。
- `run_command` 启动长期服务时只认结构化 `run_in_background=true`，并返回 process registry 的
  `session_id/pid/output_file`。命令文本里的独立 shell `&` 在 spawn 前拒绝为
  `BACKGROUND_PROCESS_MODE_REQUIRED/not_started`；这样 foreground shell 的退出或取消不会留下无账进程，
  模型也不能用同一命令内的一次 curl 探活冒充持续监听。
- `bind_task` 是 task identity 的唯一创建/补空入口：已有 goal/task_path/created_at 不可覆盖，跨 thread
  重绑 fail-closed；状态变化走显式 status/update 接口。调用者不再各自实现“记得保留旧字段”的软约定。
- 开启 per-user owner（发布默认）后，远程 channel 的 owner 解析/创建失败不得回退基础 agent；
  必须写 `OWNER_SCOPE_UNAVAILABLE` 失败响应并归档，避免重试期间或故障时串户。
- 私聊 owner 固定落 `owners/providers/<provider>/users/<user_id>`；群聊固定落
  `owners/providers/<provider>/groups/<chat_id>`。群聊必须同时有结构化 group chat type 和 chat id；
  缺少群 id 时不能凭 conversation id 猜一个 group owner。
- gateway 内部实现直接引用 owner 模块：ask 队列走 `request_worker`，lease/heartbeat 走
  `lease_service`，不保留单独的 `runtime.py` re-export 层。
- gateway ask 请求 ID 由 `new_gateway_request_id()` 生成；所有 CLI/chat/adapter 入口都应走
  `submit_gateway_ask()` 或同一生成器，不能用时间戳截断值自行拼 ID。ID 是队列、response、
  chunk stream 和审计记录的结构化关联键。
- gateway request worker 空闲轮询间隔由 `gateway_request_poll_interval` 控制，单位秒，可填小数；
  默认 `0.2`，配置小于 `0.05` 会回到默认值。
- 多 chat/gateway client 共享同一队列时，本地 IO 不应成为瓶颈；慢点应主要来自模型或外部服务。
- processing 目录只表示当前正在处理的 request；完成后的 request JSON 和 chunk stream 都必须进入
  done/failed 归档，便于多客户端观察和后续排障。
- 外部 channel 的目标结果使用 `conversation/channels.py` 的 typed decision 表达，provider validator
  由 `delivery/registry.py` 注册；投递服务不得把任意字符串交给 provider 后再依赖 HTTP 400 纠错。
  Feishu 当前使用 `receive_id_type=open_id`，因此主动外呼目标必须是 `ou_`。
- 外部附件发送不得接受模型指定的任意 channel/target，也不得只凭现存 path 发送；必须命中当前 owner
  的 artifact registry，发送前重新核对真实路径和 hash。公开 Gateway response 只返回文件名，不返回
  绝对路径；跨轮内部引用只存 owner transcript metadata。DeliveryService 从可信 request/operation
  身份为正文和每个附件生成稳定去重键；provider adapter 只能消费该键，模型参数和 ReplyEnvelope
  无权提供或覆盖。
- Feishu adapter 只有在请求已带官方 UUID 时才对 408/429/5xx 和传输超时做有界重试；同一正文、引用
  回复、分片或媒体消息的所有尝试必须复用同一个 UUID。4xx 明确错误或无 UUID 的请求不自动重试。
- adapter service 日志必须安装公共 log redaction factory/formatter；SDK 日志不因来自第三方模块而绕过
  secret 清理。投递失败日志禁止打印完整目标或消息正文。

## 2026-06-10 空闲 IO 与队列观测

- `GatewayInboxScanGate`（gateway_parts/request_worker.py）：inbox 目录 mtime 未变且上轮扫描为空时
  跳过 glob+逐文件读；带 2 秒粗粒度文件系统保护与 deferred（not_before_at）例外。每个 worker
  持有自己的门，空闲时单轮成本从全目录扫描降为一次 stat。
- worker-0 的 stale lease 恢复扫描改为按 `gateway_processing_timeout_seconds/3`（至少 2 秒）节流
  （cli/gateway_loops.py `_RecoverThrottle`），不再每个轮询周期全量扫 processing 目录。
- `conversation/runtime.py::_related_subagent_runs` 是后台完成合批、投递与 goal child-phase 的共用读取缝。
  managed 主链调用 `SubAgentManager.list_runs_for_root_report(root_task_id)`，由 LocalStore 根索引选择 exact
  run ids 后重读 canonical task；索引/load error 保守返回 unknown。只有没有 LocalStore 的显式
  local-unmanaged/fake manager 才使用全量 canonical fallback，Gateway 正常 tick 不得复制无关历史树。
- heartbeat 新增 `queue_ages`（gateway_parts/io.py `gateway_queue_ages`）：最老 pending 等待秒数、
  最老 processing lease 年龄，只读文件 mtime，仅用于观测展示，不参与调度或恢复决策。
- `agent/io/jsonl.py` 路径锁改为引用计数 + 容量水位回收，长驻 gateway 进程不再无限增长；
  Windows（无 fcntl）下线程锁仍是唯一互斥，引用计数保证不会出现双锁并行写。

## 2026-07-18 owner quota 扫描竞态边界

- `agent/user_space/owner_quota.py::owner_logical_usage_bytes` 是结构化 owner 写入口共用的逻辑用量事实。
  原子 JSON writer 与 SQLite 会在目录枚举后删除临时/WAL 文件；单 entry 随后 `stat` 返回
  `FileNotFoundError` 表示它当前已不占配额，应只跳过该 entry，不能让整个 owner 的 Scheduler/Memory/
  Persona/文件写入都误报 `OwnerQuotaUnavailable`。
- 仅 `FileNotFoundError` 可被当成并发消失：owner 根权限错误、非目录、目录遍历错误和单文件
  `PermissionError` 继续 fail-closed；regular file 只做一次 `stat(follow_symlinks=False)`，symlink 不计入也
  不跟随。锁内最终用量与整批变化的准入语义不变。
- 该分流对照 会话运行时 `会话运行时-rs/exec-server/src/local_file_system.rs::read_directory` 对枚举后 metadata 已失效
  entry 的跳过，以及 通道运行时 `src/security/installed-plugin-dirs.ts` 对 `ENOENT/ENOTDIR` 与其他读取错误
  的区分；没有按文件名、运行日志或自然语言猜“这是临时文件”。
- 回归分别制造枚举后原子删除、单文件权限失败和 owner 根权限失败，证明只放过真正不存在的 entry。
## 2026-08-21 cwd、控制根与 TUI 活动投影

- `ConversationThread` 的根 task link 是控制事实源；`/stop` 优先定位 `workspace_task_id` 对应的普通根任务，再由统一控制链递归取消其运行树，不把直属 child 当成新的用户任务。
- Tool Registry 的 cwd 来自服务启动时的项目根，或宿主显式注入的 `execution_cwd`；隐藏 task state 只保存账本与恢复材料，不能替代用户 cwd。
- `/client/notices` 的活动数量由 task link 的 `status=active` 投影，只负责展示，不参与调度、重试或恢复裁决。
## 2026-08-21 主会话 cwd 与写边界同源

- 本地/admin 普通会话进入持久任务后，`ToolRegistry.workspace_root` 同时写入可信 `execution_cwd` 并追加到
  `allowed_write_roots`；隐藏 task `work/output` 仍用于账本、临时工作和显式内部交付。
- 该扩展只发生在 default-scope 的主 conversation run。`cli_run`、task-local child 与 transient Audit
  不继承；远程 provider 的 owner/task wall 在后续步骤覆盖项目写根，继续 fail-closed。
## 2026-08-22 会话入口与恢复控制面

- `cli/parser.py` 把裸启动与显式 `chat` 都路由到轻量 fresh-session 入口；`resume` 必须携带唯一 session
  ID。`cli/gateway_client.py::cmd_default` 仅保留完整 parser 的兼容入口，不读取全局 task board。
- `cli/bootstrap.py::default_config_path` 是 CLI 与 service 共用的进程级配置默认入口：优先
  `MY_AGENT_CONFIG`，否则回到随包 YAML；显式 `--config` 做本次命令覆盖。
- `agent/startup_recovery.py` 虽保留历史文件名，但职责已冻结为 `status` 的只读投影；它不能改任务、attempt
  或队列。普通 attempt 启动恢复位于 `cli/gateway_process.py::_cmd_gateway_run_setup`，subagent 失联恢复位于
  `cli/gateway_loops.py::_GatewayOrphanReconciler`，两者都由同一 Gateway 持有。
- `agent_core/orchestration/dispatch/lock.py` 的权威是打开文件描述符上的 OS advisory lock。持有者 JSON
  只用于观测，不能授权抢占、拒绝恢复或判断进程生死；释放时禁止 unlink 共享 inode。

## Agent View And Control Service

`conversation/agent_control.py` 是 TUI、Gateway 与未来 Web 查看/控制代理树的唯一通道中立入口。
`view` 先用 owner、conversation root 与 canonical ancestry 证明 exact run 属于当前树，再返回
`conversation_agent_view.v1`；结束 attempt 不影响历史可见性。`guidance` 和 `stop` 在相同只读证明之后继续
执行 mutation gate，必须命中当前 active binding，不能凭历史关系修改已经结束或被替换的 run。

guidance 在入 canonical ledger 前还必须解析 exact current AgentAttempt：managed 模式只接受 RuntimeDB
中状态为 `pending/running` 的 attempt，task 文件里的 `runner_active_attempt_id` 只做冲突栅栏；
local-unmanaged 只接受已有的 task-local active pointer。解析成功后同一 attempt id 写入
`metadata.expected_turn_id`，供 runtime reserve、provider submission 和 consume 三段复用。拿不到 exact
attempt 时服务明确拒绝且不落消息，不能让下一代 attempt 继承无归属 guidance，也不能放宽 store 校验。

HTTP handler 只负责可信来源、`GatewayControlScope`、字段类型和错误状态映射。guidance 的稳定 operation id
进入 canonical guidance ledger，重试不重复插入。HTTP 成功只报 `queued/pending`，provider 消费由
child 的 `active_turn_input_consumed` 展示事件另行确认；stop 调用现有 `cancel_subagent_task`。客户端返回父视图只是
本地 navigation stack 变化，不会调用 stop；`Esc` 才向当前 exact run 发停止。终态 resume 不属于这组三个
入口，后续若实现必须另设显式 typed 合同。

## 模型可见的本机 Gateway 诊断面

`agent/tooling/gateway_status.py` 是本机管理员主代理唯一的模型可见 Gateway 诊断工具。它不接受路径、PID 或
端口参数，而从当前 Agent 的 canonical Gateway paths 读取 validated PID、state、heartbeat 与 hot queue。
`main_agent` 之外的 owner registry 不注册该工具，因此多用户请求不能读取宿主 PID、配置路径或全局日志。

`gateway_parts/status_rendering.py::gateway_runtime_snapshot` 是 CLI/工具可共同复用的结构化投影。Gateway 每次
启动把实际 config/model/bind/port 与 `log_start_offset_bytes` 写进 state；日志诊断只扫描该偏移后的有界尾部，
只返回计数，不返回日志正文。`no_new_log_bytes` 与 `quiet` 必须分开：前者表示没有文件观察证据，后者表示已
观察到本生命周期日志且没有异常签名。以上均为只读观测，不能反向改变任务、请求或进程终态。

## 本机多用户 thin TUI 的 owner 路由

所有 thin TUI 都连接基础 `local/main` 的同一个 Gateway 服务目录和 8420 listener，但请求身份不因此合并。
客户端把配置中的 `OwnerIdentity` 投影为结构化 `channel/user_id/chat_id`；Gateway 仅把固定
`user_id=local-agent` 的 `local/chat/cli/http` 历史入口留在 base Agent。显式
`channel=local,user_id!=local-agent` 必须进入 `OwnerScopedAgentPool`，群组使用 chat id，个人使用 user id。

scoped Agent 的 effective owner home 是 tasks、Memory、Persona、sessions、subagents、LocalStore 与审计的
共同根。请求正文中的 cwd、模型文字和 TUI 启动目录都不能覆盖它。客户端 probe 的仍是 base Gateway paths；
请求执行的则是 scoped Agent，这两个事实必须同时成立。

子代理后台 autostart 只有 exact `local/main` 使用基础 config 的 durable subprocess。任何其它 owner 都在当前
Gateway 内使用已绑定 owner 的 daemon dispatcher；原因是旧 subprocess 协议没有序列化请求级 owner，直接
复用会静默退回 `local/main`。daemon 只持有运行执行体，canonical task/attempt/lease/recovery 继续落该 owner
磁盘，Gateway 重启由统一恢复链接管。该规则按结构化 provider/kind/id 判定，不按 provider 名称粗分。

## Active-turn restart recovery boundary

`gateway_parts/recovery.py` 只负责证明旧 processing lease 的执行者已失效，并把同一 request id 写回 pending；
`request_execution.py` 只在该请求携带匹配的 `gateway_active_turn_recovery.v1` 时读取 owner-local tool index。
这两个事实都不能单独释放 RuntimeDB unknown。

最终恢复权威在 `RuntimeRepository.recover_recorded_active_turn_attempt`：调用方必须给出 request 文件中持久化的
exact task id、同值 run/request id，以及 carried archive 中的 operation id/status/tool 投影。仓储在一个 SQLite
事务里重新读取 root main/current attempt、tool operations 和 resource mutations；只有所有已启动工具均有
确定终态及匹配记录、资源稳定时，才执行 unknown→recovered、run unknown→created、exact exec lock release。
generic `create_attempt` 和 `recover_attempt_unknown` 不感知 transport marker，继续保持普通 unknown 人工恢复。

模型输出、异常字符串、PID 年龄、目录内容和“看起来已完成”都不是恢复证据。恢复后新 generation 仍走普通
`_bind_main_agent_authority`；旧操作不会被 RuntimeDB 重开，模型只从 carried records 获得已做事实。任何
EXECUTING/UNKNOWN、已启动但未 settle、缺 archive、operation 字段冲突或 DIRTY/MUTATING 资源都返回结构化
blocked，由 Gateway 停止该 request，避免重复副作用。
