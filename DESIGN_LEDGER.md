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
- 主代理长期记忆归 owner home；子代理只保留任务周期内可审计状态。
- 子代理可以写协作产物，但最终交付由主代理汇总和验收。
- 工具面要少，优先增强现有工具和运行时语义。
- 文件大小不是硬门；是否合并或拆分看调用链是否清楚。
- 大输出、compact、resume 必须靠 chunk、cursor、coverage ledger、archive 和 resume summary，不靠提示词提醒模型“别忘”。
- 参考成熟项目先于自己发明：typed protocol 参考 会话运行时/代理运行时，软 guidance/wait 参考 长期助手，subagent template 参考 模型助手 Code。
- 多用户命令隔离是执行节点启动硬门：所有 owner-scoped 前后台 shell 必须经 bwrap；缺失或自检失败返回结构化 `SANDBOX_UNAVAILABLE`，禁止降级宿主执行，也不走用户可见审批。
- 子代理的 `allowed_write_roots` 必须覆盖所有能启动进程的正式工具：文件写入、`run_command`、PTY 和 LSP 共用同一结构化写边界。bwrap 中 owner home 作为只读基座，仅把本轮精确授权根叠加为可写；不得解析 shell 文本、重定向或自然语言猜测写路径。PTY session 与 LSP server 还必须绑定创建时的 owner/task 写域，禁止按可猜 session/server id 跨域复用。
- 默认安装进入透明容器 CLI：用户仍调用 `my-agent`，包装器只挂当前工作区和 `~/.my-agent`；宿主 venv 仅为显式 `--host` 开发模式。企业 worker 在启动和 K8s readiness 重跑同一 sandbox 自检。
- 发布干净度分两层：工作树门检查 tracked 脏文件和未忽略 untracked 文件；制品门直接检查 wheel/zip/tar 内容、运行状态目录和大小预算。`.gitignore` 不是发布安全事实。
- 普通通道对话以 `owner + channel + chat/topic` 的持久 transcript 为唯一多轮事实源；旧 dialogue memory 不得重复注入或挤占稳定偏好。自然语言不自动绑定旧任务，只有结构化任务工具选择/提升；结构化 closeout 完成后关闭热候选。
- 停止/续接只认会话中的持久 task link：`/stop` 保留 interrupted task 和 workspace；后续模型若要工作，必须先用唯一参数 `task_progress(action=select, task_id=...)` 精确续接，或用 `action=start` 明确新建。任何带 `promotes_task` 的工作工具、`create_subagents` 和 `wait` 在选择完成前统一 fail-closed；选择失败不得落入懒晋升。禁止从“继续、接着做、重来”等自然语言推断 task id。该边界对照 会话运行时 `Session::steer_input/interrupt_task` 的 active-turn id + cancellation、通道运行时 active session run queue/abort；IM 只负责把结构化 conversation/task/run 关联送入同一状态机。
- 原生子代理创建入口必须有机器可校验的目标：`create_subagents.goal` 始终 required；单子代理直接使用该目标，`items` 批量模式同时携带总 goal 与每项独立 goal。不能只在工具说明里声称必填后容许空 `tool_use`。该约束对照 会话运行时 v2 `spawn_agent` 的 required `task_name + message`，不靠模型自然语言补救。
- gateway 请求进入终态归档时，response 的 `done/interrupted/failed` 是最终状态权威；processing lease 只提供 owner/attempt/heartbeat 等运行字段，不能覆盖终态。归档目录、请求 JSON、response 与 `/status` 必须表达同一事实。
- 恢复任务后，新的 request/run id 只表示这次执行尝试，不得成为新的任务事实源。default 主代理的 guidance、task_progress 工具、需求/派工 seed、coverage、wait、监督提醒和 delivery closeout 必须统一读取结构化 `conversation_task_id`；task_local 子代理仍按自己的 run id 隔离。该解析只保留一个共享实现，禁止各模块复制一套优先级。
- 租户可见路径默认取最小权限：远程 owner 只能读写自己的 owner home，另外可读组织明确发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板和旧顶层私有目录一律拒绝。外部目录只能由当前轮的结构化 capability/delivery contract 精确加入 workspace roots，不能由模型给出绝对路径自我授权；该授权也不能覆盖凭据文件或其他 owner 拒绝。随 wheel 发布的基础 tools/skills 是公共产品能力，shared 只用于组织显式共享的 skills/tools/workflows；个人 USER/SOUL、记忆、任务和产物不得由 shared 或 full mode 绕过。
- 普通通道上下文必须在同一结构化 scope 内“累计 transcript → 自动 compact → 继续累计”：raw transcript 永不因 compact 改写或删除，thread JSON 的 summary+cursor+generation 是唯一 compact 状态；旧消息只进入该 owner 的 LocalStore 派生检索索引。固定最近轮数不得再充当遗忘边界。
- 同一用户可以在后台 TaskRun 运行时继续普通聊天，但两条上下文权限不同：普通聊天继续使用 thread transcript
  与 compact；后台任务只认结构化 task lineage 的消息/观察/wake、权威 task link 以及显式 task guidance。thread
  compact、其他 request 消息和其他 task goal 不得进入后台任务 prompt；自然聊天不能暗中 steer 任务，
  只有 `/btw` 能写一次性 task guidance。
- `/verbose off|on|full` 是 per-thread 持久设置；工具进度必须以 typed event 进入 Gateway，再由有身份校验的 progress endpoint 和既有持久化 delivery worker 回送。不得从模型自然语言或混合 chunk 文本猜工具状态，也不得因进度发送失败重新执行任务。
- 普通会话控制只有一份 typed protocol：`/status` 只读当前 durable root task/request/thread/子代理事实且
  不回放引导；`/btw <内容>` 只投递给当前 active 根任务（尚未晋升才投当前 request）、消费一次后终止；
  同一 conversation/task 任一时刻只允许一个主执行 turn；已有 linked live turn 时，`/btw`
  只写入该 turn 会消费的 FIFO guidance，禁止再发 wake 启动第二个执行器。只有根任务当前没有
  linked live turn 时才可发布去重 wake。`/stop` 先持久将当前根任务转为 interrupted，
  再停止同一 typed lineage 的前台/后台主执行域及其子代理树；已进入模型 HTTP 读取时必须
  主动关闭当前传输，不能只等下一个工具安全点或整个 provider timeout。停止不删 transcript、
  task workspace、compact 或 memory。之后的普通“继续”可由模型通过精确 task id 选择重开原现场，
  不需要用户重发整段 prompt。
  live turn 续接旧任务时，引导消费必须优先使用 `task_attributes.conversation_task_id`；本轮 gateway
  request 自己的 `task_id` 只是 turn 身份，不能拿它读取持久 task guidance。
  三者必须绕过同会话普通消息单飞队列，由 CLI、Feishu 和未来 IM 共用；旧 `/btw` 列表、永久
  prompt 注入和 `/btw-clear` 不再是产品能力。Gateway 生命周期 `POST /stop` 仍是管理员接口，不能
  与用户任务停止混用。控制目标必须沿 owner+thread 的持久 task link，不能只看短暂 processing request；
  自然语言“停一下/改一下”不获得硬控制权。
- `/goal` 是当前 conversation thread 上的特殊持久 overlay，不创建第二个聊天、Agent 或工作区事实源。每 thread 同时最多一个 active/paused/blocked 目标；它绑定一个持久根任务，通过去重 wake 自动续跑，只能由 typed command 暂停/恢复/修改/清除，由 `update_goal` 在真正完成或确实阻塞时进入终态。`/stop` 遇到 active goal 只暂停它，不清除目标。
- `/audit` 是显式前缀才能启用的特殊任务模式。入口只负责把 guarantee/window 写入结构化 task attributes，子代理按调度关系继承，watch 只读这些字段。prompt、goal、summary 或普通句子中出现 `/audit` 文字都不能激活保证。
- 子代理数量由主模型按真实可独立分解项决定，不向普通用户暴露固定数量命令。调度层同时核对本批/任务/owner/全局余量；整批超限就结构化拒绝，不静默截断、不边创建边失败，也不允许用重复假工作填数量。
- Feishu 入站回调只负责提交和即时反馈，模型执行不占用 WS/webhook 回调线程；最终回复由持久化 delivery worker 轮询同一 request_id 后回送，重启不得重新执行任务。
- 用户通道正文只能使用统一 user-facing projection；`MAIN_AGENT/RUN/SUBAGENT` 内部协议留在运行时，禁止原样进入 Gateway response、飞书回复或 assistant transcript。产物发送只有一个 `send_message` 工具：目标固定为当前 owner，附件必须命中该 owner 的 artifact registry、真实路径和 hash；同会话下一轮从 transcript metadata 复用最近产物，不能因“发我”重新生成或复制。
- 内部完成协议与用户完成摘要必须分栏：模型自然最终答复与
  `submit_for_acceptance summary/note` 都只是待核对表达草稿，绝不参与验收判定。全部 closeout 门结束后，
  运行时冻结一份不含宿主路径的 `delivery_snapshot`（文件名、实际字节数、SHA-256、进度和 gate 状态），
  IM/Gateway 再用同一个 LLM 的无工具短轮基于快照重新组织最终话语。旧草稿与快照冲突时丢弃；没有合格
  模型正文时保留机器完成信封和附件事实但抑制模板正文。assistant transcript 只保存清洗后的模型摘要，
  不能再用“只有文件清单”牺牲后续上下文。参考 通道运行时 的最终文本清洗边界与 长期助手 的执行输出/最终答复分离。
- closeout ledger 是任务运行的内部硬边界，但“必须产出文件”不是默认硬门。纯分析、问答或只需人话结论的任务，只要子代理、进度和能力请求均已收口，可以 `delivery_mode=message` 结束；只有显式 artifact contract/expected output 要求文件时，缺文件才是必须返工的硬边界。
- 所有普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`：可信 `DeliveryContext` 单独持有 channel/target/reply_to，`ReplyEnvelope` 永远不带收件人；adapter/capabilities/target validator 只能通过 `ChannelAdapterRegistry` 注册，新增 IM 不得在投递主流程增加平台分支。
- 除 `/status`、`/stop` 等显式控制命令外，普通聊天、派工回执、等待说明、进度与完成说明的用户正文必须来自 LLM。运行时只提供结构化事实、禁用回执轮工具并校验/净化输出；不得用“任务正在处理”等固定系统句子替换模型正文。没有合格模型正文时宁可记录结构化失败并抑制投递，也不能用模板冒充 Agent 回答。
- 后台主代理 claim 的 `heartbeat=0` 明确表示按 TTL 自动取间隔；默认 TTL 为 90 秒。claim 保存
  `process-domain + pid + start_time`，只有同一 PID namespace/主机进程域能证明旧进程已死时才提前接管；
  跨 Pod、旧格式或身份不足时必须等待 TTL，不能把“当前容器看不见 PID”当作已死。
- Gateway SIGTERM/SIGINT 必须先写 typed stop request 与轻量 forensics（signal、pid/ppid、父命令、systemd
  环境），再复用现有 stop-file drain；计划 stop 已存在时只能追加 observed，不能改写为异常退出。
- 人格三件套只有 `update_persona` 一个写入口：USER 可由 Agent 自主维护，SOUL/AGENTS 必须用户确认；基础文件、patch、shell 和 admin full-access sandbox 均不得形成旁路。
- Harvester cursor 是一批事件已完成 engine、spool、audit 的提交水位，不是网络读取进度预告；批内任一步失败都必须保留旧 cursor 以便重试，禁止先推进游标再处理造成静默丢事件。

当前入口文档：

- 当前产品事实与 P0 冻结边界：`docs/PRODUCT_FACTS.md`
- 架构总览：`docs/design/ARCHITECTURE_GUIDE.md`
- 模块结构：`docs/architecture/MODULE_OWNERSHIP.md`
- Home 布局：`docs/architecture/MY_AGENT_HOME_LAYOUT.md`
- Subagent：`docs/modules/subagent/04-structure.md`
- Memory：`docs/modules/memory/04-structure.md`
- Gateway：`docs/modules/gateway/04-structure.md`
- 多 IM 统一投递：`docs/design/CHANNEL_DELIVERY_DESIGN.md`
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
