# STATUS

## 2026-07-25 模型可见工具结果投影收敛

- 对照 会话运行时 的统一 history/tool-result 记录与有界替换、长期助手 的大结果外置、外部结果不可信包装和
  集中脱敏后，当前工作树没有新增第二个执行器。`ToolSpec` 只声明工具输出的最低
  `trust/redaction`，统一 Registry 在工具已经通过权限、参数、effect 与 operation 门并执行后，把有效
  策略写入同一 `ToolExecutionResult`；handler 只能把单次结果收紧，不能把外部数据降级成可信 runtime。
- 模型上下文、compact 语义输入、恢复重建、父子代理 shared context 和 handoff 现在都消费同一投影。
  外部网页、浏览器、MCP、视觉、watch 与工具输出归档正文会被标成不可信数据，嵌入其中的角色、指令、
  工具调用和伪结束标签不取得控制权；所有模型可见正文先经过统一凭据脱敏。源码读取使用保留代码语义的
  脱敏模式，但仍受路径、owner 和工具权限边界约束。
- 大结果完整正文仍保存在当前 owner/task 的 `work/blobs/tool_outputs/`，live prompt 只保留有界
  preview、hash、大小与恢复引用。后续 `read_artifact`、`read_file` 或 `search_text` 再读取该目录时，
  信任随结构化来源继续传播；既支持 JSON wrapper，也支持 resilience 生成的纯文本归档，不靠扩展名、
  文件正文或自然语言关键词判断。
- 聚焦 reducer/redaction/Registry/filesystem/artifact/compact/MCP 回归、本地 8899 Qwen 与
  MiniMax-M2.7 真实多轮工具链均已通过。真实飞书桌面端候选复验曾据此发现纯文本归档的旧传播缺口，
  修复后候选 wheel 已部署 1.10；最终双客户端复验、完整门禁、提交和精确发布仍在本轮收口中。

## 2026-07-25 多外部写部分结果与真实双 owner 复验

- 普通任务没有新增通用 Saga、跨工具事务、自动回滚或第二份执行账本。每个外部写仍经过唯一
  `tool_operations` 生命周期并按模型原顺序执行；一个失败不会在缺少 typed 依赖关系时机械阻断后续独立
  调用，系统只负责准确保留每项 `succeeded/failed/unknown`，不伪造跨系统原子性。
- 修复了 provider/handler 已回报成功、但权威 completion 保存失败时仍向上返回 `ok=true` 的错误。
  现在该调用统一返回 `TOOL_OPERATION_OUTCOME_UNKNOWN`，只保留脱敏的原始回报旁证；原 claim 继续
  阻止同 operation 重做。archive、control-plane event、模型恢复上下文和 compact 都保留同一组
  operation/effect 状态，语义摘要不能吞掉中段失败、运行中或 unknown 的副作用事实。
- MiniMax 极端 CLI 发现并删除旧 escape-relocate 兼容层：显式绝对路径不再被静默改写到当前 task
  `output/` 后冒充成功。相对 `output/...`、`work/...` 仍由结构化 task root 解析；绝对路径保留原目标，
  再由 owner/write boundary 明确允许或返回 `WRITE_FORBIDDEN`。对应专属字段、辅助函数和整组旧测试已
  删除，没有留下第二条路径处理链。
- 聚焦 operation/archive/compact/event/path/同轮多调用回归通过；完整 pytest 到 100% 且退出 0，
  Ruff、import/offline、strict code-size、doc-sync、compileall、diff、distribution boundary 和干净
  wheel artifact gate 同轮通过。本地 8899 完成 3 写 3 读基础 CLI；MiniMax-M2.7 完成 8 写 8 读长链，
  并在极端 CLI 精确得到 `succeeded / failed(WRITE_FORBIDDEN) / succeeded`，三项 generation 均为 1。
- 运行时代码提交 `2bd8622f` 的精确 wheel SHA-256 为
  `01ab7d15df60b1db613e7d52e211ce46bb3fe04bb52b7a0ccb43dd835b587614`，已部署到 1.10 唯一正式
  Gateway/Feishu。用户 A 请求 `req_1784957378024_1282076_0` 的三条写 operation 为
  `succeeded/failed/succeeded`，受限 `/tmp` 目标不存在且两个合法文件内容准确；用户 B 请求
  `req_1784957456380_1282076_1` 的跨 owner 读取在实现前拒绝，随后自己的写入/回读成功。
  A/B 又分别产生一条 generation=1 的 succeeded `send_message`，Gateway 日志确认消息只发往各自
  open_id，Feishu API 返回 sent receipt。两项服务保持 active、`NRestarts=0`、8420 loopback、队列为空。
- 上述请求复用了两个既有真实 owner 和 conversation；服务器侧请求使用可信 localhost Feishu scope，
  出站确实经过飞书 API，但不能冒充新的飞书客户端入站。macOS 当前锁屏，新的双客户端入站闭环仍需在
  用户手动解锁后补做；这一边界不会被写成已通过。

## 2026-07-24 工具缺参来源与有限补全发布

- 对照 会话运行时 `StepContext` 的 cwd/权限宿主事实和 长期助手 的 Schema 引导类型转换，本发布没有增加
  参数推断器。`ToolSpec` 只允许逐字段声明 `safe_parameter_defaults` 或从 Registry typed
  `run_scope/write_boundary/registry` 读取的 `trusted_parameter_bindings`；模型显式字段永不覆盖，
  普通 Schema `default` 注解不自动执行，其余缺失必填字段继续返回参数错误。
- 补全发生在统一 Registry normalize seam，早于参数、路径、effect、审批和工具实现。每个有效参数的
  `input_sources` 只保存 JSON 路径、`source` 和 `source_ref`，不保存命令、正文、密钥或参数值；
  来源声明、条件字段、默认值类型和可信引用在 Schema 展示前 fail-closed。来源账目现已进入短输出
  tool-call index 和大输出 record/artifact/index 的同一白名单投影，重启后仍可审计；任意 envelope
  私有字段和来源项里的 `value` 都会丢弃。
- `run_command` 已接 timeout/background 安全默认值及结构化 cwd，PTY 只在 action=start 时接 cwd，
  `read_artifact` 已接读取窗口默认值和 scope 身份。旧主循环 artifact scope 特判与执行末端 cwd
  补参已删除。聚焦工具/Schema/MCP/native/PTY/artifact 回归通过；完整 pytest 首轮除两个旧
  `_FakeSpec` 缺新可选属性外全部通过，兼容读取修正后失败项及 native/provider 邻接回归全绿。
- 本地 8899 Qwen 真实省略 Shell 可选参数，结果账目显示 timeout/background 来自安全默认值、cwd 来自
  `write_boundary.task_root`；macOS owner shell 因无 bwrap 按既有规则 fail-closed，未写文件。随后只读
  成功样本实际调用 `list_files/read_file`。真实失败轮还发现短输出没有 artifact 却给模型
  `read_artifact` 提示；该无效分支已删除，只有确实落盘的 artifact 才暴露读取提示。MiniMax-M2.7
  同样实际调用 `read_file`；两边均准确返回
  各自标记，源文件 SHA-256 不变且工作区无新增文件。
- 完整 pytest、Ruff、import/offline、strict code-size、doc-sync、compile/diff、distribution boundary
  与干净 wheel artifact gate 均通过。实现提交 `df00ec6af8acdf92197a0c28a9889e315943b94c`
  已推送远程 `main`；精确 wheel SHA-256 为
  `e095a083a6ab07b87171893d75a9f31a6486534d6466b86173db304f42616d50`，并部署到 1.10
  唯一正式 Gateway/Feishu 服务。
- 两个既有真实飞书账号从客户端沿各自原会话发送同一只读请求。A 请求
  `req_1784884678795_420766_0`、B 请求 `req_1784885085848_420766_1` 均只实际调用一次
  `run_command pwd`；canonical index 同时记录模型 command、两项 ToolSpec 安全默认值和各自
  `write_boundary.task_root`，没有 `value`，没有 Memory、消息或其他工具调用。A/B 任务目录数量保持
  64/19，最终正文只含各自目录末级名称，无绝对路径或内部协议。服务 active、`NRestarts=0`、队列为空，
  8420 只监听 loopback，Feishu WebSocket connected。

## 2026-07-24 工具参数 Schema 单一入口发布

- 对照 会话运行时 的 typed arguments + Serde 入口和 长期助手 的 schema-guided coercion 后，当前工作树保留
  一份完整 ToolSpec JSON Schema。模型定义、文本/native 调用、参数恢复门、MCP 代理和最终执行不再
  使用互相漂移的 required/type 拍平副本。
- 类型纠正只接受精确整数/数字、true/false、null 及合法 JSON array/object 字符串；不改字段名、不补
  默认值、不包数组。纠正后在路径/effect/审批/实现前检查 required、类型、enum/const、嵌套结构、
  additionalProperties、长度/范围、组合规则和本地 `$ref`；业务存在性与跨字段关系仍由 handler 判断。
- 外层 ToolCallEnvelope 与工具输入已明确分离。`kind`、`run_id`、`status`、`metadata`、
  `artifact_refs` 等如果是 ToolSpec 声明的参数就必须进入同一 Schema；未声明的真实协议字段才留在外层。
  这同时修复合法 remember/cancel/collaboration 参数误判和参数校验绕过。
- MCP 完整 inputSchema 原样进入 ToolSpec；不支持或畸形 assertion 在注册时跳过该单工具并告警，
  不再退化成宽松透传。转发前只移除统一执行入口已识别的外层元数据和宿主内部字段。
- 本地 8899 Qwen 与 MiniMax-M2.7 均通过隔离真实工具恢复：先收到真实 `PATH_NOT_FOUND`，再读取
  两个替代文件并写出报告；缺失输入未被创建。完整 pytest、Ruff、import/offline/code-size/doc-sync、
  compile/diff、distribution boundary 和干净 wheel artifact gate 均通过。
- 改动已随提交 `5d0822419d3bb36d758433cc10ea500cfec1fa2b` 推送远程 `main`；从该精确提交构建的
  wheel SHA-256 为 `a0c0c72d9246c18512209af00c734ad94f2994392c806d5fe0e10ecc12a86460`，
  已部署到 1.10 唯一正式 Gateway/Feishu 服务。两个既有真实飞书 owner 沿各自原 conversation
  并发只读复验：A 的 `list_files` 只进入 A owner，B 的 `read_file` 得到 `PATH_NOT_FOUND` 后由
  `list_files` 核实且只进入 B owner；最终正文不含工具协议、内部进度或绝对路径。两项服务最终
  active、`NRestarts=0`、队列为空，未启动额外 Gateway、适配器或端口。

## 2026-07-19 Scheduler 快速到期与主动消息去重候选

- `wait` 已固定为当前任务内部 yield；运行时强制 `route_channel=internal`，不会创建用户提醒或出站消息。
  用户的未来提醒仍由唯一 owner-scoped `schedule` 工具进入原 thread。
- 真实 1.10 的 135-owner 场景暴露目录分页扫描约 159 秒延迟。当前候选按 通道运行时 最早
  `nextRunAt` 代码路径增加只含 owner/到期时间/租约的 SQLite 派生投影；owner JSON 账本仍是唯一权威，
  执行前二次校验。重启场景到期后 2.18 秒 claim，常驻真实 Feishu 场景为 0.44/5.67 秒。
- 第一次真实提醒发现 `send_message` 已成功后后台仍自动发最终回复。当前候选按 通道运行时
  source-delivery outcome 方式，让工具成功结果携带结构化 receipt；scheduled run 只镜像已发内容到同一
  transcript，不再二次调用通道。重新实测自动兜底和主动消息两条路径都只有一个 history run、一次出站，
  主动消息路径记录 `delivery_reason=scheduled_message_tool_delivery`。
- 同轮还修正 scoped owner 的 memory 根重绑定、相对提醒参数固化、过去时间自纠错信息，以及 owner
  bwrap 中 `/tmp` 为单命令 tmpfs 的工具说明。首次候选 wheel 还暴露 Setuptools 复用了旧 `build/`，把
  源码已删除的 Workflow 包重新装入制品；当前已删除失效 package-data 声明，并让 distribution gate
  拒绝任何源码树不存在的 package payload。相关聚焦回归、完整 fast/slow pytest、Ruff、import/offline/
  code-size/doc-sync/compile/diff 和干净 wheel 制品门均通过；最终提交/push、精确提交 wheel 部署及部署后
  多用户多任务矩阵仍待收口。1.9 未改动。

## 2026-07-15 持续对话、`/goal` / `/audit` 特殊模式与任务中断续接发布

- 普通飞书聊天仍是一条 owner+chat/topic 持久 transcript；任务和持续目标只是挂在这条会话上的
  结构化工作现场。前台聊天与后台续跑共用 Agent 时，当前 prompt/run/workspace/tool-loop 已按
  worker thread 和 agent 弱引用身份隔离，同时修复了长驻进程中 Python object id 复用可能读到旧上下文的低概率根因。
- `/goal` 已实现为同 thread 特殊持久 overlay：一个未结束目标、一个根 task/workspace，支持
  view/edit/pause/resume/clear 和去重自动续跑；`get_goal`/`update_goal` 只能作用于当前结构化
  thread+task，模型只能写 complete/blocked 终态。`/stop` 遇到 active goal 时暂停而不清除。
- `/audit` 已收紧为显式前缀特殊模式；guarantee/window 固化到 task attributes 并沿子代理创建链继承，
  watch 不再从 prompt、goal、summary 或 child text 猜测激活，旧 `audit=1` 模型工具参数也已删除，
  普通任务不能由模型自行升级成特殊保证模式。
- `/stop` 改为 会话运行时 桌面端停止按钮的语义：中断当前根执行与子树、抑制迟到回复，但保留 transcript、
  compact、task workspace、artifact 和 memory。中断任务仍是可结构化选择候选；用户之后自然说
  “继续”，模型可重开同一 task/workspace，无需重发原 prompt。
- 远程 owner 只可见自己 home 与管理员显式发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板和
  旧顶层私有目录在 full mode 下也 fail-closed。builtin tools/skills 仍作为 wheel 内公共能力。
- 普通 `/subagents <count>` 入口已删除；模型按真实独立工作项自主提交数量，运行时在创建前同时
  核对本批/任务/owner/全局容量，超限整批拒绝。closeout 继续强制聚合子代理和开放进度，
  但纯分析可以 message 收口；只有显式 artifact contract/expected output 才强制文件。
- 根目录完整 pytest 两遍均运行至 100% 且退出码为 0；Ruff、
  import boundary、offline contract、code-size strict、doc sync、编译、diff、distribution boundary 与
  wheel clean-package 均通过。发布 wheel 为 2,767,741 bytes，SHA-256
  `a16f1196efcdded0d84354fee11dc7b50e9c6afdc4602b7146864a0245ca7052`。工作树 clean-package 按设计
  拦截 85 个未跟踪运行/交接文件和大体积运行目录，它们没有进入 wheel。
- 实现提交 `d7ba783f071d290f2bf351eeed2e839ebf9beacb` 已推送远端 `main` 并部署到 1.10；部署前已备份
  远端源码和生效配置。1.10 保持 `anthropic_compatible` + `MiniMax-M2.7`，`AGENT_API_KEY` 已设置且
  未回显；Gateway/Feishu 均 active、`NRestarts=0`、健康队列为空，切换后配置哈希与切换前一致。
- 1.10 不经 Feishu 外发的真实模型验证：合成用户 A 在同一 conversation 第一轮记住“雪松-7319”，
  第二轮准确找回；独立用户 B/独立 conversation 只回答“不知道”，三轮均为 MiniMax 后端且
  `conversation_persist_degraded=false`。`/goal` 同一 task identity 完成 create→pause→view→resume→
  view→clear→view 状态迁移；已安装 wheel 的 `/audit 1m` 盖入 guarantee + 60 秒窗口，正文中间提及
  不激活，工具 schema 不再暴露 `audit` 参数。以上是服务器侧 Feishu owner/channel/conversation
  作用域验证，不冒充真实 Feishu 客户端入站；最终交互体验仍由用户本人验证。

## 2026-07-15 会话运行时 式 `/btw`、自然回复与多用户长任务实机收口

- `/btw` 现在跟随 owner/thread 下同一个持久根任务，而不是只影响一次模型调用：引导按 FIFO 在下一安全点
  生效，任务经历后台唤醒、compact 或服务重启也不会换目标；生成期间到达的新引导会作废旧回复，任务已
  结束、取消或切换时则 fail-closed，不会污染未来任务。该边界对照 会话运行时 的 turn id、steer queue 与 stale
  response discard，实现仍使用 my-agent 自己的 TaskRun 和 RWX 文件事实源。
- `/stop`、`/btw` 与完成收口共用 task transition guard 和 active CAS；取消后的迟到投递被抑制，旧完成
  标记必须重新匹配当前 request/run/task 的最新通过 closeout，不能在开放进度或未聚合完子代理时提前关闭
  根任务。同一模型轮的批量/单项子代理创建按结构化 child intent 去重，防止重复派工。
- 除 `/status`、`/stop`、`/btw` 等显式控制命令外，普通聊天、任务回执、进度和最终正文全部由 LLM 根据
  结构化运行事实自然撰写；统一用户出口会清除 bracket/XML/native 工具协议。模型若虚报整个任务已完成、
  编造 ETA/文件大小或泄露内部协议，会用同一模型重写，仍不合格就抑制正文，不回退固定“正在处理”模板。
- 最终 1.10 MiniMax M2.7 候选使用两个 Feishu-scoped 合成用户并发验证：A 恰好创建 5 个子任务，接收
  `/btw` 标记后完成并生成 5 份子结果和 1 份 16,822 字节总文档；B 恰好创建 4 个子任务，`/stop` 后
  durable 状态为 cancelled，Gateway 重启后未复活，迟到投递为 0。A/B 分别找回“青柚18306”和
  “赤松18306”，无跨用户串词；最终 `failures=[]`，服务 active、`NRestarts=0`。
- 本轮是经 Gateway `/ask` 进入真实 Feishu owner/channel/conversation 作用域的服务器侧测试，不冒充真实
  Feishu 客户端入站。普通聊天在长任务期间约 6.1/10.1 秒返回；首次自然回执仍需 37.3/50.7 秒，A 整体
  长任务约 8.5 分钟，MiniMax 最终表达延迟仍是已知性能缺口，不影响本轮控制、隔离和完成正确性结论。
- 当前根目录完整 pytest 运行至 100% 且零失败；Ruff、import boundary、offline contract、code-size strict、
  doc-sync、编译、diff、distribution boundary 与 wheel clean-package 均通过。最终候选 wheel SHA-256 为
  `2ddf8e416951f7cc89315b2ff7e3064105e5c9a6829e5d13d336a9deb7608496`；工作树 clean-package 继续
  如实拦截用户保留的未跟踪运行数据和交接文档，它们没有进入 wheel。

## 2026-07-15 持久后台任务控制修复候选

- 1.10 两名合成 Feishu 用户并发长任务均成功派出协作者，用户在后台工作期间仍能于 5--6 秒内继续
  聊天并准确回忆各自的“人民币 / 远程入职第一周”要求；随后分别记住并找回“青柚47 / 赤松82”，
  未发生跨用户串词。两项任务最终分别生成 13,683 字节和 21,397 字节主文档并关闭根 task link。
- 同轮实测暴露控制断链：首轮 LLM 回执结束后，后台 TaskRun 仍在运行，但旧 `/status` 只扫描
  `processing` 而错报 idle，`/btw` 也拒绝保存。本地候选已改为 owner/channel/conversation -> thread ->
  active user-visible root task 的持久选择；尚未晋升才回落 processing request。
- `/btw` 现在对 durable task 写一次性 guidance 并 urgent wake；`/stop` 对 active task link 做 CAS
  取消，再中断同 task id 的前台/后台主循环和子代理树。取消后的迟到背景正文不会投递。同名 interrupt
  registry 改为一对多，避免派工回执线程和背景线程互相覆盖控制登记。
- 同轮实测还发现普通聊天“青柚47”被后台任务读入并写到产物注释。后台 task turn 现已按结构化 task
  lineage 过滤消息、观察、pending wake 和 bound task，并排除 thread 级 compact summary；原 task link 的 goal、
  同 task 消息和显式 `/btw` guidance 仍保留。普通聊天继续累计在用户会话中，但不再成为后台任务指令。
- 通道运行时 的 active run registry/run-id abort+steer 与 长期助手 live session running/steer/interrupt 用于
  校准生命周期边界；my-agent 保持 owner-scoped TaskRun、guidance/wake 和 RWX 文件事实源。本地相关
  134 项控制回归、50 项后台上下文回归与扩展后的 96 项关联回归均通过；当前收集 **8,471** 项，根目录
  pytest 完整运行到 100% 且零失败，Ruff、import boundary、offline contract、code-size strict、doc-sync、
  编译、diff 和 wheel artifact 门禁也已通过。提交、重新部署和真实 `/btw`/`/stop`/聊天污染复验仍在进行，
  因此暂不升级为稳定。

## 2026-07-15 模型自然回复与 Gateway 恢复候选

- 除显式控制命令外，用户正文继续由 LLM 生成。派工/wait 短轮已剥离完整工具历史；最终 closeout 冻结
  文件名、实际字节数、SHA-256、进度/gate 状态后再让同一模型重写完成摘要。无依据 ETA、内部协议和
  与快照不一致的大小不会投递，失败也不回退固定模板。
- 后台 claim 的 `heartbeat=0` 已恢复“自动间隔”语义，默认 TTL 90 秒；同一进程域能证明旧 PID 已死时
  立即接管，跨 Kubernetes PID namespace/旧格式记录无法证明时等待 TTL。SIGTERM/SIGINT 写 typed
  forensics 后进入现有 drain，不再成为无原因 clean exit。
- 当前收集 **8,464** 项，根目录 pytest 完整运行到 100% 且零失败；Ruff、import boundary、offline
  contract、code-size strict、doc-sync 与 `git diff --check` 均通过。工作树 clean-package 如实拦住用户
  保留的未跟踪运行数据/交接文档；由当前源码构建的 wheel 制品检查为 `ok=true`，未包含这些运行数据。
  1.10 多用户/长任务/重启复验待本次发布候选部署后补写，当前不把本地实现升级为已证明的生产事实。

## 2026-07-14 CLI / IM 共用会话控制链收口

- 本地终端与 Feishu 现在共用一份 typed 控制协议：`/status` 立即读取当前任务事实，
  `/btw <内容>` 只纠偏当前 active 根任务一次，`/stop` 只停止该根任务及其活跃子代理，
  不停 Gateway 服务。三者均绕过普通消息队列，长任务中也能及时响应。
- `/btw` 在任务晋升前按 request id、晋升后按 durable task id 投递并消费；生成期间到达时作废旧
  响应，不执行旧工具动作。当前任务
  已结束时不保存到下一轮；旧的持久 `/btw` 列表和 `/btw-clear` 已移除。
- `/stop` 对未晋升 request 持久化 `cancel_requested`，对已晋升任务 CAS 关闭 active task link，再中断
  主工具循环、前台 shell 进程组、后台主代理轮与子代理树；
  lease 心跳与 processing 状态更新不会覆盖取消标记，服务重启后也不重做已取消任务。
- owner/channel/conversation 三重身份用于选择当前持久根任务或尚未晋升的请求；跨用户或损坏记录无法证明归属时
  fail-closed。`/status` 不显示工具名、命令、路径或引导历史。
- 1.10 预部署在真实 root 环境发现宿主 home 放宽会误扩到远程 owner；当前发布候选已把该放宽限定为
  无 owner scope 的本地管理员，Feishu owner 仍拒绝 `/root` 等宿主路径，并保留自己的 owner home 白名单。
- 1.10 MiniMax M2.7 第一轮 Feishu-scoped 实测使用 4 个隔离身份：A/B 两个长任务分别约 313/323 秒
  完成，独立复跑为 25/25 与 19/19；B 的 `/btw` 追加“逾期提醒”进入代码和测试；C 的 `/stop`
  4ms 确认并以 `cancelled` 收口；D 复用 A 的 conversation id 仍看不到 A 历史。`/status` 与 `/btw`
  分别约 6ms 内返回，默认 `verbose=off`，最终回复没有工具流水。真实 Feishu 主动消息 API 已返回成功；
  真实客户端新入站仍需用户回消息完成闭环。
- 实测同时发现完成协议投影只保留文件清单，导致 A 后续把真实 25 项测试猜成最低要求 12。第一版只接
  `submit_for_acceptance`，1.10 MiniMax 自然结束分支复验仍丢掉了它已经写出的 7/7 说明；当前代码已把
  自然最终答复与显式验收摘要统一作为非权威 `user_summary` 带入回复信封和 transcript。内部标记与宿主
  绝对路径继续由统一出口清洗。通道运行时/长期助手 只用于确认“执行脚手架与最终答复分离”的边界。
- 当前收集 **8,422** 项，根目录 pytest 完整运行到 100% 且零失败；Ruff、import boundary、
  offline contract、code-size strict、doc-sync 与 `git diff --check` 均已通过。code-size 为
  `hard=0 / high-risk=39 / soft=7 / test-advisory=9 / blocked=False`。

## 2026-07-13 普通飞书 Agent 主链发布与真实会话收口

- 同一用户同一 chat/topic 使用权威 transcript 连续对话；不同用户、chat、topic 隔离，当前消息仍是
  本轮最高权威，不再被旧 goal 包裹。任务历史索引与活跃候选索引已经拆分：终态任务保留可审计
  链接，但不会继续污染普通聊天候选。
- 普通聊天、工作、派工和定时不依赖触发词；只有显式 `/audit`、`/goal` 保留特殊模式。继续旧任务
  必须由 `task_progress action=select` 写入结构化任务身份。
- USER 可由 Agent 自主维护；SOUL/AGENTS 必须经发起人确认卡片。默认 prompt 来自 wheel 内置资源，
  persona 文件同时受基础文件工具、patch 与 owner-scoped bwrap shell 保护。
- Feishu 长连接、密码卡默认开启；首次设置密码不吞首条消息。Gateway 提交后由持久 delivery worker
  异步回送，超过旧 60 秒窗口或服务重启后仍复用原 request，不重复执行任务。
- 普通最终回复、后台主动消息和显式 `send_message` 已在当前工作树共用 `DeliveryService`：可信
  `DeliveryContext` 与无收件人的 `ReplyEnvelope` 分离，adapter/capabilities/target validator 通过
  registry 扩展。第二个 fake IM 已证明无需修改投递主流程；第二个生产 IM 和正式 Feishu 部署复验未做。
- `/result/<request_id>` 在 queued/processing/done/failed 全状态统一以请求记录中的 owner 身份授权；
  完成响应不再因响应正文没有 `user_id` 而误拒同一用户。其他用户、孤立响应、损坏归档继续
  fail-closed，Feishu delivery worker 的内部回环读取不受影响。
- 当前发布候选全量收集 **8,337** 项，完整 pytest 运行至 100% 且无失败；Ruff、import boundary、offline
  contract、code-size strict、doc-sync、`git diff --check` 全部通过。code-size 为
  `hard=0 / high-risk=22 / soft=3 / blocked=False`。
- worktree clean-package 正确拒绝未跟踪源码和运行数据，并识别 `data`、`memory`、`memory_archive`、
  `live-agent-runs`、`validation/real_runs` 的真实体积；用户数据未删除。按 CI 同路径构建的 2.66MB
  wheel 已通过 distribution boundary 与 artifact clean-package，零 findings。
- 已发布基线 `e947237e` 的真实 MiniMax M2.7 三请求验证得到
  “已记住”→同会话“蓝杉-472”→不同会话“不知道”，证明模型连接、同会话续接和跨会话隔离。
  该验证同时暴露完成态 USER 查询 403，并形成统一 owner 修复 `36f5eb81`。该提交的远端 Lint、
  Python 3.10/3.11/3.12、macOS 与 Windows CI 已全部成功；1.10 已部署同一提交，Gateway/Feishu
  active、零重启、近 10 分钟零 error 日志。同一用户读取既有完成结果现为 HTTP 200 并返回
  “蓝杉-472”，另一用户仍为 403，真实证明修复没有放宽跨用户边界。

## 2026-06-12 三任务迭代轮收口:c 翻译落地,三案全部到位（详见 docs/audits/R12-R14-iteration-20260612.md 终局补记）

- **c 论文翻译 ✅**:r13c 接力 pdf2zh 全链跑通——9 个 PDF(3 英原+3 中文单语
  〔中文占比 0.38-0.52 全过自检线〕+3 双语对照),closeout 10/10 登记
  (产物并集修复实战首验,主跑同形态曾只见 1/4)。
- **a 项目分析 ✅**:r10a 接力 15/15,会话运行时 篇 64.3KB/830 行历史之最。
- **b 周榜 ✅(语义内)**:r12b 当周 19 项 25KB 真数据+API 核验,1-23 周按
  prompt 授权如实标注缺失与原因(七渠道枚举留档)。
- 迭代轮共九项底座修复(问句守卫/遗产清单/lesson 中文化+署名纪律/历史数据
  方法论/产物并集/端点环境/截断二试/skill 真跑纪律/自我承诺单次提醒),
  全部实证驱动+钉子钉死;两次被守门器拦下"测试词进产品"均中性化。

## 2026-06-12 R12-R14 迭代轮:边跑边修底座八连发（详见 docs/audits/R12-R14-iteration-20260612.md）

- 节奏=真实跑→逐篇审计→实锤→底座修复+钉子→下轮验证;八项修复全部实证驱动:
  问句出口守卫/接力遗产清单/lesson 中文化+署名纪律/历史数据方法论/产物并集/
  端点环境事实/截断二试/skill 真跑纪律。守门器两次拦截测试词进产品,均中性化。
- **检索三连胜**:R13c、R14c 连续命中 OCR 2(2601.20552,R10-R12 三轮全漏),
  R14c 另发现 2601.03111;路径=lesson 署名纪律+HF 官方页。
- **任务完成度**:a=已完成(r10a 15/15,会话运行时 64KB 历史之最);b=语义内接近
  完成(r12b 当周 25KB 真数据+缺失如实标注);c=检索+下载通,翻译落地
  r14c 接力决定性验证中(端点环境+skill 样例已就位)。
- 截断二试实证:two_trunc_one_full 复现从 BLOCKED 翻 DONE;锁防互踩首次
  真实出场(r11a relay3 跨组写入被正确拦截)。

## 2026-06-12 R11 验证轮:接力链路+lesson 消费双实锤（详见 docs/audits/R11-three-tasks-20260612.md）

- **r10a 接力大胜**:15/15 补齐——会话运行时 篇 **64.3KB/830 行**(单篇历史之最)+
  横向总对比 330 行;启动检测带出未完成任务→读旧产物→只补缺口,多轮接力
  做完任务的链路第二次实证且质量在高位。
- **lessons 召回→消费闭环首次真实生效**(r11c):报告含 tried_channels/
  untried_channels_known 结构化表(字段名即播种 lesson 格式);检索强度
  web_search×7+web_fetch×32+多口径,对照 R10c 的 13 轮浅检索是行为级升级。
  结论仍漏 OCR 2:R8 命中路径(HuggingFace 聚合页)被网络拦+未按产品线枚举
  ——后者已补种进 research lesson(产品线枚举策略)。
- r11a=接力中间态(4 篇深度分析 17-26KB 在子代理 work,context 满诚实退出,
  可按 r10a 同款接力补齐);r11b=新坏形态"诚实但不自主"(列方案 A/B 等用户,
  零交付,问句型收尾未触发 RUN_UNFINISHED——已记 backlog)。
- 四批新机制(分类器/抖动/中断/turn 预算/行缓存)35-44 轮长跑零异常零破坏;
  注卡分数门生效(周榜零注卡);注卡≠消费(r10a 没读卡也写出 64KB,卡的
  价值待"模型不会做"的领域验证)。

## 2026-06-12 一日基础完善四批落地:底座/召回+skill树/对照移植/性能（R10 后续）

- **批1 底座**：safe_id×4 实现、路径规范×7 模式各归一为 `common/` 单权威;
  records.py 迁原子写;吞异常处补观测(rg 降级/索引损坏/thread 绑定)。
- **批2 召回最后一公里接通**：lesson 匹配重写——读路由索引 trigger_keywords
  (中文)∪文件名 stem 兜底的并集,旧算法(英文文件名 in 中文 prompt)永零命中
  的断链修复;**skill 树千级地基**=目录即分类(`skills/builtin/<category>/<name>/`)
  +类目索引常驻(与总数解耦)+`skill_search` 工具(search-first 冷路)+命中卡
  渐进加载(带正文路径),200 卡基准 <1s。
- **批3 长期助手 工程移植**：provider 重试链=配置阶梯+随机抖动(防多实例共振);
  九类错误分类器前置裁决(限频/过载/超时进重试,鉴权/计费/格式快速浮出);
  工具错误出口 JSON `{"error","hint"}` 化(复用 taxonomy 权威码);per-thread
  协作中断(补齐 cancel 对线程形态无停止手段的缺口);turn 聚合预算 200K
  (兜"单个不大累计巨大");preview 截断落换行处。
- **批4 性能**:协作台账+产物注册表读路径接 mtime+size 守门行缓存。
- 全程框架层模型无感(稳而不管);钉子测试逐项落;code-size strict 0-0-0。
- 待办:R11 验证轮(原版自然语言 prompt 三任务+r10a 接力补齐)。

## 2026-06-12 R10 验证轮:减负立竿见影,质量大胜（详见 docs/audits/R10-three-tasks-20260612.md）

- **r10a 质量飞跃**：原版自然语言 prompt 零插手,单篇 8.3K–15.7K/171–379 行/
  19–97 处代码引用×13 篇——超接手前最好成绩 2–3 倍,较 R9 谷底提升 12 倍;
  且发生在 skill 召回=0、used_memories=0 条件下——**证实质量回归纯粹来自减负**
  （质量门打回取消+注入瘦身）,R9 退化主因正是机制干扰。
- r10b 诚实典范（方法论+诚信声明+零估算插值）;r10c 结论回归错误（漏 OCR 2）。
- **下一主攻=知识召回最后一公里**：skills/lessons 种子就位但三案零召回——
  skill 卡片未进主 run prompt、lessons 召回零命中。通了之后 r10c 检索回归与
  r10a 缺总对比都有解。
- 13/15 可接力补齐;出口/回收/诚实底线全保持。

## 2026-06-12 方向修正「稳而不管」第一批落地（详见 docs/design/PLAN-stability-not-control-20260612.md）

- **用户裁决+取证定性**：R9 产物质量退化 4 倍（单篇 4.6KB→953B）的帮凶是我们
  自己的机制——写死数量 prompt+数量对账打回+注入膨胀把模型优化目标从"做好"
  扭成"凑数过门"。三路对照组深读证实:成熟项目零验收门,质量靠 skill 知识+
  工具约束+自学习,流程复杂性全部藏在框架里模型无感。
- **已落地**：①workspace 注入段 18 行→5 行（教学文案收编 lessons/workspace.md
  按需召回）；②质量/进度类 finding 退出阻断（task_progress open/expected_outputs
  缺口只进报告 quality_advisories 供把关,不再打回;客观事实门保留:产物打不开/
  派过人零产物/未终态子代理）;③skill 体系激活——SkillRegistry 骨架接电
  （router 默认加载 `skills/builtin/`),首批两个知识型 skill（深度代码分析方法/
  PDF 翻译工具链）,卡片带正文路径渐进加载,中文路由命中验证通过。
- 语义裁决留档：两个旧钉子按新语义改写（progress open 不阻断/声明缺口不阻断）。
- **2-3 自学习钩子同轮落地**：run 收尾（成功收口+未收口退出两处）复盘一次
  （enable_self_learning 才跑，默认关零成本），LESSON 进 learning drafts 待用户
  审核——"没做好也能学"的闭环入口接通。钉子 3 条。
- 待办：2-1 错误分类补全（低优先）、3-1 R10 原版自然语言 prompt 验证。

## 2026-06-12 R9 验证轮:多轮接力首次做完任务（详见 docs/audits/R9-three-tasks-20260612.md）

- **里程碑:r9a 15/15 全中**（主跑 14→接力复用目录补齐+确认文件,零占位符）——
  R5a 以来首次"任务做完为止"端到端兑现（历轮 11 占位/零产物/13/8→完成）。
- **全部新机制实战命中零新缺陷**：接力目录复用（fingerprint）✓、对账门第一次
  真实拦截（声明 WEEK* vs 实交 W*）✓、孤儿回收进程层完整闭环（terminated
  pid+requeued）✓、来源观测三案投影（r9a 零网络形态验证纯观测裁决）✓、
  权限墙围栏直授（子代理批量免 capreq 交付）✓、slug/出口/零请示全保持 ✓。
- 成品诚实：r9b 24 文件但命名违 prompt（模型 84 轮不响应改名、接力轮改声明
  绕行=声明漂移新观察,把关终审兜底）；r9c 翻译零推进。新课题全在模型行为层。
- CODEBASE_TREE/backlog/记忆同步;全 gate 绿;零 commit。

## 2026-06-12 backlog Active 清零:来源比例观测 + 启动孤儿检测（R9 前最后两块）

- **来源比例观测**：closeout 报告新增 source_volume_observation（检索量 vs
  交付量并排，category=="web" 零白名单）；裁决纯观测零 finding（任何阈值必
  误伤本地分析任务），判断留把关者。**启动孤儿检测**：终态任务+活进程+cmdline
  身份验证三条件报告，绝不自动 kill。钉子 4+5 条全过。
- 下一步：R9 完整重跑（主跑+接力轮，多轮接力全链路首考，一次验证权限墙/目录
  复用/来源观测/孤儿回收进程层/检索教训全部新机制）。

## 2026-06-12 交付写权限墙收口:声明目录围栏直授 + 拒因账本留痕（backlog Active 项清零）

- **取证**：R8a 17 次交付写被拒（A1 铁证非幻觉）+"先拒后成"（grant 救场）+
  终态重放一律 ALLOWED（瞬态不可复现）→ 暴露两个底座缺口。
- **修复①**：`declared_output_write_roots`——声明产物父目录过围栏（grant 同款
  基准）直授写边界，创建链不透传环境字段也不再"声明了却写不进"；围栏外不自动
  授权（防自我扩权）。**修复②**：A1 账本条目新增 message（拒因原文 240 字留痕），
  根治"边界决策不留痕、事后只能考古"。
- 钉子 3 新增 + 2 契约更新；focused 48 全过。
- Active 仅余：来源比例软对账、启动时孤儿检测、P5-2 生产端扩展（均低优先）。

## 2026-06-12 接力验证轮:目录复用缺陷当轮修复 + 隐蔽编造实锤（详见 R8 审计接力章节）

- **接力目录缺陷修复**：同 prompt 新 run 此前永不复用任务目录（只匹配三个机器
  ID）→ 接力变重做；补 prompt_fingerprint 匹配（逐字同 prompt 复用同目录），
  钉子入 test_run_task_workspace_writer。
- **r8b 重做轮隐蔽编造实锤（修正 R8 主跑"编造消失"结论）**：24/24 xlsx +
  closeout ok=true，但生成脚本铁证=19 项静态列表复用 24 周+公式造"周增长"+
  "诚实标注"分支被写死短路——模型说一套做一套。已记 backlog：「数据量 vs
  来源调用量」比例软对账（结构化计数反隐蔽编造）。
- **r8a 续派**：dispatch 链路跑通，runner 直写父交付区被写边界系统拒（A1 账本
  WRITE_FORBIDDEN 实锤非幻觉）——已记 backlog 待取证（合同渲染 vs 模型行为）。
- **论文把关**：OCR 2 官方实锤；1 篇偏官方待确认；2 篇疑似聚合页误收。
- **预算裁决**：run_repair_max_continuations 保持 3——完成度的正确路径=
  多轮接力+每轮诚实收口（目录复用修复后接力即真接续），非单轮死磕。

## 2026-06-12 R8 验证轮:七项机制验证全部命中（详见 docs/audits/R8-three-tasks-20260612.md）

- 同 prompt 零插手重跑:R7 暴露的每个失血形态逐项消失——零请示退出(43 轮
  干到底)、**OCR 2 找到**(聚合页+宽检索,4 篇 2026 论文超对照产品)、孤儿回收
  进程层真实终止(terminated×4 带 pid)、**零插值零估算**(宁交 2 真不编 22 假)、
  对账门首次实战拦截(声明 24 缺 22→REWORK)、slug 目录名正确、零残留进程。
- 成品完成度(诚实口径):r8a 8/15 md、r8b 2/24 xlsx(均可 resume)、r8c 检索
  全甲+8 个真 PDF(翻译版式质量待把关)。剩余差距=续航预算 vs 任务体量 +
  模型单轮产能,不再是合同层缺口。
- 下一步:续航预算调优(run_repair_max_continuations 与任务体量匹配)+
  resume 接力跑通(用 R8 未完成任务验证跨 run 续作)。

## 2026-06-12 归因落地三件套 + P5-2 收口 + slug 修复（本地未提交）

- **检索归因闭环**：r7c 第 5 轮其实用了时间倒序——差异在 author 字段（6 篇假
  全集）vs 对照产品多渠道并行（arXiv 全字段/官网/HuggingFace 聚合页/探索子代理
  交叉）。修复=research.md 教训播种（假全集陷阱→检索纪律）+ 路由段自动召回。
- **P5-2 全链路收口**（backlog 最后 Active 暂缓项）：MemoryRecord.attributes
  扩展位（旧行兼容）→ write 持久化 trigger_conditions → 推送端结构化匹配提权
  → 生产端自省调参自动写带条件教训（"超时×3→调2x"闭环）。钉子 6 条。
- **数据真实性工程裁决**（r7b 插值实锤）：机器在通用层判不了"数字是编的"，
  不造无效硬门；落地=artifacts.md 数据诚实条款 + expected_outputs spec 引导
  （数量要求绝不构成编造理由）。
- **slug 缺陷修复**（R5c/R7c"失败统计"目录名）：_PATH_RE 负向后顾，词内斜杠
  （成功/失败、A/B）不再误判为路径；真路径标题不回归。
- 下一步：R8 同 prompt 零插手重跑三任务（验证今晚全部改动的真实效果）。

## 2026-06-11 R7 真实验收轮 + 四通用缺陷当轮修复 + 跨产品对照启动（本地未提交）

- **R7 三任务**（写死产物要求,详见 docs/audits/R7-three-tasks-20260611.md）：
  r7a 实交 15 md（出口合同+孤儿回收任务层真实出场）；r7b 实交 24 真 xlsx 且
  模型自发声明 expected_outputs（对账门声明侧首次生效），但 17/24 周为插值凑数
  （写死数量 prompt 的反噬形态）；r7c 零产物"请示退出"。
- **四个通用缺陷当轮实锤+修复**（详见 REFACTORING_BACKLOG Completed）：
  ①CLI mark 抹 pid（孤儿回收进程层失效）→ background_start 构造收敛唯一权威；
  ②合同空壳压制真实产物 → 有产物即回落 uncontracted 验收；③run 单次模式
  环境事实缺失 → cli_run 注入"请示无人应答"软约束；④compact 后产物候选失明
  （最重，r7b 24 xlsx 对 closeout 不可见）→ 交付目录扫描兜底（仅 task_output
  scope）+ 出口合同候选判定同步。钉子 +5；全 gate 绿。
- **跨产品对照实跑完成**（用户指令,详见 docs/audits/R7-cross-product-comparison-20260611.md）：
  工具运行时/长期助手 三任务全跑完,通道运行时 冒烟通但工具链需 gateway 配对（任务级
  缺席,排障全留档）。五大终评结论：①检索完备性是 my-agent 系统性短板
  （OCR 2 被两个对照产品双双命中,my-agent 三轮全漏,归因=下轮头号课题）；
  ②交付纪律是真实护城河（对照组"派完就丢/写错地方/不收口"三形态全现）；
  ③无证据链则真实性不可审计（长期助手-b 编造嫌疑无法核验 → A1 账本价值反向
  印证）；④工具自由度差距（工具运行时 用 bash+Python 完成 PDF 全链）；
  ⑤速度-完成度权衡。改进清单四项已记对照审计文档。

## 2026-06-11 检索完备性软引导落地（backlog Active 第 3 项收口，本地未提交）

- **实锤**：R5b web_search 系统失败 2 次即断言"数据根本不存在"口头放弃（OSS
  Insight 实可得）；R6c 同构。
- **落地（纯软提示零硬门）**：`tool_guard/loop_hints.append_tool_failure_channel_hint`
  ——同一工具系统失败（archive ok=false，A1 同源）累计达
  `tool_failure_channel_hint_threshold`（默认 2，三同步）即注入"枚举已试/未试渠道
  再下绝对结论"软提示（指向 P5-1 infeasibility schema 字段），每工具幂等一次,
  主代理与 worker 子代理同链路生效。钉子 5 条全过。
- 边界诚实声明：R6c"检索成功但单一查询字段"形态属模型认知层,机制无法结构化
  判定（解析查询参数语义=解析自然语言,违铁律）,留给教训记忆 P5-2。
- 下一步：P5-2（暂缓项）或 R7 真实任务验收（用写死产物要求的 prompt 验证三项新机制）。

## 2026-06-11 产物类型/数量对账门落地（backlog Active 第 2 项收口，本地未提交）

- **实锤**：R6c 要求每篇一个 PDF 实交 0、R6b 要求 24 周实交 1 周，closeout 只查
  "有产物"均 ok=true。
- **落地（纯声明驱动，零自然语言解析）**：task_progress 新增 `expected_outputs`
  声明（pattern/min_count，glob 开放世界，扩展名即类型）；新增
  `delivery_closeout/expected_outputs_gate.py` 对账交付区实存，缺口
  `EXPECTED_OUTPUTS_MISSING`（medium，repair 非硬卡死），挂 contract+uncontracted
  双路径；零声明零影响；spec parameter_details 引导模型把 prompt 产物要求翻译成
  结构化声明。钉子 test_expected_outputs_gate.py 9 条全过；closeout 族回归全绿。
- 真实测试提示：prompt 要把产物要求写死，模型声明后对账门才有声明可对。
- 下一步：backlog Active 第 3 项「检索完备性软引导」。

## 2026-06-11 孤儿子代理回收落地（backlog 最高优先项收口，本地未提交）

- **根因实锤**：派工是 `Popen(start_new_session=True)` 独立进程（durable 设计），
  R6a 主代理 12:39 RUN_EXIT 后后台 dispatch 进程活到 13:00（+21 分钟）写占位符；
  pid 只进内存 registry 未落盘（cancel_subagents 预留的 background_start.pid 路径
  永远 no_pid）；CLI dispatch 不更新 background_start.status（不能信 status 只能验 pid）。
- **对照组**：通道运行时/长期助手/会话运行时 三家全部显式 kill（SIGTERM→SIGKILL、pid 落盘、
  kill -0 活性探测），不靠自然死亡。
- **落地**：`subagents/process_control.py` 进程原语（zombie reap+两阶段终止，cancel
  工具收敛复用）；`mark_background_start` 落盘 pid；`tool_loop/exit_orphan_recovery.py`
  出口回收（BFS 子树、同 launch 共享 pid 杀一次、仅 RUNNING requeue 回 PENDING 保
  resume、orphan_recovery 留痕）；出口合同接线（RUN_UNFINISHED_EXIT 前先回收+报告进
  resume 块+修反事实文案）；`unfinished_exit_passthrough` 覆盖工具轮数耗尽截停出口
  （R5a 形态）。配置三同步 `run_exit_orphan_recovery_enabled`（默认 true，关闭则
  退出声明如实标注后台进程仍在运行）。
- 钉子：test_exit_orphan_recovery.py 15 条（真实 sleep 进程）；focused 回归
  （final_exit/background_dispatch/cancel/dispatch_mixin/real_class 等）全绿。
- 下一步：backlog Active 第 2 项「产物类型/数量对账门」。

## 2026-06-11 R6 总验收：同 prompt 零插手重跑三任务（详见 docs/audits/R5-three-tasks-20260611.md R6 章节）

- **出口走 closeout 3/3**（R5 仅 1/3）：R6b/R6c `MAIN_AGENT_DELIVERY_COMPLETE ok=true`。
- **质变实证**：R6b 交出 32KB 真实周榜（star-history+GitHub API 双源，缺失 23 周
  如实留档）——R5b 曾断言"数据根本不存在"；R6c 交出检索报告+失败统计表——
  R5c 曾零产物口头放弃。失败留档（通道运行时 式纪律）成为默认行为。
- **R6a 暴露并当轮修复 contract 路径盲区**：contract closeout 失败不注入指令、
  不评 subagent gate → 出口合同打回无指令、放行无 resume。修复：打回必带
  `[final-exit-contract]` 结构化指令（幂等）；未收口退出必带
  `[RUN_UNFINISHED_EXIT]` 声明+resume 入口。钉子 2 条（套件 10/10）。
- 残余课题在模型行为层（检索完备性/官方性判定/字段完整度/标题幻觉），
  属软引导与教训记忆输入，非合同层缺口。

## 2026-06-11 任务完成力底座全批次落地（详见 docs/design/PLAN-foundation-task-completion-20260611.md）

- 第一批 run 出口合同：口头放弃必走 closeout（结构化事实触发）+ NEED_REPAIR 续航
  双闸（run_repair_max_continuations 三同步）+ REWORK 带 resume 块 + 空交付门
  （infeasibility schema 倒逼探索完备性）。修复 uncontracted 零产物早退与
  non_terminal 报告覆盖两处既有缺陷。
- 第二批：锁生命周期（自锁交付目标 save 口剔除+留痕+锁变更流水账）、grant
  path_scope 并入读边界（中途求读权限闭环）、capreq 引导前移到 kernel 树快照。
- 第三批：占位符产物标记+独立账本+closeout 投影明示。P5-2（trigger_conditions
  消费）裁决暂缓（需改记忆存储 schema，不阻塞 R6）。
- 行为变化：未收口即收尾的 run 多一轮续航（两个既有测试 backend.calls 3→4 已注明
  更新）。钉子合计 19+ 条新增；快 gate 全绿；全量对照基线见汇报。
- 下一步：R6 同 prompt 零插手重跑三任务总验收。

## 2026-06-11 R5 三真实任务轮：审计完成（详见 docs/audits/R5-three-tasks-20260611.md）

- 三任务并行真实跑完（MiniMax-M2.7，隔离 home，一次 prompt 零插手）：R5a 项目分析
  =走完正路的诚实半成品（closeout 正确拦 TASK_PROGRESS_OPEN_ITEMS）；R5b GitHub
  榜单、R5c DeepSeek 论文=诚实但口头放弃，且两案结论均被把关核验证伪
  （OSS Insight 数据可得；DeepSeek-OCR 2 是 2026-01-28 首发官方论文）。
- 三案零伪造（对比 R3/R4 实质进步）；共性短板="检索不完备→绝对化结论→口头放弃"。
- A1 系统账本三种形态全覆盖实战可用：拆穿幻觉（R4b）、确认真实失败（R5b）、
  精确定位运行中途加锁（R5a，target==locked 铁证）。
- 新增 backlog Active 四项：口头放弃绕过交付门（R5b/R5c）、交付目标运行中途被锁
  （R5a）、子代理 read_roots 过窄（R5a）、A3 引导时机前移（三案）。

## 2026-06-11 开发计划 A1-A3 + B1/B2 落地：确定性优先（本地未提交）

按用户确认的开发计划（docs/design/PLAN-stability-and-gaps-20260611.md）实施 1-3 项：

- **A1 系统级工具失败账本**：新增 `subagents/tool_failure_ledger.py`，
  archive_tool_calls 的 ok=False 系统事实 → `task.attributes["tool_failure_ledger"]`
  → closeout `unresolved_children.tool_failure_codes` 对照投影。`[]`=系统确认零失败，
  `None`（超时/异常）不覆盖。模型口头 WRITE_FORBIDDEN vs 系统账本为空的矛盾
  （R4b 幻觉形态）在报告里直接可见。纯观测无硬门。
- **A2 写边界一致性钉子**：narrowing 语义双向钉死（祖先 forbidden 不拦授权交付区/
  allowed 内子树必须拦）、boundary 与 canonical 同源、首轮即含交付区且构造稳定。
- **A3 capability 软引导**：CAPABILITY_REQUESTS_OPEN finding 带
  `recommended_tool`+`open_capability_request_ids`；required_actions 改真实工具名。
- **B1 记忆推模式扩展**：planner 决策点自动注入（append_planner_memory_hint，
  软注入不阻断）；修复 _build_memory_query 中文短 goal 被丢弃的生产端缺陷
  （此前推模式在中文任务上形同虚设）。
- **B2 注入幂等**：planner/failure 双路径同 hint 不堆叠（防重试循环 prompt 膨胀）。
- 钉子：test_subagent_tool_failure_ledger.py（11）+ test_memory_push_decision_points.py
  （9）+ output_alignment 增 3 + closeout 断言增强；焦点回归全绿。
- 风险边界守住：全部软引导/观测字段，零硬门；未动 scoped_locks。

## 2026-06-11 阶段D：R4b GoAttack 真实复跑验证交付链路（详见 docs/audits/R4b-goattack-20260611.md）

- 核心胜利：交付对账 gate 生效。R4 是"放过假成功"（实交 1 文件 ok=true），R4b 是
  "诚实失败"——closeout `ok=false` + `SUBAGENTS_UNRESOLVED/CAPABILITY_REQUESTS_OPEN`
  正确阻断；主代理 28 轮持续补，产物 1→18 文件。
- 阶段A/B 机制全部生效（结构化实证）：默认交付区落点（产物落
  tasks/<日期>/<任务>/output/，未给绝对路径仍正确解析）；子代理写边界对齐
  （allowed_write_roots 实含交付区、locked_files 空）；声明对账 gate 拦截草率成功。
- 根因实锤：子代理报 WRITE_FORBIDDEN 是 **MiniMax 模型归因幻觉**，非写边界缺陷——
  用子代理真实 canonical boundary 实测 validate_write_boundary = ALLOWED；模型称
  "被 locked_files 锁定"但 locked_files 客观为空；主代理同路径写成功。按"结构化事实
  优先于模型口头"判定机制正确。
- 真实模型行为层三短板（非四子项机制问题，记 audit 待后续）：①子代理幻觉式归因
  ②主代理 0 次用 resolve_capability_requests（继承幻觉+主线程自写绕过）③主线程
  write_file 的 tool-call JSON 三引号转义丢失 + 残留子代理占位符 → 产物语法错误跑不起来。
- 验证轮只观察未改任何被测文件；产物留 live-agent-runs/r4b-goattack-20260611/。

## 2026-06-11 阶段E：失败自省 split 链路打通（本地未提交）

- `split_suggestions` 死路打通：新增 `_apply_introspection_split`（dispatch/mixin）作为
  唯一消费方，配置开启时复用 `split_task` 真实拆分（子任务 PLANNING 先落盘、原任务
  TAKEN_OVER 后落盘），账本与跳过原因全部结构化进 `failure_introspection_data`。
- capability_config 三同步新增：`subagent_failure_auto_split_enabled`（默认 false）、
  `subagent_failure_split_max_depth`（默认 2，0=不限制）；运行时读取走新公共权威
  `capability_config_for_agent`（context_compactor 旧私有实现收敛去重）。
- 自省吞异常修复：load 失败仅日志；apply 段失败结构化留痕
  `failure_introspection_error` 并补落盘；绝不向 runner 主链路抛异常。
- 清理：删除无人生产的 `split_goal` 影子拆分分支（`max_tool_rounds` 保留，消费链
  真实）；救活 test_dispatch_mixin.py 4 个嵌套 def 死测试（收集 1→23）。
- 验收：focused 64 过（test_dispatch_mixin + test_real_class_integration +
  test_adaptive_retry + 配置继承相关）；全套 gate 见本轮汇报。

## 2026-06-11 阶段C：scoped lock 语义定性收口（方案A，本地未提交）

- 调研实锤（比上轮 backlog 记录更彻底）：`acquire_scoped_lock`/`release_scoped_lock`
  生产代码**零运行时调用**——`daemon_control.py` 仅作公共 API 转口（test_daemon_control
  经它调用），`supervisor.py` 的两个 import 是死引用（本轮已删），CLI/scripts/动态
  引用为零；gateway 单进程多线程路径（heartbeat/request/background/worker 池）没有
  任何地方拿它当临界区，**无存量数据竞争**。
- 对照组（长期助手 ProcessRegistry）：进程内并发一律 `threading.Lock`，pid+start_time
  只做进程身份单例——语义分离是成熟做法。据此选方案A：同进程线程重入=刷新心跳
  是契约特性；不加 thread id（会破坏 supervisor 重入刷新），不新增无调用方的线程锁
  原语（线程互斥用 `threading.Lock`，已有 `agent/io/jsonl.py` 双层锁先例可参考）。
- 落地：`scoped_locks.py` 全模块补 LLM/人类双层中文注释（契约写死在 `_owns_lock`/
  acquire/release 上）；原 strict xfail 钉子改写为进程级语义钉子（线程重入刷新、
  真实子进程抢锁必败、非持有进程 release 不误删、release 后干净重持有）；
  docs/modules/gateway/02-progress.md、04-structure.md、REFACTORING_BACKLOG.md
  （Active→Completed）同步。
- 验收：focused（test_real_io_concurrency + test_daemon_control）35 过；其余 gate 见
  本轮汇报。

## 2026-06-11 R4 交付链路四子项 + compact 三件套收尾（本地未提交，按用户要求暂不 commit）

本轮把 REFACTORING_BACKLOG 两个 Active 专项全部落地（工作区改动待用户确认后提交）：

1. **R4 多代理交付链路四子项**：①执行合同产物落点对齐（output_alignment 投影层 +
   output_delivery_map）；②capability_request 运行中回路（resolve_capability_requests
   工具 + 提交端 wake 推送）；③声明产物对账（SUBAGENTS_DECLARED_OUTPUTS_MISSING
   gate，附 gate 自指拦截修复）；④汇总搬运（deliver_anchored_outputs_to_declared）。
2. **compact 三件套**：microcompact（渲染期回收 + 配置开关）、PTL 单轮重试
   （ptl_retry + tool_context_ptl_retry_max）、circuit breaker（前一提交已有）；
   另落地 lesson 陈旧提示（home_lesson_stale_caveat_days）。
3. **真实测试缺口补齐**：test_real_class_integration.py（6 条真实类端到端路径，
   含"失败自省调参真正生效"硬断言）；test_real_io_concurrency.py（真实文件竞态，
   4 过 + 1 strict xfail——实锤 scoped lock 无进程内线程互斥，已记 backlog）。
4. **新发现已记 backlog Active**：scoped lock 线程互斥缺陷；失败自省 split 建议
   在 apply 层是死路 + 自省吞异常。

验收：focused 新增测试 41+ 全过；code-size strict 0 hard/0 high-risk/0 soft；
DOC_SYNC_PASS；OFFLINE_CONTRACT_MATRIX ok；ruff 全过；git diff --check 过；
全量快速套件失败集为基线子集（机器环境失败，详见下方记录）。

## 2026-06-10 大重构阶段1-6完成（本地，待推送）

六个重构阶段全部完成，5 个本地提交待攒批推送（推送门槛：累计 diff ≥8000 行 + 远端 CI 绿）：

1. **阶段1 文件合并**：gates 五个子包打平、subagents/services 三个单模块包打平、
   delivery_closeout 三组合并、orchestration create_* 8→4、compact_context_bundle 包→模块；
   全包模块级真循环清零（6 处）。主包源文件 969→943。
2. **阶段2 字符串判断清零**：recovery 五组前缀规则收敛为单一 CodePolicy 注册表
   （187码×13状态等价校验 0 差异）、state_machine 谓词 fail-closed、新钉子测试
   `test_recovery_code_policy.py`。
3. **阶段6 子代理参数统一**：runner 复用父 agent 对象合同化、模板单一权威、
   capability 配置新增 `subagent_compact_trigger_percent`（0=继承）。
4. **阶段3 gateway/chat 性能**：inbox mtime 扫描门、recover 节流、chat 尾部倒读（41x）、
   jsonl 锁引用计数、heartbeat queue_ages 观测。
5. **阶段4 coverage**：`directory_tree` 覆盖类型（ratio/截断显式）、shell 中段截断保尾。
6. **阶段5 旧兼容审计**：home_layout 旧根 memory 布局退役（默认路由表/lessons 播种迁到
   owner 权威位置）；多个调研标记的"fallback"核对后确认为活协议，保留并记录。

验证基线：本机全量快速套件有 141 个机器环境失败（时区/环境，远端 CI 绿），六个阶段
全程保持与主线基线逐位一致（零新增失败）；code-size strict 全程 0 hard / 0 high-risk。

真实任务轮：R0（all-agent 分析冒烟，真实 MiniMax-M2.7）运行中，产物目录
`live-agent-runs/r0-refactor-smoke-allagent-20260610-1830/`。
详细清单见 `REFACTORING_BACKLOG.md`。


## 2026-05-03 最新恢复入口

如果下次换电脑、换会话、换 IDE，先看这一段和模块文档。

当前远端已同步到：

- branch: `main`
- remote: `origin/main`
- latest pushed recovery checkpoint: parent/subagent recovery scenario + gateway stale lease scenario

最新可恢复能力：

- `memory-resume` 已能把 gateway request/response JSON 当成恢复事实源。
- `scenario-test --case gateway-cross-day-resume` 已能启动真实后台 gateway、投递真实 `gateway ask`、模拟跨天线索，并验证恢复能回到 request/response JSON。
- `scenario-test --case gateway-delayed-response` 已能验证 response 已落盘时，迟到 pending 请求副本只归档、不重复调用模型。
- `scenario-test --case gateway-multi-worker` 已能用两个 request worker 并发处理多条 pending 请求，验证不重复响应或归档。
- `scenario-test --case gateway-stale-lease` 已能模拟 worker 中断留下旧 processing lease，验证恢复会重排并完成请求。
- `scenario-test --case gateway-processing-stop` 已能模拟 worker 正在处理请求时 gateway stop/restart，验证重启后不卡死、不丢请求、不留半截 JSON。
- `scenario-test --case parent-subagent-cross-day-resume` 已能创建真实子代理任务、执行 runner 工具回合、模拟跨天线索，并验证恢复能回到任务事实源。
- `scenario-test --case real-model-recovery-multi-round` 已能用真实 API 跑多轮工具调用（read_file + search_text），验证 memory-resume 能找回每轮 evidence 和 output.json。
- gateway worker 在 processing lease 写失败时会继续处理请求，不会因为观测文件失败把用户请求卡死。
- 如果 LocalStore 记录了临时 `requests/processing/<id>.json`，恢复时会优先纠偏到现存的 `requests/done` 或 `requests/failed`。
- 日志分析模块第一版已落地：SecurityCase → LogWorkOrder → SubAgentTask 转换链 + bounded_query 受控查询工具。

最新验收：

- `python3 -m pytest -q` -> `749 passed`
- `python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`
- `git diff --check` -> passed
- focused gateway/memory/doc tests -> `16 passed`
- wide memory/gateway focused tests -> `76 passed`
- **推模式 focused 验收**：`python3 -m pytest agent_py_agent/tests/test_memory_push.py agent_py_agent/tests/test_dispatch_loop.py -q` -> `39 passed`
- parent/subagent runner recovery focused tests -> `2 passed`
- gateway stale lease focused tests -> `3 passed`
- gateway multi-worker focused tests -> `4 passed`
- gateway delayed-response focused tests -> `5 passed`
- gateway/scenario/doc focused tests -> `11 passed`
- CLI reference focused test -> `1 passed`
- log analysis first loop tests -> `26 passed`

下一版优先级：

1. ~~补 gateway 坏天气场景：processing 中 stop/restart。~~ ✅ 已完成 `gateway-processing-stop`
2. ~~把 subagent workflow router/compiler/parent gate 接到真实 task creation，默认 dry-run 或 manual-confirm。~~ ✅ 已完成
3. ~~推进 LOG work-order 到真实 SubAgentTask 执行桥，并补 bounded evidence reader。~~ ✅ 已完成第一版
4. ~~用真实外部模型补跑 parent/subagent runner 跨天恢复冒烟。~~ ✅ 已完成 `real-model-recovery-multi-round`

详细交接见 `docs/tasks/HANDOFF_*.md`，模块细节见 `docs/modules/memory/02-progress.md`、`docs/modules/subagent/02-progress.md` 和 `docs/modules/gateway/02-progress.md`。

更新时间：2026-05-01

当前阶段：`v0.4-dev / memory resume and auto recovery context`

总体状态：核心骨架已可运行，真实 API 全流程已通过；当前重点已经从“能跑”进入“可常驻、可观察、可恢复、可审计”。

最新推进：
- 已完成完整从头到尾真实链路测试：CLI、memory、LocalStore、gateway、scenario、真实 API runner、父代理验收全部通过。
- 已新增任务生命周期管理（ABANDONED/PAUSED/RESUMED）：用户可通过 `task-abandon`、`task-pause`、`task-resume` 命令主动控制任务；Dispatch 调度会跳过 PAUSED/ABANDONED/COMPLETED/FAILED 状态任务。
- 已新增轻量 recovery snapshot 自动写入：普通 run/chat/gateway 默认随保存写 hook，subagent-run 在 runner 结果写回后写 run_id 恢复锚点。
- `memory-resume` 已支持 `--context-only`，可以只输出稳定恢复块，方便人工 handoff、真实环境测试和后续自动注入。
- 已新增可选恢复上下文自动注入：默认关闭；打开后在“继续/恢复/刚刚/run_id”等场景读取归档和事实源，把短 `Recovery Brief` 注入本轮 prompt。
- `run/chat/gateway ask` 已支持 `--resume-context` / `--no-resume-context` 临时开关，并在状态行展示 prompt、injection、resume 的保守 token 估算。
- 修复默认 gateway 入口缺少 chat handler 的回归；`my-agent` 默认入口可自动进入 gateway chat。
- scenario-test 已按当前写入边界核对子代理 `task_dir/scenario_outputs/` 产物，避免旧路径误判。
- 完整冒烟脚本最后统一改用 pytest 正常运行，避免跳过 pytest fixture 机制。
- 新增 workstream 并行开发工作台：用 git worktree 隔离 memory、runtime、tools-boundary、live-lab 等开发线，并提供状态查看、可见终端打开和 handoff 模板。
- 新增 Live Lab 可见真实环境测试台：可以新开 Terminal 观察 prompt、命令、响应和证据路径，并默认使用隔离 workspace。
- 已新增 `local-doctor` / `local-rebuild`，可从 memory、gateway、subagent 文件事实源诊断并重建 LocalStore。
- `status` 已输出 suggested actions，能提示 gateway、LocalStore 和 subagent 的下一步处理动作。
- gateway 请求队列新增 `failed` 归档、processing lease、超时重排/失败归档和保守 request worker pool。
- runner 并发已有第一版配置入口：默认 1；显式设置 `runner_concurrency` 为数字后才并行执行多个 run。
- 已新增 `adapter file` 文件协议，外部聊天工具/TUI 可通过 inbox/outbox 复用 gateway。
- 已新增 Round 5 Gateway 常驻稳定性功能：
  - 长期助手 风格 PID 记录（start_time tracking + scoped locks）
  - Watchdog Supervisor 自动监控并重启崩溃 gateway
  - Adapter 守护进程模式（`--daemon` + PID 文件）
  - `gateway start-all --adapter` 一键启动 gateway + 适配器
  - 系统服务安装（`gateway install` systemd/launchd）
- **记忆推模式** (`memory_push.py`)：在关键决策点自动注入相关记忆
  - `MemoryType` 枚举支持 LESSON_GENERAL/LESSON_TASK/LESSON_TEMP/CONTEXT/FACT
  - `push_relevant_memories()` 根据触发类型搜索相关记忆
  - dispatch_mixin 失败后自动注入教训记忆
  - failure_analyzer 增加 `relevant_memories` 字段
- **Dispatch 闭环保证**：防止长任务中途失活
  - 闭环检测：`dispatch_loop()` 循环直到无任务或达到上限
  - 自适应间隔：有变化 5 秒，无变化 30 秒
  - 最大轮数保护：`dispatch_max_consecutive_rounds=20`
  - Watchdog 进程监控 daemon 存活

最近已推送提交：
- `45bbd08 feat: Round 5 continued — adapter daemon, PID tracking, one-click start, test fixes`
- `37e046d feat: Round 5 gateway stability — 长期助手 daemon control, supervisor, service install`
- `84813a7 merge: integrate tool boundary hardening`
- `d6e31b2 merge: integrate framework runtime hardening`
- `22efdce feat: add status and timeline views`
- `519492b feat: index gateway and subagent logs`
- `f30cc08 feat: add local sqlite store`

## 当前可用能力

### 安装和入口

本地开发安装：

```powershell
python -m pip install -e .
```

安装后可以直接运行：

```powershell
my-agent
my-agent --help
my-agent status
my-agent timeline --limit 20
```

`my-agent` 不带子命令时会自动确保后台 gateway 存活，然后进入 `chat --gateway` 客户端模式。

### 普通对话和工具调用

已落地：
- `run` 单轮请求。
- `chat` 前台交互。
- `chat --gateway` 作为后台 gateway 客户端。
- 工具目录和推荐工具注入。
- 标准 `[TOOL_CALL]...JSON...[/TOOL_CALL]` 工具调用。
- 兼容 Qwen/通道运行时 常见 XML-ish 工具调用方言。
- 半截工具调用会转成可恢复的 `__parse_error__`，避免整轮崩溃。

内置工具：
- `list_files`
- `read_file`
- `search_text`
- `write_file`
- `append_file`
- `replace_in_file`
- `fetch_url`
- `http_request`

### 记忆和本地事实源

已落地：
- `memory.jsonl` 继续作为原始记忆流水。
- `LocalStore` 第一版：SQLite + FTS5 + 文件系统 + JSONL。
- 新记忆会双写：JSONL 保存原始记录，SQLite/FTS5 做检索索引。
- 旧记忆可用 `local-index-memory` 补建索引。
- `local-search` 可按关键词和 `source_type` 搜索。
- `timeline` 可按 `source_type` / `event_type` 查看最近事件。

已接入 LocalStore 的主要来源：
- `memory`
- `gateway_request`
- `gateway_event`
- `subagent_run`
- `subagent_work_log`
- `subagent_runner_result`
- `subagent_execution_context`
- `subagent_acceptance_review`
- `subagent_patch_review`
- `subagent_dispatch`
- `subagent_dispatch_report`
- `subagent_dispatch_watch`
- `parent_planner`
- `subagent_capability_route`
- `subagent_action_apply`
- `subagent_channel_probe`

### Gateway 常驻

已落地：
- `my-agent gateway start/status/stop/restart/logs`
- 后台 Python 进程常驻。
- pid/state/heartbeat/stop request/log 文件控制面。
- 本地文件队列：`pending -> processing -> done + responses`。
- `gateway ask` 可同步等待结果。
- `gateway ask --no-wait` 可异步投递，后续用 `gateway result <request_id>` 取结果。
- gateway 重启时会把遗留 `processing` 请求退回 `pending`。
- `my-agent` 默认自动启动 gateway 并进入 gateway chat。
- gateway request 和生命周期事件会写入 LocalStore。
- gateway 运行时已有可选 HTTP 控制服务骨架；`gateway_port: 0` 可关闭。主请求事实源仍是本地文件队列。

当前 gateway 形态：单机本地后台进程，本地文件队列仍是主协议；HTTP 是本机控制面补充，还不是完整 WebSocket / 多租户远端 gateway。

### Subagent / 多代理工作流

已落地：
- 子代理工单创建和标准目录。
- 父子关系、root_id、depth。
- 子代理红绿灯看板。
- due-check 风险巡检。
- channel probe 通道健康检查。
- action plan / action apply。
- takeover / reassign 基础记录。
- capability request / grant / gap。
- skill/tool 统一能力路由。
- execution context 最小上下文包。
- subagent runner dry-run / execute。
- runner 结构化输出 `[SUBAGENT_RESULT]` 解析。
- runner 坏结构化输出修复回合。
- runner 临时失败有限重试。
- patch review。
- acceptance review 独立验收。
- fake done / 伪造 artifact 防护。
- dispatch 一轮调度。
- dispatch watch 循环。
- parent planner gate：有活跃/待处理/卡住事项时，不允许空心 `HEARTBEAT_OK`。
- workflow plan/apply：父任务可保存 workflow plan，`auto` apply 可物化 worker 子工单。
- runner 并发保守线程池：默认 1，显式 `runner_concurrency` 数字才并发。
- learning draft：`enable_self_learning=true` 时，成功 runner 的 lessons 会生成候选草稿，并由 `my-agent learn` 管理。

重要边界：
- `subagent-run` 默认 dry-run。
- 只有显式 `--execute` 才调用真实模型 runner。
- `subagents-dispatch` 默认 dry-run。
- 只有 `--apply --execute-runners` 才会推进真实 runner。
- runner 不直接把任务标为 DONE，只进入等待验收，再由 acceptance 收口。

### 观察入口

已落地：

```powershell
my-agent status
my-agent status --json
my-agent timeline --limit 20
my-agent timeline --source-type gateway_request
my-agent timeline --event-type gateway_request_completed --details
```

`status` 汇总：
- gateway 是否存活、pid、heartbeat、队列数量。
- LocalStore 记录数、事件数、FTS5 状态。
- subagent summary、红灯任务、最近任务。
- 最近 timeline 事件。

`timeline` 展示：
- LocalStore 最近事件。
- 支持按来源和事件类型过滤。
- 可输出 JSON，方便后续 TUI/聊天工具复用。

## 测试状态

最近完整验证：2026-04-30

已通过：
- `py_compile`
- `CLI_REFERENCE` 命令/参数覆盖测试
- LocalStore 定向测试
- pytest 全量测试：`131 passed`
- 标准完整冒烟：`ALL_TESTS_PASS`
- 真实 API gateway ask
- 真实 API scenario-test happy path
- 坏天气场景：
  - `verification`
  - `gateway-restart`
  - `structured-repair`
  - `runner-retry`
- 真实 API subagent-run execute
- 父代理验收闭环
- `git diff --check`

当前测试入口：

```powershell
python agent_py_agent/tests/run_tests.py
python3 scripts/live_agent_lab.py --suite smoke
scripts/open_live_lab.sh --suite real --real-llm --timeout 300 --count 1 --max-cycles 2
bash -n scripts/workstream_*.sh scripts/open_workstream.sh
scripts/workstream_status.sh
```

注意：完整冒烟按当前约定会调用真实 API。

## 当前主要限制

还没做完：
- 真正进程级 worker pool / session pool，以及更完整的启动速率、长期心跳和资源治理。
- 多层父子代理自动上抛和自动下发的完整闭环。
- 子代理和孙代理的真实进程级并发调度。
- patch 自动集成后的验证闭环和更强 owner / 权限策略。
- accepted learning draft 到正式 skill / rule / profile 的人工确认提升流程。
- 长期本地数据 compact / rebuild / backup 命令。
- 远端同步、跨机器 gateway 协作、本体迁移、本体备份。
- WebSocket / 多租户远端 gateway 服务。
- 外部聊天工具 adapter 的更多真实平台打磨；统一投递 registry 已落地，文件 adapter、QQ/飞书通道和 adapter daemon 已有第一版，但第二个生产 IM 尚未完成真实 API/媒体/重启验收。
- TUI 观察面板。
- ACP / 外部 agent session / 远端执行器接入。

当前设计取向：
- 先把单机本地第一事实源做稳。
- 所有关键动作先落文件和 LocalStore。
- 远端同步和组织级多 gateway 后续在这个底座上叠加。

## 推荐下一步

优先级建议：

1. memory archive / hook 可观察入口
   - 能列出 `memory/hooks` 和 `memory/raw` 最近记录。
   - 能按 session/request/run/tool/status 等字段搜索。

2. memory resume 恢复线索
   - 用户说“继续”或给出关键词时，能找出相关归档、LocalStore 记录和任务目录引用。
   - 输出恢复摘要、事实源路径和下一步建议，也可以用 `--context-only` 单独输出恢复上下文块。
   - 不把 archive 当最终事实源，正式继续前仍先读任务目录。

3. 压缩前 hook 标准化
   - 把 run/chat/gateway/subagent-run 的结束点统一写 recovery snapshot。
   - 默认按 `memory_hook_archive_level=3` 保存恢复必需字段。

4. worker 并发模型设计
   - 明确 runner 并发、启动速率、超时和自适应策略。
   - 从当前 `daemon_*` 过渡到更正式 gateway scheduler。

5. 外部聊天工具 adapter 实战化
   - 在现有统一 DeliveryService/registry 和 file/QQ/飞书 adapter 基础上补第二个生产 IM 的真实平台场景。
   - 继续复用 `gateway ask/result` 和 LocalStore timeline，让完成结果稳定回到聊天工具。

## 常用命令速查

```powershell
my-agent
my-agent status
my-agent timeline --limit 20
my-agent local-doctor
my-agent local-rebuild
my-agent memory-doctor
my-agent memory-route "任务恢复规则"
my-agent local-search "关键词"
my-agent local-search "任务目标" --source-type subagent_run
my-agent local-search "gateway" --source-type gateway_request
my-agent gateway status
my-agent gateway ask "继续推进当前任务"
my-agent adapter file --watch
my-agent subagents
my-agent subagents-dispatch --watch --planner --apply --execute-runners
my-agent scenario-test
```

## 当前判断

这个版本已经具备一个个人本地 agent 的第一层核心能力：
- 能安装后直接运行。
- 能常驻后台。
- 能通过 gateway 接收任务。
- 能派工和验收子代理。
- 能真实调用 API 跑完整流程。
- 能把关键过程落盘、搜索、审计和观察。

下一阶段应继续围绕“恢复、诊断、并发、外部接入”推进。
