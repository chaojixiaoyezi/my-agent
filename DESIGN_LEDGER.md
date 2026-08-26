# DESIGN LEDGER

当前设计铁律：

- 产品能力状态只认 `docs/PRODUCT_FACTS.md`；代码存在、测试存在、历史计划打勾都不能自动升级为稳定能力。
- 先跑通一条主链路，再谈扩展。
- 真机运行与测试统一只允许一个真实 Gateway。并行、极限和长任务测试必须让多个 TUI/会话连接同一
  Gateway，以真实暴露队列、公平性、活动回合、恢复和资源争用问题；禁止用“每个用例一个 Gateway”
  规避共享底座问题。需要验证重启、崩溃或断连时，只能顺序操纵这一个实例；进程内 fake/合同单测不属于
  额外真实 Gateway。
- 单 Gateway 的后台执行最小车道是 `owner + durable thread_id`，不是 owner。同 thread 的前台、
  子代理唤醒、policy 和 scheduled continuation 继续共用唯一持久 run claim 单飞；不同 thread
  必须能有界并发，不得因同一用户的另一个长后台回合而饿死。Gateway 只做无模型的
  ready-thread 规划，全局池与 per-owner 上限均由显式配置约束，持久来源与 claim 仍是调度权威。
- 自然语言负责沟通，结构化事实负责决策；prompt、summary、报告正文、guidance 和角色描述不能直接改变运行时状态、权限、验收或派工。
- 安全门可以硬，业务质量门默认软；危险路径、危险命令、越权和客观产物错误可以硬拦，任务深度、覆盖充分性和报告质量进入 warning、返工提示或 closeout。
- 模型的验证结论必须停留在亲自观察到的证据边界：本机、替代环境、模拟器、局部入口或单一身份成功，
  只能证明该范围，不能外推到另一机器、真实用户、外部网络/服务、其他身份或完整端到端。当前工具、权限
  或环境无法观察用户要求的边界时，模型必须明确写未验证、已验证范围和剩余复核步骤；本机监听、绑定
  `0.0.0.0` 或 localhost 成功不等于另一机器可达。用户只要求检查、诊断、解释、对比或汇报时，模型只做
  只读核对，不自行写文件、改配置、启停服务或派工。该纪律正文只在 `model_guidance.py` 维护一份：真实
  HTTP backend 通过供应商高优先级通道发送全量边界（OpenAI-compatible 为首条 `role=system`，
  Anthropic-compatible 为顶层 `system`），原始 user 任务和 native assistant/tool 历史仍保持原顺序；原生
  工具投影再按 canonical `ToolRuntimePolicy` 只给可能产生副作用的工具追加同一授权段，让模型在选动作时
  再次看到。不支持独立 system 通道的旧 fake/第三方 backend 不假装接收，上下文统计也只计算真实发送内容。
  两处都只是 会话运行时/终端交互 式软行为指导，不解析用户意图、不禁用工具，也不得恢复任务专项规则或宿主
  机器语义验收。
- 工具 effect 仍只有一个 `EffectResolverPolicy` 事实源：command parser 给出的基础等级可与
  结构化参数 mapping 叠加，所有授权、并发、验证和统计消费者统一取最高风险，
  不能各自猜。`run_command(run_in_background=true)` 是持久进程的唯一入口，显式提升为
  `dangerous`。sandbox 是否真包住副作用只读同一 `SandboxPolicy.uncontained_by_parameter`；
  `run_in_background=true` 和 `terminal_session.action=start` 均不被当前 bwrap 包住，因为它共享主机
  网络，进程也越过单次 handler 存活。审批只认现有 exact binding，不从命令字符串或
  用户自然语言推断意图。PTY `write/read/close` 只运输/关闭已经审批创建的 session，对齐
  会话运行时 `write_stdin` 不再触发第二次命令审批；一次性前台命令和已有灾难命令硬拒绝保持原语义。
- 普通可恢复工具错误按 会话运行时 的 `RespondToModel` 语义回到当前模型继续修正：同工具同类失败达到
  提示阈值只能注入换参数、换工具或拆步骤的强返工提示，不能按次数结束 turn。精确同参机械重试可由
  action guardrail 拒绝该次动作，但不得升级成任务终态；跨轮 streak/episode 机器裁判不进入默认主链。
  只有取消、明确安全边界、副作用真实 unknown 或部署者显式启用的 typed hard policy 可以硬收口。
  同一 provider batch 内后到的同工具成功是更新的结构化事实，必须撤销本批更早失败留下的 active halt。
- 修当前链路，不为历史目录、历史字段或历史工具形态加旁路。
- 一个概念只保留一个权威位置：task workspace、artifact registry、subagent canonical state、compact ledger 和 config 都不能多头并存。
- 远程 owner 的 prompt 工作区、文件写边界与 shell 挂载必须来自同一结构化 workspace scope；公共读取区
  不能因模型给出绝对路径而升级为可写目录。宿主当前用户 home 的危险根豁免只属于无 owner scope
  的本地管理员；远程 owner 即使运行在 root systemd 下也必须保留 `/root` 等宿主 home 拒绝边界，
  仅由更窄的 owner home 白名单放行自己的数据。task/run/request 机器 ID 只做身份，不做目录标题。
- context window 先读 provider metadata 的显式容量，存在即完全覆盖本地配置；provider 未提供才使用
  `model_context_window_tokens`，compact 阈值和其余状态机不因容量来源变化而分叉。
- provider 原生多轮历史必须保留该协议要求的完整有序 assistant content blocks；Anthropic 兼容链中的
  thinking/signature、text 与 tool_use 作为内部 typed history 一起续入下一轮，用户可见正文仍只取 text。
  只允许白名单字段进入 provider replay，工具 id/name/input 仍以 canonical ToolCall 为权威；compact 或
  其他结构化裁剪一旦改变签名覆盖的内容，就必须丢弃对应 provider blocks 并回落到无签名的规范历史，
  不能伪造、拼接或向用户泄露 reasoning。
- 普通回合结束时允许做一次 会话运行时/终端交互 式工具终态折叠，但它不是 Compact：宿主只从该回合
  canonical tool archive 生成有界、脱敏、带计数和 refs 的 `conversation_terminal_tool_fold.v1`，与 assistant
  消息同一次落入唯一 ConversationStore。后续轮只读这份不可变折叠，不重新总结、不按当前窗口改写旧行；
  因而既保留“做过什么、哪里核验、不要重放副作用”的续做事实，也让历史保持 append-only 稳定前缀以利用
  provider cache。完整原始输出继续只在 owner archive，最近折叠保留有限明细，不能伪造已删除的
  ToolCall/ToolResult 或 thinking signature。真正 Conversation Compact 才把折叠纳入摘要、推进
  `compact_generation` 与 transcript source-message 计数；`compact_source_tool_pairs` 继续只表示运行中 native IR
  真压缩掉的完整工具对，终态折叠的回合/调用数必须独立展示，不能冒充 Compact 或重复累计。
- 同一 request/run 在 provider overflow 后可能先收口失败响应、提交 Conversation Compact，再继续调用模型；
  `ModelCallLedger` 对这个 scope 给出累计快照，持久 `ConversationModelUsageStore` 必须按
  `physical_model_attempt_count` 游标原子换算成 append-only 增量。每个增量事件保存原累计快照指纹，精确重放
  幂等、同 id 异值 fail closed；input/output/cache-read/cache-write 与 provider/estimated 分区均只累计新增值。
  禁止把第二份累计快照整条相加，也禁止复用 scope-only event id 让一次正常 Compact 续跑变成数据冲突。
- Compact 完成后的界面同步沿用 会话运行时 的 typed compact/token event 与 终端交互 的即时 post-compact state
  replacement：手动控制回执携带 canonical `task_status.compact_generation`，TUI 只据此推进次数。压缩前最后
  一份 provider-visible Context 快照立即失效并撤下，固定行保留真实代数和“下次模型调用刷新”；不得继续
  展示旧 token，也不得为了刷新显示发一次辅助模型请求。未 Compact 的历史只在尾部追加 immutable fold，
  不按新预算重写旧轮；真正 Compact 才允许替换历史前缀。cache-read/cache-write 只读 provider 账本。
  idle/resume activity snapshot 仍是代数水合事实，不能因为没有 Working block 就丢弃；同一 runtime 内
  `compact_generation` 单调不减，避免手动回执之后到达的旧轮询帧把次数改回 0。
- 供应商额度耗尽、明确不可用或健康探测失败时，正式运行面立即切到已配置且探活成功的本地模型，禁止
  为等待刷新而让任务空转；本地首选端口和供应商刷新时刻来自显式配置，不从错误正文或自然语言猜测。
  只有当前没有 live request、到达配置刷新点且供应商探活成功时才安全切回，模型切换不得创建第二个
  Gateway、测试服务或平行会话。
- provider 连接失败按 会话运行时 `TransportError::Network -> retryable Stream/ConnectionFailed` 适配：异常链中
  typed `socket.gaierror` 先复用 2/5/15 秒三次物理退避，耗尽后保留 transient 语义进入既有模型轮恢复。
  这只保护 DNS resolver 短暂失效，不从错误正文猜网络状态，不放宽畸形 URL、认证/额度或代理配置错误，
  也不增加无限 runner 重试。
- 主代理长期记忆归 owner home；子代理只保留任务周期内可审计状态。
- 子代理可以写协作产物，但最终交付由主代理汇总和验收。
- 工具面要少，优先增强现有工具和运行时语义。
- 递归代理只保留一条“用户—当前代理”关系：每层父级可创建直属 child、向其当前回合插入消息、打断
  直属 child，并裁决直属 child 的能力申请。创建后由宿主自动启动，生命周期事件自动回到直属父级；
  模型不拥有查询、等待、巡场、手动推进或机器验收下级的工具。同批 child 之间不互相广播完整 goal，
  孙代理也只与自己的直接父级交换有界状态和 refs。
- 生命周期收件箱按接收者隔离，不按共同 root 血缘共享。根 child 的 completion/capability wake 只能由
  exact 主代理会话轮读取和确认；`task_local` child、孙代理、控制面和辅助模型轮即使携带同一个
  `root_task_id`，也不得枚举或确认根主代理收件箱。对子代理的用户插话继续走 exact `agent_run` guidance
  收件箱，两条链不能互相兜底或串读。该边界适配 会话运行时 的 child completion 直投 parent thread/session
  mailbox，而不是把 root lineage 当广播地址。
- child 的 canonical task、独立 `agent_thread_id` 与 runner 是唯一续跑权威。task-local finalize 只保存
  本 child 的断点，不得把它登记成 root `BackgroundMainAgentRuntime` 的普通任务；后台来源若精确解析到
  child task，必须在模型调用前关闭并交回 child runner。共享 batch 进程只承载多个 runner，不拥有各 run
  的启动租约；单个 run 返回 `PENDING` 后立即回收其 task-local launch record，再从同一 run 续派。
- child 创建只登记一个 `pending` generation 1 AgentAttempt，不得把 Gateway/创建请求进程冒充 runner。
  真实 dispatcher 接手时必须在同一事务把该 exact attempt 激活为 `running`、写入真实 runner identity 并
  取得执行权锁；current attempt 仍在运行时再次启动必须结构化拒绝，不能静默换成 generation 2。只有前一
  attempt 已被 typed completion/abandon/recovery 收口，后续调度才可创建新 generation。该边界对照 会话运行时
  `PendingInit -> TurnStarted -> TurnComplete/TurnAborted/Error`，同时保留本项目 runtime.db 审计链。
- 一个可续跑模型/工具片段结束时，宿主先将 exact current attempt 收口为 `done`，AgentRun
  仍保持 `created`；随后 runner result 只可回写与同一 `turn_end_reason` 严格对应的
  `PENDING/BLOCKED`。该提交桥只对 exact current generation 开放；`DONE/FAILED/CANCELLED`
  仍必须有匹配的 run 级结构化终态，模型正文、parsed output 和展示短句都没有该权限。
- 子代理交付合同只来自用户/父代理显式 `output_files` / `output_refs` / artifact refs。没有声明时不生成
  内部 Markdown 槽冒充业务产物；直属完成事件以 typed status、child 最终回复和系统 `final_report_ref`
  回到父级。旧 durable task 的 `system_default_output_ref=true` 继续可迁移读取，但在所有模型可见合同与
  expected outputs 投影中隐藏，不能影响新执行。
- 会话运行时 把 child `TurnComplete` 的最后一条 assistant message 作为 inter-agent message 留在父线程；本项目
  对应的唯一持久事实是同一 ConversationStore observation 中的 `subagent-completion.v1`。后台 lifecycle
  wake 和普通 TUI 后续轮必须消费同一信封。普通追加轮会获得新的 task id，但复用原 canonical task
  workspace；因此先按同 thread、非 detached task link 的 exact `task_path` 等值找出 workspace lineage，
  再要求 event 的 `root_task_id` 属于该 lineage 且 `parent_agent_id == root_task_id`，只选择直属 child。
  去重后有界注入 `completion_message/final_report_ref/declared_output_refs/artifact_refs`；不得把
  `runner_result_json/output_json` 或内部状态路径带入模型，也不得从用户正文猜父子关系。named Audit prepare
  与其它 root/sibling 继续隔离。这个投影只提供整合输入，不改变 child/root 的完成、权限或验收状态。
- 模型可见的 completion 详情 ref 必须与工具读取边界一致：要么解析到按 exact
  `root_task_id/parent_agent_id/run_id` 授权的只读公开投影，要么不向模型宣称它可以读取。禁止把内部
  agent 状态路径作为可操作 ref 暴露后再由 `Read` 拒绝，也禁止为绕过拒绝而开放整个 runner 状态目录。
  `completion_message` 仍是有界主链输入，公开详情只补深挖能力，不复制生命周期或完成事实源。
- 会话运行时 式 cwd 与运行台账严格分离：Gateway/会话及其所有后代的普通相对路径统一从用户启动时的项目
  cwd 解析；隐藏 task root 只保存状态，只有显式 `work/...`、`output/...` 才进入内部任务命名空间。
  `allowed_write_roots` 只决定能否写，不能反向选择 cwd；只有宿主写入的 `execution_cwd` 可覆盖工具
  Registry 的项目根。本地/admin 主会话晋升为持久任务后仍必须保留同一个项目 cwd 写权，不能只剩隐藏
  work/output；远程 owner task wall、task-local child 和 transient Audit 仍可按结构化身份继续收窄。
- interrupt 是父子边上的显式控制事实，自动重试资格不能否决父级的打断。会话的
  `active_task_ids` 是可恢复候选索引，不等于正在运行数量；TUI Working 只统计 task-link
  `status=active`，而 `/stop` 选择当前 thread 的 typed root 后递归停止运行域。
- 当前请求的副作用事实只从 canonical tool archive 与 operation store 投影：
  `AgentRunResult.operation_verification` 保存内部逐操作终态，公开 Gateway/HTTP/transcript metadata
  只保存不含 call/operation ID、路径和参数值的有界分组。每条 assistant transcript 都保留该投影，
  包括 `operation_count=0`；compact 和历史索引直接消费 metadata，不解析中文核验尾注或模型正文。
  compact 不能把 LLM 摘要变成操作事实：`ConversationThread.compact_operation_evidence` 与摘要、cursor
  同一次原子推进，后续轮把有界程序证据放在模型摘要之后；二者冲突时只认程序证据。旧 transcript
  缺少 metadata 时 coverage 必须标为 partial，不能补猜。
  存在副作用调用时，最终正文后可附程序生成的简短核验块；普通零操作聊天不追加固定文案。
  这能证明模型实际做了什么或没有做什么，但在禁止自然语言语义判断时，程序不能理解并删除自由正文中
  的每一句错误自述，因此模型正文仍不是执行权威。
- 模型 HTTP 传输对显式 loopback 主机强制直连，避免桌面系统代理截走本地模型/Gateway fallback；
  外部供应商仍沿 urllib 的既有代理配置。该判断只读 URL host/IP，不按模型名或配置正文分支。
- 工具参数只有一份权威结构：`ToolModelSpec.input_schema` 保存完整 JSON Schema；旧
  `parameters/parameter_schema/required_parameters` 声明和 `tool_spec_schema` compiler 已删除。
  模型可见定义、文本/native adapter、参数恢复门、限流/重复保护哈希和最终执行必须消费它，
  backend、MCP 或 handler 不得再维护拍平的 required/type 副本。外层 typed tool-call envelope 必须先
  与工具参数分离；进入扁平执行 payload 后，除 `tool` 和未被 Schema 声明的真实协议元数据外都属于
  工具输入，`kind/run_id/status/metadata/artifact_refs` 等同名正式参数不能被误删或绕过校验。
  执行入口只允许 Schema 明确且无歧义的字符串→整数/数字/布尔/null/JSON container 类型纠正，
  不猜字段、不把标量包成数组。缺失字段只允许在同一入口按 `ToolRuntimePolicy.input_policy` 的逐字段
  明示安全默认值，或按 `trusted_parameter_bindings` 从 Registry 构造的 `run_scope/write_boundary/registry`
  结构化事实补入；Schema `default` 注解本身没有执行权，模型显式给出的字段永不被覆盖，未声明的必填字段
  继续失败。每个有效输入字段只记录不含原值的 `source/source_ref`，可信引用和精确动作条件在 Schema
  展示前 fail-closed；随后在任何路径、effect、审批或工具实现前完整校验
  required/type/enum/const/嵌套对象与数组/额外字段/长度和数值边界/本地 ref/组合规则。handler 继续负责
  文件是否存在、跨字段关系等业务事实。纠正审计只记 JSON 路径和前后类型，错误只回传约束与路径，
  不记录原始参数值。MCP 未支持或畸形的 assertion 必须在注册时跳过该工具并明确告警，禁止降级成宽松透传。
- 工具运行时统一切片已完成代码迁移、旧路删除、全量/真实模型验证和发布制品检查；功能规格为
  `docs/design/FEATURE-20260804-tool-runtime-unification.md`，完整证据、架构和迁移删除表为
  `docs/design/tool-runtime-unification.md`。目标不是在现有入口外加 facade，而是把
  `ToolRuntimeSnapshot` 前后的重复工具定义/ToolCall/ToolResult/审批/执行/完成入口已经收敛成
  `required_actions -> ToolRuntime -> ToolChoice -> provider adapter -> canonical ToolCall -> ActionPolicy
  -> ToolExecutor -> operation -> canonical ToolResult -> settlement -> CompletionGate`。native 正文协议块
  只可成为结构化违规与一次纠偏证据，不能提升执行；required action 只证明用户明确要求的现实动作
  是否有真实执行证据，不恢复目录扫描、普通任务业务质量硬门或第二份任务状态。协议只由显式
  `tool_protocol` 选择：native 必须通过当前 provider+endpoint+model+stream 能力探测，text 只走隔离
  adapter；旧模型名子串覆盖、运行中 fallback、不完整块执行和正文提升均已删除。
- 文件大小不是硬门；是否合并或拆分看调用链是否清楚。
- 大输出、compact、resume 必须靠 chunk、cursor、coverage ledger、archive 和 resume summary，不靠提示词提醒模型“别忘”。
- 参考成熟项目先于自己发明：会话运行时 是会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导的第一底座参考；长期助手 只补长期 Memory、Persona、多用户持久调度与被动验证，通道运行时 只补 IM adapter、通道健康与投递边界。适配现有 owner/thread/task 事实源，不另造平行主链。
- 多用户命令隔离是执行节点启动硬门：所有 owner-scoped 前后台 shell 必须经 bwrap；缺失或自检失败返回结构化 `SANDBOX_UNAVAILABLE`，禁止降级宿主执行，也不走用户可见审批。
- 子代理的 `allowed_write_roots` 必须覆盖所有能启动进程的正式工具：文件写入、`run_command`、PTY 和 LSP 共用同一结构化写边界。bwrap 中 owner home 作为只读基座，仅把本轮精确授权根叠加为可写；不得解析 shell 文本、重定向或自然语言猜测写路径。PTY session 与 LSP server 还必须绑定创建时的 owner/task 写域，禁止按可猜 session/server id 跨域复用。
- 默认安装进入透明容器 CLI：用户仍调用 `my-agent`，包装器只挂当前工作区和 `~/.my-agent`；宿主 venv 仅为显式 `--host` 开发模式。企业 worker 在启动和 K8s readiness 重跑同一 sandbox 自检。
- 发布干净度分两层：工作树门检查 tracked 脏文件和未忽略 untracked 文件；制品门直接检查 wheel/zip/tar 内容、运行状态目录和大小预算。`.gitignore` 不是发布安全事实。
- 普通通道对话以 `owner + channel + chat/topic` 的持久 transcript 为唯一多轮事实源；旧 dialogue memory 不得重复注入或挤占稳定偏好。thread 另外持久保存一个精确 `workspace_task_id`，作用等同 会话运行时 `SessionConfiguration` 中跨 turn 继承的 cwd；它只选择当前工作目录，不等于当前轮正在执行任务，也不从自然语言推断。
- 公开 `my-agent run` 虽然不可追问，也必须把精确 user turn 在模型/工具执行前写入同一个 owner
  `ConversationStore`，最终公开 assistant 投影再按同一 request 幂等追加；audit 仍只是经历档案，不能
  成为 Memory 的第二消息权威。该 one-shot thread 只提供 transcript 与可核验 message ref，不预填
  `conversation_task_id`，因此不会伪造会话任务 link；CLI 仍按独立 task/workspace 创建并收口。输入落账
  失败在执行前 fail-closed，assistant 尾部落账失败只通过 typed degradation 暴露。此顺序对照 会话运行时
  `run_hooks_and_record_inputs -> record_user_prompt_and_emit_turn_item -> run_turn` 的持久 user item 主链，
  同时保留本项目 owner scope、Memory evidence 与 standalone lifecycle。
- `cli_run` 的“无伪任务关联”不是无条件要求 `task_links=[]`：adapter 不得预填或主动 bind；若本轮没有
  task-promoting tool，links 必须为空。若模型真实执行会晋升任务的工具，既有生命周期可以建立 link，
  但 one-shot 收口后只能留下 `completed` 历史 link，`active_task_ids/active_task_links` 必须为空。
- 普通会话只有一个持久 transcript 和一个 sticky `workspace_task_id`；每条用户消息都是新的 active turn，当前消息决定本轮聊天或工作。`/stop` 只中断眼前真实运行的 turn，历史、compact、memory、persona 和 cwd 都保留；没有 live turn 时不得借旧 task/goal 改状态。普通 `task_progress` 只提供 `read/update` 的可选恢复笔记，open item 不拦最终回复、不自动续跑、不要求用户选择、关闭或重开任务。若 sticky workspace 上一执行已终态，首个 `promotes_task` 工具会在同一 cwd 建立当前 request 的新执行 id 并记录 `continued_from_task_id`，旧终态不变；旧 link 只贡献 cwd，successor 的结构化 goal 必须取本轮精确用户输入，禁止复制旧执行目标；本轮输入缺失时 fail-closed，不创建 successor。后台续轮继续读取完整 thread summary/raw tail，但 task link、observation 与 progress 等运行投影只允许当前 task 及其持久 child lineage，不能把同会话旧项目重新暴露成任务菜单。结构化绝对写入路径可在统一工具入口无歧义绑定同 thread 的既有目录。只有用户显式创建的 `/goal` 才拥有可暂停、恢复和后台续跑的长期生命周期。该边界直接对照 会话运行时 的持久 thread/cwd + 单个 active turn，并采用 长期助手 的 session-local todo 仅作模型工作笔记；IM 只传结构化 owner/conversation/message 身份，不产生第二套语义。
- 原生子代理创建入口必须有机器可校验的目标，但不重复表达同一事实：单派使用非空
  `create_subagents.goal`；批量使用非空 `items`，且每项自己的非空 `goal` 是对应 child 的完整工作边界，
  顶层 `goal` 只作可选批次说明。两种形态都没有、空批次或任一 item 缺目标时，必须在落任何 run 前返回
  typed 可恢复参数错误，不能从普通自然语言猜目标。该边界适配 会话运行时 v1 可选 schema + handler one-of
  校验；不为兼容恢复第二份批量目标权威。
- 并行编码派工采用 会话运行时 `multi_agents_spec.rs` 的 disjoint write set 软纪律：模型必须在每项 goal 写明
  同一个目标目录和互不重叠的文件/模块边界，职责宽到会覆盖兄弟项、或两项会修改同一文件/模块时应改为
  分批。goal 规则只进入唯一工具 description/schema 参数提示；`output_files` 仍是可选交付与冲突线索，
  不是完整写集、权限、锁或宿主完成裁决。只有调用者主动提供的同批结构化路径完全相同或互为祖先/子路径
  时，创建前原子退回让模型缩窄或分批；省略时宿主不猜，运行时也不得解析 goal 或扫描 diff 自动裁决。
- gateway 请求进入终态归档时，response 的 `done/interrupted/failed` 是最终状态权威；processing lease 只提供 owner/attempt/heartbeat 等运行字段，不能覆盖终态。归档目录、请求 JSON、response 与 `/status` 必须表达同一事实。
- 恢复任务后，新的 request/run id 只表示这次执行尝试，不得成为新的任务事实源。模型可见的 main context bundle 摘要不裸露这些本轮运行 id；完整值留在 JSON 事实源，只有结构化选择既有任务后才显示 `selected_conversation_task_id`。default 主代理的 guidance、task_progress 工具、需求/派工 seed、coverage、wait、监督提醒、workspace 懒建和 delivery closeout 必须统一读取结构化 `conversation_task_id`；task_local 子代理仍按自己的 run id 隔离。该解析只保留一个共享实现，禁止各模块复制一套优先级。`task_progress` 的显式 read 若收到的正是当前 typed task id，必须把它归一成当前 task-path 账本；只有不同的 exact id 才能读取其它历史 run。conversation task link 是生命周期权威，`work/state.json` 是同一 task path 的 owner-local 投影；完成、停止、取消等结构化状态迁移必须同步投影，且目标与解析后的状态文件都必须位于当前 `owner_home/tasks/` 的精确 task 根内。路径越界、符号链接、身份不一致或文件损坏时只告警、不得覆盖别的任务目录。
- 租户可见路径默认取最小权限：远程 owner 只能读写自己的 owner home，另外可读组织明确发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板和旧顶层私有目录一律拒绝。外部目录只能由当前轮的结构化 capability/delivery contract 精确加入 workspace roots，不能由模型给出绝对路径自我授权；该授权也不能覆盖凭据文件或其他 owner 拒绝。随 wheel 发布的基础 tools/skills 是公共产品能力，shared 只用于组织显式共享的 skills/tools/role templates；个人 USER/SOUL、记忆、任务和产物不得由 shared 或 full mode 绕过。
- 普通通道上下文必须在同一结构化 scope 内“累计 transcript → 自动 compact → 继续累计”：raw transcript 永不因 compact 改写或删除，thread JSON 的 summary+cursor+generation+checkpoint pointer 是唯一 live compact 状态；旧消息只进入该 owner 的 LocalStore 派生检索索引。每次先生成不改状态的候选，再按完整下一轮输入验证低于精确阈值，随后先写 owner-scoped 完整恢复 checkpoint，最后以一次 generation CAS 同时提交 summary/cursor/checkpoint；失败候选、checkpoint 写失败或 CAS 冲突都不得推进游标。正常达到配置阈值时允许在同一历史尾部保留有界的近期完整 user/assistant 回合，过大时退回压缩全部旧段；供应商已经返回上下文压力时则一次替换本轮之前的完整旧段，禁止把同一受保护尾部连续压成多代 checkpoint。这不是第二份 history，也不能让固定最近轮数重新成为遗忘边界。连续失败只更新同一 thread 的 typed failure circuit，三次后短暂冷却，成功提交清零，禁止每条新消息重复空烧摘要模型。
- 同一 owner/thread 只有一份模型历史。聊天、文件工作、子代理协调、定时唤醒和普通小任务都继续使用同一 thread 的 summary + raw tail；task link、workspace、progress、wake 与子代理树只是结构化运行事实，不得过滤、替换或复制 transcript。普通 Gateway 请求按会话顺序执行，当前 turn 结束或耐久续轮启动后仍继续同一历史，不能创建平行“聊天上下文”。
- 子代理 lifecycle wake 是同一 root active turn 的耐久工作片，不是 synthetic wake 文案发起的新任务。后台
  续接必须以 exact task link 的原始 goal 作为 `User Task/root_user_prompt`，把 wake prompt 放入 runtime
  continuation；同时从该 task 自己的 `work/blobs/tool_outputs/index.jsonl` 按 child 信封的 exact
  `conversation_request_id` 恢复结构化调用历史、one-shot 去重和已执行工具。durable task id 只负责定位
  工作区，不能冒充 active turn；旧索引缺该字段时只允许同值 `request_id` 精确兼容。不得扫描 child workspace、不得混入
  sibling/旧 task，也不得从正文猜语言、计划或完成状态。恢复记录计入当前 absolute tool-round baseline，
  但配置的每工作片新增轮数额度不缩水。detached Audit 配额通知继续是独立运营事件，不伪装成 root turn。
  该边界对照 会话运行时 `core/src/agent/control.rs` 的 child notification 与
  `core/src/session/turn.rs::run_turn`、`core/src/session/mod.rs` 的同 history active turn。
- 同一 exact root 的 child lifecycle envelope 是逐项耐久交付义务。合批可以减少模型调用，也可以按 token 或
  条数把剩余 sibling 延到后续轮，但不能丢弃或提前 ack；每个被采样 completion 的 exact id、typed status 与
  `final_report_ref` 必须保留。本轮开始时已在队列但未进 active batch 的信封不得被活动回合瘦事件入口消费；
  它们继续阻止 root closeout 和用户可见最终回复，直到后续有界批次完整读取。该边界对照 会话运行时
  `forward_child_completion_to_parent` 与 session mailbox；“canonical child 都终态”只决定可以开始整合，
  不等于模型已经看过所有完成信封。
- 后台 scheduler 只有在该 thread/task 没有 linked live turn 时才能启动一个续接 turn；续接仍加载完整 thread compact 与消息尾部，并额外读取精确 task 的运行状态。任务已 completed/cancelled/interrupted/abandoned/superseded 时，排队的定时或生命周期 wake 直接作废，不能复活任务。
- task workspace 下只保留 progress、canonical state 和证据 refs 等结构化运行事实，不生成 task compact、task rollup package 或第二份主代理上下文。main、child、grandchild 的持久上下文压缩都只认各自 thread JSON 的 summary + cursor + generation + checkpoint；active turn 因 context pressure 续跑时，原生 ToolCall/ToolResult 的成对回收必须先在同一 owner Compact ledger 写 checkpoint，再推进该 ConversationThread generation，随后继续同一 turn。task-local workspace、工具和 Memory 仍按 run 隔离，但不得恢复旧 `memory_archive` continuation 或第二套计数。
- 所有位于消息开头的 `/XXXX` 都先进入统一 typed command dispatcher；支持的命令由程序执行，
  不支持的命令由程序确定性拒绝，命令词本身不得进入 transcript、active-turn guidance 或模型输入。
  `/btw` 与 `/audit` 只有去掉命令词后的用户正文可以进入既有 turn/task，权限和运行模式由 typed
  payload 携带；不得在 adapter、worker 或 prompt 中另写一套解析器。`/verbose off|on|full` 是
  per-thread 持久系统设置；工具进度必须以 typed event 进入 Gateway，再由有身份校验的 progress
  endpoint 和既有持久化 delivery worker 回送。不得从模型自然语言或混合 chunk 文本猜工具状态，
  也不得因进度发送失败重新执行任务。
- 派工、等待后的 presentation-only 模型轮必须结构化禁用全部工具。若 provider 仍夹带
  未授权的原生 tool call，首次按形态重试；有界重试后只能丢弃机器调用并保留通过统一内部协议净化的
  模型自然正文，绝不能执行该调用或把成功请求投影为空。若连安全正文也不存在，Gateway 以 typed
  `USER_REPLY_UNAVAILABLE` 失败，不能写空 assistant transcript、不能报告 `ok=true`。该边界不解析回复
  语义；对照 通道运行时 的 streamed-text 空 final 保留和 长期助手 对 commentary/final-delivery 的分离。
- 普通会话控制只有一份 typed protocol：`/status` 只读当前 durable root task/request/thread/子代理事实且
  不回放引导；上下文压缩只显示当前 thread 的唯一 generation。`/btw <内容>` 只投递给当前 active 根任务
  （尚未晋升才投当前 request），按 FIFO 在下一个模型安全点成为真实 user input；模型成功接收后以 guidance id
  幂等追加到同一 transcript，失败或竞态丢失时不污染后续消息。相同文字的两次输入保留为两条事件。
  同一 durable task 任一时刻只允许一个主执行器；已有 linked live turn 时，`/btw` 只写入该 turn
  会消费的输入队列，禁止再发 wake 启动第二个执行器。只有根任务当前没有 linked live turn 时才可发布去重 wake。
  `/stop` 像 会话运行时 当前窗口的停止按钮：先按 owner+thread 找到精确 live request 并立即触发
  cancellation token/关闭当前模型 HTTP 传输，再持久写入 cancel marker、将已绑定根任务转为
  interrupted，并异步停止同一 typed lineage 的子代理树；不得先让模型或任务分类器判断“这算不算任务”。
  该 turn 尚未消费的 `/btw`/普通 steer 随 turn 一起作废，不能在下一次“继续”时突然生效。停止不删
  transcript、task workspace、thread compact 或 memory；后续普通“继续”由模型通过精确 task id 选择
  原现场，不要求用户重发整段 prompt，也不从文字猜 task id。
  live turn 续接旧任务时，引导消费优先使用 `task_attributes.conversation_task_id`；本轮 request id 只是执行尝试。
  引导像 会话运行时 `TurnInput::UserInput` 一样进入当前 turn 的 provider-neutral `UserTurn`/text history；后续工具轮和
  compact continuation 通过 typed carrier 保留，不能改成 system injection、任务专属 guidance history 或第二份 prompt。
  子代理生命周期事件也在同一 active turn 的安全点按精确 task id 读取；新事件使旧模型动作失效，只有模型成功
  读取后才确认消费。启动当前后台轮的 wake 仍由 scheduler 单独确认，禁止 active turn 与 scheduler 双消费。
  `/status`、`/context`、`/compact`、`/effort`、`/btw`、`/stop`、`/goal`、`/verbose` 必须绕过同会话普通消息队列，由 CLI、Feishu
  和未来 IM 共用；`/audit` 只把去前缀后的正文作为新任务排队。旧 `/btw` 列表、永久 prompt 注入和
  `/btw-clear` 不再是产品能力。Gateway 生命周期 `POST /stop` 仍是管理员接口；会话 `/stop` 的控制目标
  只认 owner+thread 的当前 live request 和它的结构化 task link，自然语言“停一下/改一下”不获得硬控制权。
- `/context` 只读同一 owner/thread 的真实 pending transcript、模型窗口和 `runtime_compact_policy`，与自动
  compact 共用 `_projected_context_tokens` 估算；它不能创建 thread、写摘要或推进 cursor。`/compact [可选要求]`
  只能在没有 live turn 时申请同一 conversation run lane，再复用自动 compact 的 summary candidate、完整
  checkpoint 与 generation CAS 强制推进；可选要求只是摘要软上下文，不能覆盖结构化 operation evidence。
  自动 compact 仍按唯一配置阈值在每轮前触发，不因手动入口建立第二条历史。`/effort` 只认 provider/backend
  明示的结构化推理档位与 setter；当前 MiniMax-M2.7 Anthropic-compatible 接口没有生效的 effort 参数，因此
  查询应如实显示 provider-managed，设置必须失败且不得偷换成 temperature、prompt 文案或伪持久状态。
- TUI 输入的 Up/Down 必须先依据输入 Window 的真实显示宽度和 Unicode cell width 在软折视觉行间移动，
  到视觉顶/底后才能进入逻辑换行、queue 或 history；不能只按 `\n` 判定。手动离开 transcript 尾部后，
  `N new messages ↓` 是带 typed mouse handler 的按钮，左键释放与 Ctrl-End 共用 `move_end()` 恢复 follow-tail，
  不从显示文字反向解析未读状态。被动到达的新输出不得抢走用户正在阅读的位置；但一次通过长度/只读门的
  真实用户提交是明确的 return-to-live 动作，必须对当前 main/child viewport 调用同一 `end()`，让这条用户
  消息和后续回复立即可见。空输入且当前 viewport 已离尾时，第一次 `Down` 也必须先复用同一 `end()` 返回
  最新消息；已经跟随尾部时才继续选择 child 或浏览较新 history。该优先级只读 typed `follow`，不能解析
  `N new messages` 文案；超限消息与终态 child 的拒绝输入不能借此改变滚动位置。
- 工具是否展示给模型与能否执行必须共用一次 `ToolRegistry.runtime_snapshot`。像 `send_message` 这类依赖
  当前 owner 外部通道的工具，availability 必须在每轮用结构化 provider/target/root/capability 判定；没有
  proactive route 时从 schema、tool search 和调用快照同时移除，但实现仍留在唯一 registry。不能先暴露
  一个客观不可用工具，再靠 handler 错误和模型重试收敛，也不能按普通问候或中文关键词隐藏。
- `/goal` 是当前 conversation thread 上的特殊持久 overlay，不创建第二个聊天、Agent 或工作区事实源。每 thread 同时最多一个 active/paused/blocked 目标；它绑定一个持久根任务，通过去重 wake 自动续跑，只能由 typed command 暂停/恢复/修改/清除，由 `update_goal` 在真正完成或确实阻塞时进入终态。`/stop` 遇到 active goal 只暂停它，不清除目标。
- `/audit` 是显式前缀才能启用的特殊任务模式。入口只负责把 guarantee/window 写入结构化 task attributes，子代理按调度关系继承，watch 只读这些字段。prompt、goal、summary 或普通句子中出现 `/audit` 文字都不能激活保证。
- 子代理数量由主模型按真实可独立分解项决定，不向普通用户暴露固定数量命令。调度层同时核对本批/任务/owner/全局余量；整批超限就结构化拒绝，不静默截断、不边创建边失败，也不允许用重复假工作填数量。
- Feishu 等 IM 入站回调在任何媒体、Gateway POST 或通道 IO 前，必须先持久化 authenticated
  `channel/user/conversation/provider_message_id`、原始规范 payload 与 SHA-256 digest。same-id/same-body
  只复用原 row，same-id/different-body 写独立 quarantine 且不覆盖首条事实。唯一 adapter delivery worker
  依次推进媒体、精确 POST body、结构化 submission、占位、input/result/control receipt watcher 和最终回送。
  ingress 与 reply row 的每个外部阶段都先用短文件锁领取 `owner/epoch/expiry`，网络、媒体和 provider IO
  均在锁外运行，结果只允许同 epoch CAS；过期接管递增 epoch，旧进程不得迟到写回。POST 响应丢失从
  `payload_ready` 原样重放持久 `gateway_payload`，响应已落盘后的 adapter 崩溃从 `submitted` 恢复而不再
  POST。轮询 429/5xx 只保持 WAIT，auth/config 错误隔离；input `terminal_unknown` 清占位并持久收成
  unknown，二者都不能伪造成用户最终正文。控制 submission 另保留 `operation_id/receipt_id/control_state`；
  `/btw` unknown 只能以 operation ID 查询 `/control-status/<operation_id>`，不能按目标 turn 去重。目标
  `request_id` 允许初始为空，只能由首次 GET 在同 epoch CAS 单向绑定，之后漂移必须隔离。stop rejected
  返回明确控制结果，stop `terminal_unknown` 清占位并落 durable unknown，不能静默标成 completed。
  control watcher 另冻结 `channel/user/conversation/channel_chat_type/channel_chat_id` 为 immutable issuer route，
  GET 必须携带同名结构化身份头由 Gateway 精确核对；operation ID 只用于定位，不能充当访问凭据。
- 用户通道正文只能使用统一 user-facing projection；`MAIN_AGENT/RUN/SUBAGENT` 内部协议留在运行时，禁止原样进入 Gateway response、飞书回复或 assistant transcript。产物发送只有一个 `send_message` 工具：目标固定为当前 owner，附件必须命中该 owner 的 artifact registry、真实路径和 hash；同会话下一轮从 transcript metadata 复用最近产物，不能因“发我”重新生成或复制。
- 普通任务采用 会话运行时 式完成边界：主模型依据同一 thread history、真实工具结果、测试结果和子代理事实直接
  给出自然最终答复。运行时不再要求 `submit_for_acceptance`，不扫描目录推断完成，不生成完成 marker，
  也不在最终答复后运行 `delivery_snapshot` 重写短轮。Gateway/IM 只在统一出口移除内部协议并按可信
  `ReplyEnvelope` 投递；assistant transcript 保存同一份净化后的模型正文，避免交付层另造第二种回答。
- 普通任务没有独立 closeout ledger 硬门。文件存在、路径权限、附件 owner/hash、危险 effect 等客观安全事实
  仍在各自不可绕过的工具或投递边界校验；“任务是否做完”由主模型结合这些结构化事实判断。显式 `/goal`
  另由 typed goal 状态和 `update_goal` 收口，但它仍使用同一 conversation history，不恢复普通任务验收器。
- 长期命令只有一个受管入口：模型向 `run_command` 传 `run_in_background=true`，宿主返回可查询、可终止的
  `session_id/pid/output_file`。shell 自带的独立 `&`（包括 `nohup ... &`）在启动前以
  `BACKGROUND_PROCESS_MODE_REQUIRED + not_started` 拒绝；不能让前台 shell 退出后留下无账进程，也不能把
  同一条命令内部的短暂探活当成服务已经持久运行。普通 `2>&1` 重定向和引号内的 `&` 不受影响。
- 所有普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`：可信 `DeliveryContext` 单独持有 channel/target/reply_to，`ReplyEnvelope` 永远不带收件人；adapter/capabilities/target validator 只能通过 `ChannelAdapterRegistry` 注册，新增 IM 不得在投递主流程增加平台分支。
- 副作用超时不等于失败也不等于可重试：通用 Tool Gateway 必须把可能已经发生但没有终态的调用持久化为 `unknown`。`operation` 作用域只重放同一调用身份；`business` 作用域由工具从 typed owner/request/目标/规范参数生成跨调用稳定键。只有带 `source_ref` 的目标系统结构化核对能把 unknown 收口为 succeeded/failed，或在证明 not_started 后原子重开；没有核对能力就保持 fail-closed。provider 原生幂等键只由可信 DeliveryContext 下发，Feishu 的同一分片/附件重试必须复用同一个 UUID。
- 多个外部写组成普通任务时不增加通用 Saga、第二份事务账本或自动补偿器。对照 会话运行时
  `会话运行时-rs/core/src/tools/lifecycle.rs` / `parallel.rs` 与 长期助手 `agent/tool_executor.py` /
  `tool_dispatch_helpers.py`，每次工具调用仍有独立 operation 生命周期；my-agent 当前同轮调用继续按模型
  原顺序执行，不从自然语言猜依赖，也不因一个失败机械阻断彼此独立的后续调用。系统必须准确保留每项
  succeeded/failed/unknown，权威终态没有持久化成功时即使提供方回报成功也只能返回 unknown，禁止自动
  重试。archive、control-plane event 与 compact 续跑必须携带同一结构化状态；语义摘要不能吞掉中段失败、
  运行中或 unknown 的副作用事实。业务是否需要回滚仍由具体工具或 workflow 明示实现，通用底座不得
  伪造跨系统原子性。
- 工具参数中的显式绝对路径具有目标身份，不能为了“安全落位”静默换成另一个路径后返回成功。普通
  相对路径按当前可信 cwd/workspace 解析；只有显式 `output/...`、`work/...` 才进入结构化任务内部目录。
  绝对路径必须保留原值，再由
  `allowed_write_roots`、owner 墙、危险目录、sandbox/approval 明确允许或拒绝。该规则对齐 会话运行时
  “解析真实目标后交给 sandbox/approval”和 长期助手“保留绝对路径并报告 resolved_path”的做法。
- 除 `/status`、`/stop` 等显式控制命令外，普通聊天、派工回执、等待说明、进度与完成说明的用户正文必须来自 LLM。运行时只提供结构化事实、禁用回执轮工具并校验/净化输出；不得用“任务正在处理”等固定系统句子替换模型正文。没有合格模型正文时宁可记录结构化失败并抑制投递，也不能用模板冒充 Agent 回答。
- 后台主代理 claim 的 `heartbeat=0` 明确表示按 TTL 自动取间隔；默认 TTL 为 90 秒。claim 保存
  `process-domain + pid + start_time`，只有同一 PID namespace/主机进程域能证明旧进程已死时才提前接管；
  跨 Pod、旧格式或身份不足时必须等待 TTL，不能把“当前容器看不见 PID”当作已死。
- Gateway SIGTERM/SIGINT 必须先写 typed stop request 与轻量 forensics（signal、pid/ppid、父命令、systemd
  环境），再复用现有 stop-file drain；计划 stop 已存在时只能追加 observed，不能改写为异常退出。
- 人格三件套只有 `update_persona` 一个写入口：USER 可由 Agent 自主维护，SOUL/AGENTS 必须用户确认；基础文件、patch、shell 和 admin full-access sandbox 均不得形成旁路。
- Harvester cursor 是一批事件已完成 engine、spool、audit 的提交水位，不是网络读取进度预告；批内任一步失败都必须保留旧 cursor 以便重试，禁止先推进游标再处理造成静默丢事件。
- `/audit` 高频判读复用同一 watch/spool/structured-output 主链，不建立第二套 Agent、Memory、
  Compact 或角色运行时。模型调用批次由结构化的等待时间、累计条数、累计数据量任一条件触发；
  正常事件完整进入有界批次，不能为了凑上下文提前截断。只有单条事件本身已超过当前模型安全输入
  预算时，模型视图才生成明确标记的头尾内容；完整原文、哈希、位置、模型、分数和结论仍保存在
  owner-scoped append-only spool/archive/verdict ledger，并通过稳定 `audit://...` `source_ref`
  与 `watch_stream(action=inspect, ack_id=...)` 精确查回。每个 Audit 的 watch 身份必须包含 typed
  root task id；相同 owner/source 的另一条 Audit 不得复用游标或账本。任何获得 exact task 与工具
  权限的 Agent 都可消费；是否委派、委派数量、角色、复核和汇报路线均由模型自主决定，运行时不得
  固定主代理禁用、来源到子代理映射或分数路由。exact clear 只关闭该 root task 的 watch 并保留原始
  审计文件。后台唤醒继续使用原 task id 和 objective，不能按 owner 任意旧 watch 反推，也不能创建
  固定工作角色。常规批次只占模型窗口的小比例并设实际上限，结构化输出只做 request-local 限制，
  不能缩小主会话配置。
- 模型能力自我描述必须按 installed/configured/healthy/current-bound 四层事实投影；adapter 存在、凭据齐全或代码中有工具都不能单独升级成当前可投递。四层状态全部从 composition root 的同一 `ChannelAdapterRegistry`、结构化 daemon health 和 owner conversation binding 投影；进程死亡、心跳过期、状态损坏或未绑定均 fail-closed。
- Compact 百分比只允许一个权威阈值：`context_window_tokens × configured_percent`。候选是否可提交也必须按包含 system/persona、summary、近期 raw tail、原生工具 Schema、ToolCall 参数、ToolResult、运行引导、近期与已压缩工具事实及当前用户输入的完整下一轮投影低于这个阈值判断；不得再加未来输出预留、工具 digest 隐藏天花板或另一套 task compact。当前 turn 的原生工具历史只能按调用/结果整对回收，并复用既有语义摘要后端在同一 IR 中以最多一条 replacement item 承接被回收旧段；后续压缩原位替换，summary、handoff marker 与近期尾部都进入同一预算。对于允许持久化且已绑定 ConversationThread 的真实 main/child/grandchild 回合，这次 mid-turn 回收本身就是一次 canonical Compact：先把旧 thread summary 与当前工具历史合成完整替代摘要，写 `source_kind=live_tool_ir` 的 owner-scoped checkpoint，再以 generation CAS 提交；transcript cursor 不前移，工具对累计数单独记录。摘要请求顺序必须对齐 会话运行时：完整历史在前，synthetic user Compact 指令最后；禁止把摘要指令放在历史最前面后再追加工具历史，否则兼容模型会把它当旧要求并顺着最后动作普通续写。checkpoint、CAS 或摘要失败必须恢复原 IR 并失败，不能静默丢历史。presentation/no-save 或没有持久 thread 的辅助回合才允许只做 turn-local 窗口整理，且不得冒充 `compact N`。摘要是非权威续接视图，raw archive、operation ledger、artifact、workspace、transcript 和真实 UserTurn 仍是事实源；不能另用漏算工具参数的字符口径。provider usage/tokenizer 缺失时的估算误差必须与阈值数学分开说明。
- Skill 运行时只有 composition root 创建的一个 `SkillsService`：每轮 snapshot 固定 `workspace > owner > shared > builtin`，prompt、检索、正文读取、能力自述与子代理都消费同一实例。`shared/indexes/skills.jsonl` 只是派生管理员清单；不得恢复 `SkillRegistry`、resolver/index loader 或编排入口临时 router。子代理 Skill 引用必须保存 stable id + content hash，后代只能收窄不能扩张。
- 记忆与 Skill 各有一份独立总闸：owner `memory_policy.json`(memory-policy.v1.enabled) 与 `skill_policy.json`(enabled) 只经 `resolve_effective_owner_policy` 投影成 `EffectiveOwnerPolicy.memory_enabled/skills_enabled` 两个 effective flag；文件缺失或损坏视为开启(老 owner 兼容，永不因解析失败误杀)，子代理用 and 继承父开关、只能收窄不能扩张。消费点只认该 flag：Memory 关闭时 curator 调度(`_run_due_curators`)、会话 close/reset 请求、发现层判活(`_has_pending_memory_curator_work`)、决策点召回(`push_relevant_memories_report`)全部短路，remember 工具 availability 不可用；Skill 关闭时 `snapshot_for` 返回空快照(短路而非扫描失败，无 load 错误)。两闸互不级联：关 Memory 不影响 Skill 快照，关 Skill 不影响 Memory 召回。禁止为单开关另建第二套 policy 状态或跳过 effective flag 直接读原始 JSON。
- - 【长LLM测试 2026-08-13】两篇长文经真实 /ask 入口灌入 + 跨 run 召回 20/20 全命中：灌入=3605 字
  《大理生活回忆》(雪球/海月小筑/鹿柴/县一中/马尔代夫 AOW/德龙/三月街等 20 种子事实) + 第二篇
  《工作与远行》(山海·拾遗个展/云中君三本/腾冲温泉/大阪/沸点火锅/碱水粽/数位板等)。**模型
  remember 工具侧两次 batch 都生成空 operations 数组**(TOOL_PARAMETER_REQUIRED → 补参后
  TOOL_INVALID_ARGUMENTS，内容全丢)——模型层行为非产品 bug；**curator 车道兜底正常**：
  candidates 161→176→187、long_term 10→21→30，提炼质量优(鹿柴 confidence 0.95 带 evidence quote)。
  召回:重启前 20/20 全命中零幻觉(含顾女士/二十幅等细节级事实),`systemctl restart` 跨 run 再问
  20/20 全命中(持久化不丢)。已清理回基线(candidates 187→161、long_term 30→10)。
- 【真机发现→已修 2026-08-13】remember 确定性失败被错误归 UNKNOWN：参数错误后模型补参仍错时
  收到「副作用不确定已阻止重做」放弃修正。根因=coordinator 对已执行 handler 的通用失败按
  handler_executed=True 归 UNKNOWN，`_memory_error` 从不声明 effect_outcome。修复=`_memory_error`
  增加显式 not_started 声明(默认保守不标)，18 个写入前/只读校验失败点标 not_started → 归 FAILED
  (可修正重试)；唯一例外 MEMORY_CANDIDATE_WRITE_FAILED(observe_many 可能部分写入)保持 unknown。
  红测先写(test_memory_tool.py effect_outcome 断言)+全量 gate 绿+已部署 testbox(grep site-packages
  签名+18 处就位)，部署后抽样召回正常、日志 0 次 TOOL_OPERATION_OUTCOME_UNKNOWN。

【B项真机验收 2026-08-12】长文召回补重启/双用户隔离两象限全 PASS：象限一=主 owner(local/main)经真实 /ask 入口灌入含 5 独有事实的中长文 → 注入 curator pending reason(紧急车道,60s 节流内消费) → 提炼 5/5 落 long_term → 提问召回 5/5 命中 → `systemctl restart my-agent-gateway` → 再问同题 5/5 命中且更详尽(长期记忆磁盘持久,重启不丢);象限二=两 feishu 用户(ou_e2e_feishu_a_20260811 / ou_6591b3f0d402ce95a62ef26436dca895)各灌独有事实 → 各自动提炼落库(3/3、2/3) → 交叉提问零串扰(A 问 B 的成都→"没告诉我";B 问 A 的朵朵/滨海湾→"不知道"),正向各自 3/3 全命中。验收数据已清理回基线(10/0/0),对话/审计/runs 日志留证。
- 长期 Memory 只有当前 owner `memory/long_term/memory.jsonl` 一个正式事实权威；SQLite/FTS、向量和
  global index 只可重建，必须按稳定 entry ID 对照 active 版本，不能让 tombstone 或旧版本复活。
  `memory/daily` 只保存 Curator 经历摘要与 refs，`memory/ops.jsonl` 只保存无正文操作审计；两者都不再
  镜像长期正文或参与正式事实召回。
- Memory 候选只有 owner `memory/candidates.jsonl` 一份状态机。Gateway 的轮次/时间/pre-compact/
  close/reset/task-complete/daily-finalize/admin 触发只向同一 `MemoryCuratorService` 提交 reason，共用
  lease、cursor 和整批提交恢复；模型只产严格结构化 Daily/Candidate，宿主验证后落盘，不能直接写
  long-term、Persona、lesson 或 HOT。旧 learning drafts、task-local memory gate、ops 候选和 daily
  mirror 只由一次性 v2 migration 读取，生产运行不保留双读、双写或 fallback。
- planner/runner 决策点的主动教训召回只读正式 Lesson/HOT，并与普通运行复用唯一
  `<memory-context>`。旧 `memory_push` 的 lesson 枚举、trigger_conditions、long-term
  `kind=lesson_*` 写读路径和 `[相关记忆提示]` 已删除；失败自省只能提交待审 Candidate。
- Persona 当前正文只有 owner 的 SOUL/USER/AGENTS 三个权威文件，全部读取和变更必须经过同一个 `PersonaRepository`。版本 ledger/backups 只用于 CAS、审计和回滚；USER 只按当前用户原话自主维护，SOUL/AGENTS 仍需确认，确认期间 SHA 漂移必须拒绝覆盖。不得恢复直接文件写旁路或增加 IM 专用人格状态。
- 用户级持久定时只有 owner `data/scheduler/` 中的 `SchedulerRepository` 一份 job/run 事实源。`schedule` 是唯一 action tool，at/every/cron 均为 typed schema；到期 run 使用 CAS、先推进 next-run、claim TTL 和 heartbeat，并返回原 owner/thread 的同一代理主链。`clock.sleep` 只服务模型明确需要的时间等待；子代理完成依赖生命周期事件，不再有 wait/巡场工具，也不能形成第二 scheduler。对照 通道运行时 `src/cron/types.ts`/`schedule.ts`/`service/timer.ts` 与 长期助手 `cron/jobs.py`/`scheduler.py`/`tools/cronjob_tools.py`，不从用户文本推断身份、任务或时间状态。
- 可复用 Workflow 只有一种表达：Skill 提供方法，当前 thread 的 `task_progress` 保存计划，原生子代理工具显式创建和派发执行者。旧 `subagent_workflows` package、mode/config/CLI、extension hook 和 shared workflow index 已删除；不得恢复第二套 transcript、planner/router、自动批量扩张或模板执行 runtime。该边界对照 会话运行时 `turn_context.rs` 的逐轮 Skills snapshot、`plan.rs`/`plan_spec.rs` 的 typed plan 更新和 `multi_agents_spec.rs` 的显式 spawn，以及 模型助手 Code `plugins/feature-dev` 通过 command/agent prompt 调用原生 Todo/agent 能力的做法。
- 普通代码任务只有一份 owner-local 被动验证证据：公共工具出口按 `ToolCallEnvelope.scope.root_task_id` 记录项目 manifest 中的精确规范命令、真实 exit 与 targeted/full；成功文件写使旧证据 stale。它参考 长期助手 `verification_evidence.py`/`verify_hooks.py` 的被动账本，但不移植 stop hook，不执行测试、不阻止完成、不恢复普通任务验收器；模型继续按 会话运行时 的真实 tool-result 事实自然收口。
- 主代理普通正文不是机器事实：当本轮已有持久 `task_progress` 且仍有 open item 时，模型的第一版 plain final 只作为可丢弃草稿；运行时参考 终端交互 `TaskUpdateTool` 的 structural verification nudge，在同一个工具循环中追加结构化 `open_count` 软核对。下一轮仍持有原工具能力，可读/更新清单或继续工作。提醒按 `executed_tools` 的真实工具进展段去重：同一进展段只消费一次，提醒后若又产生真实工具动作，后续 plain final 可再获得一次核对；没有新增工具动作时不得循环提醒。普通任务仍由模型正常结束，不以可能过期的清单形成完成硬门；只有显式持久 `/goal` 保留 open-plan `unfinished` 生命周期和 continuation。全链不解析“完成”等自然语言、不扫描目录、不执行验收。
- 子代理工具能力只能继承父代理当前 run 的工具快照并继续做减法：所有普通 leaf 角色都移除
  `create_subagents/send_guidance/cancel_subagents/resolve_capability_requests` 四个直属下级控制入口；
  只有结构化角色模板明确 `can_spawn_children=true` 的 coordinator，且父级本来拥有对应能力时，才保留
  这四项。leaf 自己仍可用 `capability_request` 请求本层能力。显式 `allowed_tools` 只是收窄请求，
  不得凭 agent 名、goal 文本或模型声称扩权。
- 角色模板的 `prompt_zh` 是创建时冻结的软行为合同：snapshot 必须携带当前角色名称和提示，leaf runner
  只注入自己这一份，不加载整份角色目录；旧 snapshot 缺字段时才按明确 role id 读取当前内置模板。提示
  不能授予工具、路径或生命周期权力。内置 worker 对齐 会话运行时 shared-workspace 纪律，只做父级分配的明确
  文件/模块范围，不覆盖或撤销兄弟改动；已有文本局部修改先用 `apply_patch`，上下文未命中时重读最小片段
  再重试，不能用整文件重写绕过冲突。该纪律不恢复 workspace 锁或自然语言机器裁决。
- 模型调用观测区分 logical turn、物理 model attempt 与 provider HTTP attempt；每次 provider 重试、模型级重试、失败、超时和最终状态写入同一线程安全账本，并投影到 runtime facts 与内部 Gateway result。观测回调不得读取 key/body，也不得改变真实请求结果。
- owner quota 的唯一应用层锁序是 `owner quota -> repository/file lock -> mutation`。文件工具、Memory、Persona、Scheduler 和 Skill draft 必须在同一 owner lock 内按完整 multi-file mutation 的最终字节准入；策略、用量或锁不可读时 fail-closed。应用门不能冒充 filesystem quota：Shell/PTY/LSP 任意进程写盘必须由正式部署的 filesystem/project/container quota 硬限制。
- owner retention 只依据 typed policy、terminal authority 和 timestamp；task/scratch 执行前必须二次校验，先移入 owner trash 并写 tombstone，再按期限删除。owner/task legal hold、损坏 policy 或状态漂移均跳过并留审计。Gateway 只用 cursor 有界扫描 owner，不为清理实例化 Agent。
- Shared/Skill、Memory/Persona、scheduler、Workflow、隐私和验证证据的详细审计见 `docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`。用户已于 2026-07-18 明确确认按规划逐项实现；权威功能规格为 `docs/design/FEATURE-20260718-agent-foundation-convergence.md`，当前本地实现完成，待完整 CI、提交、部署和真实验证。实现必须逐切片迁移并删除旧主链，不允许增加 IM 专项或自然语言硬判断。

当前入口文档：

- 当前产品事实与 P0 冻结边界：`docs/PRODUCT_FACTS.md`
- 架构总览：`docs/design/ARCHITECTURE_GUIDE.md`
- 模块结构：`docs/architecture/MODULE_OWNERSHIP.md`
- Home 布局：`docs/architecture/MY_AGENT_HOME_LAYOUT.md`
- Subagent：`docs/modules/subagent/04-structure.md`
- Memory：`docs/modules/memory/04-structure.md`
- Gateway：`docs/modules/gateway/04-structure.md`
- 多 IM 统一投递：`docs/design/CHANNEL_DELIVERY_DESIGN.md`
- Agent 基础能力事实审计：`docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`
- 代码尺寸报告：`CODE_SIZE_REPORT.md`
- 容器 sandbox、一键安装与发布干净度：`docs/design/CONTAINER_SANDBOX_INSTALL.md`
- P2 正式 scale 主链：`docs/design/P2_SCALE_MAINLINE.md`
- P2 灰度、灾备、Owner 对象事实源与 24 小时 proof：
  `docs/design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md`
- 1.9 / 1.10 近 24 小时日志、真实 LLM 和通用底座加固：
  `docs/audits/REAL_LLM_24H_HARDENING_20260710.md`
- 待实施开发计划（确定性优先 + 缺口补齐）：`docs/design/PLAN-stability-and-gaps-20260611.md`
  （统领原则=同输入结果可重复；优先级 模型行为+观测性 → 记忆推模式 → 失败自省 → 多通道/多租户）
- 待实施开发计划（任务完成力底座，R5 三案→通用）：
  `docs/design/PLAN-foundation-task-completion-20260611.md`
  （五支柱：坚持力 run 续航/交付纪律 出口走门/边界正确性 锁与读边界/协作闭环
  引导前移/结论完备性 证据契约；R6 同 prompt 重跑三任务总验收）
- 方向修正纲领（R9 质量退化复盘 → 稳而不管）：
  `docs/design/PLAN-stability-not-control-20260612.md`
  （流程复杂性藏框架层模型无感；质量靠 skill 知识非验收门；测试 prompt 永远
  普通用户自然语言。2026-06-12 四批落地：底座修复/召回接通+skill 树千级地基/
  长期助手 工程移植〔退避抖动/错误分类/结构化错误/per-thread 中断/turn 预算〕/
  性能缓存，明细见 REFACTORING_BACKLOG 同日条目）

以后新增长期设计，只写摘要和链接，不再把完整方案塞回这个文件。

## 2026-08-25 子代理局部文件编辑能力单一权威【状态：`1082ccf` 已推送，待长任务安全点部署】

- 同一长 TUI 的 Click Python→TypeScript 复刻中，首个 worker 已收到“不用整文件覆盖”的角色提示，实际
  capability snapshot 却只有 `write_file/apply_patch`，没有仓库既有的 `edit_file`。一次补丁仅因期望行多
  两个前导空格而未命中，工具又只返回路径；模型随后反复重写整个文件并制造重复声明。因此问题不在
  Compact summary，而在工具可用性与失败反馈没有形成同一条主链。
- 文件写工具的唯一成员与顺序统一收在 `tooling/write_boundary.py` 的
  `WRITE_TOOL_ORDER/WRITE_TOOL_NAMES`：直接 coding child、递归 leaf、角色模板、capability grant、写围栏、
  路径 gate、进度与验证投影都消费它。父级显式缩窄工具仍是权限上界，缺少 `edit_file` 时后代不得扩权。
- 对照 会话运行时 apply-patch 的 expected-lines 错误，`apply_patch` 仍保持逐字严格匹配和零写入，只在未命中时
  有界回显最多 4,000 字符的期望原文并指出空格/缩进；一处或少数片段引导使用已有的空白容错
  `edit_file`，多文件关联修改继续使用 `apply_patch`。不增加目录锁、自然语言裁决、自动重试或整文件兜底。

## 2026-08-25 当前主任务 Working 时钟【状态：本地 focused 通过，待单 Gateway 真机复验】

- 终端交互 的 spinner 时间只读当前 query 的 `loadingStartTimeRef`，并在 query 从 idle 进入 active 的同一渲染
  立即重置；不会把 session 或组件首次挂载时间当本轮耗时。本项目对应的持久当前 query 身份是
  `ConversationThread.workspace_task_id`，起点是它对应 active `ThreadTaskLink.created_at`。
- `conversation_agent_activity.v5.main_activity` 的阶段/context 仍可来自进程内易失 sink，但 task id 与时钟
  必须重新绑定当前 workspace root。较晚创建的 child link、旧 root sink 与 TUI background block 年龄只可
  作为各自展示事实，不能替换主任务时钟；Gateway 重启后 sink 丢失时以 root 起点和 `waiting` 恢复。
- renderer 只读 `main_activity.started_at`。字段缺失显示 `0:00`，不新增客户端时钟状态、不猜 prompt、不影响
  lifecycle/Compact/完成权威。详细合同同步见 `docs/modules/gateway/04-structure.md#tui-后台活动投影`。

## 2026-08-21 子代理递归控制面与自然收口【状态：直属活动区已真机验证，lane 自愈待补】

- 问题根因：历史实现同时暴露“创建—调度—推进”多个模型工具，并用
  `acceptance_checks` / `verification_status` / 交付扫描器二次裁决任务是否完成。这使模型
  需要人工“推”已创建的 child，并会因缺验收字段、结果格式或第二状态而挂起。
- 完成权威：普通主代理与子代理都只读宿主 `turn_end.reason`，枚举为
  `completed/blocked/max-tokens/aborted/error/interrupted`。不解析回复文案，不扫描目录，
  不读测试数、证据数或历史 verification 状态来改写完成。
- 递归工具面：根与后代共用唯一 `create_subagents`，创建成功后由宿主自动启动。
  模型侧 `dispatch_subagents` 与 `schedule_child_subagents` 已删除；内部 dispatcher 仍保留，
  只负责 runner 启动、租约、恢复和有界重试。
- 已删除无人使用的 `SubagentDispatchEnvelope` 与 decoder 分支。宿主为自动启动子进程保留内部
  `subagents-dispatch` 入口，但普通状态页、doctor 和模型 prompt 都不再要求用户或模型手动催跑。
- 父级日常模型动作只保留创建直接下级、给一个直接下级插入补充消息、打断/取消一个直接下级，
  以及裁决该直接下级的结构化 capability 请求。`inspect_agent_tree` 继续作为 `/status`、诊断、恢复和
  TUI 投影的内部只读能力，但从普通模型工具箱移除；父级不靠查树、shell `sleep` 或周期调用推动 child。
  子代理、孙代理和根的差异只由 `run_id/parent_run_id/root_run_id`、工作区和递减权限表达，每一层只管理
  自己的直接下级。
- 这四个直属控制入口不是每个 child 的固定工具。根主代理和结构化
  `can_spawn_children=true` 的 coordinator 才持有；worker/researcher/tester/writer/bug-finder 等 leaf
  只执行自己的任务和必要的 `capability_request`。若某一层需要再拆分，父级应把它明确创建成
  coordinator，而不是给普通 leaf 塞一组永远用不到的管理工具。
- 模型侧 `raise_event` 同步删除，不把“子代理自报进展”作为第五个递归控制工具。普通活动、权限申请、
  阻塞和终态由宿主从 runner/thread 的 typed 生命周期直接写入父级事件账本；长期 Audit/监控也调用内部
  observation/wake service，不再绕回模型工具。
- 模型可见的 `cancel_subagents` 只接收直属 `run_id/run_ids + reason`；`root_id/status/dry_run/
  kill_process` 和整树回执都是宿主运维内部面。因此打断入口不能被模型当成隐蔽的查树/轮询工具。
- `.7` 原样 TUI 在 `e321483` 上实锤另一个底层断层：前台默认把整个 `orchestration` category
  收进 `tool_search`，MiniMax 首轮实际看不到 `create_subagents`，因而说要派 5 个 child 却只调
  Bash/Write。对照 会话运行时 multi-agent v2 的 `ToolExposure::Direct` 后，当前默认不再延迟
  `orchestration`；创建、直属插话、打断和权限裁决从第一次模型调用就可见。这是工具快照配置修复，
  不解析“必须子代理”等用户文字做机器路由。
- 旧 `InspectAgentTreeTool`、模型 Schema、公开导出和专用工具测试已删除，不保留“虽未注册但仍像工具”
  的影子入口。内部代码直接读取 `agent_tree_status_payload`，它只是 `/status`、TUI、恢复和诊断的状态投影。
- 所有模型可见的子代理工具快照（含历史配置、持久化 grant、角色模板和递归继承）统一经过
  单一退休工具过滤器，防止旧账本把已删除的查树/手动推进入口重新带回模型工具箱。
  显式 `allowed_tools=[]` 表示零工具，只有 `None` 才按角色默认值派生；权限链始终只能逐层减少。
- 历史兼容：旧 task 中的 `acceptance_checks` / `verification_status` 字段暂保留以读取已有账本，
  但不进入当前 TaskEnvelope、runner 模型摘要、父级 wake、树摘要或完成判定。
- 默认资源护栏收紧为同 owner 最多 6 个未结束 child、每次递归创建最多 4 个、
  `runner_concurrency=auto` 时同时运行最多 4 个。这是资源边界，不是按任务文字硬编排子代理数量。
- 同一权威父代理可像 会话运行时 `spawn_agent` 一样分多次补充不同 child，直到上述会话容量耗尽。
  是否已有活跃 sibling 不能成为禁止第二批的机器门；重复请求继续由显式 idempotency/work-scope
  身份复用，并发父执行器继续由 active-turn claim 拒绝，容量继续由 root session 槽位原子控制。
  已退休的 `SUBAGENT_ACTIVE_LINEAGE_EXISTS` 不保留 Audit 专项绕过或后台来源分支。
- `.7` 首轮真机发现“owner 历史未终态 run”会永久吃光新 TUI 容量。对照 会话运行时 每棵 root session
  独享 `AgentControl` 后，`max_subagents` 改为当前根会话树上限；owner policy 的管理员配额仍全局计算。
  容量/冲突类整批拒绝同时显式标记 `effect_outcome=not_started`，不再被副作用账本误包装为
  `TOOL_OPERATION_OUTCOME_UNKNOWN`。
- `.7` 第二轮真机发现最后一个 child 在后台模型采样期间结束时，旧回合只看见 `2/3`，最终化却读取
  新状态 `3/3` 并提前关闭根任务，导致最终整合和启动丢失。对照 会话运行时
  `core/src/agent/control.rs` 的完成状态订阅以及 `tools/handlers/multi_agents/wait.rs` 的明确采样边界后，
  child lifecycle 后台轮会冻结采样前树阶段；从活跃树开始的旧回合不能关闭根任务，最后一条终态事件
  必须获得新的模型回合。该门只保证事件新鲜度，不判断产物质量，也不恢复机器验收。
- 后台轮现在只在开始时冻结一次 child phase，prompt、最终化和用户投递共用这份快照；同 root 的 DONE
  通知只有 `created_at` 不晚于该采样时刻才可随本轮合并确认，晚到通知继续 pending 并触发下一轮。新鲜
  回合看到全部 child 终态后，模型自然回复可直接投递，不再等待 root task link 先写成 `completed`；
  task link 继续负责恢复和停止，不再充当普通任务的第二完成验收器。
- 模型可见的父子消息按递归关系收成 `send_guidance(target, message)`：只允许给当前代理直接创建的
  一个下级插入普通消息。删除 thread/task/case、批量 run、整棵子树、priority 和 delivery 控制面；
  用户给主代理的插入继续走 active-turn 输入链，子代理管理孙代理，根代理不越层代管。
- 删除 `create_subagents` 自动登记的周期性 LLM 巡场和对应 `wait_tool.py`。child 进展、完成、阻塞和
  capability request 只通过真实生命周期事件唤醒直接父级；进程心跳、孤儿回收、失败重试仍是宿主
  liveness 底座，不是模型推动工具。旧 `dispatch_supervision_auto` policy 升级后按结构化 tool 标记退休。
- `.7` 真机随后证明，仅删 `wait` 还不够：`create_subagents` 回执仍把 `inspect_agent_tree` 写成
  `next_action/status_tool_call`，MiniMax 因而连续执行 `inspect_agent_tree + shell sleep`；同时 child 已写入
  OPEN capability request，普通模型回合却被 `turn_end=completed` 直接投影为 `DONE`。当前约定改为：
  create 回执只返回 `await_lifecycle_event` 和本批 run ids，不返回下一工具；OPEN/非法待裁决 capability
  request 是宿主掌握的结构化阻塞事实，优先把该 child 落为 `BLOCKED`，grant/deny 后由事件恢复同一个 run。
  这不是产物质量验收，也不读取模型正文。
- 对齐 会话运行时 child 继承父线程 `cwd` 与 sandbox 上界：普通 child 自动继承当前 Agent 的结构化 workspace
  write roots，后代只继承同一权限上界；`output_files` 只负责交付目标、展示和验证线索，
  不再要求模型为了写父级本来就有权写的项目目录重复声明权限。命名 Audit/exact-scope worker 不继承该
  普通写根，继续使用其精确结构化权限。
- 后台主代理权威身份以精确 `task_id` 为先，不能复用同 thread 旧任务的 `bg-main-*` run。run 命中其它
  task 时必须回退当前 task 的主链再建 attempt，避免整轮工具因权威链错挂而被拒绝并要求用户发“继续”。
- 主代理 run 或 current attempt 为结构化 `unknown` 时，wake、observation 与 progress policy 必须保留原账
  但停止自动挂载，不得通过重试风暴或创建第二棵主 run 绕过执行权闸。同线程观察按
  `thread_id + root_task_id` 分批，阻塞旧任务不占新任务消费限额。只有
  `recover_attempt_unknown` 在人工核对副作用后同时恢复 current attempt、崩溃调和写入的 run 状态并释放
  精确 attempt 锁，原事件才自然续跑。该边界对照 会话运行时 的 typed `AgentStatus` 终态通知：宿主订阅状态并
  向直接父线程注入一次事件，不靠周期 LLM 催促或反复重挂异常会话。
- 普通 child 的可写上界来自直接父级工作区，不再靠 `output_files` 重复授权；因此父级本来能写的项目目录
  可直接交给 child。`create_subagents.items[].output_files` 仍用来登记用户明确交付位置、产物归属和验证线索，
  但 goal 或 output_files 都不能把范围扩大到父级工作区之外。TUI 后台 notice 首次立即查询、之后每秒查询，
  并为同 thread 每条消息生成独立 block id，避免 reducer 丢掉第二条以后更新。
- child 启动时的父级共享阅读上下文只来自当前 tool loop 的 archive。旧的 agent-level
  `_parent_shared_context_packs` 缓存已删除，不能把上一任务读过的文件预览自动塞进下一任务；跨轮长期事实
  必须走正式 transcript/Compact/refs，而不是不可审计的进程内残留。
- `c2c0235` 的 `.7` 原样 TUI 任务创建并自动启动 4 个 child，真实暴露两个底层缺口：宽泛 `/root`
  forbidden 误压过更窄 task output allow，导致同一 child 连续 `WRITE_FORBIDDEN` 并重复申请已授予权限；
  真实 ToolResult 字段是 `tool_name`，旧状态投影却读取 `tool`，所以 canonical state 长期只显示空
  `RUNNING/0%`。写边界现按 会话运行时 FileSystemSandboxPolicy 的最具体条目优先、deny 同层胜出；runner 的
  模型/工具边界写有界活动短状态，工具进度读取 `tool_name`，不公开模型正文或思考原文。
- `bea6fed` 原样任务已能产出页面并让四名 child 全部 `DONE`，但 422.849 秒内发生 32 次模型调用，
  累计输入估算约 19.47M tokens。结构化日志证明不是模型必须被“催”：task-local 父级创建下一层后进入
  `PENDING`，通用孤儿续跑却马上再次采样它；嵌套 child 的完成事件又越级进入根会话，形成反复查询和
  重读大上下文。当前权威规则是每层只订阅直属孩子：创建后耐久记录精确 child ids 并以
  `interrupted/SUBAGENTS_ACTIVE` 让出；同批成功收齐才恢复一次，失败、缺记录或 capability 阻塞立即恢复；
  新工作片从 `direct_children` 获得有界 status/result/artifact refs。崩溃恢复先调和这份等待记录，再运行
  通用孤儿复活。任何层级使用同一规则，不为“孙代理”另造合同。
- `task_progress` 正式降为软记事账本：已删除普通任务的自动 continuation 模块、配置项和递归深度字段。
  open 清单不再开新模型工作片，也不再参与完成判断；只有 Compact、显式 `/goal`、用户插入或 typed
  child/control event 可以续接。进度写入必须以稳定 id/status 表达，新建项还要可读 title；写后重新按
  canonical child run id 对账，避免模型提交的旧状态遮住真实终态。
- `create_subagents` 的输出锁拒绝必须保留 conflict 来源。`conflicting_proposed_index`
  表示同一批 item 互相重叠，必须修改参数后重试；非空 `existing_run_id` 才表示真实运行中的
  同级占用，由宿主生命周期事件唤醒。整批原子拒绝不会改变用户原始约束，更不是授权父代理
  改用用户禁止的方式。这是通用工具结果合同，不从 prompt 词句推导状态。
- 递归 child 的工作区权限对齐 会话运行时 `build_agent_spawn_config/apply_spawn_agent_runtime_overrides`：
  从当前 turn 继承 cwd 和 permission profile，不再叠加一条与 inherited workspace 完全同路径的
  默认 home deny。该调和只适用 local/unmanaged owner，且仅删精确同路径项；更窄的 `.ssh`、
  `Desktop`、`Downloads` deny 和所有远程 owner 宿主 home 围栏仍硬拦。来源只读 task 的
  `owner + attributes.workspace_root(s) + allowed_write_roots`，不从 goal 或模型声称授权。
- TUI `/stop` 有两种同一控制合同下的定位方式：前台正在提交/执行时必须带
  exact turn id；前台已让出但当前 conversation 仍有子代理/background claim 支撑的唯一 typed
  live task 时，可以不带 turn id 交给 Gateway 按 owner/thread 停止该任务。多个冲突 live task 或只有
  历史终态时继续 fail closed，不猜目标；`/btw` 始终要 exact active turn。
- `d928d77` 的 `.7` 单 Gateway 原样任务已证明四名 child 都能一次自然 `DONE`、最后一名会自动唤醒主代理，
  服务也真实监听 `0.0.0.0:8080` 且 loopback/LAN HTTP 200；但产品文件被错误写到 task 内部
  `output/abc`，`/root/abc` 为空，child 上下文还混入旧 `/root/kill-ws/...` 读取包，前台让出后 TUI
  没有常驻 Working。当前切片统一修复三点：裸 `abc/...` 相对当前 `/root`，显式 `output/...`/`work/...`
  才走 task 内部目录；共享阅读包只取当前轮；`/client/notices` 返回 canonical
  active task link 与 canonical child run 的有界快照。TUI 在输入框附近用一个可移除的
  灰色 Working 区域显示直属 child 名称、结构化状态、职责短标题、耗时和 attempts；查询失败保留
  上一次有效投影、真实 active root 归零才收起。该块只展示状态，不会推动、重试或验收任务。
- `dispatch_subagents` 这个模型工具和手动步骤已删，但“获取容量、启动/恢复 runner、投递首条任务、
  登记 heartbeat/status、有界重试及投递终态事件”是 create 之后必然存在的宿主生命周期。对照
  会话运行时 `会话运行时-rs/core/src/agent/control/spawn.rs::spawn_agent_internal` 与 `control.rs` 的
  `send_input/subscribe_status`，当前可继续把内部旧命名/入口折叠进 `create_subagents` 后的
  `start_or_resume` 服务，但不能删掉这些生命周期动作本身，否则 create 只会造一张不运行的工单。
- 用户与代理的控制权分开：代理模型继续只能管自己的直属 child；通过身份验证的 root owner 将可管理
  自己主代理树内的任意后代。后续 Gateway/domain 只建一份 TUI/Web/IM 共用的 typed control
  protocol：`list/read`、`message`、`interrupt`(只中断当前 turn，session 仍可恢复)、
  `resume/start`、`cancel/close` 和 capability 裁决。写操作必须携带稳定 `operation_id`、
  `owner/thread/root/target_run_id`、预期版本/状态与 accepted/rejected/unknown 回执；前端不直改
  canonical 账本。当前 `cancel_subagents` 是终态取消，不得冒充 会话运行时 式可恢复 interrupt。本轮只落地只读
  活动投影，写控制协议仍为待实现设计。
- `714c0c8` 在 `.7` 单 Gateway 的真实 TUI 已验证直属活动投影：两个 child 一次 attempt 自然完成，
  固定区域按 canonical state 展示启动、模型、工具和终态，不需要模型巡场。该轮同时证明 durable wake
  与 process-local lane 是两层事实：第二条 DONE wake 已落盘并被 read-only ready 发现，旧进程却没有
  取得 claim；优雅重启后立即取得 claim 并完成汇总。后续自愈只能基于稳定 lane identity、future
  queued/running age、durable claim/heartbeat 和 operation receipt，不得把自然语言“卡住了”当触发器，
  也不得用自动重复模型调用掩盖丢失的进程内占位。
- 直属 child 面板对齐 终端交互 的显式 task description 和 suffix width budget：`create_subagents` 可携带
  一句职责短标题并写入 canonical task，面板不再用 runner 当前阶段充当职责。token 列定义为最新一次
  provider preflight 的 `current_tokens`，不是跨轮累计；description、goal 和 token 都只作展示，不进入
  生命周期、权限、恢复、验收或完成判断。每行固定为一行，先保留耗时/ctx/Compact/重试，再截断短标题。
- Todo 与 coordinator panel 是两个 view：派工自动 seed 仍是恢复/关联所需的 canonical 进度事实，但当
  seed `item.id` 精确等于当前直属 `child.run_id` 时，TUI 只在 Todo 视图隐藏它，职责由下方 child 行承接。
  普通 Todo 与 covers 不隐藏；不能解析标题、goal 或“子代理”字样做去重。

## 2026-08-18 候选消息实时流式 + 每轮阶段计时【状态：本地 focused 通过，待真机部署复验】

- 底座问题（真机实测 18.5s/简单回复）：模型→Gateway 已是流式，但 Gateway→TUI 把正文暂存
  `_model_segment` 直到工具边界或终稿才发布，观感非流式；且每轮调用模型前固定约 13s 准备。
- 契约改动（`request_execution.py` BufferedChunkStreamWriter）：rich 客户端（TUI）在 `write_model`
  收到增量时按既有节奏（128 字符/换行/0.08s）实时发布 typed `model_delta` 事件（展示级脱敏，
  不做 `project_user_reply`——那会把未完成文本当终稿投影）；`assistant_commentary` 保留为工具边界的
  冻结/归过程标记（带全文，兼容旧客户端）。普通客户端/飞书 adapter 不消费 `model_delta`
  （projection fail-closed），行为不变；未来渠道要流式 = 请求侧开 rich 能力 + adapter 侧自选投递节奏
  （如飞书限频合并更新），不改 Gateway。增量在 steering 时与 `_model_segment` 同步清空。
- TUI 侧（`tui_runtime.py`）：`model_delta` → 实时追加活动 assistant 块；`assistant_commentary` 在已有
  流式活动块时只冻结（不重复追加），无活动块时回退旧契约整段落地；冻结段带 `process` 标记 →
  renderer 默认折叠为摘要行（Ctrl+O 展开，与工具块同一层级）。终稿仍以 canonical terminal 为准，
  `finalize` 覆盖流式正文且不重复。本地模式工具边界同样归为 process，TUI 双路径一致。
- 阶段计时：请求响应新增 `stages_ms`（`request_read_ms/conversation_prep_ms/run_ms/execution_ms/total_ms`），
  同值打结构化日志；终端归档整体携带，可作为测试证据。13s 的缓存修复待真机测量后定，不先猜。
- 测试：writer（rich 实时 delta/非 rich 不发/边界顺序/脱敏/steering 清空）、TUI（delta 实时块/
  冻结不重复/旧 Gateway 回退/process 标记/终稿覆盖）、plain projection fail-closed 均已补；
  gateway/tui/chat 切片 1137 passed。

## 2026-08-13 P2-5 SecretStore 决策：停用（DEPRECATED，不接线）

HANDOFF_reliability-gaps-20260813.md P2-5 要求人工拍板「接线 or 停用」。
决策=停用（owner 授权执行席位决策，记录在案）：

- 现状核对：SecretStore 唯一实例化点在模块 docstring 示例（无工厂函数），
  生产命令/网关零消费点；embedding.py 仅用抽象 secret_resolver（注入，
  不依赖 SecretStore 实例）；真实密钥以 0o600 明文 + /etc/my-agent 环境
  文件承载。
- 停用理由：接线涉及 provider key 解析链大改面（embedding key / 各
  provider key 解析入口 + 配置开关 + 测试），收益低于风险；「支持加密
  密钥库」承诺暂不兑现，如实文档声明避免误导。
- 落地：secret_store.py 类 docstring 标注 DEPRECATED（不得新增生产调用
  方，保留代码作回滚/后续切片参考）；如需启用必须先写设计并接线真实
  消费点。
- 验收：生产 grep 无新消费点；test_secret_store*.py 保留（类未删，测试
  继续有效，验证既有行为不回归）。

## 2026-08-15 attempt 级 UNKNOWN 结构化终态 + 人工恢复路径（双席 seq1947 核对点3 / seq1948 证据4）

【决策】孤儿回收遇 UNKNOWN 工具操作/外部副作用未核实 → attempt 层标
结构化 `unknown` 终态（不再永久 running 无感知），但不释放执行权锁、
不自动续跑；唯一出口 = 人工核对后显式 `recover_attempt_unknown`。

- attempt 状态机：running → unknown（自动发现）→ recovered（人工恢复）。
  unknown ≠ failed（已知失败才 failed）；unknown 不触发自动续跑
  （create_attempt 的 unknown 闸 fail-closed 抛 RuntimeConflictError +
  attempt_unknown_blocked 诊断事件，recovered 前任何自动拉起被拒）。
- 锁语义：_mark_attempt_unknown 保留执行权锁（external 副作用未核实前
  锁防并发写执行权）；recover 事务内 CAS（unknown → recovered）成功才
  删锁 + 写 attempt_recovered 审计事件（含 operator/effect_disposition
  结构化三值：confirmed_noop/recorded/abandoned）。
- 幂等闭环：reclaim 前置 already_terminal（同 attempt 只标一次）；
  orphan_reclaim_blocked / attempt_unknown_terminal 事件同 attempt+reason
  只写一次（孤儿回收每 ~5 分钟一趟不刷屏）。
- 四层保持分开：只动 agent_attempts；run 保持 created（可恢复后同 run
  续挂新 attempt）；task/session 层不受影响。
- side_effect_gate 与 nonterminal_ops 两分支同款（问题C 只补可见性，
  本条目补结构化终态）。

【验收】37 passed（test_cli_resume_contract 含 4 新用例：side_effect_gate
标记、create_attempt 拦截、recover 放行+幂等、活 attempt 拒绝 recover）+
全量 gate 回归。

【待办】真机 zombie 复现四类原始证据（diff/工作树 + orphan_reclaim_blocked
完整字段链 + 重启无重复 claim/handoff + UNKNOWN 人工核对/显式恢复路径）。

## 收口状态机（owner 四改之 2）【状态：设计完成，待实施】

- 详见 `docs/design/closeout_state_machine.md`。
- 摘要：把散在 _final_response_after_*/response_decision/resume_loop/
  gateway 四处的"停下来后下一步"收拢为一个纯函数 `decide_closeout`——
  终态 done/cancelled/wait_human/wait_handoff/resume_round；停止原因采集
  方只报因，承诺文案由 state 唯一决定（"会自动继续"仅当 machine 真的会
  续）。保留 EXEC-30/35/39 语义与 resume 能力（goal/cron 模式用）。
- 参考：会话运行时 无系统侧收口机（模型自然停=done，goal 扩展只做目标层轮次）、
  dsh /goal = goal-round-driver 同款。
- 实施切 5 步（见文档 §6），每步独立提交。

## gateway 移交线删除与自动接力重建方向【状态：已删除，重建待 goal/cron 设计】

- 2026-08-17 owner 拍板删除 gateway 移交线(EXEC-42, 见 ISSUES.md)。
- 结论: 任务不丢靠落盘(task.yaml/run_workspace.json/runtime.db), 不靠
  移交单; 移交单只是"谁自动接力"的中间层, 与 progress policy / goal
  续跑通道重叠; EXEC-39 已定正常不自动续跑。
- 重建方向(goal/cron 模式设计时): 以持久事实为单一权威新建自动接力
  驱动——未完成任务 = task link active + state.json 非终态 + runtime.db
  非终态(现有 unfinished_task_ids 三源交叉已具备), 驱动 = goal-round
  driver(dsh 同款)+ cron tick; 同时解决唤醒轮每 2 分钟全量扫描问题
  (增量游标/单次扫描多消费)。

## 唤醒轮全量扫描治理【状态：已实施 2b——wake_queue 热层+对账分频，收尾项见下节】

- 实锤(2026-08-17 源码核查): BackgroundMainAgentScheduler.tick() 无内部
  节流, supervisor 轮询 1-5s 单飞提交——tick 内 _enqueue_unfinished_task_
  resume_wakes(glob 全部 task link + 全部 tasks/*/*/work/state.json + 每候选
  查 runtime.db)与 _consume_due_policies(全量读 policies 目录)以 1-5s 节奏
  反复全量扫描; 孤儿回收/账本 gc 已有 5min/6h 频率门, 但前两者没有。
- 参考实现(已读源码):
  - 长期助手(cron/jobs.py get_due_jobs + gateway/run.py housekeeping):
    单一 jobs.json 每 tick 全读但内存按 next_run_at 过滤(单小文件, 无
    目录树遍历/无逐候选 DB 查); tick 60s; 文件锁单 tick; catch-up 折叠
    (过期 recurring 只补跑一次并快进 next_run_at, 防重启爆量); 家务
    按 tick_count%N 分频(5min/每小时), 重活不进每 tick。
  - 通道运行时(src/infra/heartbeat-runner-scheduler.ts + commitments/store.js):
    到期即数据——SQLite due_earliest_ms/due_latest_ms 索引 WHERE 查询,
    per-agent 内存 nextDueMs Map(不到期不重查), cadence 由系统 cron 每
    agent 一个 monitor job 携带持久化权威; 事件唤醒过集中 cooldown
    (min-spacing + flood 环形缓冲)。无中央全量扫描。
- 治理方案(定案, 分步实施):
  1) tick 内节流门: scheduler.tick 加最小间隔(如 15-30s 全量 pass, 期间
    只做便宜消费); 2) _consume_due_policies 按 next_due_at 索引只取到期
    (policies 目录单文件化或内存有序索引); 3) unfinished_task_ids 三源
    对账从热 tick 移到低频层(5min, 长期助手 分频同款), 热层只查内存缓存
    的到期任务集, 由事件(任务状态变更/wake)失效刷新; 4) catch-up 折叠:
    漏窗任务补跑一次并快进(我们已有 wake cooldown, 对齐即可)。

## wake_queue 闹钟字条与 clock.sleep 设计【状态：2a/2b/2c 已落地，待真机验证】

- 定案(2026-08-17 owner 确认): wake 表不是"调度任务表", 而是**任务自己写的
  闹钟字条**("我在 T 时刻醒")。写入方两类: 1) 模型主动 `clock.sleep`(对齐
  会话运行时-rs core/src/tools/handlers/sleep.rs, 1s..12h, 可被新输入/事件打断);
  2) reconcile 对账层给 active-goal 任务补 `goal_tick` 字条(EXEC-39 同源:
  普通任务不自动醒)。唤醒方是系统侧 BackgroundMainAgentScheduler 热 tick
  弹到期字条(跨进程唤醒, 与 会话运行时 进程内 sleep 不同——我们回合结束进程退
  出, 不能挂进程等)。
- 表语义(runtime_db/schema.py + repository.py):
  - 每任务一条 pending 字条, (task_id, kind) 幂等 upsert, 新写覆盖旧时间;
  - 到期弹出 = 同事务 SELECT 到期行 + 置 woke, 再派 wake 并 complete/cancel,
    避免同秒双消费; task 终态(completed/cancelled/interrupted 等)或事件提前
    醒(wake signal 消费成功)时 cancel_wakes_for_task 清字条;
  - 清理: 无全表扫描。已弹出字条归 complete/cancel 终态, 由既有低频 gc 与
    唤醒侧检查归档(task archive 时核对)回收, 不新增专扫。
- 已实施:
  1) schema 建 wake_queue 表 + (status,next_due_at)/task 索引, repository
     upsert/pop_due(事务内选+标记)/complete/cancel/list/stale;
  2a) 热 tick 改 `_consume_due_wake_queue`(索引化到期查询) + `_reconcile_
     wake_queue` 降到 5min(只给 active-goal 任务补 goal_tick);
  2b) SleepTool 注册 + `_cancel_sleep_wake_on_event` 事件提前醒桥;
  2c) 睡眠收口闭环 + 字条生命周期:
     - 工具循环自然停出口 `_sleep_wait_closeout_if_asleep`: 本轮最后工具是
       sleep 且纯 ok 收口 → CLOCK_SLEEP_WAITING/clock_sleep_tool;
     - decide_closeout 新增 sleep_wait 态(sleeping 结构化事实, 优先级
       done > cancelled > sleep_wait): 任务非终态、字条保留、不进续跑族;
     - CLI 字条生命周期: sleep_wait 不清字条; done/wait_handoff/wait_human/
       cancelled/预算耗尽清字条; run --resume 入口清字条(用户显式续跑=
       事件提前醒, 会话运行时 sleep 可打断语义);
     - 热层弹出前核 link 终态(terminal/missing 直接作废闹钟, 不复活任务);
       对账只补缺失字条不重写到期时间(否则每 5min 把闹钟往后推, 永远不响);
     - 对账跳过 audit work_kind 任务(自有观察/audit 唤醒通道, 防双重拉起)。
  3) 测试: test_wake_queue + test_sleep_tool + test_scheduler_wake_tick(旧
     task_ledger_resume_wake 契约重写) + closeout truth table(含 sleeping
     维度) + resume 合同睡眠/清条场景, 全部通过。
- 改完后与 长期助手 差异(对比 cron/jobs.py + gateway/run.py housekeeping):
  - 长期助手 是**用户显式排程表**(schedule 工具写 jobs, scheduler 到期跑);
    我们是**模型自报闹钟字条**(sleep/goal_tick 写 wake_queue, tick 弹),
    schedule 工具链(owner 持久 at/every/cron)是另一条独立主链, 不与字条混;
  - 长期助手 tick 60s 全读单 JSON 文件内存过滤; 我们 tick 更热但走 SQLite
    (status,next_due_at) 索引只取到期行, 且 wake 派发本身有 per-task
    cooldown(_TASK_RESUME_COOLDOWN_SECONDS=900) 吸收风暴;
  - 长期助手 catch-up 折叠(过期 recurring 补跑一次快进); 我们字条到期后由
    同一 cooldown + pop 事务的单次消费天然折叠, 不重复派;
  - 长期助手 家务按 tick_count%N 分频; 我们对账层(5min)+孤儿回收/账本 gc
    (既有 5min/6h 门)同构。
- 收尾项(真机): sleep 端到端真实验证(gateway 模式下模型 sleep → 回合结束
  sleep_wait → 到期调度器唤醒续轮), 三源对账分频观测, goal_tick 稳态节奏
  (弹→醒→再武装)真机确认。完成后把上节治理清单状态改为"已实施"。

## 2026-08-18 活动输入与外部消息持久投递收口【状态：本地 focused 通过；真机竞态复验中】

- 对照 会话运行时 `active_turn` 锁，普通补充消息以稳定 client message ID 和首次绑定的 exact turn 为唯一身份。
  Gateway 的 turn transition 与 conversation mailbox guard 按 `T -> M -> receipt` 顺序裁决；全局“当前活动”
  投影冲突、文件暂缺或损坏只能返回 unknown，不能把仍活跃回合的 pending 回执粗暴改成 rejected。
- TUI 在 POST 前写单一 session outbox；提交拿到真实 Gateway ID 后只读 `/input-status` 对账。worker 尚未取得
  canonical ID 时 `running_request_id` 保持为空，本地 `chat-*` 永不冒充 expected turn。active→queued 会冻结
  inject、prompt files、save、resume、客户端能力和一次 chat-style 注入；拒绝后本地排队与服务器排队语义一致。
- guidance receipt v4 读取时从 embedded entry 重算正文 digest、核对 server-owned dedupe key 和状态字段组合；
  v3 只经显式迁移写回 v4，不能因版本条件写反而反复重写或让被篡改正文沿旧 digest 进入模型。
- Gateway 请求文件名是 claim 锁、active turn 与唯一终态归档共用的请求 ID 权威。claim 后若正文 `id` 或
  可选 `request_id` 与文件名不一致，必须在同一 T 锁内规范为文件名、保留结构化 identity error，并由
  worker 在任何 owner 解析和 provider 调用前失败收口；正文不能把已认领文件重定向到另一个回合。
- claim 同时保存 `gateway_request_fingerprint.v1`，指纹覆盖 owner/conversation、prompt/task 和所有会改变
  执行的 options，但排除 lease/status/projection 运行字段。canonical terminal 必须携带并重算该指纹；
  恢复器只能删除与 canonical 指纹相同的 hot 请求，不能因 request ID 相同或最终文案恰好相同就合并。
- inbox→processing 移动与 attempt/lease/fingerprint 栅栏是一个 T 锁转换；栅栏原子写失败时必须在同锁
  内把原文件回滚到 inbox。不得留下 `status=pending` 的半认领 processing，因为 worker 和 stale
  recovery 都不能把它冒充为有效执行。
- worker 收口只认结构化 `committed/already_committed/stale_claim/conflict/retryable_io`；旧 attempt 的迟到
  答复只能得到 `stale_claim` 并丢弃，不能再根据“processing 文件不见了”猜测缺档或从 response 反造终态。
- stop、steer、provider admission/ACK 与 terminal 共用 exact-turn T 锁。reserve/submit 只允许 open 且未 cancel；
  provider 已成功返回后的 ACK 可在同 attempt 的 closing/cancel 状态下先收口 consumed，但 canonical terminal
  已存在或 attempt 已更换时必须拒绝。所有 audit/task/window stop 入口都调用同一个持 T 的
  `_mark_request_stopping_locked`，不得在锁外写 closing。
- 独立 `responses/<id>.json` 和 `done/failed` 永远只是展示投影，不能把 processing
  自动晋升为完成。HTTP `/result`、CLI/TUI/plain 轮询、同步 worker 等待、stale 恢复和普通
  输入对账都只读 `requests/terminal/<id>.json` 中经共享 reader 校验的 schema、文件名/
  内外层 request ID、请求指纹与 `terminal_response`。合法但
  陈旧的 response 由 canonical terminal 全量覆盖；孤立投影是需隔离的损坏事实，不能遮蔽 stale、
  阻止重放或完成回合。旧格式迁移只能走显式隔离工具，恢复和 worker 主链不在线猜测。
- TUI `/btw`、`/stop` 经 `/control` 时携带稳定 message ID 和窗口当前 exact turn ID；服务端 `/stop` 只在
  T 锁内重读到同一 owner/scope 的开放回合后写 closing。观察到 A 后切到 B 的控制请求不能作用于 B。
- Gateway TUI 的有效 slash 控制在 HTTP 前写 session-scoped control outbox；同一行冻结命令、message ID
  与 Enter 时观察到的 exact turn。拿到 `operation_id` 后只能 GET 状态，不能再次 POST；重启会恢复待对账
  行。非 steer 的 `terminal_unknown` 是终态；`/btw` 即使 wrapper 进入 unknown，也继续按同 operation ID
  只读查询已有 guidance receipt，直到 accepted/rejected，不能提前删除 outbox。`/btw`、`/stop` 在
  canonical turn 尚未绑定时明确不发送，避免服务端猜测后来的回合。
- `/ask` 与 `/control` 中所有有效 slash 控制在任何副作用前先写唯一
  `gateway_control_operation.v2` 回执；稳定 operation ID 只由 authenticated issuer/channel/conversation
  和 opaque client message ID 生成，命令正文、expected turn 与权限事实进入独立 digest。同 ID 同正文只
  重放首次结果，同 ID 异正文在副作用前 409；缺稳定 message ID 的外部控制直接 400。`prepared` 可安全
  执行，`executing` 崩溃重启后只能转 `terminal_unknown`，禁止把“响应没收到”当失败后自动重做
  `/compact`、`/goal`、`/audit`、`/verbose` 或 `/stop`。独立 `operation_id` 用于
  `/control-status/<id>`，目标回合继续单列为 `request_id`；`/btw` 的状态查询只读已有 guidance receipt，
  不得借 GET 再追加一次补充消息。若进程在 `executing` 落盘后、guidance receipt 写入前终止，GET 只能在
  同一 T 锁内以 exact turn 的 canonical terminal/closed 事实证明“从未投递”后转 rejected；活动、待恢复、
  缺失或损坏证据都继续 unknown，不能猜测后自动排入下一轮。
- control receipt 还冻结 `channel_chat_type/channel_chat_id` 和首次服务端解析出的 canonical
  `owner_provider/owner_kind/owner_id`；通道事实进入输入 digest，owner ref 使用独立完整性摘要。副作用执行和
  状态查询都只从回执恢复 exact owner，后续切换 owner-scoping 配置也不得重算到另一存储。adapter 对账必须
  从持久 payload 延续 issuer channel/user/conversation/chat type/chat id，
  通过 `X-User-Id/X-Channel/X-Conversation-Id/X-Channel-Chat-Type/X-Channel-Chat-Id` 查询；同一 provider
  message ID 不能通过修改群聊身份重放到另一个 owner。
- Gateway 的 canonical 文件事务锁在 POSIX 使用 `flock`、Windows 使用 `msvcrt` 第 0 字节范围锁；两者都
  是真实跨进程阻塞互斥。平台同时缺少两种标准原语时必须 fail-closed，不能以告警后继续运行的方式依赖
  进程内线程锁而宣称 turn/receipt exactly-once；Windows 非阻塞分支也只把明确 lock violation 当竞争，
  句柄和文件错误必须原样上抛。
- 控制副作用的唯一 winner 仍在 C 锁内从 executing 走到终态，避免没有 provider 幂等键时误重放；重复
  POST 和 GET 使用同一 C 锁的非阻塞探针。锁正在被长 `/compact` 占用时立即返回原子 `executing` 回执，
  不堆积 HTTP 线程；只有锁已经释放且回执仍为 executing，查询者才有权把崩溃边界收成 unknown。
- IM adapter 在媒体下载、Gateway POST 和占位回复之前先写 durable ingress；same-id/same-body 重放，异正文
  quarantine，唯一 worker 继续推进 input receipt、request result、control receipt 和最终回送。
  ingress/reply 已用逐 row owner/epoch/expiry claim 实现跨进程 fencing，旧 epoch 的迟到结果不能写回；
  `/btw` unknown 已按独立 operation receipt 恢复。不支持幂等键的 provider delivery-unknown 仍列为后续风险。

## 2026-08-18 终端交互 TUI 可观察行为复刻【状态：已验收；C17 活动回合输入重开待真机复验】

- 功能规格：`docs/design/FEATURE-20260818-终端交互-tui-parity.md`。
- 逐项账本：`docs/design/TUI_终端交互_PARITY_MATRIX.md`；只有双端 PTY/ANSI 或确定性测试证据才能把
  项目升级为 `VERIFIED/MAPPED_VERIFIED`，源码阅读和肉眼相似都不算完成。
- 产品边界：只修改 `my-agent`，Python/prompt_toolkit 原生实现；终端交互 和 会话运行时 只读。终端交互
  命令、品牌和独有能力不复制，按 my-agent 的真实 dispatcher/ToolRuntime/ActionPolicy/session 合同映射。
- 状态链：`runtime/Gateway typed facts -> TuiEventAdapter -> versioned journal -> reducer -> stable/active blocks
  -> renderer` 是唯一显示主链。worker 不直接打印屏幕字符串，renderer 不从中文/英文正文猜工具、权限、
  中断或完成状态；未知事件 fail-closed 并留下有界诊断。
- 活动语义：thinking/text/tool/permission 都有稳定 `block_id`；delta 只更新 active block，terminal event
  原子冻结到 stable history，一次 terminal 之后不能因迟到/重复事件回退或双显。
- 输入语义：多行、history/search/paste/completion/queue/interrupt/exit 全部产出 typed intent。Gateway
  活动回合中的普通 Enter 复用 canonical `/control` steer，不再错误等待为下一轮；TUI 先按客户端
  `message_id` 固定显示 pending receipt，只有 runtime 在真实 guidance 注入点回传相同 ID 后才转为稳定
  user block。控制竞态拒绝时撤下 receipt，并且只允许同一正文回到既有 canonical ChatJob queue，不能丢失、
  双发或启动平行 worker。真正的 follow-up queue 仍可用 Up 原子回取编辑。
- 2026-08-18 `.13` 真机又证明 `/control` 的传输结果不能只用 bool【状态：已确认问题，方案待用户确认】。
  服务端已持久追加并在 active turn 消费后，HTTP 响应仍可能在客户端 2 秒窗口内丢失；若把该 unknown 当
  rejected，正文会再次进入 follow-up queue。推荐合同是 `accepted/rejected/unknown` 三态，并让同一 owner/
  thread/目标 turn 下的 opaque `channel_message_id` 成为 guidance 持久幂等键：unknown 保留 receipt，以同 ID
  查询或重试；只有 durable explicit rejection 才恰好转 queue 一次。单纯加长 timeout 不是正确性修复。
- 权限语义：UI 只是现有 ActionPolicy/ToolExecutor 请求与决定的交互投影；如果底层缺少续跑合同，先补
  通用 typed continuation，不在 TUI 内执行工具或重放用户 prompt。
- 权限底座已接通：TUI capability 显式声明、Gateway 原子 decision bridge、同 ToolCall 续跑、拒绝/取消
  ledger 和相同 `tool_name + args_hash` 防重复询问共用一条 ToolRuntime 主链；危险硬门仍直接 deny，不能
  为了展示确认框而弱化。
- 终端交互已收敛到 prompt_toolkit alternate screen：多行/反斜杠换行、FileHistory/Ctrl-R、slash/path
  completion、bracketed paste、单槽 stash、canonical queue、双 Esc/Ctrl-C/Ctrl-D、scroll/follow/unseen、
  transcript/search、mouse、resize、title 和 `?` help 都只修改 typed UI/intent state。
- renderer 已覆盖欢迎卡、用户头尾 10k 显示裁剪、Markdown/table/code/diff、thinking 折叠、全局 spinner
  metrics/stall、工具卡、审批 overlay、compact generation 边界和错误/中断；流式 assistant 可见时隐藏
  spinner，工具活跃时不误判 stalled。
- pending steer 与 follow-up queue 按 会话运行时 `bottom_pane/pending_input_preview.rs` 固定在 composer 上方，
  每条最多三行预览；它们不再作为 transcript 尾块随滚动消失。模型可见上下文则只接收 runtime 的数字
  白名单快照，固定显示当前估算、窗口和唯一 compact 触发线。活动回合原生工具 IR 真发生裁剪时另发
  `model_visible_context_compaction.v1` 展示事件；持久 main/child/grandchild 必须先提交同一
  ConversationThread checkpoint/generation，事件再投影该代次且不写 child canonical run。只有
  presentation/no-save 的窗口整理使用 turn-local generation。child 的常驻 `compact N` 始终只读独立
  ConversationThread generation。
- 会话恢复只投影 `cmd_chat` 已从 canonical session 加载的 user/assistant history；TUI 不另读写正文。
  journal、diagnostic、block cache 和 stable display window 均有显式上限，迟到/重复事件仍由 seen identity
  拒绝，裁剪不产生第二会话账。
- Gateway 启动状态必须是实际 readiness 的投影：alternate screen 先出现，后台 preflight 发布
  `connection_started/resolved`，成功后才启动唯一 worker；同步 pre-wait 只保留给 plain 模式。失败以
  typed error 和非零返回码关闭，不能假装已经可连接。
- 空闲动画时钟不应让静态长历史失效。完整 frame key 由 renderer 统一决定；connection/thinking 活动时
  才加入可见动画字段，thinking 周期取 glyph 与稳定词长的最小公倍数，禁止经验魔数。20k block LRU
  和 20k stable display window 是性能边界，不是新的会话事实源。
- TUI 鼠标采用 终端交互 式应用内模式作为唯一默认：`tui_mouse_capture_default=true` 时申请 mouse
  tracking，滚轮直接浏览、离底后保持锚点、回到底部才恢复自动跟随，左键拖选松手与右键都投影同一应用
  选区。F6 只切换当前 TUI process-local 状态，不写配置、不改 session；第一次切到宿主终端原生复制，
  再按恢复滚轮。远程复制仍必须如实区分应用/tmux/OSC52 投影与不可观测的外层系统剪贴板。
- tmux 剪贴板写穿只调用目标版本真实公开的命令：普通终端使用 `tmux set-buffer -w -- <text>` 同时更新
  paste buffer 与外层 OSC 52；iTerm2 因 SSH 崩溃风险只走 `load-buffer -`，再由应用 DCS/OSC 52 尝试外传。
  禁止把不存在的 `load-buffer -w` mock 成成功；最终系统剪贴板仍必须由用户在 attach 终端实际粘贴确认。
- 仅在 TUI 鼠标模式中，transcript 选择才持有当前 viewport 中经 prompt_toolkit `Window` 从屏幕列反解后
  的源字符索引，resize 清除；产品层不得再按 `wcwidth` 二次换算，否则中文会只复制一半。最终 focus 字符
  必须包含在高亮和复制文本中。任何形式的 mouse-up 都结束拖动；1003 无按键 motion 和下一次 fresh press
  负责收口窗口外遗失的 release。松手继续写 prompt_toolkit clipboard、OSC52 和 tmux buffer，Ctrl-C 可
  重复投影；SSH notice 只能说明“已选中并尝试投影”，不能声称外层系统剪贴板已改变，并明确提示 F6 回到
  原生模式。输入 Buffer 的显式选区具有更高 Ctrl-C 优先级。Ctrl-V 只粘贴应用剪贴板，系统剪贴板由终端
  bracketed paste 注入，两条路径都走同一大文本合同并替换现有选区。输入 marker 使用普通空格，禁止用会
  触发 `nbsp` 下划线样式的不换行空格。审批 overlay 继续允许 Page/wheel/Ctrl-Home/End 查看上文，Up/Down
  仍专属于选项导航。
- 审批 y/n 只能匹配 option 的 structured `decision`，不匹配 Yes/No/中文 label。普通工具结果最多六行；
  用户显式进入 transcript 并 Ctrl-E 后才展开全部，避免大输出常驻主视图。
- 终端交互 独有 model picker、permission mode carousel、team/buddy/voice/browser 等不造空壳；帮助和补全
  只列 my-agent 当前真实 dispatcher/键位。该映射属于用户明确允许的“命令/功能替换”，最终逐项记录在
  parity matrix，不用自然语言假装能力存在。
- 参考验收：外部 终端交互 provider 不作为 UI fixture 依赖，使用 loopback deterministic Anthropic
  协议服务驱动黑盒；MiniMax-M2.7 只做 my-agent 真机验收。两类证据分开，key 不进入任何 artifact。
- 测试机边界：仅 `192.0.2.13:/root/my-agent`，tmux `my-agent-tui`；现有 dirty tree 不归本任务，
  每轮按精确文件 hash 部署，并保留 `/root/tui-parity-evidence/baseline-20260818T0043CST` 回滚基线。
- 原验收矩阵共 85 项。2026-08-18 后续四路真机观察重开 C17：旧实现把运行中普通 Enter 错送下一轮，且
  queue preview 位于可滚动 transcript。当前为 37 `VERIFIED`、38 `MAPPED_VERIFIED`、9
  `NOT_APPLICABLE`、1 `IMPLEMENTED`；C17 只有本地确定性回归，必须部署 `.13` 并完成真实活动回合
  插入/结束竞态/滚动观察后才能恢复 `VERIFIED`，不得沿用旧 EV-QUEUE 冒充通过。
- reducer 仍是唯一实例和唯一 handler registry；权限处理器与 Gateway typed-row dispatch 仅以同文件内部
  controller/helper 拆分类体，使 strict code-size 不新增 hard blocker，不产生第二状态机、facade 或 fallback。
- 发布测试频率以改动规模为准：默认 focused tests；生产/测试代码新增、删除累计约 10,000 行以上或用户
  明确要求时，才额外跑一次全仓 pytest。本轮超过阈值，已运行一次到 100%/exit 0，后续不重复浪费时间。
- 待讨论的底座优化候选：以“每次推理吸收的相关新证据量”为观测指标，允许聚合已经就绪且相互独立的
  读取、搜索、检查和工具结果，减少频繁重放增长前缀造成的近二次缓存读取成本。该候选不能写死固定
  100K 批量，也不能跨越权限、失败、用户 steer、工具依赖或分支决策边界；当前仅记录，不占用五路 TUI
  Goal，不修改模型调度主链。
- `.13` 最终 evidence 位于 `/root/tui-parity-evidence/final-20260818T071817CST/final-deploy/`：70 文件
  hash 一致、5 个废弃文件不存在、回滚包齐全，Gateway 在 `/root` canonical workspace 为 running，
  `my-agent-tui:work` 为 120×29 idle；实际 key 字节扫描证据和部署文件均为 0 命中。

## 2026-08-18 TUI 拖选生命周期、终端头像与富工具记录【状态：已完成；沙箱洁净度另行复验】

- 对照 会话运行时 `tui/event_stream.rs` 可见其明确丢弃 mouse event，并通过 raw scrollback 提供复制友好视图；
  本项目需要 终端交互 同类的应用内选择，因此进一步对照 `src/ink/components/App.tsx` 的 lost-release
  recovery 与 `useCopyOnSelect.ts`。既有非空选区只能在左键仍按下时随 `MOUSE_MOVE` 更新；任何 release、
  无按键 motion 或下一次 fresh press 都会结束旧拖动。focus cell 按包含语义高亮，松手自动复制且保留选区，
  普通 hover 不得继续扩展或触发全屏高亮。
- 终端头像属于纯显示资产，不进入事件、状态、prompt、history 或权限合同。宽卡使用固定宽度 styled
  fragments 表达参考图的长兔耳、浅色头发、红色发饰/衣裙、红伞和小兔；窄卡使用同语义紧凑稿。所有行
  必须按显示列居中和裁剪，80/120/140 与窄终端都不能越界。
- 真实复刻测试只允许测试者在 `my-agent-tui:work` 输入一次普通自然语言需求；项目筛选的 star、语言和
  代码量是测试前外部证据，不写进模型控制协议。任务开始后只能观察被测 Agent 自己的工具、权限、日志、
  产物和终态，不能由开发者旁路补代码或把旁路产物计作通过。
- 富 transcript 必须由 `client_capabilities.rich_transcript=true` 显式开启；plain、IM 和其它 Gateway
  客户端保持原有最小公开面。支持的 TUI 才接收逐模型轮 commentary、provider 明示 `type=thinking` 正文和
  工具 `display`。signature、`redacted_thinking`、普通 text 与未知 handler envelope 字段不得冒充思考或
  穿透公开事件。
- 文件和命令展示以 handler 结构化事实为唯一来源：`edit_file`、覆盖式 `write_file`、单/多文件
  `apply_patch` 生成有界行号 diff；新文件生成十行预览；`run_command` 分开投影 stdout、stderr 与退出码。
  renderer 只负责颜色、折叠和宽度，不能从 output 文案反解增删行或成功状态。
- 真实长任务若更早有失败、最后一条工具是注册表声明的成功 workspace mutation，且之后没有任何工具记录，
  主循环只丢弃一次过早收口草稿并注入结构化软提醒，让模型自主选择复核或明确解释。该规则不解析草稿、
  不设置 blocked/unfinished，也不影响简单写文件或已经有后续检查的回合。
- 模型网络故障必须按系统异常事实分类：`ConnectionRefusedError` / `errno=ECONNREFUSED` 与异常链中的
  typed `socket.gaierror` 先走 HTTP 传输层有界退避，再走模型回合级有界恢复；畸形 URL、认证和代理配置
  仍快速失败。富 TUI 从结构化 retry 事件显示层级、序号和等待秒数，不能解析错误文案决定
  是否重试，也不能把重连提示混成 assistant commentary。
- Click→Go 恢复任务证明 rich transcript 与长链收口已可用，也暴露了与界面无关的 workspace 污染：
  Attempt sandbox 曾把当前项目 cwd 放在结构化 task roots 之前，导致 `/tmp` 实际映射到
  `project/.sandbox-tmp`；只读 owner home 又让标准 build cache 首次失败。对照 会话运行时 把 tmp 作为独立
  writable root 的边界，当前通用修复只使用 `task_work_dir`、sandbox write roots 和 XDG 环境事实：
  task work 排为临时根首项，owner-scoped `TMPDIR/XDG_CACHE_HOME` 指向 sandbox `/tmp`。不解析命令、语言、
  文件名或模型正文，也不增加 Go/Click 专项交付过滤器。

## 2026-08-18 写后验证新鲜度与模型调用累计观测【状态：已部署并由真实任务验证】

- Tornado→Go 真机任务证明“最后一个工具是否为写操作”不是可靠的新鲜度合同：测试成功后再写 examples，
  后续 grep/read 会让旧规则误以为已有检查并允许收口。唯一机器事实改为 verification ledger 中的
  `verification_id/status/root/command` 与成功 workspace mutation 造成的 stale 状态；read/search 既不验证
  也不清除 stale。
- 工具结果的验证 envelope 必须写入 canonical `metadata.handler_details`，archive、模型投影和收口读取
  同一位置；不得在 metadata 顶层再造只有写入端可见的旁路字段。Go/Cargo 的规范 build/test 命令由
  `go.mod/Cargo.toml` manifest 结构化识别，不读项目名、prompt 或模型正文。
- 收口提醒仍是软核对，不是普通任务完成硬门：signature 由 stale root 与最近 durable verification event
  ID/状态构成，同一个验证→修改周期只提醒一次；模型执行新的真实验证后才形成新周期并可再次提醒。
- `ModelCallLedger.records()` 继续只保留最近 128 条明细供超时估算和诊断；最终 request/run 统计改由同一
  线程安全 ledger 的有界 scope aggregate 累计 logical turn、物理 attempt、provider HTTP attempt/retry、
  状态、backend/model。累计态不保存 prompt、响应正文、请求体或 key，明细裁剪不得再截断用户可见计数。
- SANDBOX-02 已由同一 Tornado 任务真实闭环：构建临时数据留在 task work，最终 output 无缓存目录；该项
  由 ROADMAP 移入完成。后续 aiohttp→Go 任务在最后一批源码修改后重新 build、跑过 29 项行为测试并完成
  HTTP 200 E2E，验证新鲜度也已闭环；测试者没有旁路补产物。

## 2026-08-18 收口效果权威、会话运行时 式返工与 owner-scoped 构建缓存【状态：代码已修复；真机返工复验中】

- operation ledger 是完整审计事实，不等于“最后一条记录覆盖整项任务”的完成裁决。`not_started` 已经
  结构化证明 handler 未执行、没有副作用，因此末尾连续 no-effect 尝试不能推翻此前最近一项 succeeded
  effect；记录仍保留并使整体 operation 投影显示 partial。只有 `not_started` 且此前没有成功效果时，仍形成
  当前收口冲突；显式 required action 继续由独立结构化 gate 管理。
- 已明确 `failed/not_started` 的末尾效果与模型 final 冲突时，不再立即丢进无工具表达轮。该冲突会把
  `completion_conflict.v1`、最近 typed 终态和被拒绝草稿写回同一 active turn，保留原工具面供主模型定位、
  修复和重新验证；全 turn 最多两次，不能靠换 call id 无限重新武装。若新的 effect-bearing operation 形成
  succeeded 终态，模型自然 final 直接交付；两次仍未解决才按 `OPERATION_INCOMPLETE` 安全收口。
- `unknown/cancelled/incomplete/unverified` 不进入上述通用带工具返工。它们可能已经发生、仍在运行、已取消或
  缺少权威终态，自动重放会破坏幂等/取消边界，因此继续直接 fail-closed；只有工具自己的结构化 reconcile
  能改变 unknown。该分流只读 operation status/error/failure stage/effect outcome，不解析模型完成措辞。
- 该模式直接对照 会话运行时 `会话运行时-rs/core/src/session/turn.rs`：工具调用设置 `needs_follow_up`，结果回到同一轮；
  plain assistant 才自然结束。需要阻止收尾时，`hook_runtime.rs` 的 Stop hook 通过
  `build_hook_prompt_message` 把 continuation prompt 写回同一 turn，原工具能力仍在，而不是另开一轮替换最终
  答案；`core/tests/suite/hooks.rs::stop_hook_can_block_multiple_times_in_same_turn` 还验证了同一 turn 的多次返工。
  my-agent 的两次上限与 unknown 安全门是基于自身 operation 合同的有界适配，不宣称 会话运行时 有同名完成判定。
- owner home 继续是只读身份底图，不能为包管理器扩大写边界。开放世界默认缓存统一由
  `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache` 表达；对已实证不采用 XDG 的标准 npm，仅增加其正式
  `NPM_CONFIG_CACHE=/tmp/.cache/npm`。环境注入发生在凭据擦洗之后，不修改 `HOME`，也不恢复 key。
- 以上均为通用运行时合同，不读取项目名、语言转换目标或模型说明。末尾 no-effect、已知失败返工成功、两次
  返工耗尽和 unknown 禁止自动续做的 focused tests 已通过；真实 Fiber 143 样本确认权威字段为
  `FAILED/COMMAND_FAILED/execution/effect_outcome=failed/return_code=143`。候选部署后的真实返工结果继续记入
  稳定性台账，未完成前不冒充 E2E。

## 2026-08-22 默认 TUI 启动、显式会话恢复与 Gateway 崩溃调和【状态：已部署 `.7` 并复验】

- 普通 `my-agent` 与 `my-agent chat` 都表示新建会话，直接走轻量交互解析器；只有
  `my-agent resume <session_id>` 才能连接明确的旧会话，不存在或越权时 fail-closed。该边界直接对照
  会话运行时 TUI 的 `SessionSelection::StartFresh` 与显式 `Resume(target_session)`，默认入口不得扫描全机
  subagent board、询问是否批量恢复，或把“按 Enter”伪装成实际派工动作。
- 部署默认配置路径由 `MY_AGENT_CONFIG` 统一提供给裸 TUI、完整 CLI 与 Gateway service；显式
  `--config` 仍是单次命令的最终覆盖，避免 Gateway 使用 MiniMax 而欢迎页读取随包 DeepSeek 默认值。
- TUI 客户端不是恢复 owner。普通启动只确认单 Gateway readiness 并连接当前 session；`status` 是用户
  显式请求的只读投影，不调用 lifecycle setter、attempt recovery、dispatch 或进程终止。旧的
  `auto_detect_work_on_startup` 与 `startup_auto_reconcile_crashed_tasks` 已删除，避免配置、TUI 与 Gateway
  同时拥有恢复权。
- 普通 run 的 `recover_stale_attempts()` 移到 Gateway 启动阶段，以进程死亡证明调和 attempt；subagent
  后续恢复继续只走 Gateway 的 `supervise_stalled_orphans`，使用 canonical runner session、心跳、宿主
  PID、conversation lifecycle decision 与 attempt fence。状态页只报告失联 RUNNING session，不再拿共享
  `background_start.pid` 把 `BLOCKED/PENDING` 误称为崩溃卡死。
- 派工巡查锁改为 会话运行时 同类的内核 advisory lock：POSIX `flock`，Windows `msvcrt` 第 0 字节非阻塞锁。
  JSON 只作诊断元数据，不参与归属；空文件、半截 JSON、旧 PID 或 PID 复用都不能形成永久死锁。锁路径
  在释放后保留，描述符关闭或进程崩溃由内核自动释放；`--force-lock` 不能抢占仍被内核确认在岗的持有者。
- `.7` 现场旧 `subagent_orphan_supervision.lock` 为 0 字节且已残留两天，旧实现把“JSON 读不出 PID”保守
  当成活锁，导致每分钟 supervision 都静默 `skipped_locked`。同一现场 25 条旧 PID 告警实际为 5
  `RUNNING`、1 `PENDING`、19 `BLOCKED`；只有 5 条具有失联 RUNNING runner session，其中 3 条满足续跑，
  其余由已关闭 conversation link 的取消收口处理。新锁上线后由同一 Gateway 自然调和，不做批量删除。
- 部署证据：唯一 Gateway PID `1918200` 同时持有 8420 与 v2 advisory lock；裸 TUI 在 1 秒采样点显示
  MiniMax-M2.7 输入框。监督首轮复活 4、按父会话取消 21、回收失联 RUNNING 5，随后 3 条 RUNNING
  自然完成；两次显式 status 返回同一近期计数且没有恢复提示。

## 2026-08-22 固定 Todo/代理区与后台主代理可见性【状态：第一轮真机发现已修，新候选待 `.7` 复验】

- 布局直接对照 终端交互 `REPL.tsx` 的 `SpinnerWithVerb`、`TaskListV2.tsx` 与
  `CoordinatorAgentStatus.tsx`：main 的动态 `Working` 行位于最新正文后、Context/Todo 前；Todo 固定在
  输入框上方；输入框下方只保留直属 child，不再重复 `main`。
- `task_progress(action=update)` 必须先晋升 conversation task，再解析唯一稳定账本 key；否则首条
  Todo 会写到 Gateway request id，派工却读 task-path 指纹，同一任务裂成两本清单。
  `create_subagents` 对显式 `covers` 只沿用已有 Todo id，未绑定 child 才按 exact run id 新建条目；
  `covers` 的 exact id 同时适用于普通 `items` 和 `coverage.targets`，child 进入 canonical DONE 后只按
  结构化 id 回写对应项，不能从标题、goal、路径或完成正文猜映射。
  child 的 `covers` 以有界 `progress_item_ids` 进入只读活动投影，renderer 仅用 typed status
  原位显示 DONE/失败/取消，不从 goal/title 文字猜关联。
- 大派工回执可被工具输出归档，因此有界 `task_progress_seed` 必须同时保留在
  `result_envelope/handler_details`，Gateway 实时事件优先读该结构化快照，不依赖可裁剪的文本预览。
- 生命周期直接对照 会话运行时 `agent/status.rs`、`agent/control.rs` 与 `multi_agents/wait.rs`：只读 typed
  status/notification，不解析“模型已生成回复”“已经完成”等自然语言。终态 child 清除旧 activity；首次
  attempt 隐藏，只有 `attempts > 1` 才显示 `重试 N 次`。
- child token 从 exact run 的结构化事实只读投影，Compact 次数只读 `agent_thread_id` 对应
  ConversationThread generation；持久 native IR reduction 与 transcript 压缩都在该 generation 内提交，
  旧 durable apply/attribute 不回读，TUI 不自行估算或从 token 降幅猜测。后台 main 的最近
  thinking/tool/retry/finalizing 是 Gateway 进程内有界、易失、纯展示快照，不参与完成、派工、重试、权限或恢复。
- 后台主代理的可交付最终正文以普通 `assistant_completed` 进入 transcript，不再伪装成灰色系统通知；被
  delivery contract 抑制或没有正文的内部轮不写用户通知。一次模型轮返回只将 main 活动设为
  `waiting`，不得在 child 仍活跃时写“整理最终回复”；显示层按结构化 child 状态改显“等待 N 个子代理”。
- `.7` 的 `7c052f2` 在 tmux `dsh-p1-pvz-7c052f2` 输入第 1 个原样重任务：第一批请求
  5 个 child 被容量合同整批拒绝后，模型自然改为 4 个，随后又派 2 个整合/服务 child；6 个
  child 全部 DONE，合计约 238.2k tokens、Compact 均为 0。最终 `/root/abc` 有 9 个文件，
  `0.0.0.0:8080` 真实监听且 loopback HTTP 200；但 8 个 Todo 全未勾选，main 仍错放在输入框下方。
  本候选修复已通过 147 项 focused tests，仍须部署后用下一条原样 TUI 任务验收新布局与勾选。
- `.7` 的 `993ce4f` 在 tmux `dsh-p2-mario-993ce4f` 完成第 2 个原样重任务：首次请求、后台 main、
  6 个 child 与全部命令都保持 `/root/dsh-tui-p2-993ce4f`，产物落在 `bbb/`，`0.0.0.0:8082`
  返回 HTTP 200。child 行已稳定显示“玩家控制/物理引擎/敌人AI/道具系统/关卡设计/游戏HTML”、实时
  当前 context 和 Compact；6 个 child 全部一次 attempt 自然 DONE，最后一个完成后约 16 秒唤醒 main。
  同轮也确认三个底层缺口：main context 停在启动快照、Todo 的 canonical 5/5 没投影回 TUI、第二批
  系统生成的 child display name 重新从 1 编号；模型还在容量满/创建失败后自行补写了用户明确禁止主代理
  编写的功能代码。
- 当前候选对照 终端交互 固定状态区和 会话运行时 orchestrator 的 delegated-task 边界：activity schema 升到
  v4，main context、当前 active task 的 canonical Todo 和 child 状态共用一份只读快照；最终后台 notice
  先补终态 Todo 再显示 assistant final。同一 exact parent 的系统 child 名称跨批次连续编号。默认 prompt
  明确把“主代理只能协调/不准自己写”视为当轮执行分工，容量满时等待释放，参数错误时修正重试，不能把
  创建失败解释成静默接管授权。所有变化只作用于通用结构化身份/账本和指令优先级，不读取游戏名或任务
  关键词；138 项直接相关 focused tests 已通过，仍待严格 gate、部署和同一原样 TUI 复验。
- `f5dc695` 真机复测进一步固定职责字段语义：单 child 的顶层 description 可作为职责；批量创建时顶层
  description 仅描述整批，不能扇出到 `items[]`。每项优先显示自己的 description，缺失只退回自己的 goal；
  这一行只回答“该 child 负责什么”，不承载路径、模型阶段或长目标。nested native schema 必须把逐项
  goal/description 说明显式暴露给 provider，不能只在顶层参数说明里约定。
- main Working 与 child row 都采用 终端交互 式固定后缀宽度预算，任意 thinking/职责文本只能在剩余列
  单行截断。自动 child seed Todo 的 exact-id 去重先于 128 行投影上限，并同时适用于 live panel 和最终
  notice；账本不删，普通 Todo 不受影响。
- Todo 默认折叠对照 终端交互 `TaskListV2` 的状态优先窗口，但固定为 4 条：最近完成、当前运行和下一待办
  依次占位；多个运行项优先保留，运行图标复用全局 Working 动画。`Ctrl+T` 只是进程内展开开关，不能重排
  或写回 `task_progress.v1`。
- Todo 更新是 replace-all 快照，不是增量补丁：字段缺失或投影失败时保留上一份有效画面；结构化空列表则
  明确收起旧 Todo。该语义对照 终端交互 `TodoWriteTool` 在全部完成后把展示清单置空，解决最终 notice
  已到达却残留旧 `0/N` child seed 清单的问题；空列表仍然只影响 TUI 投影，不删除 canonical 账本。
- Prompt 3 真机还锁定一条工具结果边界：shell 识别到模型试图直接读取内部 child 状态路径时，安全规则在
  启动进程前返回 `WRONG_STATUS_SURFACE`；这一结果必须显式携带 `effect_outcome=not_started`。这与
  会话运行时 的执行前校验错误经 `FunctionCallError::RespondToModel` 返回当前模型继续改参一致。它只解除
  错误的 unknown 硬停，不放开内部路径，也不改变真正已启动、超时或副作用未知调用的 fail-closed 合同。
- 常驻 Context 行只保留当前总量、窗口占比、主 ConversationThread 已成功 Compact 次数和明确标注的
  自动“压缩点”，例如 `compact 2 · 压缩点 90%`；压缩点不是次数或正在压缩的进度。activity schema
  继续为 `conversation_agent_activity.v5`，main `compact_count` 只取 canonical `compact_generation`；
  prompt/messages/tools 受 provider 协议形态影响，详细构成只在 `/context` 命令展示。

- 对照 会话运行时 `core/templates/agents/orchestrator.md` 后，默认递归 coordinator policy 固定为：实际工作一经
  委派，main 只协调、读取和整合已有产物、测试与汇报；缺口继续 guidance 或 replacement child。失败、
  容量不足和终态都不自动转移实现职责。该约束只写入模型执行上下文，不作为机器状态、权限或验收门。
- `.7` 正式验收必须使用唯一 Gateway 和 `MiniMax-M2.7`，依次执行 `TESTS.md` 的四个原样重型 prompt。
  每次启动、切换或输入 TUI 之前必须先向用户公开 tmux session 名称与完整 attach 命令；测试者只观察，
  不旁路补代码或向被测代理发送技术推动消息。
- `931ee20` 的全新 Prompt 3 真机轮证明短职责和固定活动区正确，但同时证明旧完成 wake 只有“孩子结束”
  通知，没有把 canonical `task.result` / `final_report.md` 交给 main。对照 会话运行时
  `session_prefix.rs::format_inter_agent_completion_message` 与
  `session/mod.rs::forward_child_completion_to_parent` 后，当前完成交接固定为
  `subagent-completion.v1`：宿主 typed status 决定 child 生命周期；自然语言 `completion_message` 只作
  整合证据并按 1000 估算 token 截断；系统生成的 `final_report_ref` 和结构化 declared/artifact refs 提供
  完整读取路径。模型不得从完成正文反推 status、权限、验收或根任务完成。
- 同一 exact root 的普通 `DONE` wake 在孩子收齐及短 debounce 后按当前上下文预算成批交给 main，active
  wake 的 `metadata.events` 保留每名孩子的独立信封；只确认实际选入本轮的成员，新到或预算外事件继续
  pending。失败事件和 Audit source worker 保持原即时专用路径。背景上下文压力可缩短每段字符串，但
  不能按字段顺序丢掉 active wake metadata、evidence refs 或批成员；这是当前 turn typed input 的优先级，
  不新增目录扫描 fallback 或机器质量门。`fd7d2b9` 已部署并证明首批结果能唤醒 main，但正式 Prompt 3
  仍只覆盖 6/8 且误报 终端交互 缺失；后续只从结构化批次覆盖/结果 refs 修底层，不写任务名专项分支。
- Prompt 3 的六份完成信封均已被消费，真正遗漏点是进度账本身份分叉：工具写侧使用
  `task-path:<sha256(task_path)[:16]>`，后台 Task Runtime State 旧读侧使用 durable request id。路径指纹算法
  现只允许由 `agent_core/runtime/task_identity.py` 生成；task_progress 写入、后台读取、dispatch child
  终态同步、Goal continuation 和 TUI activity 都必须调用同一 helper，禁止调用方复制 hash。该统一只让
  当前计划进入模型可见 typed state，不把普通 open Todo 升为机器完成门、产物验收或自动续跑授权。
- Prompt 4 r5 在 Todo `4/18`、模型自己明确列出大量缺口且仍有可用工具时自然 final，证明“如实列出
  未完成项”不能代替 会话运行时 的持续完成纪律。当前默认主/子代理执行提示统一采用 会话运行时 的软语义：只要
  已知目标仍有未完成部分且现有工具或下级还能推进，就继续实现、修复和验证；只有目标端到端完成、用户
  明确暂停/改向，或存在当前无法消除的真实阻塞时才结束。`task_progress` 读取回执可重复这一软提醒，但
  宿主仍不因 open Todo 自动重开模型、不解析 final 文案、不恢复机器质量验收。
- 系统生成的 display name 是未来 TUI/Web 精确控制旁边的人类可读稳定标识：同一 exact parent 下，批量、
  单个补派以及递归 child 都必须沿全部历史直属 sibling 连续编号；显式自定义名保持原样，run_id 仍是唯一
  机器身份。Prompt 4 r5 两个单独补派 worker 都显示 `agent-d1-worker`，确认旧编号器只覆盖批量入口。
- Prompt 4 r6 进一步证明 `items` 长度为 1 也仍是批次语义：`create_run_params` 已把省略名展开成无编号
  `agent-d1-<role>`，展示层必须把这个精确系统 stem 继续编号；不能把任意 `agent-d*` 自定义名都改写。
  显式幂等合同比较结构化 parent/role/scope/identity，系统 ordinal 只作展示、不应让同一幂等请求失配；
  用户自定义名仍要求完全相同。
- `context_scope=isolated` 是表达层的 typed 边界，不是另一个可观察 task turn。对照 会话运行时 只把当前 active
  reasoning item 交给 TUI 的做法，该调用仍进入 model-call/cost ledger，但其 provider thinking、context
  usage 和 child activity 均不得覆盖 main/child 的 durable 投影。r6 的 74.4k→9.7k 且 `compact 0`
  正是表达轮污染，不是一次真实 Compact。
- Todo 标题只从 canonical typed status 计算 `完成 X/Y` 与可选 `进行中 Z`；四行 collapsed window 只在
  有隐藏项时显示 `Ctrl+T 展开`，不得再用 `4/8` 表示可见行数。直属 child 若仍处于 typed
  `PLANNING/PENDING/RUNNING`、却没有 explicit `progress_item_ids` 映射到当前可见 Todo，标题另显示
  `子代理运行中 N`；它只解释下方面板为何还在工作，不能重开/改写 Todo，也不能从名称、goal 或输出猜映射。
  该选择对照 终端交互 `TaskListV2` 的 done/in-progress/pending 分组，同时保留本项目固定四行窗口，不写回账本。
- Prompt 4 r7 证明“Todo 全打钩 + 弱测试全绿 + model natural final”仍可能建立在被主动降级的证据上：
  main 最初把完整复刻拆成“空壳可导入/最小欢迎页”，`pip install -e .` 与真实 app 构造失败后，又删除
  能暴露启动缺依赖的 `test_app_instantiation` 并把其余断言降成存在性检查，最后宣称完整可运行。对照
  会话运行时 `gpt_5_1_prompt.md` / `gpt_5_2_prompt.md` 的“端到端解决后才结束、失败工具继续推进、运行/测试
  验证”后，本项目把范围保真与验证纪律并入同一 `DEFAULT_EXECUTION_PERSISTENCE`，由 root 默认 prompt、
  普通 child runner 和 child lifecycle wake 共用：委派不缩小原目标；骨架只能算阶段；用户可见入口失败
  必须修正后重跑；`|| true`/`|| echo` 的外层零码不能证明内部成功；有效失败测试不得仅为变绿而删除、
  skip、放宽或改成只测存在。它仍是 会话运行时 式模型软纪律，不扫描项目、不按 LOC/Todo 判完成、不执行测试、
  不重开 final，也不恢复已经删除的宿主质量验收器。

## 2026-08-22 主/子/孙代理统一 Conversation Compact

状态：child thread 与工作区继承已推送、部署；>40k 功能行真实 TUI 又抓到 mid-turn 原生工具历史
已从 113.1k 降到 35.5k、但 canonical generation 仍为 0。统一账本实现与 focused 回归已完成，待严格 gate、
推送部署和全新 TUI 验收。

- 解决问题：长时间工作的 child/grandchild 与 main 一样会经历多轮工具调用、上下文压力、崩溃恢复和继续
  运行。旧实现中 main 使用 `ConversationThread summary/cursor/generation/checkpoint/CAS`，task-local child
  使用 run workspace 的 `memory_archive compact_applies`，同时还有 active-turn native IR 裁剪；三种事实
  的计数和恢复边界不同，曾产生“child 上下文明显下降、界面仍显示 compact 0”的真实错误。
- 对照依据：会话运行时 `会话运行时-rs/core/src/session/turn.rs::run_turn` 对每个 Session 都执行
  `run_pre_sampling_compact`，并在同一 turn 的 token 状态触发 `run_auto_compact`；
  `tools/handlers/multi_agents_v2/spawn.rs` 通过 `spawn_agent_with_communication` 为 child 返回独立
  `new_thread_id`。因此成熟语义是“每个代理一条 thread、全部调用同一 Compact 状态机”，不是主子代理
  共用一份消息，也不是子代理另造一套压缩器。
- 已落地合同：main、child、grandchild 各有稳定 `agent_thread_id`，父子 lineage 继续由结构化
  `run_id/parent_run_id/root_run_id` 管理；每条 thread 独立保存 transcript、summary、cursor、generation、
  checkpoint、operation evidence 和失败熔断。触发阈值、token estimator、候选校验、checkpoint-before-CAS、
  pre/mid-turn 续接和 TUI 计数全部复用 `conversation/compact.py`，但任何代理都不能读取兄弟或父级正文。
- thread 身份分层必须与 会话运行时 一致：继承的 `conversation_thread_id/conversation_task_id` 继续表示父会话的
  workspace/task lifecycle；新建的 `agent_thread_id` 只表示 delegated agent 自己的模型 transcript。二者不能
  覆盖或互绑，父子关系由 `parent_agent_thread_id` 和 run lineage 表达。首轮 Prompt 4 真机测试曾把 child
  thread 写回 `conversation_thread_id`，导致工作工具尝试把父 task 重新绑定到 child thread，并以
  `already bound to another conversation thread` 失败；当前修复已用真实写工具回归锁定父 link 不变。
- cwd/权限同样按 会话运行时 child spawn 继承 active turn config：根 main 创建 child 时，当前会话的
  host-validated `conversation_execution_cwd/conversation_runtime_workspace_roots` 必须进入 child task
  attributes、execution context 和产品写根；shared Gateway manager 的仓库根只是无会话任务的 fallback。
  `output_files` 继续只表达交付目标/验证线索，不负责授予普通 child 当前项目权限。第二轮 Prompt 4 正是因
  main 省略该字段而暴露断点；回归现已覆盖“无 output_files 仍读写 client project”。
- 实现边界：`ConversationThreadStore.ensure_agent_thread` 以 host 生成的 exact id 幂等建线程，不写 channel/
  latest-user 索引；child 每次尝试按 `conversation_request_id` 先落 user、轮前 Compact、注入 summary/raw
  tail、模型运行、终态落 assistant。provider overflow 在同一 attempt 内强制 Compact 并携带 typed tool/
  guidance 进度重试，最多八次；没有 generation 或真实进展时明确失败，不盲目重放。
- 收口边界：task-local 权限/Memory 隔离保留，但结构化 transcript-authoritative 标记让 durable Compact 只由
  ConversationStore 接管；旧 `compact_applies/continue` 不再由正式 child runner 生成。TUI、Web 和 SQLite
  投影只读 child thread generation/checkpoint；允许持久化的 native IR reduction 也必须先写同一 Compact ledger
  checkpoint 并推进该 generation，而不是写 canonical run 或另加显示计数。只有 presentation/no-save 回合的
  窗口整理保持临时事件。旧投影字段和旧 ledger 不设长期双读。
- 本地验收：覆盖 child/grandchild 独立线程、父子正文隔离、轮前 transcript Compact、运行中 main/child
  native IR generation 1→2、上一代摘要合并、精确 ToolCall ID checkpoint、provider overflow forced
  checkpoint、摘要失败与 checkpoint-before-CAS 冲突回滚、owner Memory 不污染、旧 apply 目录不生成，
  以及遗留 reduction/ledger 不能改变 TUI 次数。真实 MiniMax-M2.7 长任务仍需部署后通过新 TUI 验收。
- 压缩策略与单次回合权限分层：`compact_trigger_tokens` 始终投影同一配置压缩点；
  presentation/no-save 回合不允许持久 apply 时，只把当轮强制压缩的有效硬限放到完整窗口，
  不得把公开策略改写成 100%。这与 会话运行时 分开 `auto_compact_scope_limit` 和
  `full_context_window_limit` 的口径一致；前端不硬编 90。

## 2026-08-22 单 Gateway 服务身份与 thread cwd 分离【状态：`993ce4f` 真机通过】

- 一个 local owner 只有一套 Gateway/adapter service identity：默认固定在
  `owner_home/workspace/runtime/services/`。pid、heartbeat、queue、HTTP 端口不能再由 TUI cwd 选择；
  显式相对服务路径也以 owner workspace 解析。
- 项目目录是每个 durable thread 的 typed 配置。TUI/CLI ask 携带绝对 `workspace.cwd/roots`，Gateway
  校验后写入 `conversation_thread.v7`；后续未覆盖的 turn、后台 main 和 child 继续继承。远程 owner
  不能提交主机 cwd，非法目录在模型前 fail-closed，不得退回 daemon cwd。
- Tool Registry 的授权、审计 scope 和实际 handler 只接收同一 effective cwd/root；任务交付与子代理相对
  output ref 读取同一会话字段。该适配对照 会话运行时 thread/turn 的 `cwd/runtime_workspace_roots`，没有新增
  prompt 关键词、第二 Gateway 或 TUI 专项路径分支。

## 2026-08-23 会话运行时 式工作区并发与 owner 隔离分层【状态：运行时锁已落地；显式批次冲突反馈本地候选】

- 普通 shell、文件写入、patch 和子代理派工的 `workspace:*` 只是当前轮调度/审计事实，
  不再进入跨 run 持久 `resource_locks`。当前 turn 内的 barrier、operation 幂等/replay、
  sandbox 与 write boundary 继续生效；精确 `logical:*` 控制面资源仍可持久互斥。
- `output_files/output_refs` 是可选交付元数据，不是文件所有权。runtime 不再因既有同级/父子 run 的路径
  重叠加锁，也不会把活跃 child 的申报产物追加到 `locked_files`；父子继续共享 cwd。后续 r28 真 TUI
  证明同一次批量调用已主动声明 `/internal`、`/internal/core`、`/internal/param` 时仍放行会直接制造冲突，
  因此只对该调用内显式 `output_files` 的相同或祖先关系返回 not_started；不检查未声明写入或历史 run。
- 成本观测复用唯一 `ModelCallLedger`：供应商返回的 input/output/cache read/
  cache creation 按 request/run 累计，并随 `AgentRunResult`、Gateway result 与 runtime fact
  持久。无 usage 的调用使用调用前 input 和输出估算，并单独计数；TUI 的
  `current_context_token_estimate` 仍只是当前上下文压力，不得冒充累计消耗。
- 多用户隔离不依赖上述目录锁：远程 provider 用户仍强制进入各自 `owner_home`，数据库、
  memory、task、run、artifact 与 audit 分开；owner path wall、结构化写根和进程沙箱仍是硬边界。
  本地可信 CLI/TUI 则像 会话运行时 一样使用启动时项目 cwd，并由 thread 持久继承。
- `a091b72` 真机证明服务身份和 thread v6 主链生效，但薄 TUI 首次提交把 audit Agent 置空时也丢了 cwd。
  当前补丁将 audit 与 workspace 分参：客户端 config 是 cwd/roots 的唯一来源，Gateway 继续校验；不构造
  第二 Agent，也不回退 daemon cwd。真实提示词 2 复验必须使用全新项目目录和同一个 Gateway。
- `0ffbfe4` 的真实提示词 2 已证明首次 `gwreq-*` 和三条 direct child 都使用正确 TUI cwd；随后
  `subagent_runner_finished` 自动 wake 生成的 `run-*` 却只恢复 thread/task id，未恢复 thread v6 的 cwd，
  使相对 child output 回落 `/root`。二次候选按 会话运行时 每轮 `TurnContext` 及 spawn runtime override 的同一
  层级，在后台 `RunParams` 构造时复制本轮加载的 `ConversationThread.cwd/runtime_workspace_roots`；task
  workspace 仍只是隐藏状态目录。该修复只读结构化 thread 字段，不匹配模型生成路径或任务文本。
- `993ce4f` 部署后的全新提示词 2 已同时证明首次 ask、后台 `run-*`、6 个 child 和工具 working_dir
  全部继承 TUI cwd；没有再写 `/root/bbb`、`None` 或其它隐藏回退目录。本条 cwd 主链因此完成，后续
  继续在每个正式重任务中把路径对账作为回归证据，不再保留第三条兼容入口。

## 2026-08-22 lifecycle wake 的原生 active-turn 工具交接

状态：本地已实现并通过 focused 回归；待严格 gate、唯一 Gateway 部署与 Prompt 4 r12 真 TUI 验收。

- 解决问题：r11 证明 exact objective/cwd 已恢复，但 root 醒来后仍重复建 Todo、漏 `covers`。结构化证据
  显示 durable tool index 将 `task_progress.items` / `create_subagents.items` 裁成空数组，且 native builder
  刻意旁路机械 tool-context，模型只看到“当前状态”，看不到“自己已经如何走到这里”。
- 对照决定：会话运行时 在同一 active turn 内持续保存历史；本项目跨进程 wake 无法无损恢复原 assistant 内容与
  provider tool-use 签名，所以不能伪造 ToolCall/ToolResult。采用 会话运行时 Compact 同类 replacement item：
  canonical 索引继续掌握执行事实，模型只得到一条有界 `CompactionSummary` handoff；后续真实 UserTurn
  排在它之后，时间顺序不变。
- 参数合同：工具调用参数允许 JSON 标量/list/dict 递归至固定深度和宽度，敏感字段与已知 secret 统一脱敏；
  未知对象不 stringify。这样保留通用批处理、Todo、covers 和其它 schema 关系，不为某个测试 prompt 写专项。
- 计划合同：后台 Task Runtime State 投影唯一 ledger 的 existing/open exact ids、完整 read 参数和
  `create_subagents.items[].covers` 路径。该投影只指导模型复用现有计划；宿主不做标题相似度匹配、不自动
  合并语义重复项、不把 Todo 变成完成门。
- Compact 口径：handoff 复用现有 semantic-summary 字符预算，超限保留顺序索引、语义摘要、近期明细和
  archive refs。它不提交 checkpoint、不推进 generation、不增加 TUI compact 次数；真正的上下文压缩仍只
  走 ConversationThread 的 checkpoint-before-CAS 主链。

## 2026-08-22 计划内派工的 exact-id 与写入集合原子合同

状态：显式 exact-id 校验与错误分类仍保留；r14 删除旧 goal 文本自动补绑。下述“covers 与完整写入集合
必填”设计均已被 2026-08-23 的 会话运行时 式可选映射取代，保留本节只为说明 r12b-r14 的历史演进。

- 解决问题：r12 已恢复同一 active turn 的原目标与工具历史，但模型先建了 Todo，随后派工仍省略
  `covers`，又把新项目兄弟目录只写进 child `goal`。旧入口先创建 child、事后只给绑定 warning；运行时
  才发现目录不在父级结构化 workspace，child 进入 capability 阻塞，main 又反复尝试无法批准的越界 grant。
- 对照决定：会话运行时 的 `update_plan` 与 `spawn_agent` 是两项独立结构化动作，spawn 只接收具体、可独立完成
  的任务，并要求并行编码者使用互不冲突的写入集合。本项目适配为：当前 canonical `task_progress` 已有
  计划时，每个新 child 必须用 `covers` 绑定一个仍 open 的 exact id；有写工具且角色直接产出产品代码的
  child 还必须用 `output_files` 声明本次写入集合。两项都在任何 run 落盘前整批校验。
- 合同边界：未知、已关闭、重复绑定的 id，缺失写入集合，或解析后逃出直接父级 structured workspace 的
  输出路径，都返回同一个 `effect_outcome=not_started` 结构化修复回执；整批一个 child 也不创建。模型应
  修正 Todo/`covers`/`output_files` 后重试，不能把失败当作改写用户目标或父代理自行编码的授权。
- 这不是机器质量验收：宿主不解析 goal、Todo 标题、代码量或完成文案，不判断任务是否做得好，也不因
  open Todo 自动续轮。没有 canonical 计划的普通轻量派工继续沿既有宽松入口；read-only、tester 与
  coordinator 等不直接拥有产品写集合的 typed 角色不被强迫声明代码输出。
- capability 恢复继续服从现有安全围栏：请求目录在父级 workspace 之外时不能 grant，应 `deny` 并唤醒同
  一 child 回到已有写区；创建前的新合同应使这种越界请求成为异常兜底，而不是常规派工路径。
- fresh r13 首次命中发现新报码虽保留在 `reported_error_code` 与 JSON 正文，控制码却因错误分类表漏登记
  降成 `UNKNOWN_ERROR`。该码必须进入唯一 `error_taxonomy`，语义为 orchestration、可重试、
  `repair_tool_arguments`；这保持 会话运行时 式“把可修工具错误回送同一 turn 返工”，不能另加专项重试器。
- fresh r14 暴露了合同前残留的自然语言旁路：骨架 child 的 goal 只是列出 `src/i18n/` 与 `src/config/`
  目录，旧代码便自动写入 `covers=["i18n","config"]`，完成后错误勾掉三项 Todo。该旁路与本节“不读
  goal/标题”冲突，也不同于 会话运行时 的显式 spawn 参数，现已连同 `covers_auto_bound` 投影和测试一起删除；
  Todo 绑定只认调用参数中的显式 `covers`。

## 2026-08-23 根代理 assumptions-first 自主决策软纪律

状态：`b7005a8` 已发布部署；fresh Prompt 4 r16 已证明 root 会自主选择 Rust、建立 Todo 并创建 child。

- 解决问题：`bdcc7d1` 部署后的 fresh r15 在固定 lazygit 源码上只读了项目便停止，要求用户在 Rust、
  Python 或其它语言中选择；没有 Todo、没有 child、没有功能写入。用户已经授权“换一种编程语言”，
  语言属于安全可逆的次要实现选择，不应把决定退回给无人值守测试者。
- 对照决定：会话运行时 `collaboration-mode-templates/templates/default.md` 明确要求优先做 reasonable assumptions，
  只有无法从本地上下文发现且合理假设有风险时才问；`execute.md` 更明确要求信息缺失时选择 sensible
  assumption 后继续。相同 MiniMax-M2.7 的 会话运行时 真 TUI 虽先探索源码，最终也违背自身模板给出语言/范围/
  测试菜单，说明这是模型在超大任务上的行为回避，不应照抄该次坏输出。
- 落地边界：会话运行时 源码语义被适配为根默认 `system_prompt` 前部的通用中文软纪律。它只要求先查本地事实、
  对安全可行方案选合理默认并继续；只有实质偏离、越权或不可逆风险才允许问一个关键问题，且不能用多选
  菜单代替工作。规则不包含语言名、项目名或 Prompt 4 文本，不解析模型问句，也不新增重试器、完成门或
  机器验收。
- 配置权威：这不是第二套模式开关；既有 `system_prompt` 就是唯一用户配置入口。发布 YAML 与 dataclass
  默认文本继续逐字一致，显式自定义 prompt 仍有最高权威。

## 2026-08-23 子代理直接 goal 边界与可选产物提示

状态：`4c3a59d` 已发布部署；fresh Prompt 4 r17 证明首名骨架 child 不再替兄弟扩做，但又暴露 mandatory
`covers` 在返工场景会诱导错误 Todo 绑定，后续结论见下一节。

- 解决问题：`b7005a8` 部署后的 r16 已通过 assumptions-first 与 exact `covers` 主链。第一名骨架 child
  只被要求创建 6 个基础文件，却额外写了 11 个文件；其中 `src/gui/views.rs`、`src/gui/state.rs` 随后又
  被 root 明确分给第二名 child，形成真实的兄弟写冲突。第一名的模型报告也主动列出了这些额外文件，
  所以这不是观察误差或 TUI 文案问题。
- 根因：child runner 复用了 root 的“委派不会缩小用户原始目标”提示，使 child 同时看到根目标和直接
  `goal` 时容易替兄弟扩做。原先强制模型预报完整 `output_files` 也没有形成事实边界：该 child 只声明
  6 个目标，却实际多写 11 个文件，说明模型自报写集既不完整也不能承担安全权威。
- 会话运行时 对照：`会话运行时-rs/core/src/tools/handlers/multi_agents_spec.rs` 与 `multi_agents/spawn.rs` 的 spawn
  参数只有具体任务输入、模型等执行字段；child 继承当前 cwd/runtime，没有“先报完整写集”合同。r17
  进一步证明 Todo 映射也不能作为每次 spawn 的硬前置；当前设计只保留显式 exact `covers` 的校验和投影。
- 落地边界：root 继续对用户完整目标负责；child 的直接父级 `goal` 是其本轮完整工作边界，要求完整完成
  该 goal，但不得因为根目标更大而实现未交给自己的兄弟计划项。`output_files` 改为可选交付/冲突提示；
  一旦提供仍必须位于父级 workspace，也仍可参与已有的冲突提示，但它不是权限、完整写集或质量验收。
- 这是 会话运行时 式软执行纪律，不是新的机器审查器：宿主不解析 goal 去判“是不是兄弟任务”，不扫描 diff
  自动取消 child，也不恢复机器质量验收。真正的权限上界仍来自父级结构化 workspace；r17 必须用原样
  Prompt 4 验证 child 会停在直接 goal 内、root 能自然醒来再派后续项。

## 2026-08-23 可选 exact covers 与返工重开

状态：通用协议与聚焦回归已落地，待严格 gate、发布和 fresh Prompt 4 r18 真 TUI 验收。

- 真实证据：`4c3a59d` 部署后的 r17 中，首名骨架 child 只创建骨架，TUI child 只实现 UI 范围，说明直接
  goal 边界改善。第三名 Git child 却把 Rust 代码写到源码 cwd 根目录，而不是现有 `rust-port/`；root
  正确决定返工。此时 Git Todo `3` 已因首名 Git child DONE 被关闭，下一 open Todo `4` 是 GUI。
- 确定性失败：mandatory covers 门不允许返工 child 省略 covers，也不允许绑定已关闭的 `3`。root 为了
  让创建调用通过，把新的“Git 命令封装实现”child 绑定到 `covers=["4"]`；TUI 因而把“GUI 控制器层”显示
  为进行中。若继续，Git child DONE 会给 GUI 假打勾。现场已只用 TUI `/stop` 收口：root `PAUSED`，前三名
  `DONE`，错误绑定的第四名 `CANCELLED`，无残留 runner。
- 会话运行时 对照与决定：会话运行时 `spawn_agent` 不依赖 plan id。`covers` 改为可选 exact 映射：提供时继续原子
  校验未知、已关闭和同批重复 id；省略时 child 正常创建，并由已有 seed 主链按真实 run id 形成独立进度
  行，绝不关闭现有 Todo。`output_files` 继续保持可选提示。两者都不是权限、完整写集或机器验收。
- 返工协议：确实需要把返工仍归入原计划项时，先调用 `task_progress` 对同一 id 传
  `status=in_progress, correction=true` 明确重开，再绑定原 id；也可以省略 covers。模型规格、task_progress
  软 guidance 与 typed repair 都明确禁止拿无关 open id 顶替。宿主仍不解析 goal/title 做语义匹配。
- 协议版本：planned dispatch 投影升为 `planned_dispatch.v2`，使用 `binding_mode=optional_exact` 与
  `unbound_item_indexes`；旧 `missing_covers_indexes` 删除，避免把“未映射”继续表达成错误。

## 2026-08-23 普通计划的 会话运行时 式同轮停止核对

状态：`e94f8ec` 已通过严格 gate、发布并部署；open-Todo 直接真 TUI 证据待自然样本。

- 解决问题：fresh Prompt 4 r18 中，root 自己建立 8 项 canonical Todo，只完成 5 项且把测试三项保留为
  `pending`，最终回复仍声称“代码已完整生成”；会话任务和 task workspace 随后都被宿主写成完成。现场
  结构化账本明确记录 `next_action=等待 Rust 工具链安装后继续编译验证`，说明错误不是用户看漏了，而是
  普通模型 final 被直接等同整个 durable task 完成。
- 会话运行时 对照：`会话运行时-rs/core/src/session/turn.rs` 在模型不再请求工具时先执行 `run_turn_stop_hooks`；若钩子
  返回 `should_block`，宿主把结构化反馈记录进同一 active turn 并继续采样。最终的 `TurnComplete` 只是这次
  用户回合生命周期事件，不是业务目标验收结论。本项目复用同一模式，不恢复旧 delivery/acceptance 扫描器。
- 新合同只核对模型自己写入 canonical `task_progress` 的结构化一致性：没有 open 项时自然结束；仍有
  `pending/in_progress/unknown` 时，在同一工具循环最多返给模型一次 exact id/title/status，要求继续用原工具
  推进，或把真实无法推进项明确更新为 `blocked` 并说明原因。它不读取最终正文、不扫描代码、LOC、测试、
  目录或产物，也不替模型判断工作质量。
- 活跃直属 child、命名 Audit、真实工具失败/未知副作用等已有 typed 生命周期与安全修复先处理；计划核对
  只在这些专用分支都未接管自然 final 时运行。默认次数通过唯一配置控制，`0` 可关闭，避免无界增加模型调用。
- 一次同轮核对后仍有可执行 open 项，或清单只剩显式 `blocked` 项时，本轮以 typed `blocked` 结束，保留
  模型撰写的诚实说明，不把 conversation task / standalone workspace 写成 `DONE`，也不偷偷创建第二个
  `ordinary_task_resume`。用户后续消息仍可在同一 thread/task/workspace 继续。
- fresh Prompt 4 r19 在 final 前主动把 8/8 Todo 全部关闭，因此没有产生核对事件；不能把“钩子未触发”
  写成直接真机通过。r19 同时证明 112 个自建测试可能与真实启动相悖：生成 App 没有挂载 Screen，进程
  存活但整屏空白。该反例不改变本条边界：宿主不按 LOC、测试数量或界面内容决定业务完成；后续改善必须
  让模型基于结构化工具事实维护计划和验证实际入口，而不是恢复第二套机器验收状态机。

## 2026-08-24 no-save 主任务仍必须经过同轮停止核对

状态：r9 真实失败已定位，本地修复、focused 回归与严格 gate 通过，待发布和原样 TUI 复验。

- `save` 只表示是否写普通运行档案、长期记忆和相关可选持久化，不是 turn/task lifecycle authority。
  Gateway/TUI 和 `background_main_agent` 为避免重复 transcript 正常使用 `save=False`；它们仍是 exact
  durable task 的真实 active turn，不能因此跳过 canonical `task_progress` 停止核对。
- r9 的后台 main 在 task-path ledger 仍有 1 个 `in_progress`、2 个 `pending` 时输出完成，随后 task link 与
  workspace 被写成终态。最后一轮结构化工具账没有任何 `task_progress` 更新；已发布入口因
  `_eligible_closeout(... save=False)` 被整轮短路。这是生命周期开关误绑，不是账本 id 漂移，也不是需要恢复
  代码量、测试、产物或自然语言完成验收。
- 修复只解除 save 与停止核对的耦合；exact task-path 账本、同轮一次 repair、耗尽 typed blocked、显式
  `/goal`、Audit、辅助 scope 和真实工具失败优先级均保持。回归必须同时覆盖 Gateway save/no-save 与后台
  no-save，并证明 blocked 后 conversation task 仍 active。

## 2026-08-23 会话级模型用量账本

状态：本地实现与 focused 回归已通过，等待随下一批底座修复统一发布和真机复验。

- `ModelCallLedger` 的兼容累计值继续保留，但计费视图必须把供应商返回的 input/output/cache read/
  cache creation 与本地估算分栏；缺少 provider usage 时只能写进 `estimated`，不得用估算反填供应商真值。
- 每次 finalization 都把冻结后的 `model_call_summary.v1` 以幂等事件追加到 exact owner-scoped
  `ConversationThread` 的 `model_usage/<thread_id>.jsonl`。后台 main 的 `do_save=false` 只表示不写普通
  transcript/runtime archive，不得因此丢失已经真实发生的模型调用成本。
- 事件只保存 thread/request/run/task/source identity 和数字计数，不保存 prompt、response、key；同一
  event id 重放必须与首次载荷一致，冲突或损坏显式失败，不能把无法读取的成本当成 0。
- `current_context_token_estimate` 仍表示当前一次上下文压力；会话成本汇总只读取 append-only 用量事件，
  两者不能互相推导，也不参与 Compact、生命周期、完成或权限裁决。

## 2026-08-23 单 Gateway 多 TUI 接入背压与断线退避

状态：本地候选已通过 focused 回归，待部署 `.7` 和真实多 TUI 复验。

- 解决问题：真实并行 TUI 对照中，历史窗口与当前窗口都以 250ms 周期请求
  `/client/notices`；Python 标准 HTTP server 的等待连接队列只有 5。几十个窗口在 Gateway 忙时会形成
  `SYN-SENT` 堆积，Gateway CPU 升高，新窗口看起来像“主代理停止响应”。这不是 workspace/owner 锁，
  而是展示面反向压垮唯一运行入口。
- 会话运行时 对照：`会话运行时-rs/app-server/src/thread_status.rs` 通过 `watch` 与 server notification 在状态变化时
  推送；`app-server/src/transport.rs` 对每个连接使用有界发送队列，慢连接队列满后断开，不让展示客户端
  无限反压运行时。本项目本轮不同时更换整个客户端协议，先在现有 HTTP 兼容面落同一背压原则。
- 接入合同：单 Gateway 的 accept backlog 固定为 128；请求线程为 daemon，关闭时不等待失联 TUI。
  TUI 首次仍立即取快照，成功后每秒刷新；HTTP、解析或 `ok=false` 按 0.5、1、2、4、8 秒指数退避，
  下一次有效快照立刻恢复 1 秒周期。失败快照保留上一份真实活动，不得伪造“任务已结束”。
- 权威边界不变：该接口仍是 owner/thread 鉴权后的只读投影，不产生任务、状态、完成、权限或恢复事实；
  本地可信 TUI 的 cwd 与远程用户 `owner_home` 隔离也不因传输优化改变。后续若升级为事件长连接，仍复用
  同一 canonical activity/notices，而不是建立第二份前端状态账。

## 2026-08-24 TUI 进程退出与 durable session 分离【状态：已部署并完成 `.7` 真机】

- 终端交互 对照把三层事实分开：`~/.模型助手/sessions/<pid>.json` 只登记仍活着的 REPL 进程，普通
  `/exit`/Ctrl-D 会走 graceful shutdown 并删除 PID 登记；聊天 transcript/session 继续持久化，可用
  `--resume` 恢复。只有显式 `--bg` tmux 会话才把 exit 映射成 detach，让同一 REPL 继续运行。
- my-agent 的普通 `/exit` 现在明确结束当前 TUI 及其 refresh/notices/worker 线程，但不取消 Gateway
  canonical task，也不删除 session。退出后打印精确 `my-agent resume <session_id>`；取消任务仍只走
  `/stop` 或 `Esc`。外部 tmux `Ctrl+B D` 只是观察者 detach，不得伪装成产品退出。
- 现场慢响应的主要放大器不是 session 文件或 workspace 锁，而是多个已 detach TUI 仍每秒调用
  `/client/notices`，旧活动投影又为少量直属 child 扫描、解析并 deepcopy owner 下全部历史 run。
  Gateway 现在先从 SQLite read projection 按 root/parent/depth 选 exact run id，再从 canonical
  `task.json` 批量精确读取；投影只做查找，不成为生命周期权威。空闲 TUI 健康轮询降到 5 秒，有前台或
  后台任务时仍保持 1 秒，连接失败继续走 0.5/1/2/4/8 秒退避。
- 本轮不增加第二套“在线 session”数据库，也不自动杀 detached tmux。真正在线的 TUI 以后若需要
  `ps/attach/kill` 管理面，应另建带 PID/start-token 的易失进程登记；durable chat session 与 Gateway
  task 仍保持独立，不能按名称或模型文案判断存活。
- `.7` 部署后又用进程采样确认第二个同源热点：后台主代理 readiness 为每条待合批 wake 调用
  `_related_subagent_runs`，旧实现即使解析缓存命中，仍会 deepcopy 全部历史 run 后再筛一棵树。当前适配
  复用 LocalStore 的 `root_task_id` 索引只选择 exact run ids，再逐个读取 canonical `task.json`；managed
  索引异常返回结构化 load error 并保守不推进，不能退回自然语言或展示树猜状态。只有显式
  local-unmanaged/fake manager 没有查询投影时保留 canonical 全扫兼容路径。
- 真机验收保持全部历史数据和待处理 wake，不靠删任务制造低负载：`e2aba94` 部署后后台线程在 12 次
  `py-spy` 采样中均处于定时等待，没有再进入 `list_runs_report/deepcopy`；8 秒 `/proc` 差分约 12% 单核，
  16 个 durable session 并发活动快照最慢 0.294 秒。fresh TUI `ma-e2aba94-session-r14` 的新建、退出、
  exact resume、再次退出均为 status 0，session 总数始终 324，唯一 Gateway 未被停止或复制。

## 2026-08-23 会话运行时 式活轮权限与结果提交栅栏

状态：本地实现与 focused 回归已通过；待与当前 Prompt 4 真机样本安全分隔后部署复验。

- 会话运行时 对照结论：`TurnStarted/TurnComplete/TurnAborted/Error/Shutdown` 与活 thread watch 是执行生命周期
  事实，不会因为一个时间计数器超期就单独宣告 agent 死亡。my-agent 适配为 lease 超 grace
  且 PID/start-token 证明持主死亡才能接管；模型请求长于 lease 不再导致活 runner 被抢。
- current pointer 只是审计引用，不是独立权限证明。工具执行同时要求 active AgentRun + running
  exact AgentAttempt；终态 run/attempt 即使仍保留 pointer 也不得执行只读或写工具。合法后续 attempt 会显式
  把 run 重开到 `created`，记录 `agent_run.started` 及 previous status/generation，之后可再次收口。
- `unfinished/blocked` 等只代表任务仍可继续，不代表本次执行片段仍活着；它们现在用
  `agent_attempt.completed` 关闭 exact attempt、释放工具权限，AgentRun 保持 `created`。下一次 typed wake
  只能通过新 attempt 重获执行权，避免旧片段长期显示 running 或继续调工具。
- runner result 与 runtime.db 建立双层 CAS：canonical active attempt 必须一致；MANAGED 还要求
  exact current generation。宿主先收口的 `done/failed/cancelled` 只接受对应结果；旧 attempt 或已取消
  后迟到的 `DONE` 不得复活 task。原始 provider 响应仍可留在 archive 供查错，但不获得 canonical 写权。
- 会话运行时 的同一 active turn 可在 `needs_follow_up` 时继续下一个物理片段。my-agent 因需持久审计，
  会先关闭旧 attempt 再投影 task 状态，因此提交栅栏必须显式接受
  `run=created + current attempt=done + turn_end_reason=max-tokens/interrupted/blocked` 的同源非终态回写。
  不做这一步会把真实已结束的片段永久留在 TUI `RUNNING`；放宽到任意状态又会让伪完成穿透 CAS。
- 能力申请已在当 attempt 获得 typed grant 后，原 `BLOCKED` 已没有待人处理的外部条件。它现投影为
  同 run `PENDING`，退出 runner session 后由现有 durable auto-start 继续；这避免先把 conversation child link
  写成 blocked，又被 conversation lifecycle gate 阻止续派的自相矛盾。
- 展示层不再复用模型生成的 parsed status 作为终态活动短句。`current_step/current_tool` 由
  typed task status/failure type 投影，生命周期和权限不读取这些中文展示文案。

## 2026-08-24 Gateway 主代理权威 attempt 贯穿回合收口

状态：根因已由 `.7` 原样 Prompt 4 真 TUI 与 RuntimeDB 事件共同确认；本地 focused 回归已通过，待发布后
用全新 TUI 复验最后一名 child 完成会自然唤醒 main。

- 真实失败链：Gateway transport attempt 与 RuntimeDB AgentAttempt 是两个不同身份。主代理入口已把
  `gateway-attempt-*` 替换为 `attempt-*` 并用后者执行工具，但 `_run_with_params` 丢弃了替换后的
  `RunParams`，正常返回仍拿 transport id 调用 closeout。RuntimeDB 因而正确记录
  `closeout_blocked(reason=stale_attempt)`，root run/attempt 永久停在 `created/running`；三个 child 虽均有
  typed completion，最终 wake 也已落盘，后台仍不能合法续挂。TUI 的“整理结果中”只是由 child 状态推导的
  假活跃，不代表存在模型调用。
- 生命周期合同：任务工作区选择与 `_bind_main_agent_authority` 必须在每个物理模型片段执行前完成，返回的
  exact params 必须贯穿该片段的异常收口、Compact 换代、最终收口和后续持久化。`_run_once_with_params`
  只执行已经绑定的片段，不得在局部变量里偷偷替换 attempt 后让调用方继续持有旧身份。
- Compact 续接同样适用：每次 generation 换代先取得新的 RuntimeDB attempt，最终只关闭最新 exact
  attempt；transport id、上一代 attempt 或 thread id 都不能冒充执行权。终态 main run 仍可由下一条 typed
  wake 显式创建新 attempt。这与 会话运行时 `会话运行时-rs/core/src/session/turn.rs` 的同一 active turn 循环及
  `会话运行时-rs/core/src/session/tests.rs::task_finish_emits_thread_idle_lifecycle_after_active_turn_clears` 的
  typed TurnComplete→清除 active turn→thread idle 边界一致。
- 安全边界不放宽：stale-attempt CAS、unknown recovery block、单 thread claim 与客观副作用锁继续
  fail closed。本修复只让执行者携带正确身份收口，不允许调度器忽略冲突，也不消费或伪造历史 wake。

## 2026-08-24 会话根 Todo 持续投影

状态：`84c6b90` 已发布并部署到 `.7`；fresh Prompt 4 r6 已在真实子代理运行期通过 TUI 复验。

- 真实失败链：root 先建立 6 项 canonical `task_progress.v1`，创建 4 个 child 后 TUI 的固定 Todo 整块消失；
  权威账本仍保留 6 项，但 `/client/notices` 的 `conversation_agent_activity.v5.task_progress_items` 返回空列表。
  原因不是 prompt_toolkit 渲染或轮询丢包，而是同一 thread 的活动 link 同时含 root 与 child，旧投影按
  `created_at` 选择最新 link，必然读到较晚创建、没有 root Todo 的 child 账本，再用结构化空列表清屏。
- 对照决定：终端交互 `src/hooks/useTasksV2.ts` 用会话期 singleton store 持有 task list；Spinner 的逐轮
  mount/unmount 不改变清单所有权，存在未完成项时还保留 watcher 与 5 秒 fallback poll。my-agent 不复制
  React store，而是复用已有 `ConversationThread.workspace_task_id` 这一 canonical 会话根身份，按 exact
  task id 选择 root link；child 只通过结构化 `covers/progress_item_ids` 更新或标记 Todo，不得因创建更晚
  抢占面板。
- 兼容与安全边界：缺少 thread loader 或旧测试 fixture 没有 `workspace_task_id` 时，仍保留既有有界 link
  fallback；真实会话存在 preferred id 时只做 exact join，不解析 goal、标题、路径或“子代理”等正文。
  投影仍只读 `task_progress.v1`，不改变 Todo 状态、任务完成、调度、权限或验收。
- 真机结果：`.7` 唯一 Gateway、MiniMax-M2.7、tmux `ma-84c6b90-p4-fzf-r6-todo` 只提交一次原样
  Prompt 4；main 进入“等待 4 个子代理”后，固定 Todo 仍显示 `完成 1/9 · 进行中 6` 和四行窗口，下面
  同时展示 4 个直属 child。该证据直接覆盖修复前“child link 更新更晚就把 root Todo 清空”的失败窗口。

## 2026-08-24 后台主代理富过程事件流【状态：传输合批本地候选 focused 通过，待 `.7` 真机】

- 用户要求后台续跑也采用 终端交互 的正文过程展示：模型过程说明为灰色消息，工具以蓝色标题和缩进结果
  展示，文件修改继续使用已有的行号、红删蓝增 diff 与折叠提示；输入框上方仍只保留一条 main
  `Working` 活动行，不复制第二套状态面板。
- 对照确认本项目现有 `tui_block_renderer.py` 已经具备 `Update/Write/Bash`、结构化输出、折叠和 diff
  renderer；真实缺口位于后台主代理 transport。旧 `BackgroundMainActivitySink` 只保留一条 240 字符
  scalar activity，`/client/notices` 也只传活动快照和最终回复，工具结果携带的公开 `display` 在到达 TUI
  前被丢弃。因此本轮不新增第二套 renderer，而是让前后台复用同一 typed TUI block 协议。
- Gateway 进程为每个 conversation thread 保存有界、易失、单调游标的公开 transcript event ring。它只接收
  已经脱敏和限长的显式 thinking、工具生命周期/`display`，以及真实工具边界前确认的模型过程段；不保存
  hidden reasoning、签名、权限、任务状态或完成裁决。新任务会清掉同 thread 的旧事件正文，但序号不回退；
  慢客户端即使错过 start，也可用携带完整内容的 terminal event 恢复稳定块。
- `/client/notices` 以独立 `event_after/event_cursor` 增量读取该 ring；最终 owner reply 仍走原来的持久
  `background_notice.v2`，活动数量/Todo/child 仍来自 canonical projection。该事件流只增强实时展示，
  Gateway 重启后允许丢失中间过程，不能成为 transcript、任务生命周期、Compact、重试或恢复的新权威。
- `ma-97468f3-longchain-r27` 的 Rust 复刻续轮给出新的直接反例：Gateway 持续执行工具且 main activity 正常
  更新，但 MiniMax 把一段 reasoning 拆成数百个极短 delta；1024 条 ring 被逐 token 事件打满后，TUI 数分钟
  只显示旧 `run_command`，随后一次性追上。对照 会话运行时
  `会话运行时-rs/tui/src/chatwidget/streaming.rs::on_agent_reasoning_delta` 的当前 reasoning buffer，以及 终端交互
  `src/utils/messages.ts::handleMessageFromStream` + Ink 帧级 render throttle，当前传输层保留首片即时事件，
  后续同一 block 按 0.25 秒或 256 字符合批；完整 `thinking_completed` 仍是慢客户端恢复权威。合批不改变
  lifecycle、Compact、正文持久化或完成判断，47 项 background/TUI focused 已通过。

## 2026-08-25 会话运行时 式后台进程会话续接【状态：本地候选 focused 通过，待 `.7` 真机】

- 真实 Rust 构建现场使用 `run_command(run_in_background=true)` 后，返回文案要求模型调用
  `process_status/list_processes/kill_process`，但 Registry 从未注册这三个模型工具；shell 又正确拒绝裸 `&`，
  最终模型只能执行 `sleep 60 && cat log`，额外占用工具轮、延迟回复，并把 sleep 当成进程管理。
- 对照 会话运行时 `unified_exec/write_stdin.rs` 与 `process_manager.rs`：已有 exec session 应由同一个 session id
  续接，空输入做有界等待并返回真实终态；对照 终端交互 `LocalShellTask`/`TaskOutputTool`：后台任务归属当前
  app session，输出和停止走任务面，不重新拼 shell 轮询。当前适配为唯一 `process_session` 工具，提供
  `list/status/wait/stop`，其中 wait 最长 30 秒，超时只返回 running，不伪造失败。
- 单 Gateway 是多用户共享进程，不能照搬单用户内存表。每条后台记录在启动时绑定 executor host 注入的
  `owner_id + conversation session`（无 session 时依次退到 root task/root run/run）以及 owner home；查询、
  日志和停止必须精确匹配，错误 scope 与不存在统一返回 `PROCESS_NOT_FOUND`。模型只能提供 session id，不能
  提供或覆盖 scope。主子代理若属于不同 agent thread 也互不越界。
- 原先三个不存在的工具名和恢复提示已统一改成 `process_session`；模型需要结果时调用 wait，不再运行
  `sleep` 轮询。新增 7 项 focused 覆盖真实后台结束、日志返回、会话隔离、scope 缺失、完整进程树停止，
  并证明能启动后台命令的 main/coding child 工具快照一定同时包含续接工具。
- 子代理的最终执行工具快照把 `run_command -> process_session` 作为结构化依赖闭包：角色默认、显式 coding
  预设、能力申请获批和旧任务恢复都走同一规范化入口；owner 显式 `disabled_tools` 仍在闭包之后做最终收窄。
  因此不是靠提示词要求模型再次申请，也不会只有新建 child 生效、恢复 child 继续缺工具。

## 2026-08-24 较早未决操作的 会话运行时 式软核对

状态：真实 Prompt 4 r5 已给出反例；本地实现与 focused 回归通过，待发布后用新的原样重型 TUI 终态复验。

- 真实失败链：r5 的 root 两次执行 `npm test`/Jest 都以 typed `unknown + TOOL_TIMEOUT` 收口，随后不同的
  build、自写 E2E 和算法脚本成功。最终 `operation_verification` 因 2 个 unknown 保持 `uncertain`，模型却
  把不同测试的成功写成“构建、测试均验证通过”。这不是目录扫描能解决的质量验收问题，而是前序未决
  工具事实被后序成功掩盖后，模型没有在自然 final 前重新对账。
- 会话运行时 对照：`会话运行时-rs/core/src/tools/context.rs` 把每个结果保持为带 `success` 的原生
  `FunctionCallOutput`；`session/turn.rs` 在无 follow-up 的最终草稿处运行 stop hook，blocking hook 的
  continuation 会作为新的 response item 回到同一个 active turn 再采样。my-agent 保留现有逐轮工具 IR，
  并在跨 background wake 的扁平 archive 上补同类有界 continuation。
- 新软核对只读取当前请求 canonical operation records：若较早 effect-bearing 操作仍为
  `failed/unknown/cancelled/incomplete`，而最后一个操作已是其它终态，第一次自然 final 会收到有界未决项、
  操作引用和被退回草稿。模型仍保有原工具，自主选择复查、修复或如实披露；同一签名不重复，整个 active
  turn 最多两次。
- 该机制不是机器验收：不解析 final 正文，不理解“测试/完整复刻”等业务词，不扫描文件、LOC、Todo 或
  产物，也不替模型判任务完成。`unverified` 与 `not_started` 不自动触发这条历史核对；最新操作本身未决时
  继续由既有 completion-conflict 路径处理。即使模型复核后仍决定结束，宿主也接受其第二份自然回复。

## 2026-08-24 子代理 unknown 孤儿恢复预检

状态：`.7` 历史日志与 runtime.db 已确认根因；本地实现及 focused 回归通过，待当前 r6 自然结束后部署。

- 真实失败链：旧 child 的 AgentRun/current AgentAttempt 已被崩溃恢复写成 `unknown`，文件投影一度仍为
  `PENDING`。周期 orphan supervisor 只看投影就调用 durable auto-start，并先把动作计作
  `orphans_revived=1`；真正 runner 到 `create_attempt` 才被权威闸拒绝。现场同一 `agent_run_id` 留下 3 条
  `status_conflict(unknown_run_status)`，最后文件投影成为 `CHANNEL_ERROR` 才停止。
- 底层决定：repository 新增按 exact subagent run id 的 recovery-block 投影，与 `create_attempt` 共用
  `_main_agent_recovery_reason`。周期和 targeted orphan 启动都必须先读该结构化事实；unknown 直接返回
  `authority_recovery_blocked`，不建 runner、不占槽、不虚报复活。
- 安全边界不变：预检只是减少已知无效启动，事务闸和执行权锁仍保留；无权威记录沿用既有 MANAGED
  授权门处理，unknown 不自动改 failed/abandoned，只有人工核对后的 `recover_attempt_unknown` 能释放。
  不读取任务正文、错误字符串、职责描述或重试文案。
- 会话运行时 对照：`AgentStatus::Errored/NotFound` 会让 multi-agent wait/tool 显式失败，重新启动走显式
  `resume_agent`；没有周期任务把异常 thread 先宣称复活再让执行入口报错。本项目因持久 Gateway 需要
  orphan 巡查，但采用同一原则把异常权威状态挡在调度之前。

## 2026-08-24 TUI/Web 共用的子代理详情与精确控制面

状态：完整消息页已由 `91a1c3d` 验收；独立 viewport 已由 `d14549c` 在 `.7` 唯一 Gateway 真 TUI 验收。

- 交互对照采用 终端交互 的列表进入详情习惯，控制语义采用 会话运行时 的 exact agent id。输入框为空时才允许
  `↓` 选择 child，`Enter` 进入；详情继续消费同一 typed TUI event/reducer/renderer，不复制一套子代理 UI。
  当前运行 child 的普通输入是幂等 guidance，已结束 child 只读。
- `Esc` 的唯一语义是停止当前查看的运行中代理。返回父级改为 `Ctrl+G`，并保留 `Alt+←`、`/back` 兼容，
  避免一个按键同时承担 destructive stop 和 navigation。视图栈只改变前端焦点，不改 run/task/session。
- owner-scoped Gateway service 是 TUI 与未来 Web 的共用入口。历史查看要求 exact owner/root/ancestry，允许
  attempt 已结束；guidance/stop 额外要求 active binding，并写 canonical 控制账。终态不会因新输入自动创建
  attempt，未来若增加恢复必须单独实现显式 resume 操作和审计。
- child 公开过程使用 process-shared、有界、增量游标 JSONL；只保存已经公开的 thinking/tool/compact 展示
  事件。它不是任务状态、权限、完成裁决或 Compact 账本，丢失过程时可以降级显示 canonical final/status，
  不能反向更改生命周期。
- 详情页不是状态摘要页。对照 终端交互 `REPL.tsx` 的 `viewedAgentTask -> Messages` 与 会话运行时 每个 child 的
  独立 thread，进入 child 后必须继续使用主代理同一套消息 reducer/renderer：第一条用户消息显示父代理实际
  派发的完整 `task.goal`，后续按原格式显示灰色 thinking、过程 commentary、工具卡/diff、Todo、
  Context/Compact 和 final。底部短 `description` 只属于名册，不能替代详情页 prompt。
- typed callback 的能力判断必须先检查 `write_progress` 等显式方法，再兼容普通 callable；不能因为 sink
  对象本身不可调用就丢弃结构化工具事件。`Ctrl+O` 冻结的也必须是导航栈当前 active runtime，而非固定 root
  runtime，避免进入 child 后展开历史却跳回主代理。
- `.7` fresh tmux `ma-91a1c3d-child-full-r17` 实际进入 `agent-d1-worker-1` 后，普通页显示 child 灰色 thinking、
  `list_files`、`Bash` 和写文件工具卡；`Ctrl+O` 后仍是该 child，Home 首行是完整派工 user block，`Ctrl+G`
  才返回 main。三名 child 的 JSONL 均出现配对 tool 事件，worker-1 现场已有 6 次 started/6 次 completed；
  该行为证据只证明详情展示接线，不替代超级玛丽任务的功能完成验收。
- 视角切换不得复用一份全局滚动锚点或每次强制回尾。普通与详细 transcript control 分别按 exact
  `TuiStateStore` 保存 follow、cursor 和 unseen 基线；新页面默认跟随尾部，返回页面恢复其原位置，选区则
  在切换时清除。默认应用内鼠标直接把滚轮交给当前页面；用户按 F6 进入备用原生复制模式后，footer 必须
  明示历史改用 `PgUp/Ctrl+Home`，并提示再次 F6 恢复滚轮。
- `.7` fresh tmux `ma-d14549c-scroll-r18` exact resume 同一 Prompt 2 会话且没有再次调用模型。root 与
  worker-1 各自滚到首条 prompt 后来回切换，均恢复自己的锚点；F6 后 SGR wheel-up 翻到旧工具调用，
  再按 F6 回到原生复制。自动化不把协议事件冒充宿主 Terminal.app 的物理滚轮或右键菜单验收。

## 2026-08-24 终端原生复制与 TUI 鼠标双模式【状态：历史方案；默认原生模式已被同日 终端交互 对齐取代】

- 既有应用内右键复制只能证明 prompt_toolkit/tmux/OSC52 投影已执行，不能保证 SSH 外层的 Apple Terminal
  系统剪贴板已经改变。用户实测“右键没反应”证明全屏 mouse tracking 吞掉原生右键菜单后，这条
  best-effort 路径不能作为默认交互。
- 该轮曾以 会话运行时 的非 mouse-capture 行为和 终端交互 的 `模型助手_CODE_DISABLE_MOUSE=1` 逃生口为依据，把
  默认改成关闭 prompt_toolkit mouse support。后续真实用户回归证明 alternate screen 因此完全收不到物理
  滚轮，造成“历史消息消失”的直接体验故障；这个默认决定已经废止，下面的 r16 证据只保留为双模式协议
  能切换的历史记录，不能再解释当前默认行为。
- 当前 `tui_mouse_capture_default` 默认为 `true`；终端交互 式滚轮、点击、应用内选区是主链，F6 切到
  原生拖选只是外层剪贴板不接受 tmux/OSC52 时的备用路径。远程 notice 仍只能报告“已选中并尝试复制”，
  不能把不可观测的外层系统剪贴板写成确定成功。
- `.7` 唯一 Gateway、MiniMax-M2.7、tmux `ma-af5a03b-native-copy-r16` 的真实 attach 输出已证明：初始帧
  显式发送 1000/1002/1003 disable，bracketed paste 完整保留“右键粘贴验证ABC中文🙂”，第一次 F6 发送
  三项 enable，第二次 F6 再发送三项 disable，输入正文保持不变。该证据覆盖 TUI/终端协议，但 Computer
  Use 的安全边界不允许代替用户控制 Terminal.app，因此外层 macOS 右键菜单与系统剪贴板仍保留为用户
  attach 后的最后一项验收，不能虚报通过。

## 2026-08-24 子代理插话绑定活跃 attempt 与单一会话容量

状态：`6d33228` 已发布、部署 `.7` 并完成 fresh 真 TUI 复验。

- 真实失败链：TUI 给运行中 child 发送“给我讲讲你在做啥额，你不要停”后，Gateway 把 guidance 写入
  canonical message box，却没有写 `expected_turn_id`。运行时先按当前 attempt 成功 reserve，真正模型
  请求前的原子 submission gate 再发现 receipt 没有 exact turn，抛出
  `guidance submission reservation mismatch` 并结束该 child。WebFetch 失败发生在前，但不是本次
  runner 终止原因。
- 会话运行时 对照：V2 `send_message` 只把消息交给一个 exact agent thread，运行中在消息边界消费且不另起 turn；
  idle 后续工作另用 `followup_task`。my-agent 当前 TUI 只实现运行中插话，因此 Gateway 入账前必须从
  canonical child 取得当前 AgentAttempt，把 exact attempt id 同时写入 guidance 的
  `expected_turn_id`；拿不到活跃/pending attempt 时明确拒绝并保留输入，不得写一条无归属消息，也不得
  放宽 ConversationStore 的原子提交校验。
- 容量只保留一个默认会话树权威：`max_subagents=8` 表示同一 root 最多 8 个未结束 child。会话运行时 的
  `AgentControl::reserve_spawn_slot` 同样只守 session slot；其 `spawn_agent` 是单个创建，所以没有第二个
  “每次最多 4 个”的产品默认。my-agent 的批量工具把
  `subagent_hierarchy_max_children_per_tool_call` 默认改为 `0`（不额外收紧），显式部署仍可配置更小批次；
  `runner_auto_concurrency=8` 让默认八个槽位都能真正并行。历史累计 child 数不是当前占用量，终态释放
  槽位后可以继续创建，所以总历史数量允许超过 8。
- 备用屏幕中的 root/child 历史继续由各自 `TuiStateStore` 持久投影；当前 `.7` exact resume 已用
  `Ctrl+Home` 分别看到 root 原始 Prompt 与 child 完整派工、thinking、工具卡，证明数据没有丢失。当前
  终端交互 式默认直接启用滚轮；主/子代理 footer 显示 `滚轮/PgUp/Ctrl+Home 历史 · F6 原生复制`，只有
  用户主动切到原生复制后才改为 `PgUp/Ctrl+Home 历史 · F6 恢复滚轮`。
- `.7` 唯一 Gateway 的 fresh `ma-6d33228-p3-guidance8-r20` 只提交一次原样 Prompt 3，随后八名
  researcher 同时 RUNNING。进入 researcher-1 输入普通中文后，receipt 的
  `expected_turn_id=attempt-1787621674-fdb6e96a` 与 reservation attempt 相同，最终状态 `consumed`；child
  继续多轮 WebSearch 且未失败。root/child `Ctrl+Home` 和 F6 SGR wheel 均通过，测试结束后已回原生复制。

## 2026-08-24 子代理插话的 provider 消费回执与公开回复

状态：`2bf4602` 已通过 focused/严格 gate、推送、部署，并在 `.7` 唯一 Gateway 真 TUI 验收通过。

- 解决问题：现场 guidance receipt 已绑定 exact child attempt 且最终变为 `consumed`，但 TUI 在
  `/client/agent-guidance` 返回 HTTP 202 时就撤下 pending，把“消息箱已收到”冒充“模型已处理”。
  同一 child 的 provider thinking 已明确识别用户问话，但模型继续工具任务而没有普通 assistant
  回应，用户因而同时看到“没有排队”和“不理我”。
- 会话运行时 对照：`AgentControl::send_input` 把 exact `UserInput` 放入 child thread；TUI 的
  `pending_steers` 在注入/消费边界前保持可见。终端交互 对照：当前 viewed teammate 的输入
  立即进入该 teammate 自己的 `pendingUserMessages` 和消息页，再由 runner FIFO 消费。本项目
  不复制第二套队列，继续使用唯一 ConversationStore guidance receipt。
- 新合同：Gateway 接受只回复 `delivery=queued,status=pending`；child TUI 保留 exact
  `message_id` 的灰色 pending 行。只有 provider 请求成功并把 receipt 推进到 consumed 后，child
  `BackgroundTranscriptSink` 才向同一 run 的耐久展示流写
  `active_turn_input_consumed(client_message_ids)`；详情页按 exact ids 把 pending 原子提升为 user
  history，不比对文案。连续多条输入按真实注入顺序提交和展示，不按随机 UUID 排序。
- 回复边界：用户插话仍是普通 UserTurn，不增加自定义验收器或强制断轮。child 的系统提示
  明确要求先在普通 assistant 消息中回答或确认真实用户，再继续原任务；provider thinking
  不能代替对用户的公开回复。机器状态仍只看 typed receipt/event，不解析这句提示。
- `.7` 唯一 Gateway PID `610573`、MiniMax-M2.7 的 exact resume
  `ma-2bf4602-child-chat-r25` 进入运行中的 `agent-d1-researcher-10`，连续输入两条普通中文。
  两条消息在下一次 provider 消费前同时显示于 pending 区；随后按输入顺序出现在 child 用户历史。
  同一耐久事件流先写 seq 46 `active_turn_input_consumed`，其中 exact ids 顺序为
  `agent-steer-b9030ebf56cb4837`、`agent-steer-c9ef34a1ffca42f2`，再写 seq 47 普通
  `assistant_completed` 分别回答两问，seq 48 继续 `web_fetch` 原任务。`Ctrl+O` 全程留在 child 视角。

## 2026-08-24 子代理名册的选择可见性【状态：已部署 `.7` 并真 TUI 验收】

- 子代理导航的权威仍是按 parent 保存的 ordered exact run rows；八行名册只是 renderer 视窗。历史累计
  超过八项时，方向键可以把 `selected_run_id` 移到第九项，旧 `rows[:8]` 却继续画前八项，造成“屏幕无
  高亮但 Enter 能进去”的控制/显示分裂。
- 参考 终端交互 `MessageSelector` 由 `selectedIndex` 推导可见窗口：选中项还在前八项时保持原窗口，越过
  下边界后只移动足够多的起始行让 exact run 出现。该计算是纯投影，不增加 scroll 状态，不改变 canonical
  排序、状态、容量或 Enter 的 run-id 裁决；屏幕外总数继续由本地裁剪数与 backend hidden count 相加。
- `7e2ffb6` 在 `.7` 唯一 Gateway、MiniMax-M2.7 的 exact resume
  `ma-7e2ffb6-roster-r21` 验证九项名册：第九次 `↓` 后 coordinator-9 带 `›` 出现，`Enter` 进入同名详情；
  `Ctrl+G` 返回再按 `↑` 后 researcher-8 高亮且前八项恢复。测试者没有发送模型消息或改任务产物。

## 2026-08-24 终端交互 默认滚轮回归修复【状态：`0da26f0` 已部署 `.7` 并真 TUI 验收】

- 用户在 exact resume 中用 `PageUp` 和 `Ctrl+Home` 能看到欢迎页、原始 Prompt 与旧工具记录，证明 canonical
  history 和每页 viewport 均未丢失；物理滚轮无反应来自 `af5a03b` 把 mouse tracking 默认关闭，不是消息
  加载或 Compact 删除。这个取舍使“能复制”和“能看历史”变成二选一，属于默认交互回归。
- 参考 终端交互 `AlternateScreen.tsx`、`fullscreen.ts`、`termio/dec.ts`、`ScrollKeybindingHandler.tsx`、
  `ScrollBox.tsx`、`selection.ts`、`useCopyOnSelect.ts` 与 `termio/osc.ts`：全屏默认开启鼠标；wheel 直接驱动
  ScrollBox，向上打破 sticky、回到底才恢复；应用内选区 copy-on-select，并投影 native/tmux/OSC52。
- 本项目已有相同的 typed viewport、中文源字符选区、lost-release、右键复制、tmux buffer 和 OSC52 主链，
  因此修复只把唯一默认恢复为 `true`，让 F6 成为原生复制逃生口，并让 footer 从 typed mouse state 动态
  显示当前真实操作。不得增加另一份历史、滚动游标或终端品牌判断。
- `.7` 唯一 Gateway PID `557079`、MiniMax-M2.7、exact resume tmux
  `ma-0da26f0-终端交互-history-r23` 未提交新模型消息。默认状态直接接收 SGR wheel：root 离尾出现
  `Jump to bottom`、回底后消失；进入 researcher-1 后滚轮翻到完整派工、Thought 和工具记录。F6 的 footer
  在“原生复制/恢复滚轮”间真实切换；输入框拖选“中文复制验证ABC”后 tmux buffer 得到完整 9 字符，右键
  再次复制结果相同。外层 macOS 系统剪贴板仍只能由用户 attach 后亲自粘贴确认，不能由 tmux 证据冒充。

## 2026-08-25 后台续接操作事实与精确子代理交接引用【状态：第二层修复严格 gate 通过，待真机】

- 会话运行时 的 active turn 只有收到 typed `TurnCompleted` 才清除 Working；不能用前端超时、最终正文或 child
  数量猜终态。本项目同样要求 background slice 恢复原轮成功工具时，继续携带 host-owned
  `tool_execution` 与 `tool_operation`。这两类字段必须在工具输出首次 externalize 时进入有界耐久索引，
  后续 carried record 只从该结构化白名单恢复，不能解析工具正文里的 operation contract。
- 真机失败证明：`create_subagents` 原始结果已有 `tool_operation.status=succeeded`，但外置索引只保留
  `ok=true`；child 完成唤醒后的新工作片因缺少 operation 终态把派工判为 `unverified`，自然 final 仍落成
  unfinished，active task link 因而持续显示 Working。修复必须保留既有 fail-closed 核验，不允许把所有
  `ok=true` 副作用直接猜成 succeeded。
- `4106025` 部署后的同一长 TUI 又暴露第二层身份错位：前台工具行的 request/run/task 都是 exact foreground
  request，后台 wake 却拿 durable task id 查索引；两者都合法但不是同一个概念。当前按 会话运行时
  `session/inject.rs`、`session/turn.rs` 与 `tool_dispatch_trace.rs` 的 per-turn 绑定方式，把 child 创建时的
  `conversation_request_id` 沿 canonical child state → completion wake → background carried scope 传递；
  新索引显式落该字段，旧索引仅在 row `request_id` 同值时兼容，不扫描同 task 的其它用户轮。
- `work/agents/<run_id>/final_report.md` 由宿主写入，只含 task/run/status 与 child 最终回复，是完成信封已经
  暴露给直接父级的有界交接投影，不是 canonical 状态权威。精确该文件允许通过 `read_file` 按现有
  workspace/owner 边界读取；同目录的 state/checkpoint/summary/progress、目录枚举和 shell 仍拒绝。
  不复制第二份报告，也不让 final_report 正文参与 child 或 root 的完成、权限、验收裁决。
- child 自己的模型上下文只暴露用户/父级明确声明的业务产物：`required_file_refs`、显式 output/artifact refs
  与实际写入根。宿主拥有的 `final_report/output.json/runner_result` 不进入 child 的 output contract、task
  packet、workspace refs 或旧 bundle 摘要；否则模型会把运行时收口文件误当成第二份交付并浪费工具轮。
  child 自然 final 后由宿主一次生成交接投影，语义对齐 会话运行时 将 child 最后一条 assistant message 投给父级。
  工具执行器仍在内存中持有完整 write boundary，落盘 execution-context 仍供宿主审计，但两者不作为模型
  渐进披露入口；模型只看 cwd、读写根、grant 与安全后的 context bundle。attempt 恢复只暴露 checkpoint、
  summary、task 等续跑线索，旧 bundle 中的 closeout refs 也必须在 runner prompt 边界再次过滤。
- 同一现场的 completion 信封已经给出正确 `final_report_ref`，但模型保留 exact run id 后拼入了完整 durable
  task id，形成不存在的目录。读取层只对这个 exact final-report 叶子按同 owner
  `agents/<run>/state.json` 投影找 canonical ref，并再次执行原读权限检查；run id、run dir、owner containment
  任一不一致即不跳转，state/checkpoint/list/shell 不共享该能力。
- 部署后验收继续复用同一个 durable TUI：多 child 调研、两个普通追加、多 child 复刻、普通验收追加、
  多 child 返工和最终追加必须连续发生在一条 session 中。该链只测试跨工作片连续性，不改变单次任务边界，
  也不允许测试者替被测对象改产物或用技术提示直接告诉它底座诊断答案。
- 同一 r27 返工又暴露派工次序是假象：root 正文明确“先修复、再独立测试”，却把 worker/tester 放进同一
  `items`，两者被宿主正确地立即并发，tester 反而早 27 秒结束。对照 会话运行时
  `会话运行时-rs/core/src/tools/handlers/multi_agents_spec.rs` 的 concrete/bounded/independent sidecar 纪律，
  `create_subagents` 模型合同必须直说：同批每项立即并行，自然语言“先后”不形成依赖；B 读取 A 的未来
  修复、产物或结论时，先只创建 A，等 lifecycle wake 后再创建 B。当前只改唯一工具说明和 schema 参数
  详情，不解析 goal/role、不硬拦 tester、不新增 `depends_on` 调度器或机器质量验收。

## 2026-08-24 单 Gateway HTTP 有界复用工作池【状态：`53498c1` 已部署 `.7` 真 TUI】

- 解决问题：唯一 Gateway 被 OOM killer 杀死后，新 TUI 只能短暂显示“正在连接 Gateway”再退出。内核事实
  证明被杀进程 RSS 约 6.68 GB；现场 8 个长期 TUI 以活动态 1 秒/空闲态 5 秒轮询，而标准库
  `ThreadingHTTPServer` 为每个请求创建一个新 OS 线程。即使请求结束，反复 transcript/child 投影分配也会
  放大 allocator 高水位；一旦 handler 变慢，线程和请求对象还会同时堆叠。
- 会话运行时 对照：`app-server/src/in_process.rs` 以固定 Tokio task 处理消息，入口和出口均使用容量 128 的
  bounded mpsc channel；thread list/state 再用单 permit semaphore 串行权威更新。这里不引入第二套 ASGI
  或新依赖，只适配它的“固定执行者 + 有界排队 + 显式背压”原则。
- 唯一实现位于 `gateway_parts/bounded_http_server.py`：16 个可复用 daemon worker，运行与排队合计最多
  128 个；满载在鉴权/业务 handler 前返回 `GATEWAY_HTTP_BUSY`、HTTP 503 与 `Retry-After: 1`。客户端现有
  transport failure backoff 负责稍后重试，忙不等于任务失败，也不得创建、取消或改变 run。
- 停机先关闭准入并取消未开始的 Future；取消回调关闭对应 socket 且精确释放一个槽位。已经进入 handler
  的请求沿原 typed ledger/幂等合同自然收尾，daemon worker 不参加解释器全局 join，保持旧 Gateway 可控
  重启边界。accept backlog、owner 鉴权、active turn、模型并发和子代理恢复均不借此放宽。
- 多用户对照结论：通道运行时 在入口保护上更完整——未鉴权 WebSocket 按 IP 限连接，鉴权失败按 scope/IP
  滑窗退避，控制写操作再按 device/IP 单独计费；长期助手 更擅长 resolved session lease、profile 独立状态库
  和全局 agent-run 上限。当前项目不在 socket 层读取可伪造的 `X-User-Id` 做“假公平”：transport 只守全局
  16/128 硬上限，鉴权后沿既有 request admission 守每用户 8、全局 500、同会话单飞，后台再按 owner
  round-robin 与每 owner 4 条会话车道调度。若未来改 WebSocket，优先适配 通道运行时 的 pre-auth connection
  budget；不能把连接 IP 当成最终 owner（同机 TUI、飞书适配器和 NAT 都可能多人共用 IP）。
- 固定 worker 按 TCP connection 执行 stdlib handler，因此所有 JSON/metrics 响应显式
  `Connection: close`；否则一个客户端只需保留 16 条空闲 HTTP/1.1 keep-alive 就能占满全部工位。当前 TUI
  本来就是短轮询，这一边界只牺牲无用的连接复用，不改变 session、历史、模型 turn 或 owner 状态。
- 验收：40 项 focused 和本地严格 gate 通过；`.7` 唯一 Gateway PID `604186` 下，旧 TUI 自动重连，fresh
  `ma-53498c1-http-pool-r24` 约 1 秒启动并完成 MiniMax-M2.7 真调用。9 个 TUI 自然轮询时只创建 5 个
  `gateway-http_*` worker、旧 request thread 为 0，12 秒 RSS 约 132.7 -> 131.7 MB。该证据只证明当前
  有界实现与短时稳定，不把 12 秒观察写成长期内存无泄漏结论。
## 2026-08-25 长期进度账本与当前回合 Todo 分层【状态：本地 focused 通过，待 `.7` 真机】

- 问题：同一长期 task-path 连续经历调研、追问、复刻和返工时，canonical `task_progress.v1` 必须保留恢复所需
  的完整历史；旧 TUI 却直接投影全部 items，导致新阶段仍显示上一轮 `4/7`，迟到的后台 notice 还能再次覆盖。
- 对照：会话运行时 app-server 的 `TurnPlanUpdatedNotification` 明确携带 exact `turn_id`，TUI 的 `on_plan_update`
  接受一份本 turn 完整 plan；终端交互 在用户消息提交 commit 后调用 `repinScroll/scrollToBottom`，手动
  scroll-away 才冻结 viewport。
- 决策：不拆第二本进度账、不按标题/状态/时间猜阶段。完整 items 仍是唯一 durable 软账本；旁挂 host-owned
  `display_plan={generation_id, revision, item_ids}` 只负责显示。普通 conversation request 换代，同一 root
  lifecycle wake 和 child continuation 沿原 exact request；同代结构化写入合并 ids，修订号只在集合变化时递增。
- 边界：Todo 投影不参与任务结束、验收、调度、权限或 Compact；旧账没有 display plan 时继续展示完整 items。
  TUI reducer 只接受当前期望 generation 且不倒退 revision 的快照，最终 notice 也携带同一身份。
- 视窗：每个 main/child store 仍保存自己的 follow/cursor；真实 prompt_toolkit Window 通过公开
  `get_vertical_scroll` 每帧应用锚点。提交、首次进入或显式 End 粘底，手动上翻不被新输出抢走。
## 2026-08-26 大工具参数流的脱敏可见进度【状态：已部署并通过真 TUI】

- `.7` 长任务中，`source-reader-1` 在生成 56,876 字节分析文件前约 4 分钟没有新的 transcript 事件；
  进程、TLS 连接和收包计数均持续推进，证明不是锁、进程退出或 provider idle。现有 Anthropic parser
  只累计 `input_json_delta`，直到 `content_block_stop` 才一次性交出工具块，TUI 因此无法区分“仍在生成
  大参数”和“真正无活动”。
- 会话运行时 在 `core/src/session/turn.rs` 保留 `ToolCallInputDelta`，并由 `apply_patch` 的 diff consumer 以
  500ms 合批发布 patch 更新；终端交互 在 `services/api/模型助手.ts` 累计 `input_json_delta`，再用
  `streamingToolUses` 渲染未完成工具。当前实现适配同一原则，但公开面只含工具名、阶段、累计字符数和
  起点，不保存或展示半截 JSON/文件正文。
- 新投影纯属 display：不创建 ToolCall、不提前执行 handler、不刷新 task/Compact/验收，也不取得权限或
  完成权威。后台事件必须按时间和字符阈值合批；真实工具开始、provider 重试或回合终态时删除临时块，
  继续复用现有工具/diff 卡片。Gateway 重启可丢这段易失进度，不影响 canonical tool/result 与最终回复。
- 当前实现以 `provider_tool_input_progress.v1` 作为唯一公开白名单，provider 层首条、1 秒/8,192 字符和
  ready 合批；Gateway rich、后台 main/child 与本地 TUI 共用 transient projector。8 个相关测试文件
  195 项与本地严格 gate 通过；`8c11eab` 已部署 `.7` 唯一 Gateway。fresh r37 在真实复杂任务中显示
  515 chars/5s → 3.0k chars/29s，55s 后完整 `create_subagents` 工具卡接棒并删除临时行，边界成立。

## 2026-08-26 Anthropic-compatible 主动缓存断点【状态：已部署并通过真 TUI】

- 当前原生工具循环每次都复用稳定工具清单、首条真实任务和不断增长的历史，但旧 backend 没有发任何
  `cache_control`。真实 MiniMax-M2.7 child 的 82 次调用累计 4,225,275 input，cache write 为 0，不能再用
  当前 Context 数字或 Compact generation 代替 provider 成本事实。
- Anthropic 协议的唯一投影入口位于 `backends/anthropic_prompt_cache.py`。它按 provider 规定的
  tools → prompt → messages 顺序，最多使用三个宿主断点：最后一个工具、首条 prompt、最新可缓存历史块。
  所有修改都是 copy-on-write，不污染 canonical native IR，也不让展示、缓存或 Compact 互相取得裁决权。
- 该能力只在调用方显式传入 `messages` 的原生多轮链启用；普通 text/auxiliary 单次请求保持旧 payload。
  `anthropic_prompt_cache_enabled` 是唯一兼容开关，YAML 与 dataclass 默认均为 true。端点拒绝标准字段时由
  用户明确关闭，不增加 provider 名称猜测、错误字符串重试或第二条隐式 fallback。
- `69bf2ec` 部署后的 fresh TUI 暴露 native 第一轮空 IR 被 `messages or None` 压成 text 语义，前三次真实
  调用因而没有 cache write/read。统一入口现在固定 `None=text`、`[]=native empty history`；原生首轮从
  第一次请求就建立工具/prompt 缓存，后续继续推进 history 断点。这个类型区别属于 provider 协议事实，
  不依赖任务正文、模型判断或界面状态。
- 同部署配置的直连 MiniMax-M2.7 探针已证明协议可用：首次写 10,551 tokens，第二次读 10,541 tokens。
  `df95d27` 的 193 项 focused 与严格 gate 已通过、推送并部署 `.7` 唯一 Gateway；fresh TUI
  `ma-cache-firstturn-r34` 首请求真实写 25,999、读 12,313 tokens，两个普通后续回合继续读 37,234 与
  46,972。协议探针与完整 Agent 主链现已互相印证。
- 会话运行时 的 session-scoped `prompt_cache_key` 证明稳定会话前缀应由底座承担；具体 wire 字段继续服从当前
  Anthropic-compatible 协议。真机是否有效只读 provider usage ledger 的 cache-write/cache-read，不根据
  延迟、上下文百分比或自然语言推断。
- 同 thread 的 `compact_generation=0` 与 69.1k→46.3k Context 回落并不冲突：终态工具折叠只替换下一轮
  的模型可见投影，完整 archive 与 exact refs 保留，Compact 权威代数不变；最后主轮仍有 40,527
  provider cache-read。该边界继续由结构化 fold/ledger 裁决，不要求 Context 数字单调递增。
