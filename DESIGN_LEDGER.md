# DESIGN LEDGER

当前设计铁律：

- 产品能力状态只认 `docs/PRODUCT_FACTS.md`；代码存在、测试存在、历史计划打勾都不能自动升级为稳定能力。
- 先跑通一条主链路，再谈扩展。
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
- 工具参数只有一份权威结构：`ToolSpec.input_schema` 保存完整 JSON Schema；尚未迁移的 builtin
  只允许由 `tool_spec_schema` 一处把 `parameters/parameter_schema/required_parameters` 编译成同一
  Schema。模型可见定义、文本/native 入口、参数恢复门、限流/重复保护哈希和最终执行必须消费它，
  backend、MCP 或 handler 不得再维护拍平的 required/type 副本。外层 typed tool-call envelope 必须先
  与工具参数分离；进入扁平执行 payload 后，除 `tool` 和未被 Schema 声明的真实协议元数据外都属于
  工具输入，`kind/run_id/status/metadata/artifact_refs` 等同名正式参数不能被误删或绕过校验。
  执行入口只允许 Schema 明确且无歧义的字符串→整数/数字/布尔/null/JSON container 类型纠正，
  不猜字段、不把标量包成数组。缺失字段只允许在同一入口按 `ToolSpec.safe_parameter_defaults` 的逐字段
  明示安全默认值，或按 `trusted_parameter_bindings` 从 Registry 构造的 `run_scope/write_boundary/registry`
  结构化事实补入；Schema `default` 注解本身没有执行权，模型显式给出的字段永不被覆盖，未声明的必填字段
  继续失败。每个有效输入字段只记录不含原值的 `source/source_ref`，可信引用和精确动作条件在 Schema
  展示前 fail-closed；随后在任何路径、effect、审批或工具实现前完整校验
  required/type/enum/const/嵌套对象与数组/额外字段/长度和数值边界/本地 ref/组合规则。handler 继续负责
  文件是否存在、跨字段关系等业务事实。纠正审计只记 JSON 路径和前后类型，错误只回传约束与路径，
  不记录原始参数值。MCP 未支持或畸形的 assertion 必须在注册时跳过该工具并明确告警，禁止降级成宽松透传。
- 文件大小不是硬门；是否合并或拆分看调用链是否清楚。
- 大输出、compact、resume 必须靠 chunk、cursor、coverage ledger、archive 和 resume summary，不靠提示词提醒模型“别忘”。
- 参考成熟项目先于自己发明：会话运行时 是会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导的第一底座参考；长期助手 只补长期 Memory、Persona、多用户持久调度与被动验证，通道运行时 只补 IM adapter、通道健康与投递边界。适配现有 owner/thread/task 事实源，不另造平行主链。
- 多用户命令隔离是执行节点启动硬门：所有 owner-scoped 前后台 shell 必须经 bwrap；缺失或自检失败返回结构化 `SANDBOX_UNAVAILABLE`，禁止降级宿主执行，也不走用户可见审批。
- 子代理的 `allowed_write_roots` 必须覆盖所有能启动进程的正式工具：文件写入、`run_command`、PTY 和 LSP 共用同一结构化写边界。bwrap 中 owner home 作为只读基座，仅把本轮精确授权根叠加为可写；不得解析 shell 文本、重定向或自然语言猜测写路径。PTY session 与 LSP server 还必须绑定创建时的 owner/task 写域，禁止按可猜 session/server id 跨域复用。
- 默认安装进入透明容器 CLI：用户仍调用 `my-agent`，包装器只挂当前工作区和 `~/.my-agent`；宿主 venv 仅为显式 `--host` 开发模式。企业 worker 在启动和 K8s readiness 重跑同一 sandbox 自检。
- 发布干净度分两层：工作树门检查 tracked 脏文件和未忽略 untracked 文件；制品门直接检查 wheel/zip/tar 内容、运行状态目录和大小预算。`.gitignore` 不是发布安全事实。
- 普通通道对话以 `owner + channel + chat/topic` 的持久 transcript 为唯一多轮事实源；旧 dialogue memory 不得重复注入或挤占稳定偏好。thread 另外持久保存一个精确 `workspace_task_id`，作用等同 会话运行时 `SessionConfiguration` 中跨 turn 继承的 cwd；它只选择当前工作目录，不等于当前轮正在执行任务，也不从自然语言推断。
- 停止、完成与续接只认会话中的持久 task link 和 `workspace_task_id`：`/stop` 中断 active turn，closeout 结束 task lifecycle，但两者都保留当前 workspace。下一轮普通聊天继承目录但不重开任务、不写任务归档；第一个带 `promotes_task` 的工作工具、`create_subagents` 或 `wait` 才按精确 sticky task id 重新激活已完成/中断任务。`task_progress(action=select, task_id=...)` 只用于切换到同一 thread 的另一个精确候选，`action=start, new_task=true` 才显式创建并切换全新任务。`read/update/select/start` 是严格动作变体，混用在产生 task link 前失败；失败选择不得落入懒晋升。禁止从“继续、接着做、重来”等自然语言推断 task id。该边界直接对照 会话运行时 `SessionConfiguration.environments` 的逐轮继承与 active-turn cancellation；IM 只负责把结构化 conversation/task/run 关联送入同一状态机。
- 原生子代理创建入口必须有机器可校验的目标：`create_subagents.goal` 始终 required；单子代理直接使用该目标，`items` 批量模式同时携带总 goal 与每项独立 goal。不能只在工具说明里声称必填后容许空 `tool_use`。该约束对照 会话运行时 v2 `spawn_agent` 的 required `task_name + message`，不靠模型自然语言补救。
- gateway 请求进入终态归档时，response 的 `done/interrupted/failed` 是最终状态权威；processing lease 只提供 owner/attempt/heartbeat 等运行字段，不能覆盖终态。归档目录、请求 JSON、response 与 `/status` 必须表达同一事实。
- 恢复任务后，新的 request/run id 只表示这次执行尝试，不得成为新的任务事实源。模型可见的 main context bundle 摘要不裸露这些本轮运行 id；完整值留在 JSON 事实源，只有结构化选择既有任务后才显示 `selected_conversation_task_id`。default 主代理的 guidance、task_progress 工具、需求/派工 seed、coverage、wait、监督提醒、workspace 懒建和 delivery closeout 必须统一读取结构化 `conversation_task_id`；task_local 子代理仍按自己的 run id 隔离。该解析只保留一个共享实现，禁止各模块复制一套优先级。conversation task link 是生命周期权威，`work/state.json` 是同一 task path 的 owner-local 投影；完成、停止、取消等结构化状态迁移必须同步投影，且目标与解析后的状态文件都必须位于当前 `owner_home/tasks/` 的精确 task 根内。路径越界、符号链接、身份不一致或文件损坏时只告警、不得覆盖别的任务目录。
- 租户可见路径默认取最小权限：远程 owner 只能读写自己的 owner home，另外可读组织明确发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板和旧顶层私有目录一律拒绝。外部目录只能由当前轮的结构化 capability/delivery contract 精确加入 workspace roots，不能由模型给出绝对路径自我授权；该授权也不能覆盖凭据文件或其他 owner 拒绝。随 wheel 发布的基础 tools/skills 是公共产品能力，shared 只用于组织显式共享的 skills/tools/role templates；个人 USER/SOUL、记忆、任务和产物不得由 shared 或 full mode 绕过。
- 普通通道上下文必须在同一结构化 scope 内“累计 transcript → 自动 compact → 继续累计”：raw transcript 永不因 compact 改写或删除，thread JSON 的 summary+cursor+generation+checkpoint pointer 是唯一 live compact 状态；旧消息只进入该 owner 的 LocalStore 派生检索索引。每次先生成不改状态的候选，再按完整下一轮输入验证低于精确阈值，随后先写 owner-scoped 完整恢复 checkpoint，最后以一次 generation CAS 同时提交 summary/cursor/checkpoint；失败候选、checkpoint 写失败或 CAS 冲突都不得推进游标。正常达到配置阈值时允许在同一历史尾部保留有界的近期完整 user/assistant 回合，过大时退回压缩全部旧段；供应商已经返回上下文压力时则一次替换本轮之前的完整旧段，禁止把同一受保护尾部连续压成多代 checkpoint。这不是第二份 history，也不能让固定最近轮数重新成为遗忘边界。连续失败只更新同一 thread 的 typed failure circuit，三次后短暂冷却，成功提交清零，禁止每条新消息重复空烧摘要模型。
- 同一 owner/thread 只有一份模型历史。聊天、文件工作、子代理协调、定时唤醒和普通小任务都继续使用同一 thread 的 summary + raw tail；task link、workspace、progress、wake 与子代理树只是结构化运行事实，不得过滤、替换或复制 transcript。普通 Gateway 请求按会话顺序执行，当前 turn 结束或耐久续轮启动后仍继续同一历史，不能创建平行“聊天上下文”。
- 后台 scheduler 只有在该 thread/task 没有 linked live turn 时才能启动一个续接 turn；续接仍加载完整 thread compact 与消息尾部，并额外读取精确 task 的运行状态。任务已 completed/cancelled/interrupted/abandoned/superseded 时，排队的定时或生命周期 wake 直接作废，不能复活任务。
- task workspace 下只保留 progress、canonical state 和证据 refs 等结构化运行事实，不生成 task compact、task rollup package 或第二份主代理上下文。主代理上下文压缩只认 thread JSON 的 summary + cursor + generation；active turn 因 context pressure 续跑时，typed compact carrier 继续同一 turn。每个子代理作为独立 agent 在自己的 run workspace 调用同一通用 Compact。
- `/verbose off|on|full` 是 per-thread 持久设置；工具进度必须以 typed event 进入 Gateway，再由有身份校验的 progress endpoint 和既有持久化 delivery worker 回送。不得从模型自然语言或混合 chunk 文本猜工具状态，也不得因进度发送失败重新执行任务。
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
  `/stop` 先持久将当前根任务转为 interrupted，再停止同一 typed lineage 的主执行域及其子代理树；已进入模型
  HTTP 读取时必须主动关闭当前传输。停止不删 transcript、task workspace、thread compact 或 memory；后续普通
  “继续”由模型通过精确 task id 选择原现场，不要求用户重发整段 prompt，也不从文字猜 task id。
  live turn 续接旧任务时，引导消费优先使用 `task_attributes.conversation_task_id`；本轮 request id 只是执行尝试。
  引导像 会话运行时 `TurnInput::UserInput` 一样进入当前 turn 的 provider-neutral `UserTurn`/text history；后续工具轮和
  compact continuation 通过 typed carrier 保留，不能改成 system injection、任务专属 guidance history 或第二份 prompt。
  子代理生命周期事件也在同一 active turn 的安全点按精确 task id 读取；新事件使旧模型动作失效，只有模型成功
  读取后才确认消费。启动当前后台轮的 wake 仍由 scheduler 单独确认，禁止 active turn 与 scheduler 双消费。
  `/status`、`/btw`、`/stop` 必须绕过同会话普通消息队列，由 CLI、Feishu 和未来 IM 共用；旧 `/btw` 列表、
  永久 prompt 注入和 `/btw-clear` 不再是产品能力。Gateway 生命周期 `POST /stop` 仍是管理员接口；控制目标
  只认 owner+thread 的持久 task link，自然语言“停一下/改一下”不获得硬控制权。
- `/goal` 是当前 conversation thread 上的特殊持久 overlay，不创建第二个聊天、Agent 或工作区事实源。每 thread 同时最多一个 active/paused/blocked 目标；它绑定一个持久根任务，通过去重 wake 自动续跑，只能由 typed command 暂停/恢复/修改/清除，由 `update_goal` 在真正完成或确实阻塞时进入终态。`/stop` 遇到 active goal 只暂停它，不清除目标。
- `/audit` 是显式前缀才能启用的特殊任务模式。入口只负责把 guarantee/window 写入结构化 task attributes，子代理按调度关系继承，watch 只读这些字段。prompt、goal、summary 或普通句子中出现 `/audit` 文字都不能激活保证。
- 子代理数量由主模型按真实可独立分解项决定，不向普通用户暴露固定数量命令。调度层同时核对本批/任务/owner/全局余量；整批超限就结构化拒绝，不静默截断、不边创建边失败，也不允许用重复假工作填数量。
- Feishu 入站回调只负责提交和即时反馈，模型执行不占用 WS/webhook 回调线程；最终回复由持久化 delivery worker 轮询同一 request_id 后回送，重启不得重新执行任务。
- 用户通道正文只能使用统一 user-facing projection；`MAIN_AGENT/RUN/SUBAGENT` 内部协议留在运行时，禁止原样进入 Gateway response、飞书回复或 assistant transcript。产物发送只有一个 `send_message` 工具：目标固定为当前 owner，附件必须命中该 owner 的 artifact registry、真实路径和 hash；同会话下一轮从 transcript metadata 复用最近产物，不能因“发我”重新生成或复制。
- 普通任务采用 会话运行时 式完成边界：主模型依据同一 thread history、真实工具结果、测试结果和子代理事实直接
  给出自然最终答复。运行时不再要求 `submit_for_acceptance`，不扫描目录推断完成，不生成完成 marker，
  也不在最终答复后运行 `delivery_snapshot` 重写短轮。Gateway/IM 只在统一出口移除内部协议并按可信
  `ReplyEnvelope` 投递；assistant transcript 保存同一份净化后的模型正文，避免交付层另造第二种回答。
- 普通任务没有独立 closeout ledger 硬门。文件存在、路径权限、附件 owner/hash、危险 effect 等客观安全事实
  仍在各自不可绕过的工具或投递边界校验；“任务是否做完”由主模型结合这些结构化事实判断。显式 `/goal`
  另由 typed goal 状态和 `update_goal` 收口，但它仍使用同一 conversation history，不恢复普通任务验收器。
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
- 模型能力自我描述必须按 installed/configured/healthy/current-bound 四层事实投影；adapter 存在、凭据齐全或代码中有工具都不能单独升级成当前可投递。四层状态全部从 composition root 的同一 `ChannelAdapterRegistry`、结构化 daemon health 和 owner conversation binding 投影；进程死亡、心跳过期、状态损坏或未绑定均 fail-closed。
- Compact 百分比只允许一个权威阈值：`context_window_tokens × configured_percent`。候选是否可提交也必须按包含 system/persona、summary、近期 raw tail、近期与已压缩工具事实及当前用户输入的完整下一轮投影低于这个阈值判断；不得再加未来输出预留、工具 digest 隐藏天花板或另一套 task compact。provider usage/tokenizer 缺失时的估算误差必须与阈值数学分开说明。
- Skill 运行时只有 composition root 创建的一个 `SkillsService`：每轮 snapshot 固定 `workspace > owner > shared > builtin`，prompt、检索、正文读取、能力自述与子代理都消费同一实例。`shared/indexes/skills.jsonl` 只是派生管理员清单；不得恢复 `SkillRegistry`、resolver/index loader 或编排入口临时 router。子代理 Skill 引用必须保存 stable id + content hash，后代只能收窄不能扩张。
- 长期 Memory 只有当前 owner `memory/long_term/memory.jsonl` 一个权威 operation ledger；`remember` 的 add/list/replace/remove/batch、daily mirror、SQLite/FTS 与向量召回都必须按稳定 entry ID 对照该 ledger 的 active 版本。派生索引不能让 tombstone 或旧版本复活，不得另建 IM/任务专用 Memory。
- Persona 当前正文只有 owner 的 SOUL/USER/AGENTS 三个权威文件，全部读取和变更必须经过同一个 `PersonaRepository`。版本 ledger/backups 只用于 CAS、审计和回滚；USER 只按当前用户原话自主维护，SOUL/AGENTS 仍需确认，确认期间 SHA 漂移必须拒绝覆盖。不得恢复直接文件写旁路或增加 IM 专用人格状态。
- 用户级持久定时只有 owner `data/scheduler/` 中的 `SchedulerRepository` 一份 job/run 事实源。`schedule` 是唯一 action tool，at/every/cron 均为 typed schema；到期 run 使用 CAS、先推进 next-run、claim TTL 和 heartbeat，并返回原 owner/thread 的同一代理主链。`wait` 仍只是 active task 内让出，不得成为第二 scheduler。对照 通道运行时 `src/cron/types.ts`/`schedule.ts`/`service/timer.ts` 与 长期助手 `cron/jobs.py`/`scheduler.py`/`tools/cronjob_tools.py`，不从用户文本推断身份、任务或时间状态。
- 可复用 Workflow 只有一种表达：Skill 提供方法，当前 thread 的 `task_progress` 保存计划，原生子代理工具显式创建和派发执行者。旧 `subagent_workflows` package、mode/config/CLI、extension hook 和 shared workflow index 已删除；不得恢复第二套 transcript、planner/router、自动批量扩张或模板执行 runtime。该边界对照 会话运行时 `turn_context.rs` 的逐轮 Skills snapshot、`plan.rs`/`plan_spec.rs` 的 typed plan 更新和 `multi_agents_spec.rs` 的显式 spawn，以及 模型助手 Code `plugins/feature-dev` 通过 command/agent prompt 调用原生 Todo/agent 能力的做法。
- 普通代码任务只有一份 owner-local 被动验证证据：公共工具出口按 `ToolCallEnvelope.scope.root_task_id` 记录项目 manifest 中的精确规范命令、真实 exit 与 targeted/full；成功文件写使旧证据 stale。它参考 长期助手 `verification_evidence.py`/`verify_hooks.py` 的被动账本，但不移植 stop hook，不执行测试、不阻止完成、不恢复普通任务验收器；模型继续按 会话运行时 的真实 tool-result 事实自然收口。
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
