# DESIGN LEDGER

当前设计铁律：

- 产品能力状态只认 `docs/PRODUCT_FACTS.md`；代码存在、测试存在、历史计划打勾都不能自动升级为稳定能力。
- 先跑通一条主链路，再谈扩展。
- 真机运行与测试统一只允许一个真实 Gateway。并行、极限和长任务测试必须让多个 TUI/会话连接同一
  Gateway，以真实暴露队列、公平性、活动回合、恢复和资源争用问题；禁止用“每个用例一个 Gateway”
  规避共享底座问题。需要验证重启、崩溃或断连时，只能顺序操纵这一个实例；进程内 fake/合同单测不属于
  额外真实 Gateway。
- 自然语言负责沟通，结构化事实负责决策；prompt、summary、报告正文、guidance 和角色描述不能直接改变运行时状态、权限、验收或派工。
- 安全门可以硬，业务质量门默认软；危险路径、危险命令、越权和客观产物错误可以硬拦，任务深度、覆盖充分性和报告质量进入 warning、返工提示或 closeout。
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
- 供应商额度耗尽、明确不可用或健康探测失败时，正式运行面立即切到已配置且探活成功的本地模型，禁止
  为等待刷新而让任务空转；本地首选端口和供应商刷新时刻来自显式配置，不从错误正文或自然语言猜测。
  只有当前没有 live request、到达配置刷新点且供应商探活成功时才安全切回，模型切换不得创建第二个
  Gateway、测试服务或平行会话。
- 主代理长期记忆归 owner home；子代理只保留任务周期内可审计状态。
- 子代理可以写协作产物，但最终交付由主代理汇总和验收。
- 工具面要少，优先增强现有工具和运行时语义。
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
- 原生子代理创建入口必须有机器可校验的目标：`create_subagents.goal` 始终 required；单子代理直接使用该目标，`items` 批量模式同时携带总 goal 与每项独立 goal。不能只在工具说明里声称必填后容许空 `tool_use`。该约束对照 会话运行时 v2 `spawn_agent` 的 required `task_name + message`，不靠模型自然语言补救。
- gateway 请求进入终态归档时，response 的 `done/interrupted/failed` 是最终状态权威；processing lease 只提供 owner/attempt/heartbeat 等运行字段，不能覆盖终态。归档目录、请求 JSON、response 与 `/status` 必须表达同一事实。
- 恢复任务后，新的 request/run id 只表示这次执行尝试，不得成为新的任务事实源。模型可见的 main context bundle 摘要不裸露这些本轮运行 id；完整值留在 JSON 事实源，只有结构化选择既有任务后才显示 `selected_conversation_task_id`。default 主代理的 guidance、task_progress 工具、需求/派工 seed、coverage、wait、监督提醒、workspace 懒建和 delivery closeout 必须统一读取结构化 `conversation_task_id`；task_local 子代理仍按自己的 run id 隔离。该解析只保留一个共享实现，禁止各模块复制一套优先级。conversation task link 是生命周期权威，`work/state.json` 是同一 task path 的 owner-local 投影；完成、停止、取消等结构化状态迁移必须同步投影，且目标与解析后的状态文件都必须位于当前 `owner_home/tasks/` 的精确 task 根内。路径越界、符号链接、身份不一致或文件损坏时只告警、不得覆盖别的任务目录。
- 租户可见路径默认取最小权限：远程 owner 只能读写自己的 owner home，另外可读组织明确发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板和旧顶层私有目录一律拒绝。外部目录只能由当前轮的结构化 capability/delivery contract 精确加入 workspace roots，不能由模型给出绝对路径自我授权；该授权也不能覆盖凭据文件或其他 owner 拒绝。随 wheel 发布的基础 tools/skills 是公共产品能力，shared 只用于组织显式共享的 skills/tools/role templates；个人 USER/SOUL、记忆、任务和产物不得由 shared 或 full mode 绕过。
- 普通通道上下文必须在同一结构化 scope 内“累计 transcript → 自动 compact → 继续累计”：raw transcript 永不因 compact 改写或删除，thread JSON 的 summary+cursor+generation+checkpoint pointer 是唯一 live compact 状态；旧消息只进入该 owner 的 LocalStore 派生检索索引。每次先生成不改状态的候选，再按完整下一轮输入验证低于精确阈值，随后先写 owner-scoped 完整恢复 checkpoint，最后以一次 generation CAS 同时提交 summary/cursor/checkpoint；失败候选、checkpoint 写失败或 CAS 冲突都不得推进游标。正常达到配置阈值时允许在同一历史尾部保留有界的近期完整 user/assistant 回合，过大时退回压缩全部旧段；供应商已经返回上下文压力时则一次替换本轮之前的完整旧段，禁止把同一受保护尾部连续压成多代 checkpoint。这不是第二份 history，也不能让固定最近轮数重新成为遗忘边界。连续失败只更新同一 thread 的 typed failure circuit，三次后短暂冷却，成功提交清零，禁止每条新消息重复空烧摘要模型。
- 同一 owner/thread 只有一份模型历史。聊天、文件工作、子代理协调、定时唤醒和普通小任务都继续使用同一 thread 的 summary + raw tail；task link、workspace、progress、wake 与子代理树只是结构化运行事实，不得过滤、替换或复制 transcript。普通 Gateway 请求按会话顺序执行，当前 turn 结束或耐久续轮启动后仍继续同一历史，不能创建平行“聊天上下文”。
- 后台 scheduler 只有在该 thread/task 没有 linked live turn 时才能启动一个续接 turn；续接仍加载完整 thread compact 与消息尾部，并额外读取精确 task 的运行状态。任务已 completed/cancelled/interrupted/abandoned/superseded 时，排队的定时或生命周期 wake 直接作废，不能复活任务。
- task workspace 下只保留 progress、canonical state 和证据 refs 等结构化运行事实，不生成 task compact、task rollup package 或第二份主代理上下文。主代理上下文压缩只认 thread JSON 的 summary + cursor + generation；active turn 因 context pressure 续跑时，typed compact carrier 继续同一 turn。每个子代理作为独立 agent 在自己的 run workspace 调用同一通用 Compact。
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
  不从显示文字反向解析未读状态。
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
- 工具参数中的显式绝对路径具有目标身份，不能为了“安全落位”静默换成另一个路径后返回成功。相对
  `output/...`、`work/...` 可以按结构化任务边界解析；绝对路径必须保留原值，再由
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
- Compact 百分比只允许一个权威阈值：`context_window_tokens × configured_percent`。候选是否可提交也必须按包含 system/persona、summary、近期 raw tail、原生工具 Schema、ToolCall 参数、ToolResult、运行引导、近期与已压缩工具事实及当前用户输入的完整下一轮投影低于这个阈值判断；不得再加未来输出预留、工具 digest 隐藏天花板或另一套 task compact。当前 turn 的原生工具历史只能按调用/结果整对回收，并复用既有语义摘要后端在同一 IR 中以最多一条 replacement item 承接被回收旧段；后续压缩原位替换，summary、handoff marker 与近期尾部都进入同一预算。摘要是非权威续接视图，raw archive、operation ledger、artifact、workspace、transcript 和真实 UserTurn 仍是事实源；不能另用漏算工具参数的字符口径。provider usage/tokenizer 缺失时的估算误差必须与阈值数学分开说明。
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
- 子代理工具能力只能继承父代理当前 run 的工具快照并继续做减法：普通 worker 永远移除 child-creation 工具；coordinator 只有父代理本来拥有对应能力时才能继续派工。显式 `allowed_tools` 只是收窄请求，不得凭角色模板或模型文本扩权。
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

## 2026-08-21 子代理递归控制面与自然收口【状态：本地已落地，待 `.7` 真机复验】

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
  write roots，后代只继承同一权限上界；`output_files` 继续负责交付目标、冲突锁和 workspace 外显式授权，
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
  可直接交给 child。`create_subagents.items[].output_files` 仍用来登记用户明确交付位置、产物归属和冲突锁，
  但 goal 或 output_files 都不能把范围扩大到父级工作区之外。TUI 后台 notice 首次立即查询、之后每秒查询，
  并为同 thread 每条消息生成独立 block id，避免 reducer 丢掉第二条以后更新。

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
  `model_visible_context_compaction.v1` 计数事件；它不冒充 durable conversation compact generation。
- 会话恢复只投影 `cmd_chat` 已从 canonical session 加载的 user/assistant history；TUI 不另读写正文。
  journal、diagnostic、block cache 和 stable display window 均有显式上限，迟到/重复事件仍由 seen identity
  拒绝，裁剪不产生第二会话账。
- Gateway 启动状态必须是实际 readiness 的投影：alternate screen 先出现，后台 preflight 发布
  `connection_started/resolved`，成功后才启动唯一 worker；同步 pre-wait 只保留给 plain 模式。失败以
  typed error 和非零返回码关闭，不能假装已经可连接。
- 空闲动画时钟不应让静态长历史失效。完整 frame key 由 renderer 统一决定；connection/thinking 活动时
  才加入可见动画字段，thinking 周期取 glyph 与稳定词长的最小公倍数，禁止经验魔数。20k block LRU
  和 20k stable display window 是性能边界，不是新的会话事实源。
- transcript 鼠标选择只持有当前 viewport 中经 prompt_toolkit `Window` 从屏幕列反解后的源字符索引，
  resize 清除；产品层不得再按 `wcwidth` 二次换算，否则中文会只复制一半。最终 focus 字符必须包含在高亮
  和复制文本中。任何形式的 mouse-up 都结束
  拖动；1003 无按键 motion 和下一次 fresh press 负责收口窗口外遗失的 release。松手自动写
  prompt_toolkit clipboard、OSC52 和 tmux buffer，Ctrl-C 仍可重复复制；无选择时才进入审批取消、turn
  interrupt 或双击退出。输入 Buffer 的显式选区具有更高 Ctrl-C 优先级，鼠标松手也自动复制且不清高亮；
  full-screen mouse tracking 已取得事件后，SSH 内应用无法强制宿主终端弹出原生右键菜单；已有非空选区
  的右键按下因此复用同一 clipboard 投影直接复制，正文与输入框均须在原 handler 前拦截并保留高亮，
  配对 release 只收口不重复复制。右键 release 丢失时由无按键 motion 或下一次其它 press 解锁，不能令
  后续鼠标永久失效；`Copied` notice 只证明应用已发起投影，外层系统剪贴板仍需真实粘贴确认。
  Ctrl-V 只粘贴应用剪贴板，系统剪贴板继续由终端 bracketed paste 注入，两条路径都走同一大文本合同并
  替换现有选区。输入 marker 使用普通空格，禁止用会触发 `nbsp` 下划线样式的不换行空格。审批 overlay
  继续允许 Page/wheel/Ctrl-Home/End 查看上文，Up/Down 仍专属于选项导航。
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
- 模型网络故障必须按系统异常事实分类：`ConnectionRefusedError` / `errno=ECONNREFUSED` 是服务端点临时
  拒绝连接，先走 HTTP 传输层有界退避，再走模型回合级有界恢复；DNS、无效地址和其它未证明为瞬时的
  连接故障继续快速失败。富 TUI 从结构化 retry 事件显示层级、序号和等待秒数，不能解析错误文案决定
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
