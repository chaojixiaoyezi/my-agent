# Gateway Progress

## 2026-07-24 工具参数 Schema 单一入口发布与双真实 owner 复验

- 代码参考固定在 会话运行时 `808d3c27` 的 `会话运行时-rs/core/src/tools/router.rs` typed
  `serde_json::from_value` 入口和各 handler 的 `JsonSchema`，以及 长期助手 `91546b83` 的
  `model_tools.py::coerce_tool_args`、registry schema 和 MCP schema 处理。my-agent 没有复制第二套
  provider 专用校验器，而是把既有 ToolSpec Schema 作为 provider 展示、text/native 解析、恢复、
  MCP 注册和最终执行的唯一参数事实源。
- 强类型纠正只处理无歧义的整数/数字、boolean、null 和合法 JSON array/object 字符串；随后在
  effect、审批、路径和 handler 前统一检查 required、类型、enum/const、嵌套对象、
  `additionalProperties`、长度/范围、组合规则和本地 `$ref`。外层 ToolCallEnvelope 与工具参数分离，
  Schema 明确声明的 `kind/run_id/status/metadata/artifact_refs` 不再因协议同名而被误删或绕过。
  已删除旧 required/type 拍平副本和入口特判；MCP 畸形或不支持的 assertion 在注册时跳过单工具，
  不会宽松透传到执行器。
- 本地完整 pytest 两次均到 100% 且退出 0；Ruff、import/offline、strict code-size、doc-sync、
  compile/diff、distribution boundary 与 wheel artifact clean gate 均通过。本地 8899 Qwen 和
  MiniMax-M2.7 均完成真实 `PATH_NOT_FOUND → 替代读取 → 写出 → 回读` 工具恢复，缺失输入未被创建。
- 提交 `5d0822419d3bb36d758433cc10ea500cfec1fa2b` 已推送远程 `main`。从该精确提交构建的
  wheel SHA-256 为 `a0c0c72d9246c18512209af00c734ad94f2994392c806d5fe0e10ecc12a86460`；
  部署后本地源码树、1.10 staged 源码树和 venv 安装树的 18 个改动生产文件 SHA-256 全部一致，
  Linux bwrap sandbox 探针通过。
- 正式 8420 沿两个既有真实飞书 owner 和原 conversation 并发只读复验。请求
  `req_1784876282599_415881_0` 由 A 实际调用 `list_files`，工具路径只落在 A owner；B 第一条回答
  因 `tool_rounds=0` 未计作工具证据，随后请求 `req_1784876388592_415881_3` 实际调用
  `read_file`，收到 `PATH_NOT_FOUND` 后再调用 `list_files` 核实，两个工具路径都只落在 B owner。
  两边 `conversation_persist_degraded=false`，最终正文不含工具 XML、内部进度或绝对 owner 路径，
  失败读取没有创建缺失文件。该轮是可信 localhost 的 Feishu scope 主链测试，不冒充新的客户端入站。
- 最终 1.10 仍只运行 `my-agent-gateway.service` 与 `my-agent-feishu.service`，唯一监听端口为
  loopback 8420，模型保持 `anthropic_compatible + MiniMax-M2.7`；两项服务 active、
  `NRestarts=0`、Gateway 队列为空、Feishu WebSocket connected。1.9 未触碰。

## 2026-07-24 MCP 恢复、能力自述与双真实飞书用户收口

- 代码级复核使用当前干净参考：会话运行时 `808d3c27` 的
  `会话运行时-rs/core/src/session/step_context.rs`、`tools/router.rs` 和 spec plan 把一次 sampling
  的 MCP binding、模型可见 Schema 与最终 dispatch 固定在同一 `StepContext`；长期助手 `91546b83` 的
  `tools/registry.py` 用 `ToolEntry.check_fn`、registry `RLock/generation` 形成稳定快照，
  `tools/tool_search.py::scoped_deferrable_names` 又在 bridge 调用前复核当前 session scope。
- my-agent 没有复制第二套 registry 或 长期助手 的全局 generation cache，而是在既有
  `ToolRuntimeSnapshot` 窄腰上补齐两个漏口：`list_capabilities` 也消费当前 run 的同一快照，
  未授权工具完全隐藏，已授权但当前不可用的能力只显示为 unavailable，私有 readiness 原因不出站；
  `list_tools/tool_search/Schema/execute` 继续只在同一交集内做减法。
- MCP availability 查询保持无副作用；真正重连只发生在下一次 run 固定快照之前。启动失败的合法 MCP
  配置不再被永久丢弃，断线 client 由单连接 lifecycle lock 串行重建，失败按 1–60 秒有界指数退避。
  重新握手后以整张 dict 指针替换方式发布该 client 的精确新目录：旧 proxy 删除、同名 builtin 保留、
  冲突工具跳过。已经开始的 run 不会因新目录而扩大权限；若其旧实现已被替换或连接掉线，调用在实现前
  fail-closed，不把目录刷新伪装成当前 run 的热升级。
- 本地聚焦回归覆盖 stdio 进程死亡后同 binding 重连、启动失败后下一 run 恢复、工具目录
  `before → after` 精确替换、重连退避和 builtin 不丢失。普通 CLI 又由本地 8899 模型真实调用
  `list_tools → list_files → read_file`，读取隔离标记成功；真实 MCP echo/add、浏览器、clangd、网络、
  shell/PTY/process、Scheduler、Memory/Persona/Skill/compact 等工具族沿各自安全测试面复验，没有为
  飞书增加工具分支。
- 1.10 最终 wheel（SHA-256
  `e07e9b9ec75d846be683c6b3a711c88e1044690d00f90be0b631976309f65acd`）只运行唯一正式 Gateway（8420）
  和 Feishu 长连接。真实平台用户
  `ou_1be…f921` 从飞书客户端发起文件任务，并在同一 active request 发送 `/btw`；权威
  `guidance_delivered.json` 只消费一次，同一 thread 最终只有一个
  `output/飞书工具链复验.md`，准确包含原要求和引导两行，最终回复经原平台消息引用投递。此前真实平台
  用户 `ou_6591…a895` 的长任务与中途聊天闭环仍保留，因此已有两个不同真实用户各自的平台入站证据。
- 本地模型轮暴露的是模型效率问题：它多次猜错 owner/date 路径，但 owner gate 均在副作用前拒绝，
  随后模型从 run context 找回精确旧 task 并完成；没有用日期、用户名或中文任务内容加底座特判。
  MiniMax-M2.7 在 00:00 CST 刷新后的最小探针为 HTTP 200/`MINIMAX_OK`，队列为空时同一正式服务安全
  切回供应商。两个既有 owner 并发只读复验中，A 实际调用
  `list_capabilities/list_tools/read_file` 并只读到自己文件；B 的跨 owner `read_file` 返回
  `TOOL_INVALID_ARGUMENTS`。这两轮是可信 localhost 的 Feishu scope 主链测试，不冒充新的平台客户端入站。
- 两项服务最终均 active、`NRestarts=0`、8420 只监听 loopback、Feishu WebSocket connected。仍未覆盖
  MCP 主流 server 长稳、浏览器/LSP 的多版本组合、两个真实客户端同时跑长任务和十万 owner 容量。
- 最终 fast/slow pytest、架构守卫、Ruff、compileall、import/offline/strict code-size/doc-sync、
  distribution boundary 和 wheel clean-package 均通过；strict code-size 为
  `hard=0 / high-risk=170 / soft=61 / blocked=False`。worktree clean-package 只因明确保留的未跟踪
  `data/` 与 handoff 文档失败，并同时报告数 GB 运行数据；这些内容不在 wheel 中。
- 精确部署后请求 `req_1784828035414_319820_0` 在同一真实 owner/conversation 上由 MiniMax-M2.7
  实际成功调用 `list_capabilities/list_tools`，返回 45 个可用工具，视觉与 LSP 均未虚报；请求没有调用
  Memory、文件、消息或其他副作用工具。源码树、venv 安装树与本地七个改动模块逐文件 SHA-256 一致，
  队列最终为 `pending=0 / processing=0`。

## 2026-07-23 单一工具运行快照与双模型双 owner 反证

- 代码级第一参考为 会话运行时 每个 turn 固定 `StepContext/spec plan`，第二参考为 长期助手 的 session toolset、
  `check_fn` 与 search bridge 复核。my-agent 适配为每个 run 一个 `ToolRuntimeSnapshot`：目录、推荐、
  原生 Schema、`list_tools`、`tool_search` 和执行入口只能消费同一份
  `registered ∩ owner policy ∩ allowed_tools ∩ availability`，后续工具就绪不能在本轮扩权。
- `availability` 与授权分离：授权先 fail-closed，避免向未授权主体泄露 readiness；执行前再实时复检。
  视觉/LSP 的空配置、已退出 MCP 和不可用浏览器不会发给模型，检查过程不启动进程、浏览器、LSP、
  MCP 或网络请求。旧 `granted_capabilities` 没有任何工具要求或授权消费者，已从公开 run 参数、
  context、manifest、registry 和测试中整条删除；真实扩权仍只认 owner policy、`allowed_tools` 与现有
  typed grant ledger。
- 首次普通 CLI 真测只设置 `MY_AGENT_HOME`，但正式配置已经显式给出 `my_agent_home`；既有合同和回归都
  是“显式配置优先、留空才回落环境变量”。没有为测试改变正式优先级，只把写反的配置注释纠正；最终
  隔离复验使用独立临时配置文件，profile 只写 `/tmp`，真实工具记录只有
  `list_tools/list_files/read_file`。
- 本轮实际部署 wheel SHA-256 `fd9e6a1c28ff8235ddda59cf6db878b7ca45d4bb5415374f328beb3fc88adcfd`
  已装到 1.10。先用本地 8899，再在 20:00 CST 刷新点后用最小探针确认 MiniMax-M2.7 返回
  `MINIMAX_OK`，随后在队列为空时沿同一配置/服务链切回供应商；没有第二个 Gateway、飞书适配器或端口。
- 两个模型都完成普通 CLI 真实只读工具调用；正式 8420 又复用 Chi/Chalk 两个已有 Feishu-scoped
  合成 owner 与各自原 conversation 并发测试。四个 Feishu 请求都只调用 `list_tools/read_file`，
  `used_memories=0`；各自读取自己的标记成功，跨 owner 读取返回结构化拒绝，B 读取 A 的 result endpoint
  为 403。两边 USER/SOUL/AGENTS、Memory、Skill、tool policy 与 Scheduler 测前测后 SHA-256 相同，
  transcript 不含对方标记，临时文件已删除，用户正文和 channel delivery 均无内部协议。
- 当前正式配置为 `anthropic_compatible + MiniMax-M2.7`，Gateway/Feishu active、`NRestarts=0`、
  队列为空且飞书 WebSocket connected。以上是服务器侧 Feishu scope 主链测试，不冒充两个平台客户端
  真实入站或收件证明。

## 2026-07-21 Provider 原生多轮历史与本地模型连续运行

- [MiniMax Anthropic 兼容接口](https://platform.minimax.io/docs/api-reference/text-anthropic-api)要求多轮
  function call 把上一响应的完整有序 `content` 回放到历史，其中包括 thinking/signature、text 与
  tool_use。旧实现只保留可见 text 和重建后的 tool_use，签名推理块在下一轮丢失。该问题是 provider
  协议历史不完整，不是任务 prompt 或具体模型名称问题。
- 代码级第一参考为 会话运行时 的 typed `ResponseItem::Reasoning`、completed response item 记录和 conversation
  history 回放；长期助手 的 Anthropic adapter 只用于核对 replay block 白名单及 thinking signature 边界。
  当前候选在 `ModelResponse -> AssistantTurn -> message adapter` 唯一链保存有序白名单块；thinking 不进入
  可见 chunk、最终正文或 transcript，tool_use 的 id/name/input 仍由 canonical ToolCall 覆盖。compact 若
  改变原始块则清除对应签名块，不能拼接失效签名。
- 非流式、流式重建、下一轮回放、工具字段权威、无 reasoning 泄露、截断终态和 compact 失效处理的
  聚焦回归共 71 项通过，相关 Ruff 通过。最终完整门禁仍按收口阶段只运行一次，不把聚焦通过提前写成
  发布完成。
- 供应商 quota/unavailable 是结构化运行状态：MiniMax 未刷新时，1.10 唯一正式 Gateway 立即切到已探活的
  本地 8899 继续原 owner/thread/task，不等待、不新建测试服务或第二条会话。只有没有 live request、到达
  配置刷新点且供应商探活成功时才安全切回；4000 仅在自身探活成功时作为后备。

## 2026-07-21 会话运行时 式 thread 工作目录跨轮继承

- 真实双长任务在停止后能够通过显式 `task_progress select` 找回项目，但这仍与 会话运行时 单个 task 的体验有
  差距：会话运行时 在 `会话运行时-rs/core/src/session/session.rs` 的 `SessionConfiguration.environments` 中持久保存
  thread 级环境/cwd，`apply` 在新 turn 没有覆盖值时继承旧值；`turn_context.rs` 每轮都从该 session
  configuration 重建 TurnContext。中断 active turn 不会清空 cwd。
- 当前工作树把同一语义适配到现有 conversation/task 事实源：`ConversationThread` 升级为 v3，并以唯一
  `workspace_task_id` 保存精确根任务。Gateway 入站只把它投影成 cwd；纯聊天没有
  `conversation_task_turn_active`，不会重开 task link、写任务运行或在结束时误关闭任务。第一个
  `promotes_task` 工具才激活该精确任务；`select` 只切换其他候选，`start + new_task=true` 才新建并切换。
  旧 v1/v2 数据只在持续目标精确命中或仅有一个合法根任务时无歧义迁移，正文不参与判断。
- store 是 sticky workspace 的唯一耐久写入口，写前核验同一 thread 的 task link 和索引；选择完成后才向
  Gateway processing record 发布 `thread_id/task_id/task_path`。聚焦回归覆盖完成/中断后的纯聊天、首次
  工作工具激活、重启后继承、显式新任务切换、后台主代理和子代理不误关父任务，相关会话/后台套件全绿。
- 最终 wheel `4b882778…bdcb` 已部署到 1.10 唯一正式 Gateway/Feishu 运行面，两个服务 active 且
  `NRestarts=0`。两个 Feishu-scoped 长任务请求均在首次工作工具后发布原 thread/task/path：Chi 进入原
  `chipy` 项目并独立跑出 51/51；Chalk 进入原 `pychalk` 项目，在供应商不可用时直接使用本地 8899 分段
  修复，远端原项目和逐文件哈希一致的独立干净副本最终均为 42/42。两项质量都以请求终态后的独立副本
  验收为准，本节不接受模型自报提前升级。

## 2026-07-21 1.10 正式飞书单运行面

- 1.10 的真机测试拓扑固定为唯一正式 `my-agent-gateway.service`（`127.0.0.1:8420`）和唯一正式
  `my-agent-feishu.service`（飞书长连接）。隔离 Gateway、额外飞书适配器及 `8421`–`8423` 测试端口全部
  退出后续测试链；模型后端切换仍发生在这同一正式运行面内。
- 每次部署和测试都先后核对 systemd 单元、监听端口、进程与 `NRestarts`。2026-07-21 本轮核验只有上述
  两项服务和 `8420` 监听，二者均 active 且 `NRestarts=0`；飞书 WebSocket 已连接。
- 同一正式服务上用 10 个 Feishu-scoped 合成 owner 做三轮上下文反证。第一轮 10/10 各自回复正确词，
  但本地 Qwen 10/10 误调用长期 Memory；第二轮 10/10 召回且 `used_memories=1`。通过正式版本化 remove
  接口 tombstone 全部合成记忆后，第三轮 `used_memories=0` 仍 10/10 找回各自词且无串 owner，9 条逐字
  一致、1 条多出空格。该失败保留为模型质量事实，没有用自然语言特判掩盖。
- 当前候选 wheel `739b330d…6019f4` 已通过 distribution boundary 与 clean-package artifact，并安装到
  正式服务；普通
  `create_skill` 工具已删除，供应商额度耗尽有正式错误合同。模型仍临时指向本地 8899，刷新后将在同一
  8420/Feishu 运行面切回 MiniMax，不创建测试旁路。
- 完整本地 pytest 在 83% 处发现 `task_local` 子代理误进入主代理的用户回复阶段：子代理已经生成的
  `SUBAGENT_RESULT` 会被改写成普通正文，外层因而一直认为子代理没有完成并重复 compact。对照 会话运行时
  为 child thread 显式保存 `SessionSource::SubAgent` 和 `parent_thread_id` 的边界，候选只按已有的结构化
  `context_scope=task_local` 禁止该用户出口阶段；不解析代理名称或任务文字。原无限循环回归现以 5 次
  backend 调用结束，完整本地 pytest 已运行到 100% 并通过。
- 同一轮完整测试还证明旧窗口逻辑只限制 `tool_context` 不够：工具目录、Persona、任务正文和原生工具
  message 合计后曾形成 `40054 > 40000` token。候选复用 conversation 已有的整段模型输入计量，把非会话
  工具轮也按完整 provider 可见输入回收最旧工具对；raw archive 仍保留，未增加第二套 compact。
- 同一正式 8420/Feishu owner 主链又用本地 Qwen 完成一轮精确三子代理真测：模型只提交一次
  `create_subagents(items=3)`，最终三个 canonical child 均为 `DONE/VERIFIED`，没有第 4 个 child；
  `python-context.md`、`http-idempotency.md`、`sqlite-wal.md` 三个产物均存在、非空且 SHA-256 不同。主代理
  在全部 child 终态后才写自然中文汇总，普通 transcript 没有子代理命令或内部协议。整轮约 34 分钟；
  本地模型反复尝试被 `USE_WAIT_FOR_DELAY` 拒绝的 shell `sleep` 并过度搜索，保留为模型效率失败，不用
  prompt 关键词或项目特判掩盖。
- 该轮运行中一条真实 `/btw` 以 task-scoped typed guidance 进入同一 thread，消费一次并写回 transcript。
  它到达时，本地 provider 的旧流随后两次返回 `MODEL_EMPTY_RESPONSE`；旧前台 Gateway 记录因此失败，但
  同一 durable task 被现有 background claim 接管并在第三个 child 结束后正确收口。对照 会话运行时
  `session/mod.rs::steer_input` 把输入加入 active turn 队列的行为，候选现把“已有 pending turn input 时旧
  provider 流结束为空”视为过期输出：在安全点把 typed input 注入原 turn 后重试，成功响应前仍不确认
  guidance。没有按 `/btw` 文本内容判断；runtime-guidance、Gateway control 和空响应回归三组聚焦测试通过。

## 2026-07-20 会话运行时 式 active turn、Compact 溢出恢复与真实双长任务候选

- 普通 Gateway conversation 以前被 task identity resolver 当成非 main scope，导致 `/btw` 已写入同一
  durable task，却可能在真实工具循环里按当前 request id 取错账本。候选把结构化
  `context_scope=conversation` 纳入 main-agent scope；child/control scope 仍保持隔离，没有从中文正文猜
  “这是不是续作”。聚焦回归和 1.10 同一任务实测均证明引导各消费一次，且没有启动第二个 executor。
- 对照 会话运行时 “active turn input 保留在 compact item 之外”的行为，当前 user message 即使已先持久化，也从
  本次 summary 输入中排除；历史压缩为单一 summary，raw transcript 继续保留。provider 返回结构化
  `context_overflow` 时，Gateway 强制推进同一 thread generation 后重试同一 turn；generation 不前进或
  压缩后仍溢出则明确失败，不重开会话。
- 1.10 本地 Qwen 真机压力轮已独立复核：模型窗口 30,000、配置 90%，事件准确记录
  `trigger_tokens=27000`、generation 1，压缩后当前上下文估算降到 22,134；会话暗号在后续提问及服务重启
  后均正确召回。该证据证明单一 thread 的阈值和恢复，不外推为所有供应商的极限质量。
- 同轮在隔离服务上让两个 Feishu-scoped owner 分别复刻 Chalk 与 Chi 的 Python 版本，并在活跃 turn 中各
  注入一条 `/btw`。截至本节记录时两个请求仍在原 task 内持续修错、运行测试，服务 active、零自动重启；
  最终质量和干净交付仍须等请求终态后由外部独立验收，不能用模型过程自述提前升级。
- `write_file` 恢复普通明确语义：省略 `mode` 始终覆盖，只有显式 `mode=append` 才追加。已删除根据“任务
  目录里同名文件存在”偷偷改成 append 的运行时分支，避免返修完整文件时把第二份模块拼在旧内容后。
- 双长任务真机 `/stop` 首次暴露模型传输关闭回调会同步拖住控制回复十余秒。对照 会话运行时 取消 token 后
  只等 `100 ms` 再 abort task handle 的实现，当前 typed interrupt 仍立即立旗，但关闭回调只在前台有界
  等待 `100 ms`，余下幂等清理由 daemon thread 完成。新增慢回调回归证明 control ack 小于 `0.5 s`；
  两个真实 turn 均进入 `interrupted` 后，普通中文“继续”通过 `task_progress select` 精确重开原 durable
  task，而非新建任务或项目。
- 真机 `/status` 还发现任务原文中的宿主绝对路径没有经过已有的外部出口脱敏。修正放在
  adapter-neutral 的 control result 边界，任务摘要和最近进展在确定性文字、typed DTO 两种投影中都只保留
  basename；没有新增飞书分支，内部 transcript 与结构化 task path 继续保留完整路径用于续接。
- 同一次真机检查还发现失败 turn 已没有 processing record、活跃 child 或 live claim，但 durable task link
  为了允许后续“继续”仍保持 active，旧 `/status` 因而误报运行数小时。候选按 会话运行时 的 persistent task 与
  active turn 分层：durable link 继续可选择和续接；只有 processing record、活跃 child 或结构化 execution
  source 才显示 running。无执行器时显示 idle，并清空旧 elapsed/progress；聚焦控制回归通过。
- 两个本地 Qwen 长任务虽然一直收到有效流式 chunk，旧模型 guard 仍在累计墙钟时间超过配置后报
  `ProviderTimeoutError`。对照 会话运行时 `provider.rs` 的 `stream_idle_timeout` 与 `sse/responses.rs` 的逐次
  `stream.next()` 等待，候选把流式 `request_timeout` 收敛为空闲超时：有效 `data:` 事件重置等待，注释、
  半行和静默不重置；工具循环不再把同一数值叠加成总时长上限。非流式 backend 的总时长保护、typed
  `/stop` 和工具安全点保持不变。聚焦回归已覆盖“总运行时间超过配置但持续有事件仍成功”与真正静默超时。
  本地 Qwen 真机进一步以 `5 s` idle timeout 运行 `7.807 s`，收到 `323` 条有效 SSE data，首条
  `0.124 s`、最大事件间隔 `0.066 s`，证明总时长超过阈值但持续有进展时不会被误杀。

## 2026-07-19 前台与后台共用唯一 conversation execution lane

- 1.10 的 B owner 在 `/stop` 后自然续作时，foreground request 与 `scheduled_progress_report` 对同一个
  `thread-2cad… + req_1784434105431…` 并行。后台在 12:24 主动发送“项目完成”，但前台随后仍执行
  13 个以上工具轮并在约 20 分钟后才真正 `done`；后台消息 metadata 明确记录
  `background_delivery_reason=internal_scheduled_completion`，不是工具主动消息或最终 request 回复。
- 具体代码参考不是概念类比：会话运行时 `会话运行时-rs/core/src/session/inject.rs` 在自动 idle turn 前原子预留
  `active_turn` 并在 pending input 竞态下撤销；通道运行时 `src/process/command-queue.ts` 用 lane queue 串行，
  `src/infra/heartbeat-runner.ts` 在 resolved session lane busy 时跳过 heartbeat。候选复用本项目已有
  conversation claim 文件作为持久 lane，不增加飞书分支或自然语言判断。
- `conversation/run_claim.py` 现在承载 claim heartbeat 和 foreground lane 生命周期；
  `gateway_parts/request_execution.py` 只在 lane 外解析 durable thread identity，拿到执行权后才读取
  compact/history/task state，并把 user append、模型 turn、assistant append 全部包在同一租约内。后台
  scheduler 仍走同一 store claim，因此同 thread 拿不到 claim 就不启动；不同 owner/thread 不互锁。
- 三个新增竞态回归覆盖 foreground/background 互斥、等待后新鲜历史以及两 foreground 串行，连续五轮
  无抖动；Gateway/conversation/scheduler 相关 196 项与 Ruff、strict code-size、diff 已通过。完整本地门、
  提交、精确部署和真实 LLM 双 owner 复测仍待下阶段。

## 2026-07-19 Scheduler 到期索引与飞书单次投递收口

- 真实 1.10 压测先暴露旧 Gateway 的 owner-page 轮扫会让短提醒在 135 个 owner 下晚约 159 秒。
  对照 通道运行时 `cron/service/timer.ts` 的最早 `nextRunAt` 定时器，新增全局 SQLite due-owner 投影；它只含
  owner 身份、最早到期时间和短租约，owner `data/scheduler/store.json` 仍是唯一 job/run 权威，实际执行前
  必须回到 owner 账本二次校验。升级扫描遇到不可读账本会在下次重启重试，且不跟随 owner symlink。
- 1.10 上已完成真实重启和常驻时延反证：合成 owner F 在到期后 2.18 秒被重启后的 Gateway claim；
  真实 Feishu owner 的后续两次 one-shot 分别在到期后 0.44 秒和 5.67 秒 claim。三者均只有一个 scheduler
  history run。`wait` 同时固定为 `internal` 路由，只唤醒当前 Agent，不再与用户提醒混淆。
- 第一次真实提醒暴露模型调用 `send_message` 后后台又自动发送最终回复的双发。按 通道运行时
  `embedded-agent-subscribe.handlers.tools.ts` 提交消息投递证据、`cron/isolated-agent/run.ts` 判断 source
  delivery 的代码路径，`send_message` 成功结果现在带 `message_tool_delivery.v1` 内部回执；scheduled run
  只把实际已发内容按 receipt 幂等镜像回原 transcript，并跳过兜底投递。普通任务中途主动消息不改变其
  最终回复语义。
- 候选重新部署后，自动兜底路径只有一次 `NATIVE_CHANNEL_SEND_OK`；明确要求主动飞书发送的真实 LLM
  路径历史为 `scheduled_message_tool_delivery`，同一时间窗也只有一次原生出站，原 transcript 只有一条
  对应最终提醒。Gateway/Feishu 均 active、`NRestarts=0`，1.9 未改动。完整 fast/slow pytest 与全部本地
  静态/制品门已通过；最终提交/push 和精确 commit wheel 重部署仍待本轮最终收口。

## 2026-07-18 基础能力发布、双 owner 长任务与运行时候选修正

- 基础能力提交 `5da7e21e` 已推送 `main`，干净 wheel SHA-256 为
  `ae24bbd1ab149ae3f097e480080f59231aadd55ccf0d1f38138fec0a94e591e0`；1.10 精确安装同一
  wheel/source，Gateway 与 Feishu active、`NRestarts=0`、模型保持 MiniMax-M2.7，1.9 未改动。
- A/B 两个全新 Feishu-scoped 合成 owner 各使用唯一 thread。A 保存称呼“青禾”、回答偏好、暗号和项目
  长期事实后完成 `event-lens`；B 保存自己的独立 Persona/Memory 后完成 `tree-sync`。双方召回只命中自己，
  owner 产物、Persona、USER、Skill 和 Memory 未发现交叉读取。该入口与 Feishu adapter 共用 owner/channel/
  conversation Gateway 主链，但不是平台客户端真实入站或收件证明。
- 两个模型均自主拆分而非由系统固定数量：A 创建 3 个 child，B 创建 4 个 child；A/B 各 3 条 `/btw`
  进入同一持久任务。B 长任务执行时，普通聊天可立即在同一 thread 回答，原任务继续运行。A/B 正式
  compact 阈值均为 90%，本轮 generation 仍为 0，故只证明单一 history 续接，不冒充 compact 触发证明。
- 独立验收不采用模型自报。B 首次在 macOS 暴露 `/var` 与 `/private/var` 路径别名、重复入口和缓存问题；
  沿原 thread/task 修复后 67/67 通过，JSON/CSV/Markdown、坏输入、汇总、稳定原因码、去重和干净交付通过。
  A 首次虽自报 19/19，但 `core.py`/`cli.py` 是两套逻辑；第一次纠错后虽自报 35/35，又由外部时区样例发现
  offset 只被删掉而未换算 UTC。第二次沿原 thread/task 纠正后，外部 41/41、弃用警告当错误、三格式、
  六类原因码、去重、坏输入及 1 小时时区间隔均通过；第三条短纠错只清理原项目缓存，远端最终扫描为
  18 个目录/文件、零 symlink、`.pytest_cache`、`__pycache__`、pyc/pyo 或 egg-info。
- 真任务还暴露并形成通用候选修正：owner quota 只忽略枚举后消失的单文件，其他错误继续 fail-closed；
  capability grant 用 child-link CAS 恢复同 run 且不复活 `/stop`；`raise_event` 按结构化 source child 取 lineage；
  子代理事实改为互斥 `status_counts`；删除 findings ledger 自动拼接用户正文的整条死路径，内部账只交主代理
  整合。具体参考到 会话运行时 `AgentStatus`/`wait`/notification、通道运行时 requester handoff 与 ENOENT 分流、
  长期助手 delegate summary 边界，未在 IM adapter 加特判或自然语言机器判据。
- 上述运行时候选已通过 181 项相关会话/工具回归及新增聚焦测试；完整本地门禁、提交、精确重部署和
  部署后 `/stop`/自然续作、Scheduler、协议出口复验仍待本轮最终收口。
- 首次部署后 capability 真测还发现 scoped owner 从自己的私有 gateway 目录读取 adapter PID/state，因而
  把健康 Feishu 误报为 `CHANNEL_ADAPTER_NOT_RUNNING`。对照 通道运行时 Gateway live snapshot 优先于本地
  config snapshot 的代码边界，现改为从基础 Gateway composition root 显式注入同一只读 health provider；
  owner registry、凭据与当前 thread binding 仍完全独立。聚焦回归实际写入共享 PID/state，再创建 scoped
  owner，已证明 `health=healthy + current_bound=true + state=ready` 且不泄露目标 ID；待 1.10 重部署复验。

## 2026-07-18 通道能力四层事实统一

- 对照 通道运行时 的 channel configuration/outbound selection 主链，把 installed、configured、health 和
  current-bound 收回现有 `ChannelAdapterRegistry`。`list_capabilities` 与 `send_message` 复用
  `SimpleAgent` composition root 的同一 registry/DeliveryService，不再扫描 adapter 模块或临时重建注册表。
- adapter manager 按每个实际通道记录 `starting/healthy/unhealthy/stopped`、检查时间和稳定错误码；daemon
  持续刷新结构化状态。Gateway 只通过 PID、heartbeat 和 JSON 状态投影健康，不解析日志或用户文字；
  进程死亡、状态损坏和心跳过期均 fail-closed。
- 当前投递绑定只从 owner-scoped conversation thread 的结构化 delivery binding 解析；能力清单只显示
  `current_bound` 与目标类型，不暴露 `open_id/chat_id`。QQ WebSocket 重连耗尽会同步清除 running 状态。
- 35 项首批能力/投递/健康回归、119 项 Gateway/后台唤醒/工具注册扩展回归、QQ 生命周期回归和
  完整本地 CI 通过；当前仍是未提交工作树，须完成提交和 1.10 真实 Feishu 探活才算发布。

## 2026-07-17 重启恢复不重放已结束 runner

- 1.10 切换最终文档快照后的只读恢复审计发现：一个多日前旧 task 的主状态残留 `RUNNING`，但 runner
  session 已明确 `completed`；旧 dead-worker reclaim 把所有非 fresh session 都当成宿主猝死，因此重启时
  又拉起同一个 child。
- 恢复入口已收紧到结构化 `runner_session.status in {starting,running}` 且心跳失效；显式终态不由该入口
  重放。真正宿主死亡、父/child link 都 active 的续跑语义保持不变，父生命周期门仍先于 reclaim 生效。
- `80a0527d` 的 35 项聚焦回归、选中 8,003 项且退出码为 0 的本地 fast suite、
  Ruff/import/doc-sync/strict code-size/compile、
  clean wheel 与三组 GitHub Actions 均通过。1.10 使用源码归档 SHA-256
  `5b4cd7f6c88bce79878c4ab3f46f02fad0ac4731f0dd8c9f8133e836863f12ae` 和 wheel SHA-256
  `47d05a83c3dd8664af536c4df4a6251ee15941a2fe326d37df2845fbc3469684` 精确部署；三处 marker 一致，
  source/site-packages 哈希一致，Gateway `/status` 为 running、队列 0、Feishu WebSocket 已连接。
- 反证目标在部署前后及一次周期 supervision 后的
  `session_id/status/history_count/updated_at` 完全相同，且日志没有非零 orphan reconcile；模型仍为
  `anthropic_compatible + MiniMax-M2.7`，1.9 未改动。

## 2026-07-17 `a7d6044e` 等待回执发布与双用户续作收口

- `ccb8d7f8` 的单一 thread history/compact 收口与 `acb1cfc5` 的父 conversation 生命周期门均已进入远端
  `main`，三组 GitHub Actions 通过；1.10 运行精确 `acb1cfc5`，Gateway/Feishu 全程 active、零重启。
  真机重启反证覆盖：父 task 已关闭的旧 child 被取消、父/child 都 active 的精确 child 可恢复、链接缺失或
  损坏的 child 保持 hold；1.9 未改动。
- 部署后另建 A/B 两个稳定 Feishu-scoped 合成身份。A 在唯一 `log-lens` task 中创建 5 个不同 child，B 在
  唯一 `tree-diff` task 中创建 3 个 child；没有重复 task 目录或重复派工。B `/stop` 后三个 child 全部取消，
  transcript、口令和 workspace 保留；自然补充后继续选择原 task，未重新创建 child。长任务让出期间，A/B
  普通聊天分别约 4 秒和 10 秒完成并只召回各自口令。
- A 的 2 条、B 的 3 条 `/btw` 均由 `guidance_delivered.json` 证明只消费一次，并在各自唯一 transcript 中
  各出现一次，零 pending。A/B 只有各自 1 个 task root，owner ID、口令、产物和符号链接零交叉；两个 root
  最终均 `completed`，work state 均 `DONE`，所有 progress policy 均 disabled。
- 独立干净副本验收不相信模型自报。A 首次遗漏 Unix 时间与 Top 5/unknown、README，沿同 task 返修后新
  虚拟环境安装、119/119 测试及 ISO/Unix 秒/毫秒、Top 5/unknown 外部断言通过，正式目录零缓存/临时脚本/
  checkpoint/符号链接。B 首次漏测二进制哈希、ignore 规则、CLI 崩溃和产物残留，沿同 task 两轮返修后
  新虚拟环境安装、64/64 测试及 11 项外部断言通过，`output/` 零缓存/旧副本/符号链接。
- 部署态同时暴露等待回执事实过少：A 模型说“继续等待进一步指示”，B 回执没有说明正在做什么。通用修复
  给同一个无工具表达轮提供有界的当前请求、已执行动作、当前引导和精确子代理统计，并明确
  `task_continues_without_more_user_input`；普通句子仍由模型生成，状态不从自然语言判定。`a7d6044e` 的
  60 项相关回归、8,057 项完整 pytest、Ruff/import/offline/code-size/doc-sync/compile、2.62MB wheel
  clean-package 与 distribution boundary 均通过；GitHub Actions Lint `29590701614`、Test `29590701591`
  和 Cross-platform guard `29590701693` 全绿。
- 1.10 以源码归档 SHA-256 `5a5914c5768a27b624ee7f7aae7f3b77efec65a5dd7dfca3f513f6536a44da66`
  和 wheel SHA-256 `595c510578f884940ba8f58ecb443ba0d7ac126baecbd5a04cd5a966f55b478c` 精确部署
  `a7d6044e`；源码、site-packages 与本地关键模块哈希一致，三处部署标记统一。Gateway/Feishu active、
  `NRestarts=0`、队列空闲，模型保持 `anthropic_compatible + MiniMax-M2.7`。
- 部署后新建 `ou_waitproof_a7d_20260717` / `oc_waitproof_a7d_20260717` 合成 scope。首轮 27.563 秒回执
  准确列出 parser/test 两个 child，明确无需用户补充且会自动整合；两个 child 依次 `DONE`，主代理在同一
  root/thread 读取报告、生成代码、运行测试并修复失败，约 12 分钟后自然最终回复。干净临时副本重新
  `py_compile`、运行测试和 parser 自测，结果为 23/23、零 symlink；root link=`completed`，transcript 恰为
  user/interim/final 三条。统一 DeliveryService 把内部宿主路径脱敏为 `wait-receipt-proof/` 与 `output/`。
- 上述请求使用与 Feishu adapter 相同的 owner/channel/conversation Gateway 主链，但 open_id/chat_id 是
  合成身份；这仍不是 Feishu 平台真实客户端入站或真实引用回复证明。

## 2026-07-17 双用户长任务矩阵与单一历史反证

- 1.10 MiniMax M2.7 上完成 A/B 两个 Feishu-scoped 合成用户的完整长任务矩阵。A 完成 Hyperfine、
  Zoxide、8 项对比、SL、Pastel；B 完成 Tokei、Navi、9 项对比、Tealdeer、Miniserve。B 长任务运行时，
  A 的普通追问 9.417 秒完成并只召回 A 的上下文；owner、thread、workspace、产物和口令未串线。
- 17 个不同 `/btw` 输入（3 request-scoped、14 task-scoped）全部在目标执行轮只确认一次，pending 最终为 0，
  并以 UserTurn 留在同一 transcript 的准确工具历史位置。确认事实来自 `guidance_delivered.json`，不可变
  inbox 只保留输入最初落账状态。控制与去重只认 guidance/request/task/thread id，不解析中文语义。
- Miniserve 的补充与修复继续绑定原 task `req_1784264255535_1355192_2`、原 thread 和原 workspace；没有
  另开 task 或重复派子代理。首次外部黑盒验收 54/58 暴露 4 个真实缺陷，修复后的干净 wheel 为 58/58；
  这项外部验收没有被加入 Gateway 或普通任务完成主链。
- 本轮请求经 Gateway `/ask` 进入 Feishu owner/channel/conversation 作用域，但使用合成身份，不代表
  Feishu 平台客户端真实入站/投递。正式阈值 90% 下本矩阵没有触发 compact；唯一 thread compact 的真机
  证据仍是既有 50% 压力轮。删除第二套 task history/compact 的 `ccb8d7f8` 已随 `acb1cfc5` 部署，并由
  post-deploy A/B 单一 history 续作和 `a7d6044e` 等待后自动续跑再次反证。
- 当前边界复核固定参考 会话运行时 `03bb3b12367397e14a8facc2e018d645ff4d8e83`、通道运行时
  `f2a46b0661206a0b7264ad05749e2304fbfe6a61`、长期助手
  `7d0246ab5715e9e18e156eb08912f4e24bd8d175`；只适配语义，不新增 IM 专属底座。

## 2026-07-17 父任务生命周期控制孤儿恢复

- 1.10 部署前只读检查发现旧 owner 下仍有多日前的 PENDING/BLOCKED/RUNNING 子代理投影。此前周期孤儿
  恢复只看 child status、runner heartbeat 和 PID，无法证明其 conversation root 仍允许执行，重启可能
  复活已经结束或 `/stop` 的旧任务。
- `acb1cfc5` 新增一份 batch conversation lifecycle decision，同一 thread 只读一次 task links。auto-start、
  普通 dispatch、watch takeover、RUNNING reclaim 和 orphan revive 共用它；父 root link 与当前 run link
  都是 active 才能启动同一 run。completed/cancelled/interrupted 等已关闭链接调用现有取消链收敛 canonical
  run，链接缺失、损坏、跨 thread、重复或未知状态一律 hold，不从 goal、聊天文字或展示状态猜测。
- 无 conversation attrs 的本地/admin run 保持原恢复语义；只出现 thread/task 其中一个身份字段则 fail-closed。
  聚焦回归覆盖 active 恢复、completed/interrupted 取消、missing/corrupt hold，以及 auto-start/dispatch 两个
  绕行入口；远端 CI 与上述 1.10 重启反证均已通过。

## 2026-07-18 持久 Scheduler 接入同 thread 主链

- composition root 为每个 owner 创建唯一 `SchedulerRepository`/`SchedulerService`，
  `schedule` action tool 不接收 owner/thread 参数，只从当前可信 RunParams 绑定原 thread。
- owner `data/scheduler/` 持久 job/run、历史、CAS 版本、claim/heartbeat 和 misfire。
  owner disk discovery 识别 due/queued 事实，重启后不依赖旧进程内存。
- 到期 run 使用自己稳定 scheduler run id，但仍进入原 owner/thread 的
  `BackgroundMainAgentRuntime`；加载同一 transcript/compact 和 owner tool policy，用原通道绑定投递。
  同 thread 同时到期的多个 job 不走普通 wake coalescing，每个 run 均单独关闭历史。
- 对照 通道运行时 `src/cron/service/timer.ts` 的持久 timer 与 长期助手 `cron/scheduler.py`
  的 claim heartbeat；my-agent 复用自己已有 ConversationStore 和 owner wake 路由，没有新建 IM 专用队列。
- 相关聚焦测试、完整本地 CI、distribution boundary 和干净 wheel artifact gate 已通过；
  1.10 和真飞书到期验证待本轮最终收口。

## 2026-07-18 owner quota 与自动 retention

- `OwnerQuotaEnforcer` 作为 composition root 的 owner-local 单例接入文件工具、Memory、Persona、Scheduler
  和 Skill draft；非 Agent 的飞书 Persona 确认入口从相同 `quota.json` 重建同一门。锁序统一为
  `owner quota -> repository/file lock -> mutation`，整批最终字节在 owner lock 内计算。
- 子代理创建前把 `max_active_agents` 与 owner/task/per-call 各级容量取严格交集；权威运行状态不可读时
  整批拒绝，不按零占用继续。
- Gateway 新增独立 maintenance controller，每 tick 只扫描一个有界 owner page，不实例化 Agent；owner
  自己的 policy 决定实际 24 小时维护间隔。task/subagent scratch 只依赖结构化终态和 `updated_at`，执行前
  二次校验；先移入 owner trash 并写 tombstone，再按期限删除，支持 legal hold 与 audit。
- 配额、retention、owner 发现、Gateway controller 和 Memory/Persona/Scheduler 组合写入聚焦回归与
  完整本地 CI 通过。Shell/PTY/LSP 任意进程写盘仍须正式部署的 filesystem/project quota 兜底；
  1.10 尚待收口。

## 2026-07-17 单一 thread 历史收口

- 复核 会话运行时 当前实现后，Gateway 收敛为一个 owner/thread 和一份 summary + raw tail。聊天、文件工作、
  子代理协调、定时唤醒和普通小任务都续接同一份模型历史；IM 只传输消息，不建立
  额外 session、lane、任务 transcript 或 compact。
- `Running Work` 只保留为同一 prompt 中的结构化工作索引，用来阻止同一个后台任务被第二个执行器重复
  启动。task link、workspace、progress、wake 和 agent tree 都是运行事实，不得过滤、替换或复制 thread
  transcript；普通用户消息也不再复制进 task guidance 账本。
- 同轮还发现 `work/state.json` 过去只在建目录时写一次：task link 已完成或 `/stop` 后，它仍可能永久显示
  `RUNNING`；旧任务首次续接并懒建 workspace 时还可能把本轮 request id 写成 task id。当前 workspace
  writer 复用唯一 `durable_task_id` 解析器，conversation task link 的结构化生命周期迁移再投影到精确
  task path 下的 state；路径必须位于当前 `owner_home/tasks/`，越界、身份不一致或状态文件损坏时告警并
  跳过，不跨目录修补。

## 2026-07-17 模型首段原话进入低延迟进度通道候选

- Sl 真任务的续接轮在约 14 秒已经生成“先检查项目”的自然模型文字，但旧主链把全部 model delta
  只留在 chunk 文本缓冲，直到 94.342 秒的前台协作让出后用户才收到自然回执。当前候选把真实
  model delta 与 provider/runtime notice 分成 typed sink：第一次工具开始前已经形成的模型正文会经统一
  用户出口净化后写成一条 `assistant_commentary`，不拼“正在处理”等固定句子。
- commentary 是 presentation-only（只负责展示）的有序事件：每个 request 最多一条，`/verbose off`
  也可见；工具名、工具输出和逐步命令仍只在 `/verbose on|full` 下显示。它不写任务终态、不替代最终
  assistant reply，也不会让 delivery worker 误以为已经完成最终投递。
- 触发边界读取 typed `phase=started`，不读取本地化 `status` 展示词；commentary 发出后不再保留后续
  model delta，避免长任务把无用分片持续积在内存。
- commentary 投递失败按 cursor 至多尝试一次，避免坏 IM 路由每秒刷屏或阻塞耐久最终回复；最终回复仍
  沿原 pending/sent receipt 重试。runtime 自动恢复提示、provider notice 和旧 generic chunk callback
  不能伪装成模型原话。
- 代码边界对照 长期助手 `gateway/stream_events.py`、`stream_dispatch.py`、`stream_consumer.py` 的
  `Commentary`/final 分栏，以及 通道运行时 `reply-delivery.ts`、`block-reply-pipeline.ts`、
  `get-reply-run.ts` 在模型/工具边界按序投递 block reply 的做法；只复用 typed presentation event 与
  最终交付分离，不复制其 session 或 adapter 实现。聚焦回归通过，尚未发布到 1.10。

## 2026-07-16 分步任务追加要求进入同一 thread 历史

- 1.10 的分步复刻曾暴露：workspace 虽续接正确，后台轮却因读取 task-scoped transcript 而退回旧目标。
  把用户要求复制到 task guidance 的旧候选方案已经删除；它会制造第二份历史，并让
  同一句用户消息在 transcript 与任务账本之间产生确认竞态。
- 当前实现把每条普通用户消息幂等追加到唯一 thread transcript。前台、后台、retry 和 compact 后的续轮都
  读取同一份 thread summary + raw tail；`task_progress select` 只选择结构化 workspace，不改变会话历史，
  也不复制用户正文。持久化失败仍 fail-closed，归属只认 owner/thread/request/task 等结构化身份。

## 2026-07-16 旧任务续接与用户停止的结构化硬边界

- 1.10 双用户分步长任务实测发现：同一用户第二步已经拿到 Recent Completed Work，但 MiniMax 仍调用
  `task_progress action=start`，底层原先无条件接受，因而新建了第二个工作区，随后在错误目录里连续
  `PATH_NOT_FOUND`。这不是 transcript 缺失，而是 task start 入口缺少结构化确认。
- 现在会话存在 active/interrupted/recent-completed 候选时，`start` 必须显式携带布尔字段
  `new_task=true`；否则返回同一 `CONVERSATION_WORKSPACE_DECISION_REQUIRED` 和精确候选。继续旧任务仍只用
  `select + task_id`。没有候选时普通任务可直接 start。实现不匹配“继续、第二步、新任务”等自然语言。
- 后续双用户复刻真测又捕获到更具体的协议误用：模型虽在正文里说“继续同一个项目”，却一次调用
  `start + new_task=true + summary/next_action/items`。旧入口会静默忽略这些只属于 `update` 的字段，并立刻
  建立错误工作区。当前候选把 `read/update/select/start` 改为严格动作变体：`start` 只接受 `new_task`，
  `select` 只接受精确 `task_id`，进度字段只能在后续独立 `update` 中提交；混用返回
  `TOOL_INVALID_ARGUMENTS`，且不会创建 task link 或目录。该门只检查结构化字段，不判断用户文字。
- 该边界对应 会话运行时 的显式 `turn/start` 与带 expected turn id 的 `turn/steer`，并参考 通道运行时
  `src/talk/agent-run-control.ts` 的 active session + typed mode；my-agent 只适配自己的 owner/thread/task
  文件事实源，没有引入第二套控制协议。
- 同轮 `/stop` 已真实中断错误任务，但后台 claim 把 `InterruptedError` 误记成 failed，日志又因其继承
  `OSError` 而显示 I/O 故障。调度入口现在单独接住该类型，安静以 `cancelled/user_interrupted` 结束租约，
  不作为 recovery takeover 候选；持久任务链接仍保持 `interrupted`，所以用户随后选择原 task 可以继续。

## 2026-07-16 会话运行时 completion and `/goal` parity

- 普通任务完成路径已经改为 会话运行时 方式：主模型基于当前对话、工具、测试和子代理事实给出自然最终回复，
  回合随即结束。旧 `delivery_closeout`、`submit_for_acceptance`、完成 marker、目录扫描验收器、自动返工轮和
  最终摘要重写已从生产代码删除。
- `/goal` 公开状态、字段、三个模型工具、创建/更新约束、token 与在线时间记账、零工具停止续跑、用量限制
  和错误状态均按当前 会话运行时 实现适配到 owner/thread/task 文件事实源。普通任务无需 `/goal`，也可使用工具
  和子代理。
- artifact registry 继续负责附件路径、hash、owner 和发送权限；文件格式检查继续留在写入工具边界。两者
  都不再决定普通任务是否完成。
- 下方带日期的旧 closeout 条目保留为问题发现与演进记录，不再描述当前主链；当前事实以上述规则和
  `docs/PRODUCT_FACTS.md` 为准。
- 所有定时 progress wake 在消费前都重新读取精确 task link；任务已
  completed/cancelled/interrupted/abandoned/superseded 时直接归档旧唤醒，不得在终态后重新启动执行器。

## 2026-07-16 `/btw` 被自然回执误消费的真测与候选

- `0794c9fb` 部署后的双 Feishu-scoped owner 长任务确认了 owner/上下文隔离、模型自主
  子代理数量和最终收口；独立重跑分别得到家庭账本 37 项、日志分析器 30 项测试通过。A 任务的 `/btw`
  账本虽然被标为 delivered，最终 HTML 和测试却没有要求的导入/成功/跳过计数，因此不能把“账本已投递”
  当作真实执行通过。
- 根因是派工后还有一个只负责写用户自然回执的 isolated model round。旧链让这个展示轮读取 task guidance，
  随即提前写 `guidance_delivered.json`；展示轮结束后，真正的任务轮再也看不到该引导。
- 本地候选给展示轮显式设置 `consume_pending_turn_input=false`。展示轮开始前或生成中出现新的 `/btw`/任务
  事件时，旧展示草稿被丢弃，真实 active task turn 在下一安全点读取输入；不靠中文语义判断。guidance 与
  runtime event 统一延后到 provider 成功返回后确认，注入后崩溃仍保持 pending，恢复轮可重放。
- 对照 会话运行时 `core/src/session/input_queue.rs` / `turn.rs` 的同 active-turn drain，通道运行时
  `attempt.queue-message.ts` 的 transcript-commit 后确认，以及 长期助手 `conversation_loop.py` /
  `agent_runtime_helpers.py` 的真实工具轮 drain。聚焦回归覆盖展示轮前到达、生成中到达、旧回复丢弃和
  未确认恢复重放；候选尚待发布并在 1.10 重新做真实 `/btw` 产物验收。

## 2026-07-16 后台任务索引与第二执行器卡口候选

- `047e24f7` 部署后的双 owner 长任务证明后台工作可以耐久续跑，但非阻塞 `wait` 结束当前 turn 后，
  scheduler 后续 turn 与新普通请求的生命周期仍不同于 会话运行时 的同 turn `wait_agent`。本轮只删除第二份
  history/compact，不把这项既有调度差距伪装成已解决。
- 同一轮还发现后一条消息被模型再次 `task_progress select` 到已经运行的根任务，第二个执行器因此重新
  检查任务现场，并把精确工具轮数复述给用户。根因不是用户说了“继续”，而是 task candidate 缺少结构化
  execution occupancy；修复不得增加中文触发词。
- 当前候选从 enabled progress policy 和未过期 background claim 读取执行占用。运行中的 active task 只进入
  同一 thread prompt 的 `Running Work` 工作索引，选择卡口再次核验同一事实；已运行返回
  `CONVERSATION_TASK_ALREADY_RUNNING`，读取错误返回 `CONVERSATION_TASK_STATE_UNAVAILABLE` 并禁止创建
  第二执行器。该索引不是第二份上下文；内部执行来源和精确工具轮数也不进入用户回复。
- 对照 会话运行时 `multi_agents_spec.rs` 的明确 child message、turn/steer 的单 active turn，以及 通道运行时
  `sessions-spawn-tool.ts` 的 required task、active-run steer queue；my-agent 保留自己的 owner/thread/task
  文件事实源，不把 `/goal` 变成普通派工前置条件。专项回归和 code-size 基线已通过，待发布真测。

## 2026-07-16 运行中子代理事件进入同一主执行轮候选

- 1.10 双用户长任务进一步证明，子代理完成通知没有丢，但一条已启动的 scheduled progress turn
  可持有 thread background claim 四十余分钟；其间后续完成 wake 只能积压，必须等该轮退出后才统一处理。
  用户看到的是长时间无新阶段反馈，主代理也不能及时按最新子任务事实调整整合路径。
- 本地候选把子代理完成、能力申请和能力获批作为结构化 runtime event data 接入现有 tool-loop 安全点。
  每次 provider 返回后、工具副作用前都会检查同一 durable task 的新事件；若有新事件，丢弃基于旧状态
  的模型动作，下一轮按 FIFO 注入。事件文本明确标为运行事实而非用户指令，不做自然语言判断。
- 启动当前后台轮的 wake id 从 active-turn inbox 排除，仍由 scheduler 确认；运行中到达的其他 wake 只有
  在模型成功返回、证明已读取包含事件的 prompt 后才标 handled。provider 在注入后失败时 wake 保持 pending，
  可由同一任务下一轮重试。
- 对照代码：会话运行时 `session/input_queue.rs`、`session/turn.rs` 的 turn-local input drain 与 stale action
  边界；通道运行时 `agent-steering-queue.ts`、`subagent-announce-delivery.ts` 的 active-run steer、顺序投递和
  transcript commit。聚焦回归已覆盖同轮接收、旧响应丢弃、单 claim 以及 provider 失败不丢事件；完整门禁、
  发布和 1.10 真实复验尚未完成。

## 2026-07-16 `/btw` 单执行轮与 `/stop` 模型传输中断候选

- 1.10 双 Feishu owner 分步复刻真测暴露了同一任务的双主执行器：前台 request 已绑定根任务且
  仍在执行时，`/btw` 既将 guidance 写给这条 live turn，又发 urgent wake 启动一条后台主轮。
  两条执行链随后可并发修改同一 workspace，导致引导看似被忽略、测试修改被覆盖和 token 异常消耗。
- 控制层候选现只在根任务没有 linked live request 时发 wake；已有 live turn 时仅持久写入 FIFO
  guidance，由原执行链在下一安全点消费。这与 会话运行时 的 expected-turn + same-turn input queue、
  通道运行时 的 active-run steer queue 以及 长期助手 的 live session pending steer 保持同一运行身份边界。
- 同轮真测还发现，`/stop` 虽已持久记录 cancel 并给执行线程立旗，但若线程正阻塞在最长 600 秒的
  provider 响应读取中，旧实现只能等模型返回后才看到旗标。候选现让阻塞传输在同一线程中注册
  关闭回调，停止时直接关闭模型 HTTP/SSE 响应，并保留为 typed interrupt，不进入网络重试。
  进一步审视发现 provider 实际运行在 wall-timeout guard 子线程，而任务名登记在外层 worker；
  候选因此在模型调用边界增加外层到子线程的 typed interrupt relay，并有界等待连接收回。
- 本地已覆盖“live turn 收到 `/btw` 不产生 wake”、“同名停止触发传输关闭且退出时清理”、
  “600 秒模型流在停止后 2 秒内解除阻塞”以及“外层任务停止穿过超时保护线程到达真正 provider”。
  focused tests 与 Ruff 已通过；完整门禁、发布和 1.10
  双用户真实复验尚待完成，当前不升级为已部署事实。

## 2026-07-15 持续目标、显式审计模式与中断后续接

- `/goal` 按 会话运行时 的 thread-persistent overlay 边界落地：它是同一 owner/channel/chat/topic
  对话上的特殊持续目标，不创建第二会话。每 thread 同时只有一个未结束目标，
  绑定同一根 task/workspace，可查看、修改、暂停、恢复和清除；自动续跑只在目标仍 active 时
  发布一个去重 wake。模型只能通过 `update_goal` 写入 `complete` 或 `blocked`，不能绕过
  owner/thread/task 绑定改其他目标。
- `/audit` 收紧为显式前缀模式：入口把 guarantee/window 固化到结构化 task attributes，
  子代理通过调度继承，watch 只读这份权威事实。普通语句、goal、summary 或 child prompt 中提到
  `/audit` 都不会暗中开启审计保证。
- `/stop` 从“删掉可续接任务”收紧为 会话运行时 桌面端式 interrupt：立即停止当前根执行和子树，
  抑制迟到回复，但保留 transcript、compact、task workspace、artifact 和 memory。已中断任务仍可作为
  结构化候选；用户之后自然说“继续”，模型选中精确 task id 后重开原现场，无需重发原 prompt。
  active goal 被 `/stop` 时转为 paused，不被 clear。
- 同一 `SimpleAgent` 的前台聊天和后台续跑不再共享一份可变“当前 prompt/run/workspace”字段。
  运行态按 worker thread 与 agent 弱引用身份分栏；对象释放时自动清理，避免长驻服务中 Python object id
  复用导致低概率串 prompt 或串工作区。
- 远程 owner 的文件默认黑名单扩大到所有其他 user/group owner、根模板和旧顶层私有目录；
  只放行自己 owner home 与管理员明确发布的 `~/.my-agent/shared/`。随 wheel 发布的 builtin tools/skills
  仍是公共代码能力，不依赖私有文件穿透。
- 子代理数量由模型基于真实独立工作项显式提交，不再向普通聊天暴露固定数量
  `/subagents` 入口。运行时在任何创建前同时计算每批/任务/owner/全局余量，超限整批拒绝，不截断、
  不部分创建。
- closeout 仍强制聚合子代理、进度和能力请求，但只有显式 artifact contract/expected output
  要求文件时才强制文件交付。纯分析或问答可以 `delivery_mode=message` 收口，不再因“派过子代理”
  就人为要求生成空报告文件。

## 2026-07-15 会话运行时 式当前任务引导、模型回复出口与最终收口重验候选

- `/btw` 的语义从“一次模型调用”校正为“当前这一项持久任务”：同一任务经历前台回执、后台唤醒、
  compact 或多轮工具执行时，引导仍按 FIFO 在下一安全点进入该任务；每个工具循环只注入一次，不进入
  普通聊天、其他任务或未来任务。当前执行轮存在时，控制层在写入前后核对 processing record 上的精确
  task binding；没有执行轮时才核对 owner/thread 下 active 根任务。任务已结束或切换就拒绝迟到引导。
  provider 生成途中到达的新引导会使旧响应失效，旧响应不得执行工具或结束任务。
- 任务状态、引导和完成共用 `task_transition_guard` 与 active CAS。`/stop`、`/btw`、closeout 完成互斥
  迁移；取消/切换优先时，旧完成通知不会复活或外发。实现边界对照 会话运行时
  `db887d03e1f9` 的 `steer_input`、expected turn id、FIFO input queue、next safe point 和 stale response
  discard；my-agent 沿用自己的 owner/thread/TaskRun/RWX 文件账本，不复制 会话运行时 UI 或进程内会话存储。
- 除 `/status`、`/stop`、`/btw` 等显式控制命令外，普通聊天、派工回执、等待说明和最终交付正文必须由
  LLM 根据结构化运行事实自然撰写。表达短轮不再携带旧任务正文、工具历史或内部 advisory，避免模型把
  “写一句回复”误当成重新执行任务；出口仅按空正文、真实工具调用和内部协议等机器形态拒绝，重写仍
  失败则抑制正文，不回退“正在处理”一类固定模板。任务终态、时间和产物事实只认 typed runtime facts，
  不再用中英文关键词或正则反向猜测模型文案的语义。
- 用户出口净化收敛到 `conversation/user_visible_text.py`：Gateway response、IM、后台主动投递和 transcript
  共用一套 bracket/XML/native 降级协议清洗，内部 envelope 不进入后续 compact 或 memory。交付保障层
  只归集真实产物，不再拼接“系统自检汇总”用户文字。
- MiniMax M2.7 真机复验暴露同一 assistant turn 先批量创建 5 个子代理、又重复发出 4 个单项创建。
  一次工具循环现同时记录 exact call key 与结构化 child intent key；后续完全重叠调用不再产生重复副作用，
  compact 恢复也重建同一去重状态。
- 真机还暴露旧完成标记可绕过最新 `closeout.json`：根任务在清单仍 open 时被提前标成 completed，后台
  虽继续整合却无法发送最终结果。现在完成标记必须与当前 request/run/task 的最新通过报告一致，且进度
  无 open 项、子代理聚合门通过；人类可读任务目录不再冒充 task id，后台整合按 `params.task_id` 认领
  子代理。阶段性结果可以自然汇报，但不能关闭根任务。
- 最终候选 wheel（SHA-256 `2ddf8e416951f7cc89315b2ff7e3064105e5c9a6829e5d13d336a9deb7608496`）
  已部署到 1.10，MiniMax M2.7 双 Feishu-scoped 合成用户长任务 `failures=[]`：A 精确 5 子任务并在
  `/btw` 后完成，B 精确 4 子任务并在 `/stop` 后 cancelled，Gateway 重启后未复活且迟到投递为 0；
  独立口令无串词，用户 transcript 无内部协议。该实测经 Gateway `/ask` 进入真实 Feishu owner/channel/
  conversation 作用域，不冒充 Feishu 平台真实入站。首次模型自然回执为 37.3/50.7 秒，普通并行聊天为
  6.1/10.1 秒，MiniMax 表达延迟仍需后续优化。

## 2026-07-15 回执释放后控制继续跟随持久任务

- 1.10 双用户长任务复验发现：派工回执结束后，根 TaskRun 和子代理仍在 owner 目录运行，但旧控制层只
  扫描 Gateway `processing` 请求，因而 `/status` 错报空闲，`/btw` 错报没有运行中任务。任务执行本身
  没丢，这是控制目标生命周期短于任务生命周期造成的真实断链。
- 控制目标现按可信 owner/channel/conversation 解析同一 thread，优先选择最新的 user-selectable active
  根 task link；只有任务尚未晋升时才回落当前 processing request。普通聊天请求不会盖掉后台任务控制权，
  跨用户、跨 conversation 或 task-link 读取损坏仍 fail-closed。
- `/btw` 对已晋升任务写 `target_type=task` 的一次性 guidance，并发布带相同 root task id 的 urgent wake；
  下一安全点消费后即结束，不进入未来聊天。任务恰好终态时使用 active CAS 拒绝迟到引导。
- `/stop` 先用 active CAS 把根 task link 持久化为 interrupted，再中断同 task id 下可能并存的前台/后台主代理
  执行域，并异步取消准确 lineage 的子代理。背景轮在发送前读取 durable task status，取消后的迟到旧回复
  被抑制；同名 interrupt registry 支持多个执行线程，不再由后注册线程覆盖先注册线程。
- 双用户实测中，A 在旧并行设计下把同一 thread 的后一条聊天写进正在运行的旧任务产物，证明并行 turn
  本身会让消息的时间归属不明确。当前方案不再按 task lineage 切割 transcript，也不建立平行聊天入口：
  用户消息按顺序进入同一 thread；`/btw` 作为当前 active turn 的真实 UserTurn 在安全点注入并写回同一
  transcript。task id 只约束 wake、workspace、进度和子代理树等运行事实。
- 设计复核 通道运行时 的 session/active-run registry 与按 run id abort/steer，以及 长期助手 的 live session
  `running` 状态、`session.steer`/`session.interrupt`。复用的是“控制跟随稳定 run/session 身份而非一次 HTTP
  请求”的边界；my-agent 仍使用自己的 owner-scoped thread、TaskRun、guidance/wake 和 RWX 事实源。

## 2026-07-15 精确执行轮控制与 bwrap 后代树收口候选

- 双用户分步复刻真测暴露了比“持久任务可控”更细的一层断链：A 的当前 Gateway request 正在执行第五步，
  但会话里另有更新更晚的 active link；旧 `/status`、`/stop` 选中了旧根 id，而真正运行的 request 仍继续。
  当前执行轮现把选中/晋升的 `thread_id/task_id/task_path` 原子写入自己的 processing record。写入失败会在
  工具副作用前 fail-closed，不能创建一个控制面无法定位的任务。
- `/status` 现在用精确绑定的当前 request 显示本轮任务、时长和 typed progress，同时合并根 task 与当前
  request 的子代理；`/btw` 写同一根 task 的 FIFO guidance，并以 processing record 作 expected-turn 复核；
  `/stop` 同时把根 task 标为 interrupted、给当前 request 落 `cancel_requested`，并中断两种 runtime id 与
  两条 lineage 的子代理。中断只保留现场，不改为不可恢复的 completed/cancelled。
- 同一真测还发现 bwrap 内层 `--new-session` 会建立新 session/process group。旧代码只 kill 外层 pgid，
  内层 npm/Vitest 可继续存活并持有 stdout/stderr，使 Python 的无界 `communicate()` 看起来永久卡住。命令
  终止权威已收敛到 `tooling/process_registry.py`：先快照宿主后代树和进程出生标识，SIGTERM 宽限后对
  仍存活者 SIGKILL；shell 只做 2 秒有界 pipe drain。该边界对照 通道运行时 的 process-tree termination 与
  会话运行时 的 bounded pipe drain / bwrap signal forwarding，不复制它们的运行时。

## 2026-07-16 子代理进程工具写边界候选

- 真实 1.10 任务证明：子代理的 `write_file` 会被 `allowed_write_roots` 拒绝，但同一子代理可通过
  `run_command` 的重定向写入 owner 根项目。根因不是命令规则漏了某个语法，而是 shell bwrap 把
  整个 owner home 挂成可写，文件工具与执行工具没有共享同一结构化写域。
- 本地候选把 `allowed_write_roots` 从 registry 统一传给 `run_command`、后台命令、PTY 和 LSP；
  bwrap 将 owner home 作为只读基座，再叠加当前 task 的精确可写根。实现不识别命令文本或自然语言。
- PTY session 增加 owner/task scope；LSP server 的 scope 改变时关闭重建，避免长驻进程带着上一个
  task 的可写挂载被后续任务复用。聚焦 sandbox/shell/PTY/LSP/registry 回归已通过，1.10 真机尚待部署。
- 对照代码：会话运行时 `会话运行时-rs/linux-sandbox` 的只读基座与精确 writable roots；通道运行时
  `src/agents/sandbox/{docker,workspace-mounts,fs-bridge-path-safety}.ts` 的 `none/ro/rw` workspace
  access 和挂载路径校验。
- 后续 1.10 分步复刻又发现同一 owner 的根任务也需要同一边界：模型新建任务账本后仍可用绝对路径
  写旧任务。runtime ledger 现从结构化 provider/owner/task workspace 自动生成当前 task 可写域，并过滤
  已有授权中的兄弟任务；子代理已有的更窄根保持更窄，畸形 task root 明确 fail-closed，local/admin
  bypass 不受影响。该修复与 shell/PTY/LSP 共用同一个 `allowed_write_roots`，没有增加命令文本判断。
- 本地已覆盖“后代自行 `setsid` 且继承 pipe”、普通进程组、后台 kill、日志 watchdog、精确 task binding、
  `/btw` 不被无关更新 link 抢走，以及 `/stop` 同时中断 root/current turn。1.10 安装包和真实双用户续跑
  尚待本候选完整门禁、发布与部署后复验，当前不能写成已发布事实。

## 2026-07-15 模型自然回执提速、最终事实快照与中断恢复

- 派工/wait 的 LLM 自然回复不再携带完整 tool context、native tool IR、runtime injection 或 delivery
  contract；当前用户请求与 persona 文件仍保留。1.10 先前 58/155 秒的一句话回执由此去掉主要上下文负担。
- interim/final 回复共同拒绝内部协议和无结构化依据的 ETA；完成轮会在 closeout 最终时刻冻结
  `delivery_snapshot`，再由同一模型根据真实文件名、字节数、SHA-256、进度和 gate 状态重写摘要。旧
  `submit_for_acceptance` 摘要只作为可丢弃草稿，不再把修复前的“约 21KB”带到修复后的 25,771 字节文件。
- `background_claim_heartbeat_interval_seconds=0` 的实现已修正为自动间隔；默认 claim TTL 从 900 秒降到
  90 秒。claim 新增 process-domain+pid+start_time，同一进程域旧 gateway 已死时立即接管，Kubernetes
  不同 PID namespace 或旧 claim 无法证明时保守等待 TTL。
- Gateway SIGTERM/SIGINT 现在写 typed stop request 和轻量 shutdown forensics，再走既有 stop-file drain；
  计划 stop 保留原 reason，外部信号记录为 `signal_shutdown`，不再只在 systemd 中显示无原因 clean exit。
- 设计对照复核了 通道运行时 gateway signal/drain、typing/final delivery 分栏，以及 长期助手
  `shutdown_forensics`、`resume_pending`、PID+start-time process registry；复用其边界，仍沿用 my-agent 的
  owner-scoped transcript、RWX claim 和统一 DeliveryService。
- 本地聚焦合同覆盖模型短轮、无依据 ETA、最终大小一致性、同域死进程接管、跨 Pod fail-safe、信号分类
  与既有 closeout 全族；真实 1.10 多用户长任务/重启复验在发布后执行。

## 2026-07-14 普通聊天与后台任务并行、生命周期可诊断

- 未晋升的普通 thread 不再提前创建 task workspace；只有注册表 `promotes_task` 或结构化任务动作能在真实
  工作开始时惰性晋升。派出子代理后当前 IM/Gateway 请求立即释放会话顺序槽，用户可以继续聊天、
  `/btw` 纠偏或 `/stop`，后台 TaskRun 独立继续。
- 派工回执只从 lifecycle envelope 读取 recorded/accepted/running/failed，accepted 不再冒充 running；
  模型的 `[TOOL_CALL]` 文本和子代理命令日志不会进入普通 transcript，统一用户投影另有末端净化。
- 自动 `dispatch_supervision_auto` policy 保存子任务 material signature。状态、进度、阻塞、能力申请、
  产物和结果都不变时只顺延，不调用 LLM；发生结构化变化才叫回主代理。用户显式 wait 和数据监控
  仍按原节奏执行，不被状态指纹误停。
- Gateway 的 watch 返回现在有三种持久终态：计划 stop、有限 `max_cycles` 完成、无 stop 的意外返回。
  第三种写 `GATEWAY_WATCH_UNEXPECTED_RETURN` 并以非零码退出；cleanup 单独记录 heartbeat/request/
  background 三线程的 drain 结果，超时写 `GATEWAY_DRAIN_INCOMPLETE`，不再显示成正常 stopped。
- 对照检查了 通道运行时 gateway lifecycle/restart coordinator、keyed wake coalescing、subagent acceptance/
  completion outbox，以及 长期助手 shutdown forensics/first terminal completion。复用的是 typed 生命周期与
  事件驱动原则；my-agent 继续使用自己的 owner-scoped file queue、TaskRun 和投递合同。

## 2026-07-14 普通会话即时状态、纠偏与停止

- 新增 adapter-neutral conversation control protocol。只有精确 `/status`、`/btw <内容>`、`/stop`
  能进入硬控制面；普通自然语言仍是软消息，不通过语义猜测获得取消或运行时改写权限。
- `ChannelManager` 在附件下载和 `/ask` 前识别控制命令，直接调用 Gateway `POST /control` 并通过既有
  `DeliveryService` 回复。它不占同会话普通请求单飞队列，因此长任务运行时仍能即时查询、纠偏和停止；
  新 IM 复用 manager/HTTP 协议，不增加平台分支。
- `/btw` 在任务尚未晋升时写当前 request id 的一次性 guidance；任务晋升后改由上方持久 task 主链承接。
  工具循环每轮构建 prompt 时消费并标记 delivered；若 guidance 在 provider 生成途中到达，旧响应不执行
  工具也不直接结束，下一轮先纳入新要求。任务不存在或刚结束时不保存到下一任务；旧 `/btw` 列表、
  永久 runtime injection 与 `/btw-clear` 删除。
- `/stop` 对尚未晋升的 processing 请求原子写 `cancel_requested`；已晋升任务改由 durable task link 先落
  取消事实。两条路径都按 typed lineage 中断主循环并异步取消活跃子代理树，不从 goal/agent name 猜归属。
  工具循环在模型前后和工具前检查；前台 shell 每 200ms 检查并终止整个进程组。管理员 Gateway 生命周期
  `POST /stop` 保持原义，用户任务使用 `POST /control`，两者没有混用。
- `/status` 只从当前 owner/channel/conversation 的 durable task/request、typed progress、subagent state 和
  thread compact/verbose 事实渲染；不显示内部工具名、命令、路径、引导内容或“最近一次引导”。用户只看到
  同一 thread 的 compact generation；task recovery checkpoint 是运行恢复文件，不是第二种上下文或状态
  计数。跨用户和损坏请求无法证明 owner 时 fail-closed。
- 1.10 双 owner 深度对比真测捕获了 MiniMax 的另一种协议降级：自然回执夹带
  `tool_call` Markdown 代码围栏，原 bracket/XML 清洗没有命中。统一用户出口现同时剥离 fenced
  tool/function call/result/output 块，围栏外模型正文继续投递；不在 Feishu adapter 做特判，也不解析任务
  中文。对照 通道运行时 最终 assistant text 的统一 sanitizer 和 长期助手 的结构化 tool_calls/message 分离。
- 2026-07-17 Tealdeer 长任务真测中，模型第二次自然回执实际生成“分析完成后再回来汇报进展”，旧
  `INTERIM_FINAL_CLAIM` 正则跨句把它误判为“工作已完成”，两次生成用尽后 Gateway 返回空正文并把空
  assistant 错记成落账降级。当前本地修复删除完成/ETA/大小语义正则及其死代码：辅助回执只校验 typed
  runtime status、结构化 tool call、空正文和内部协议；任务状态仍由运行事件决定。该改动对照 会话运行时 的
  `AgentMessage` 与 `TurnCompleted` 分离，不通过解析 agent prose 决定 turn 状态，待发布后真机复测。
- 同一 Tealdeer 的 `/stop` 后自然续接又暴露另一条空回复链：模型已经在流里两次写出“继续原项目收尾”，
  task 也按原 id 恢复到后台，但 presentation-only 轮仍夹带未授权的 native tool call；安全闸两次拒绝后
  把 `user_reply_unavailable` 当成 `ok=true` 空 final，并为不允许的空 assistant 创建 repair。候选保持首次
  重试，在第二次仍违规时只删除结构化调用、保留经过统一协议净化的真实模型正文；若没有正文则以
  `USER_REPLY_UNAVAILABLE` 终止 Gateway 请求，用户消息和后台 task 仍耐久保留。实现只检查 pending
  reply phase、tool blocks、runtime status 和协议净化结果，不判断中文含义。对照 通道运行时
  `tui-stream-assembler` 的空 final 保留已流式正文，以及 长期助手 stream consumer 明确不把 commentary
  误当最终交付的边界。
- 同轮 Sl 真测的第一条 `/btw` 恰好落在前台 request 原子移入 done、同一 durable task 接管后台的窗口，
  旧 expected-turn 检查把“request 文件已退休”混同为“task 已切换”而拒绝，重试才成功。当前本地修复
  将 linked request 分成 `current/retired/mismatch/unavailable`：仅 `retired` 可回落核对同 thread 的当前
  active TaskRun，真实 mismatch 或账本不可读仍 fail-closed；退休窗口接受后发布幂等 wake，避免没有执行
  线时引导滞留。该语义沿用 会话运行时 expected turn 的防串线原则，但 expected identity 适配为 my-agent 跨
  前后台执行轮不变的 durable task id，聚焦回归覆盖同任务交接和真实 task-switch race。
- 设计对照：通道运行时 `src/status/status-text.ts` / `src/status/status-message.ts` 的确定性状态投影；会话运行时
  `会话运行时-rs/core/src/session/mod.rs` 的 typed interrupt/steer 与 expected turn 边界、
  `会话运行时-rs/tui/src/chatwidget/status_controls.rs` 的独立状态渲染。复用的是边界，不复制其上下文实现。

## 2026-07-14 完成摘要进入回复信封与会话历史

- 1.10 双长任务实测发现 `submit_for_acceptance` 已写出准确摘要（25/25、19/19、`/btw` 新增功能），
  但旧 `project_user_reply` 只渲染 artifact 文件名，后续同用户追问只能从原需求猜测试数。
- closeout 现在优先保留模型自然最终答复；工具提交轮则从当前 run 最后一次成功验收提交提取
  `summary`，兼容旧 `note`，以结构化 `user_summary` 传递。1.10 MiniMax 复验捕获了“自然结束但未调用
  `submit_for_acceptance`”的真实分支，防止已经生成的 7/7 说明再次被机器完成块替换。摘要只负责沟通，
  不改变任何验收 gate。统一投影保留摘要和文件名，拒绝
  `MAIN_AGENT/RUN/SUBAGENT/TOOL_CALL` 脚手架并把宿主绝对路径替换成 basename。
- 对照 通道运行时 `agent-runner-payloads.ts` / `sanitize-user-facing-text.ts` 的“保留最终文本、只清洗内部
  脚手架”，以及 长期助手 `长期助手_cli/oneshot.py` 的“执行输出静默、最终答复单独输出”。没有引入第二套
  IM 路由或额外模型总结调用。

## 2026-07-13 普通飞书对话、工作与定时共用常规主链

- 普通最终回复、后台主动消息和显式 `send_message` 已收敛到同一 `DeliveryService`。可信
  `DeliveryContext` 持有 channel/target/reply_to，`ReplyEnvelope` 只持正文和 typed attachment；
  模型不能提供或覆盖收件人。`ChannelAdapterRegistry` 统一注册 adapter、capabilities 和 target
  validator，第二个 fake IM 契约证明接入无需修改投递主流程。
- Gateway response、飞书最终回复、后台自动续跑报告和 assistant transcript 共用
  `project_user_reply`：内部 `MAIN_AGENT/RUN/SUBAGENT` 协议只留在运行时，用户只看到简短正文；
  后台轮写 transcript 前先投影，防止机器协议进入后续 compact/旧聊天检索。公开 response 不包含
  服务器 path，owner transcript metadata 才保存最小产物引用。
- 主代理注册唯一通道无关 `send_message`。目标固定取当前 scoped owner 的真实 Feishu `open_id`，
  附件在副作用前核对 task artifact registry、owner 真实路径边界、ready 状态和 SHA-256，再走 adapter
  原生 `send_image/send_file`；发送回执持久化去重。同会话下一轮“发我”直接复用最近产物 path，
  不再搜索、复制或重新生成。用户可用 per-thread `/verbose on|full` 显式开启逐工具进度；更高层低频
  阶段汇报仍是后续项。
- 飞书适配器不再把 `user_id` 当会话：普通消息使用真实 `chat_id`，话题消息使用
  `chat_id:thread:<thread_id/root_id>`；该值从 adapter 一直传到 `/ask` 的结构化 conversation。
- 每轮执行前读取同一 owner、同一 channel conversation 的累计 user/assistant 历史；当前用户消息
  保持原文和最高当轮权威，回答后双方消息按 request/message ID 幂等写回。用户消息落账失败会在
  模型前拒绝；assistant 落账失败会先交付真实结果并进入持久化 repair，下轮幂等修复。
- 固定最近轮数不再是遗忘边界。Gateway 复用现有 runtime compact 阈值、token 估算和模型 backend：
  阈值前注入完整未 compact tail；到点后把较早段总结进同 thread 的 summary+cursor+generation，原始
  transcript 永不删除，保留近期 raw tail 后继续累计；message ID + byte cursor 让后续轮直接读取新增尾部，
  不随整份历史线性重扫。旧消息幂等投影到 owner-local LocalStore，
  `session_search` 可跨 compact 找回，但不同 owner 的索引物理分离。
- `/verbose off|on|full` 按 thread 持久化。工具循环把 typed progress 与 model delta 分栏写 chunk；
  `/progress/<request_id>` 复用 result owner 鉴权，delivery worker 按 cursor 回送且不重提任务。
  `full` 输出先做凭证脱敏、owner path 替换和长度限制。
- 删除“自动选择 active task 并把旧 goal 包住当前 follow-up”的默认行为。普通聊天即使同 thread
  有旧 active task，也不注入 goal/workspace；只有结构化内部 task ref 或显式特殊模式才续接。
- 普通“帮我做事/明早提醒我”不需要关键词和斜杠命令。模型仍在同一常规对话链上自然选择工具；
  `task_progress`、`create_subagents`、`wait` 真正执行时才把当前 run 绑定为后台任务。
- 同一会话准入上限固定为 1，确保第二条消息读取历史前第一条已落库；同一用户的不同会话仍可并行，
  每用户 8 / 全局 500 的原有公平上限不变。
- per-user owner 默认开启。远程 channel 身份缺失、owner home/agent 创建失败时返回
  `OWNER_SCOPE_UNAVAILABLE` 并终态归档，禁止回落到共享 main agent。
- adapter 现在把结构化 `channel_chat_type/channel_chat_id` 一并送入 Gateway：私聊按发送用户进入
  `users/<user_id>`，群聊按真实会话进入 `groups/<chat_id>`；同一群成员变化不会改 owner，群数据也
  不会落进首个发言人的私人目录。
- 远程 scoped agent 的 prompt、文件工具和 shell 共用 owner home 这一份工作区事实；文件写工具拒绝
  owner 外路径，shell 拒绝把公共 `service-cwd` 作为额外可写挂载，只有 delivery contract/write
  boundary 明确声明的外部输出根会临时加入本轮写入范围。
- root systemd 进程的宿主 home 放宽只用于无 owner scope 的本地管理员；Feishu 等远程 owner 保留
  `/root` dangerous-root 拒绝边界，只以精确 owner home 白名单读取自己的数据，避免宿主身份扩大租户权限。
- Feishu 默认长连接、私聊密码卡默认开启；SOUL/AGENTS 修改必须由发起人点击确认卡片，USER 偏好
  仍可由 Agent 直接维护。首次设置卡不吞首条消息。默认 prompt 使用包内 `builtin:` 资源，不受
  service cwd 影响。
- 普通对话 transcript 不再重复写 owner-global dialogue memory；历史 dialogue 在检索层先扩量后
  排除，避免把有效 preference/lesson 挤出 top-k。
- active task 只作为只读候选；模型用 `task_progress action=select` 结构化选择后，update/wait/子代理
  继承同一 task id/workspace。完整任务 ID 索引由 `task_ids` 保存，候选热索引由 `active_task_ids` 保存；
  结构化 closeout 完成后只从热索引移除，后台策略和审计仍能读取历史链接。子代理继承该引用只为
  归账，`subagent_*` run source 的完成块不能关闭父会话任务。最近完成项另作为有界、非默认候选；
  `subagent-*` 与 `bg-main-*` 内部链接不进入普通用户的 active/completed 候选，也不触发工作区选择；
  用户明确要继续/修改时模型必须在文件操作前结构化 select，原工作区才重新打开，误建的新任务链接
  标为 `superseded`。候选加载和 compact 加载各自报告错误，不会把残缺上下文伪装成正常空历史；后台
  主代理也携带精确 thread/task 引用，closeout 后不会继续唤醒已交付任务。同会话存在候选时，首次
  progress update 必须先结构化 `select` 或 `start`，普通用户无需特殊命令。
  select 后统一工具轮会把仍指向本轮占位目录的结构化路径改写到所选任务根，派工目标、输入输出引用
  和命令中的完整目录保持同一工作区，不会只切进度账本。
  子代理迟到完成时若其 root task 已非 active，wake 只归档不执行，避免 superseded/completed 旧任务
  回流污染当前聊天。
- Feishu adapter 提交后立即返回，持久化 delivery worker 负责长任务最终回送和重启恢复；scale worker
  也复用同一 gateway 对话主链，ASGI 卡片 action 不再进入普通消息队列。
- Feishu 主动发送和引用回复共用 8000 字符分片边界；长任务结果逐片引用原消息，所有分片都会尝试
  发送，任一失败则保留 delivery 失败态供既有重试链恢复。
- Gateway typed progress 与被认领的权威请求记录放在同一队列目录；per-owner Agent 只负责执行，不能
  把过程流写进自己的私有 Gateway 目录，否则 HTTP `/progress` 与 Feishu delivery worker 看不到。
- 长任务交付扫描不把 `node_modules`、`.venv/venv`、`_deps`、`site-packages`、`.tox`、测试/lint/Python cache 当用户产物，避免有界
  artifact 清单先被数千个依赖文件占满、真正代码和测试证据反而不可见；文件本身不会被删除。
- `/result/<request_id>` 的 USER 授权在所有状态都读取请求记录 owner：热请求读 pending/processing，
  完成请求读 done/failed 归档；响应正文不充当身份来源。归档缺失、损坏或多份身份不一致时默认拒绝，
  同一用户完成态可读且跨用户仍为 403。USER 的完成 JSON 另走字段白名单，默认不公开新增运行字段，
  且移除 request/chunk path、lease、prompt 和内部错误细节；可信管理员仍可读完整诊断记录。
- 参考核对：长期助手 用稳定会话重放；会话运行时 用统一 Regular 主链和结构化 tool call；claw 用持久
  per-user session。未照搬 会话运行时 goal 自动续跑、claw 群聊首发言人归属或双 persona 路径。
- 原生附件发送另核对 通道运行时 Feishu typed media dispatcher/outbound，以及 长期助手 的通用
  `send_message` + adapter native media；复用统一工具、结构化附件和当前通道 adapter 三个边界。
- 累计上下文另核对 通道运行时 stable sessionKey/per-session verbose/compaction handler，以及 长期助手
  stable gateway_session_key 贯穿 session、compression、memory provider 的作用域；只复用作用域传递，
  不复制它们的 compact 算法或 profile-wide memory 默认值。

## 2026-07-10 外部通道目标与日志边界加固

- 主动外呼在构建 adapter/发网络请求前按 channel 声明校验目标类型；Feishu `open_id` 只接受合法
  `ou_` 目标，无效目标返回 `CHANNEL_TARGET_INVALID`，不再让同一错误持续打到外部 API。
- `DeliveryReceipt` 提供结构化 delivery status/error code；adapter 不可用、明确发送失败和发送异常
  分别使用已注册恢复合同。相同 channel/target/error 的日志在进程内有界去重，日志只保留目标长度。
- service adapter 启动即安装统一日志脱敏；第三方 SDK 打出的 URL query、Bearer、secret assignment 等
  在进入 stderr/journald 前清理，避免 Feishu WebSocket ticket/access key 出现在运维日志。

## 2026-07-09 P0 lint 收敛

- `request_worker.py` 与 `gateway_process.py` 仅做 Ruff 的导入归属、排序和前向注解清理；gateway 入口、队列、lease、审批和执行语义没有变化。

## 2026-06-11 scoped lock 语义定性：进程级单例，非线程互斥（方案A）

- 全仓调用点排查实锤：`acquire_scoped_lock`/`release_scoped_lock` 生产代码零运行时调用
  （`daemon_control.py` 仅作公共 API 转口；`supervisor.py` 的 import 是死引用，已删；
  CLI/scripts/动态引用为零）；gateway 单进程多线程路径（heartbeat/request/background/
  worker 池）均未把它当临界区用，无存量数据竞争。
- 对照组核查（长期助手 ProcessRegistry）：进程内并发一律 `threading.Lock`，pid+start_time
  只做进程身份。据此定性"同进程线程重入=刷新心跳"是契约特性而非缺陷，采用方案A：
  文档化语义 + 模块注释禁止线程临界区用法；不给锁记录加 thread id（方案B 会破坏
  supervisor 重入刷新），也不新增无调用方的线程锁原语（线程互斥直接用
  `threading.Lock`，参考 `agent/io/jsonl.py` 双层锁先例）。
- 原 strict xfail 钉子（8 线程计数互斥）按正确语义改写为
  `test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`：
  锁定同进程线程重入刷新、真实子进程抢锁必败、非持有进程 release 不误删、
  release 后干净重持有四条契约。`scoped_locks.py` 补齐 LLM/人类双层中文注释。

## 2026-06-10 空闲扫描门 + recover 节流 + 队列年龄观测

- 空闲 gateway 的每 0.2s 轮询不再做 inbox 全量 glob 和 processing 恢复扫描（mtime 门 + 节流）；
  请求拾取延迟上界不变（mtime 变化即扫描）。
- heartbeat 增加 queue_ages 结构化观测；jsonl 路径锁内存泄漏修复（引用计数回收）。
- chat 消息读取改尾部倒读（conversation/store.py `read_jsonl_tail_report`），5000 行账本取最近
  20 条实测 41x 提速、结果与全量读逐位一致。
- 评估后推迟：subagent tree 投影缓存（≤10 子代理的真实场景全量扫描仅数毫秒，待 R2 真实并发
  测出瓶颈再做，避免给可变 task 对象引入缓存别名风险）；lane 化并发同理待 R2 数据。

## 2026-06-09 Gateway ready、日志和后台提醒降噪

- `gateway start` 不再只等 PID 文件出现；启动确认会等当前 pid 对应的 `gateway_state.json`
  或 `gateway_heartbeat.json` 写入 `status=running`。子进程秒退或超时未 ready 时，start
  返回失败，避免 CLI 看到“pid 活着但 state 还是上轮 killed”。
- `status` 里的 `gateway.request_counts` 只表示当前 pending/processing 热队列；
  历史 done/failed/responses 进入 `gateway.archive_request_counts`。当前建议只看热队列，
  不会因为旧 failed 归档提示“现在要排查”。
- gateway run 会在 stdout/log 中写当前运行分隔线：
  `[gateway-run] status=starting ...`、`[gateway-run] status=running ...` 和停止 marker。
  `gateway logs` 优先从最近一次 starting marker 往后显示，因此旧 run 的
  `[gateway-background-main]` 行不会污染当前 tail。
- 同步 `gateway ask` 如果已经把流式正文打印给用户，最终 response 渲染只补结构化状态行，
  不再把同一段回复打印两次。`gateway result` 和 `--json` 仍按完整 response 输出。
- 后台主代理的 periodic progress policy 不做陈年追补：错过窗口太久的周期提醒只被 snooze
  到下一次，不唤醒模型；同一 thread/task/route 的重复 policy 同轮只跑一条，其余写入
  `progress_policy_suppressed` 诊断事实。这个降噪只基于 `ProgressPolicy` 和
  `ThreadTaskLink.status` 等结构化字段，不解析自然语言。

## 2026-07-15 后台完成事件合并与用户通知分层

- 成功子任务的 `subagent_runner_finished` 信号先按
  `background_completion_coalesce_seconds`（默认 5 秒）短暂合并；同一 thread 在窗口内连续完成的兄弟
  任务只触发一次后台主代理整合。失败、阻塞和能力申请不进入这个等待窗口。
- 后台主代理仍可逐批读取结果、补派依赖工作和更新任务账，但一次成功完成后若同 root 仍有子任务未结束，
  本轮自然语言回复标记为 `partial_subagent_success` 并留在内部，不写普通 transcript、不主动发 IM。
  root 下子任务全部结束、任一失败/阻塞或需要用户决定时才进入公开投递。
- 自动监督的 material signature 继续负责跳过“状态完全没变”的周期 LLM 调用；完成信号合并负责处理
  “短时间多次真变化”。两者职责不同，显式 wait 与监控发现仍保持原语义。
- 后台轮从 `ThreadTaskLink.task_path/goal` 恢复原任务 workspace 和标题；内部“定时唤醒”提示、子代理 runner
  prompt 不能再创建同级伪任务目录，也不能覆盖根任务 goal、task path 或 owner task index 标题。
- runner 完成通知用“wake 先落盘、observation 后落盘且双向关联”的单一发布入口，消除调度器恰好夹在
  两次写入之间造成的重复 LLM 轮；wake 写失败时保留 observation fallback，并沿用生命周期 reason。
- `wait`、自动派工监督、open-coverage 续推属于内部继续工作。相关 root 仍有非终态子任务时，模型可继续
  派工/整合，但“仍在处理”占位正文不进入普通 transcript；异常或全部终态仍公开。

## 2026-07-16 停止后续接与 live steer 精确绑定

- 对照 会话运行时 active turn 的 expected turn id/cancellation、通道运行时 active session run queue/abort 后，
  `/btw` 的消费目标改为本轮已结构化选择的 `conversation_task_id`；gateway request 自己的 task id
  不再冒充持久任务。引导仍按 FIFO 一次消费，已有 live turn 时不发布第二个 wake。
- interrupted task 以 `task_id/status/goal/task_path` 进入 Resumable Work Candidates。`task_progress select`
  只保留唯一 `task_id` 参数，删除 select 的旧 `run_id` 兼容入口。
- 文件写入、命令、浏览器、PTY/LSP，以及 `create_subagents`/`wait` 都由统一 `promotes_task` 执行门检查：
  同 thread 有可选现场但本轮未绑定时，必须先精确 select 或显式 start；选择失败后不得懒创建本轮任务。
- 状态判断只读 task links、RunParams task attributes 与工具结构化调用，不匹配“继续”等自然语言。

## 2026-07-16 live steer 跨前台/后台续跑

- 1.10 双用户复测曾暴露 `/btw` 在前台 turn 确认后、耐久续轮中丢失。把已确认内容复制成 task-id
  task-id 历史投影的候选已经删除，因为它会形成第二份 task history。
- 当前实现对照 会话运行时 `TurnInput::UserInput`：`/btw` 在安全点成为 provider-neutral UserTurn，模型成功接收后
  幂等追加到唯一 thread transcript；compact continuation 用 typed carrier 保留准确位置，后续耐久 turn 则从
  同一 thread summary + raw tail 读取。未确认输入仍按结构化 request/task identity 重试，不解析补充文字。

## 2026-07-17 `/btw` 在工具历史中的真实用户位置

- 1.10 Pastel 长任务真测中，前两条 `/btw` 能改变 gradient 实现，但后续“只接受标准 wheel 安装”在首次
  投递后，模型下一轮仍回到手工 wheel 和源码 `PYTHONPATH`，最终还把未完成的安装验收写成收口报告。
- 代码级复核发现旧 native 链只把 steer 临时接成一次收尾 `role=user`；下一轮精确文本去重后该消息从
  messages 尾部消失，只剩动态首轮 prompt 中时间位置错误的 runtime injection。它不是自然语言识别问题，
  而是 turn history（执行轮历史）缺少 user item 类型。
- 对照 会话运行时 `session/mod.rs::steer_input`、`session/input_queue.rs::TurnInput::UserInput` 和
  `session/turn.rs` 的 pending-input drain，本地候选给 provider-neutral 工具 IR 增加 `UserTurn`。`/btw`
  在安全点按 guidance id 注入一次，但作为同一 turn 的真实 user message 保留在准确的工具往返位置，之后
  每次采样继续可见；text 协议用同一位置的 `ACTIVE_TURN_USER_INPUT` transcript 条目。
- 用户引导不再进入 runtime injection，也不再携带 guidance id、target、priority 等内部控制字段给模型。
  task/subagent 运行事件仍走结构化 runtime guidance；两者不混用。模型成功接收后才确认 delivery，provider
  失败前仍可重试。判断和去重只认 typed guidance id 与 task/run identity，不解析中文内容。
- 同一 run 达到上下文压力后，compact 自动续跑会新建 `ToolLoopExecuteParams`。候选版本把已送达的
  current-turn user input 作为独立 typed packet 从 loop result 带到下一份 run params，再在新 loop 中
  重建 `UserTurn`/text transcript。它不回放 guidance inbox、不二次确认，也不从 compact 摘要或提示词
  反解析用户要求；多次 compact 以 input id 去重，文字相同但 id 不同的两次输入仍分别保留。

## 2026-07-16 终态归档一致性

- worker 收口时以最终 response 的 `done`、`interrupted` 或 `failed` 覆盖请求 lease 里的旧
  `processing` 状态，再移动到 `done/failed` 目录；owner、attempt 和 heartbeat 字段继续从 lease 保留。
- 这避免 `/stop` 后出现“task link 与 `/result` 已 interrupted，但归档 request 仍 processing”的三方漂移。
  本地专项回归已通过；1.10 真机复测需等当前长任务自然收口并部署后完成。

## 2026-07-16 恢复执行与持久任务共用同一事实账本

- `/stop` 后自然续接会创建新的 gateway request/run id，但这只是新的执行尝试；原任务、workspace、进度、
  子代理树和监督提醒仍归结构化 `conversation_task_id`。
- runtime 现在通过唯一的 task identity 解析器给 guidance、task_progress 工具、需求/派工 seed、coverage、
  wait、监督提醒和 delivery closeout 提供同一个账本键。主代理恢复轮不会另读一份空账，子代理 task_local
  scope 也不会因继承父 `conversation_task_id` 而误写或关闭父任务。
- 本地回归覆盖“恢复轮存在开放待办时必须被收口门看见”“恢复后派工仍写原账”“恢复后的主代理仍认领
  原任务下未结束子代理”；不读取“继续做”等自然语言来决定归属。

## 2026-07-16 目标续跑的紧凑任务现场

- `/goal` 每个 continuation turn 继续读取同一 thread 的 summary + raw tail；精确 task link、子代理树与
  `task_progress_summary` 只是补充运行事实，不替代或裁剪会话历史。
- task recovery rollup 将根任务进度与 child 状态一起写入 `work_state_snapshot.json` 和
  `continue_packet.json`。派工种下的 child 进度项只按精确 `run_id + canonical DONE` 自动闭合；
  它们只供崩溃恢复和进度核对，不作为另一份模型上下文。`integrate-and-verify` 仍由主代理根据真实整合与
  测试事实更新，不成为系统验收硬门。
- `task_progress select` 成功结果同时返回 task 状态、是否复用原 workspace、匹配 goal 状态和是否已安排
  continuation。普通回复仍由模型生成，但模型不应在结构化事实显示原目标已恢复后再次向用户索要任务。
- `/stop` 后用户补充要求或说继续时，消息仍追加到原 thread 历史；模型可用结构化 task candidate 重新选择
  原 workspace。系统不再拼装一份“最近 guidance”表达事实包，也不要求用户重发原 prompt。

## 2026-07-17 live turn 期间普通消息不再排队等待

- 真实 Feishu 客户端从同一用户发起约十分钟长任务，任务运行中再问一句普通问题；旧实现直到长任务最终
  回复后才启动第二个 Gateway request，用户实际等待约八分钟。上下文回答正确，根因是同 conversation
  admission 把所有普通消息一律留在队列，而不是记忆或模型问题。
- 对照 通道运行时 默认 active-run `steer` queue，Gateway `/ask` 现在先按认证 owner/channel/thread 和 live
  processing record 决定是否进入当前 turn；消息正文保持不透明，不做自然语言任务分类。命中时复用现有
  `/btw` durable guidance、expected-turn 竞态保护、UserTurn 注入与旧响应失效主链，不新增 chat/task 会话。
- 只有 durable task、没有 live Gateway request 时，普通消息仍创建新 turn 和回复信封；显式 `/btw` 才能
  直接纠偏这种后台任务。adapter 收到 typed `status=steered` 后复用原 request 的投递记录，不重复派工。
  新用户输入会重新开放一次模型自然 commentary，普通工具轮仍保持抑制。
- 本地针对性回归覆盖 HTTP 真入口、owner 隔离、精确 linked task、无 live turn 回落、重复投递抑制、模型
  commentary 重开和 guidance safe point；网关/adapter/context 关联测试 207 项通过。1.10 真实 Feishu
  平台复验完成前仍按候选能力记录，不升级产品事实等级。
- `9e04affd` 精确部署到 1.10 后，真实飞书用户 `ou_6591…a895` 从客户端发送
  `FEISHU-LIVE-STEER-A-20260718` 长任务；request `req_1784311406795_1506981_0` 在约 12 分钟内始终是
  唯一 processing request。任务运行中到达的 `FOLLOW-A-20260718` 进入同一个 typed guidance/UserTurn
  队列，模型先用一句话回答双报告类型，原任务继续运行并最终 `DONE`，没有第二个 Gateway request。
- 最终飞书引用回复只包含模型自然摘要、相对任务位置和测试结果；当前任务的可见 commentary/final 未出现
  XML、工具协议或成串命令。交付目录复制到不含原缓存、原报告和 `work/` 的干净临时目录后，38/38 测试、
  CLI help、四份 YAML/JSON 示例预览、正式写出、坏 JSON 和缺失文件路径均独立复验；预览为 27 个改动和
  1 个刻意设置的引用错误，成功重新生成 JSON/Markdown 报告。第二个真实飞书用户尚未完成，因此本项仍
  保持“部分可用”，不把单用户平台证据外推为两用户隔离或规模证明。
- 第二个真实飞书用户 `ou_1be7…f921` 从客户端发起“青竹账单”长任务时，原消息在模型调用前失败为
  `ConversationPersistenceError`。根因不是模型或 Feishu：该 thread 留有旧版本合法 `cleared` goal tombstone，
  新版本目标状态枚举把它误判为损坏。当前工作树只兼容这一种有明确历史来源、身份字段完整的 tombstone，
  等价为“没有活跃 goal”；其他未知状态继续 fail-closed。store 与真实 Gateway 普通聊天回归均覆盖该边界。
- 修复后通过同一 Feishu owner、channel 和 `conversation_id` 的可信 localhost `/ask` 重试；该次是服务器侧
  模拟，不冒充第二次平台客户端入站。长任务只创建一个 child，并在 Gateway 重启后续跑到 48/48 自测，
  普通 follow-up 与 `/btw` 都作为同一 thread 的 UserTurn 入账，最终经真实 Feishu 主动投递。过程中还复现了
  child goal 携带同 owner 的旧 `tasks/.../output/<project>` 绝对路径，导致它反复写入被 owner wall 拒绝；
  当前工作树在已有输出引用边界内把这种显式路径事实重绑定到当前任务的 canonical output，保留 `output/`
  后的项目尾部，结构化 `user_requested_output_dir` 仍具有优先权，未增加项目名或中文语义判断。
- 独立干净副本验收没有接受模型的“48/48”自述：CSV 报告会重复写缺失项，常见“两笔银行扣款对一笔支付”
  没有产生 `DUPLICATE_CHARGE`，支付端已退款而银行无退款时没有产生 `MISSING_REFUND`。验收同时发现一个
  无关历史 child 记录损坏会让后台送达策略 fail-open；统一运行门已改为仅在精确根任务链接为 `completed`
  时允许最终发送，否则压住中间整合，并用真实坏 `task.json` 回归覆盖。
- 1.10 恢复后部署候选 wheel，并沿同一 owner `ou_1be7…f921`、同一 conversation
  `oc_388c…cddd` 通过可信 localhost Feishu-scoped `/ask` 发起纠错 request
  `req_1784346588830_4174_0`。该次是服务器侧模拟，不冒充第二次平台客户端入站。模型最初选中本轮空
  占位 task，但第一次文件变更带有原项目的显式绝对目标；统一 effect 前门据此只在同 thread 的精确旧 task
  中选回 `req_1784318904169_1524072_0`，重新打开旧现场并 supersede 空占位，没有创建或复制第二个项目。
- 运行中两次 `/btw` 都返回 typed `steered/active_turn_input`，在同一 request 的安全点成为真实 UserTurn；
  第二次纠正模型手敲错的中文目录名后，后续读取和编辑命中原项目。请求共 81 个工具轮、1715 秒并自然
  完成；HTTP response 与同一 transcript 的用户投影没有 XML、工具协议或成串命令。服务器侧 `/ask` 不走
  Feishu adapter 的 pending reply 队列，因此这条完成回复本身不作为平台原生收件证明。
- 把原项目复制到独立临时目录后，55/55 测试、CLI help、JSON/CSV/Markdown、月度/商户汇总、坏输入、
  六类稳定原因码、无逻辑重复异常均通过；远端项目和独立副本都没有 symlink、缓存、pyc/pyo，任务 output
  清理后只剩 `qingzhu-bill`。A/B owner 的产物、SOUL/USER/AGENTS、skills、memory 反向检索无交叉命中，
  A 的私有 token 在 B 为 0，B 的青竹项目 token 在 A 为 0。
- 真测还暴露出 selection 后进程工具默认 cwd 仍停在服务 workspace，导致模型反复拼接超长路径。对照 会话运行时
  的 turn cwd 和 通道运行时 的 workspaceDir，统一 registry 候选在没有显式 `working_dir` 时把结构化
  `task_root` 注入 `run_command` 与 PTY start；显式目录仍优先，ShellTool 继续校验 workspace roots，且完全
  不检查命令正文或用户文字。SHA-256
  `bd8f9eb2828f53a27622aeaa17f0596224b3219177861e2d27ce3718c9906ca1` 的最终 wheel 已精确部署到 1.10；
  安装后源码探针确认新逻辑已加载，Gateway/Feishu active、`NRestarts=0`、WebSocket connected。
- 部署后请求 `req_1784350294487_12075_0` 用同一 B owner/conversation 的普通中文要求主动发一条无附件
  结论。模型在 1 个工具轮调用统一 `send_message`，Gateway 记录
  `NATIVE_CHANNEL_SEND_OK channel=feishu attachments=0 mode=proactive`；响应投影为纯自然正文，无工具协议。
  A 中 `qingzhu-bill` 目录数仍为 0，B 中仍为 1，没有因发送验证新建或复制项目。

## 2026-07-19 前后台共用会话 lane 已发布

- `f19d0be4` 把 Gateway 前台 turn 与 scheduler/background main 都接到
  `conversation/run_claim.py` 的同一持久 per-thread lane；拿到 lane 后才读取 history、compact 和 task
  状态，释放前先写入 assistant transcript。不同 thread 不共享该 lane。
- 完整本地门禁、远端 main 和 1.10 精确 wheel 部署均完成。真实一次性提醒跨 Gateway/Feishu 重启后只
  产生一个 scheduler history 和一次 `NATIVE_CHANNEL_SEND_OK`；另一提醒到期时前台长任务持有同 thread，
  后台未并发执行，直到 `/stop` 释放 lane 后才投递一次并自动暂停。这条证据不依赖回复正文判断 busy、
  完成或送达。

## 2026-07-19 promotion 后动态 workspace 重绑定已发布

- 真实长任务在 `task_progress start` 后已有正确 owner task 目录，但 write/run 连续返回
  `WRITE_FORBIDDEN`。根因是 `ToolLoopExecuteParams.task_attributes.run_workspace` 已动态更新，而初始
  `write_boundary` 仍保留旧 service cwd；两份结构化路径事实发生漂移。
- 候选修复位于所有主/子代理工具共用的 `tool_runtime_ledger.write_boundary_with_runtime_ledger`：当前
  `run_workspace` 每次工具调用覆盖旧 task root/output/work；远程主会话以精确
  `conversation_thread_id + conversation_task_id + context_scope=default` 重绑定当前 task 写根。
  `task_local/control_plane` 仍过滤并保留子代理自己的窄授权，非法 owner/task 路径继续 fail-closed。
- 代码参考不是文档类比：会话运行时 `会话运行时-rs/core/src/session/turn_context.rs` 从当前
  `TurnEnvironment.cwd/workspace_roots` 构造 turn；通道运行时
  `embedded-agent-runner/run/attempt-tool-base-prepare.ts` 把 `effectiveCwd/effectiveWorkspace` 传给实时工具
  构造，`agent-tools.ts` 再用该 root 建 filesystem/shell guard。my-agent 只适配这一“当前结构化 workspace
  是工具权威输入”的做法，没有新增 IM 分支或自然语言规则。
- 聚焦回归已覆盖旧 bootstrap root 被替换、主会话重绑定、子代理不扩权、任务选择、write boundary、
  owner 隔离和并发隔离；完整本地 CI 通过。`04c34947` 已推送远端 main 并精确部署 1.10，配置未漂移，
  Gateway/Feishu active 且 `NRestarts=0`。正确 A/B conversation 的普通续作均选回原任务；A 的
  run/write 工具不再命中旧 bootstrap root 的 `WRITE_FORBIDDEN`。长任务独立产物验收继续单列记录，
  不用“能写入”替代功能正确性证明。

## 2026-07-19 completed projection 与 live turn 交接候选

- 真实 A owner 的根 task link 已是 `completed`，但 exact background claim 仍为 live；旧控制入口只读 active
  link，造成 `/status` 假空闲、`/btw`/`/stop` 失联。当前控制入口先读取该 thread 全部可选 root links，
  再用一次 per-thread execution snapshot 识别 `completed + live` 的短暂交接态。`interrupted`、过期 claim
  和不可读状态均不进入候选；`/stop` 用被选 link 的真实旧状态作 CAS expected status。
- `/btw` 命中已有 live claim/policy 时只追加同一 task 的 durable guidance，不再额外发 wake 创建第二执行器；
  状态不可读时不猜测。`_durable_task_is_current`、status、steer、stop 共用同一候选解析器。
- 代码级参考为 会话运行时 `session/mod.rs::steer_input` 对 exact active turn/expected turn id 的检查、
  `tasks/mod.rs::on_task_finished` 对 active turn 的清理；通道运行时 active-run control 也把 steer/abort 绑定到
  runtime run id。my-agent 的适配权威是 task link、claim、policy 和 CAS，没有自然语言分类。
- 聚焦回归覆盖 live completed 可 status/steer/stop、过期不复活、interrupted 不复活、同 thread 多个历史
  completed task 仍只读一次 policy/claim。完整本地门禁、发布和 1.10 真机反证尚待完成。

## 2026-07-19 显式文件发现不再对忽略目录假阴性

- 真实 B owner 两次宣称原项目没有 pyc，但外部 `find` 每次都看到同一 `__pycache__ + 4 pyc`。tool-output
  index 证明模型传入精确原路径和 `**/*.pyc`；结果为成功且 0 命中，因为公共 file discovery walker 在
  glob 匹配前裁掉了缓存目录。
- 当前 `find_files/list_files` 对宽泛发现仍默认跳过 `.git`、`node_modules`、venv 和缓存；只有调用方没有
  显式设置 `include_ignored`、显式 glob 在默认可见区零命中时，才自动补查忽略目录并在 result envelope
  标出原因。显式 false 可关闭回退，显式 true 始终包含，分页和上限语义不变。
- 对照的具体代码是 会话运行时 `linux-sandbox/src/bwrap.rs::ripgrep_files` 中 `rg --files --hidden --no-ignore`
  的显式 glob 语义，以及 工具运行时 `packages/工具运行时/src/tool/glob.ts` 到
  `packages/core/src/filesystem/ripgrep.ts::filesArgs` 的 host-side glob walker。my-agent 保留自己的降噪默认，
  只修显式查询忠实性；没有以用户正文或样例项目名作判断。

## 2026-07-19 子代理 workspace 与父 conversation task 解耦已验证

- 1.10 真实 A 长任务中，父代理开出两个 child 后，child 为写入用户明确延续的旧项目绝对路径，命中了
  `select_current_conversation_task`。旧实现把“child 选择自己的工作目录”错误提升成“全局切换当前
  conversation task”，将本轮父 link 写成 `superseded`；parent-link lifecycle 随后按结构化状态取消两个
  child，前台只留下进度话而没有最终交付。
- 当前候选保留 `conversation_task_id` 作为 child 汇报所归属的父任务 lineage，只在 child runner 的
  `run_workspace` 内重绑定精确命中的 owner task path。child 不调用 reopen、supersede 或全局 select；
  任务 promotion 只验证父 link 仍 active，不能把已重绑定 cwd 覆盖回父目录。另一个真实失败面
  `capability_request` 现按缺参数、tool not allowed、run lookup 三类结构化错误返回，删除无码
  `UNKNOWN_ERROR` 回落。
- 具体对照 会话运行时 `会话运行时-rs/core/src/tools/handlers/multi_agents_common.rs`：`thread_spawn_source` 单独保存
  `parent_thread_id`，`apply_spawn_agent_runtime_overrides` 单独复制 turn `cwd`；以及 通道运行时
  `src/gateway/session-child-sessions.ts`、`src/agents/tools/sessions-spawn-visible.ts`：child session 与
  `parentSessionKey/spawnedBy` 分字段持久化。适配没有新增 IM 分支，也不解析用户自然语言。
- 回归锁定：child 精确重绑定后原 task 与本轮父 task 均保持 `active`，父 task id 不变，后续 promotion
  不覆盖 child cwd；主代理原有 completed-task select 仍会 reopen 旧 task 并 supersede 占位 task。
  聚焦测试、完整本地 CI、wheel 发布边界和 artifact clean-package 均通过。
- 1.10 双 owner 真测各沿原 Feishu conversation、persistent root task 和原项目创建两个新 child，四个 child
  全部 `DONE`；父 task 未被 supersede，A/B 都没有新建或复制第二个项目。A 的 typed `/btw` 进入同一 live
  task，结束后的迟到引导明确拒绝；公开回复只含自然 Markdown，没有新工具协议、命令串或宿主绝对路径。

## 2026-07-19 scheduler 空轮询不再重写 owner 账本

- 1.10 两个真实任务结束后，Gateway 仍稳定占约一个 CPU 核。`py-spy` 的具体调用栈落在
  `reserve_due_runs -> _write_store_unlocked -> OwnerQuotaAdmission.check -> owner_logical_usage_bytes`：没有
  due run 的 background owner tick 仍重写相同 store，并扫描大体积 owner home。
- `SchedulerRepository.reserve_due_runs` 现在只有真正预留 run 或推进 misfire（`reserved or skipped`）时才
  写账本；未到期、或已有 active run 而不能选择的轮询保持字节与 mtime 不变，也不触发 quota scan。
  回归使用真实 `OwnerQuotaEnforcer`，把 quota walker 替换为“被调用即失败”，同时覆盖上述两个 no-op 分支。
- 具体参考 通道运行时 `src/cron/service/jobs.ts::nextWakeAtMs` 只返回最早 enabled `nextRunAtMs`，以及
  `src/cron/service/timer.ts::armTimer` 在无下次执行时等待、对 past-due tight loop 使用最小重触发间隔。
  my-agent 只适配“没有状态变化就不写权威账本”的边界，仍由 owner JSON 保存 job/run、SQLite 只保存 wake
  投影，不复制 通道运行时 的调度存储。
- 候选 wheel `8acd7d73…c3d7` 部署 1.10 后，同一 5 秒 `/proc` 口径从 `1.008` 核降为 `0.030` 核，RSS
  从约 444 MB 降为 130 MB，线程从 18 降为 12；398 MB active-task 索引的 mtime/size 未变化，`py-spy`
  只见各 Gateway loop 等待，原 owner quota 栈消失。配置 SHA-256 未变，Gateway/Feishu 均 active、
  `NRestarts=0`。

## 2026-07-21 正式 Gateway 不再运行全局模型 planner

- 1.10 空闲期在 `owners/local/main/.../subagents/parent_planner_report.json` 与
  `PARENT_PLANNER_LOG.md` 看到同一陈旧 cancelled child 被每 30–90 秒重复送入 LLM planner，决策均为
  `applied=false`；同一正式 Gateway PID 因而长期占第二个本地推理槽。该调用不是 Feishu owner 的 request、
  scheduler due run 或 active continuation。
- 根因是 `gateway run` 主线程仍执行 process-wide `watch_subagents`，其默认身份为 `local/main`；正式
  request pool 与 owner-scoped background loop 已经各自管理用户任务，第三条全局模型循环没有 owner
  authority。当前 `gateway run` 主线程只等待显式 stop record；用户 request 和 durable continuation 继续由
 既有 owner-scoped event loop 执行。Gateway 专属 planner/watch/force-lock CLI、options、临时 capability
  router 已删除，显式独立 daemon/subagent dispatch 能力不受影响。
- 同轮流式工具真测发现，完整工具块已经可执行时，provider worker 的尾部仍可能继续生成并与下一轮并占
  两个槽。公共 model-generation 边界现在先调用该 worker 已注册的 typed transport close、短暂 drain，
  再把完整工具块交给执行器；普通 `/stop` 复用同一路径。持续有字节到达的 SSE 则由 transport 自己执行
  idle timeout，不再被外层 `request_timeout` 当总墙钟二次截断。
- 聚焦 Gateway command/background/tool-generation 回归 92 项通过；候选 wheel
  `6d7d031171c33918e56e95cf9d1225f0c8dba71374f3683ed5fe385e481ba9b1` 部署后，正式服务 active、
  `NRestarts=0`，旧 parent planner 报告 mtime 冻结，空闲时无 8899 连接。Chalk 本地 Qwen 任务只出现一条
  正式 Gateway 模型 socket；模型自身长 reasoning 和 `MODEL_INCOMPLETE_RESPONSE` 作为完成质量失败单列，
  不再用第二个 planner 或自动 replay 掩盖。

## 2026-07-21 同一 Chalk thread 的 steer/stop 真实反证

- 普通 Feishu-scoped `/ask` 以顶层 `conversation_id` 精确恢复原
  `thread-316db5cc5d814cba`、task `req_1784617354048_243904_1` 和原 `output/pychalk`；没有创建第二个项目。
  第一轮本地 Qwen 用满 16,314 token 后返回 `MODEL_INCOMPLETE_RESPONSE`，没有编辑；底座没有把半截
  reasoning 当成功，也没有自动重放同一 turn。
- 第二轮运行中 `/btw` 返回 typed `steer`，其 guidance 在下一安全点以 dedupe key 写入同一 transcript，
  后续模型上下文确实包含纠偏消息；没有第二个 Gateway request。模型仍未形成编辑后，`/stop` 返回 typed
  stop，当前 request 进入 `interrupted/INTERRUPTED`，8899 socket 关闭，原 task 标为 interrupted，thread、
  workspace 和 transcript 保留；随后直接使用本地模型像 会话运行时 停止按钮一样在同一现场说“继续”，没有
  等待 MiniMax 刷新。
- 后续普通中文工作按样式顺序、list casting 与 `apply/call/bind`、HEX/level 0/`visible` 三段推进，多次
  `/btw` 都进入当时同一 active request；一轮无新工具动作后再次 `/stop`，然后仍从同一 task 继续。模型
  曾重复 helper、猜错路径和改错 level 0，分别由后续工具事实、写边界和测试纠正，没有项目专用底座补丁。
- 远端原项目最终 42/42。重新复制的 `/tmp/my-agent-accept-chalk-clean.Fs2Fij` 与远端 8 个文件逐文件
  SHA-256 一致，独立运行同为 42/42 且无 cache、pyc、egg-info、build、symlink；另一个临时副本完成
  fresh install、导入和关键行为断言。控制链和完成质量现在分别有证据，但本地模型的长 reasoning、重复
  读取和多次纠偏仍是效率限制。
- 最终 wheel SHA-256 `4b882778888ee915a54a8414651965753999acfd935a4c0053d0c2755052bdcb` 的
  安装 archive hash 在 1.10 精确匹配；正式配置继续指向本地 8899，Gateway/Feishu active、`NRestarts=0`、
  请求队列为空、飞书 WebSocket connected。部署过程没有启动隔离 Gateway 或第二份飞书服务。

## 2026-06-09 活跃请求状态可观测

- CLI/gateway status 会显示 `requests/processing/` 中活跃 request 的结构化事实：
  `id`、`status`、`lease_owner`、`attempts`、lease age、heartbeat age、updated age，以及仍存在的
  `chunk_stream_path`。这只用于观察和排障，不改变调度、不新增硬门。
- processing request JSON 损坏时会显示 `gateway processing_load_error=...`，避免慢请求、坏记录或
  worker owner 丢失时只看到队列计数。

## 2026-06-07 本地轮询降噪和入口唯一化

- `gateway_process.py` 统一导入 `cli/gateway_loops.py`，删除旧的 `_gateway_process_service.py` 副本，
  避免 gateway worker loop 两份实现漂移。
- gateway 命令实现并回 `gateway_process.py`，删除 `_gateway_commands.py` 私有转发层；测试也改为 patch
  公开入口，避免真实入口和测试入口分裂。
- gateway run 状态、线程装配、恢复记录和 stop/kill helper 都并回 `gateway_process.py`；
  gateway 命令主链路不再绕同目录私有 helper。
- systemd/launchd service unit 生成和安装/卸载收回 `gateway_service.py` 一个公开边界文件，删除
  `_gateway_service_handlers.py` / `_gateway_service_unit_gen.py` 私有 facade helper。
- `agent/gateway_parts/supervisor.py` 保持一个直接 supervisor 边界文件；启动、停止、重启、
  heartbeat 健康判断和 runtime status 写入在同一文件内组织，不再保留中段 facade 注释或重复模块
  import 块。
- 进程控制入口收敛到 `agent/gateway_parts/process_control.py`；`daemon_control.py`
  不再顺手转出口 `is_pid_alive` / `wait_for_pid_exit`，只保留 PID record、后台化、
  scoped lock 和 shutdown request。
- chat/TUI 和 `gateway ask` 的响应等待改为按响应文件 `(mtime,size)` 状态读取：
  响应文件不存在或未变化时不反复 JSON 解析，出现或变化时仍走统一 load_error 结构化报告。
- `gateway_request_poll_interval` 改为小数秒配置，默认 `0.2`；后台 request worker 空闲时更快发现新请求，
  但仍保留最小 `0.05s` 防止误配造成空转。
- 流式 chunk 不再长期留在 `requests/processing/`：request 完成时随 request 一起归档到
  `requests/done/` 或 `requests/failed/`，response 记录 `chunk_stream_path`；
  chat/TUI 和 `gateway ask` 会从 processing/done/failed 候选路径补读，避免 response
  先出现时丢最后一段流式输出，也避免 processing 被历史 chunk 污染。
- `submit_gateway_ask()` 统一使用 gateway UUID 请求 ID 生成器，不再用毫秒时间戳拼 `gw-*`。
  多个 chat/TUI/gateway client 同一毫秒提交时，请求文件不能互相覆盖。
- 普通 CLI `gateway ask` 不再提交孤立 request：默认带 `gateway-cli/default` conversation；
  HTTP `/ask` 也会从结构化会话字段生成 conversation。worker 执行 follow-up 时可直接注入
  active task root/output/work，避免续接请求新建空任务目录后反复找不到上一轮产物。

## 2026-06-06 入口收敛

- 删除 `agent/gateway_parts/runtime.py` 聚合层。
- adapter 直接调用 `request_worker`，processing 恢复直接调用 `lease_service`。
- `agent/gateway_parts/__init__.py` 仍是包级公开入口；内部实现不再通过 `runtime.py` 多跳转发。
- CLI/gateway/TUI 状态行的当前上下文 token 展示归 `response_renderer.py`，不再通过单函数
  `context_tokens.py` 跳转。

## 2026-06-06 会话上下文精确化

- gateway conversation follow-up 只把 `status=active` 的 task link 当作活跃任务。
- `completed`、`done`、`closed` 等旧/展示状态不会再通过“非关闭即活跃”的逻辑污染后续请求。

## 2026-06-04 收敛

- gateway 文档改为只描述当前 `gateway_parts/` 队列、worker、lease、HTTP 和 renderer。
- request/response/history/status/PID 读取错误必须结构化显示，不能吞成空状态。
- gateway heartbeat 和 HTTP `/status` 只统计 pending/processing 活跃队列；status/doctor
  等显式诊断命令才读取 done/failed/responses 归档计数，避免历史响应目录变大后拖慢本地热路径。
- 后台启动失败需要早期健康确认并写明确失败状态。
- chat/gateway 多客户端共享队列时，应减少本地膨胀和重复读写，避免本地成为模型之外的瓶颈。
