# Gateway Progress

## 2026-07-16 分步任务追加要求进入原任务持久上下文

- 1.10 部署 `47cc1dc9` 后，A/B 的第二步都在第一条工具调用中用精确 `task_id` 选择了各自原任务，工具路径
  也确实回到原工作区，证明 workspace 续接硬边界生效。但两条后台结果分化：A 主动更新了
  `task_progress`，所以保留了第二步；B 只读文件、没有主动写进度，后台轮随后只看见第一步旧目标，最终又
  报告“第一步完成”。这说明目录续接正确，但本轮追加要求没有自动成为原任务的持久上下文。
- 当前实现复用唯一 task guidance ledger：只有前台模型通过结构化 `task_progress select(task_id)` 明确选择
  旧任务后，runtime 才把本轮已读取的 `root_user_prompt` 以 `selected_task_followup` 提交到该精确 task。
  它会直接标为 delivered，因为当前模型已经读过；后台、retry 和 compact 继续把它当作任务历史，普通
  transcript、其他 task 和其他 owner 都看不到。整个归属判定只读 authoritative transcript 标志、request id
  与精确 task id，不检查“继续、第二步”等文字。
- 提交使用 durable request id 作为幂等键：同一网关请求重试只保留一条；相同键却出现不同正文时按账本
  冲突 fail-closed。持久化失败时 select 不会把本轮切入 task lane。对照 会话运行时 的 `TurnInput::UserInput`
  进入同一 active turn history，以及 通道运行时/长期助手 的 follow-up session/transcript 续接原则；my-agent
  只适配自己的 owner/thread/task 文件事实源，没有新增第二套 prompt 或任务识别器。
- `3fdf8637` 部署 1.10 后，B 的生产请求 `req_1784220189006_116909_0` 第一条工具调用为精确 select，
  原话只写入一次且在同轮标记 delivered；processing record 也绑定原 Navi workspace。后台随后真实读取
  第二步 guidance，修改 config/parser/item 并执行命令，证明执行上下文不再退回第一步。
- 同次真测又暴露独立的展示缺陷：前台 cooperative-yield 回执仍说“第1步跑通”。执行轮已读当前消息，
  但无工具辅助回复只拿旧 `task_progress` summary，模型因此把旧步骤写成当前进展。当前候选把本轮
  `root_user_prompt` 作为只读回执事实；若结构化 archive 显示本轮先 select 旧任务、但尚未 update/start
  进度，则不向展示轮提供旧 summary/next_action/open counts。模型仍自行写自然回复，规则不读取用户正文，
  也不以回复内容改变任务状态。
- `ddfd942a` 部署后的 A/B 第三步真测确认，B 的首次回执已经围绕本轮“第三步”且后台在原 Navi 工作区
  完成，独立重跑为 62 项测试通过；A 则先发生一次失败的 progress update，随后成功 select 原 Zoxide
  工作区并进入后台，但辅助表达轮仍错误声称“没有工具”并向用户重复索要路径。执行没有丢失，展示事实
  仍不合格。
- 当前本地候选只把 `ok=true` 的 task-progress transition 当作本轮进度刷新；失败的 update 不再使旧摘要
  重新进入回执。同时从同一 archive 生成 `task_workspace_selected_this_turn` 与
  `runtime_access_confirmed`，明确告诉无工具表达轮：原工作区已精确选择、执行轮已经成功访问文件，表达轮
  自身不携带工具不等于后台没有工具。以上只约束模型如何表达，不参与执行、续接或完成判定，也不解析
  用户或模型正文。

## 2026-07-16 旧任务续接与用户停止的结构化硬边界

- 1.10 双用户分步长任务实测发现：同一用户第二步已经拿到 Recent Completed Work，但 MiniMax 仍调用
  `task_progress action=start`，底层原先无条件接受，因而新建了第二个工作区，随后在错误目录里连续
  `PATH_NOT_FOUND`。这不是 transcript 缺失，而是 task start 入口缺少结构化确认。
- 现在会话存在 active/interrupted/recent-completed 候选时，`start` 必须显式携带布尔字段
  `new_task=true`；否则返回同一 `CONVERSATION_WORKSPACE_DECISION_REQUIRED` 和精确候选。继续旧任务仍只用
  `select + task_id`。没有候选时普通任务可直接 start。实现不匹配“继续、第二步、新任务”等自然语言。
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
- 前台安全让出所登记的 `foreground_task_continue` 也是一次性结构化唤醒。消费前必须重新读取精确
  `task_id` 的 task link；任务已 completed/cancelled/interrupted/abandoned/superseded 时直接归档旧唤醒，
  不得在终态后重新启动执行器或重复验收。`runtime_cooperative_yield` 登记的 progress policy 走同一
  终态口径，conversation root 使用的 `completed` 与任务替换使用的 `superseded` 都会退休该策略。

## 2026-07-16 `/btw` 被自然回执误消费的真测与候选

- `0794c9fb` 部署后的双 Feishu-scoped owner 长任务确认了 owner/上下文隔离、并行普通聊天、模型自主
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

## 2026-07-16 后台任务只读投影与第二执行器卡口候选

- `047e24f7` 部署后的双 owner 长任务已证明前台安全让出有效：两项任务都在 4 个工具轮后释放聊天入口；
  A 的普通侧聊正确回答且后台自主创建 3 个子代理，B 最终交付 47 个通过测试并退休全部续跑策略。
- 同一轮也发现 B 的侧聊被模型再次 `task_progress select` 到已经运行的根任务，第二个前台执行器因此重新
  检查任务现场，并把精确工具轮数复述给用户。根因不是用户说了“继续”，而是 task candidate 缺少结构化
  execution occupancy；修复不得增加中文触发词。
- 当前候选从 enabled progress policy 和未过期 background claim 读取执行占用。运行中的 active task 只进入
  `Running Work` 只读投影，选择卡口再次核验同一事实；已运行返回
  `CONVERSATION_TASK_ALREADY_RUNNING`，读取错误返回 `CONVERSATION_TASK_STATE_UNAVAILABLE` 并禁止创建
  第二执行器。相同 task/kind 的续跑 policy 复用，内部执行来源和精确工具轮数不进入自然回复模型事实。
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
  “写一句回复”误当成重新执行任务；内部协议、工具 XML、无依据 ETA、虚构文件大小和与完成状态矛盾的
  文案会被拒绝，重写仍失败则抑制正文，不回退“正在处理”一类固定模板。
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
- 双用户实测中，A 在任务运行时用于聊天记忆核对的“青柚47”被后台轮读到并写入产物注释，证明“会话可并行”
  还缺上下文权限隔离。后台 task context 现按结构化 task lineage 保留本任务消息/观察/wake 与 task link，排除
  thread compact、普通聊天和其他 task；`/btw` 仍通过 task guidance ledger 在安全点注入。判定只读结构化
  id，不按自然语言猜消息是否相关。
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

- 普通 chat lane 不再提前创建 task workspace；只有注册表 `promotes_task` 或结构化任务动作能在真实
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
- `/status` 只从当前 owner/channel/conversation 的 durable task/request、typed progress、subagent state 和 thread
  compact/verbose 事实渲染；不显示内部工具名、命令、路径、引导内容或“最近一次引导”。跨用户和损坏
  请求无法证明 owner 时 fail-closed。
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
  继承同一 task id/workspace。完整任务历史由 `task_ids` 保存，候选热索引由 `active_task_ids` 保存；
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

- 1.10 双用户复测确认辅助回执不再提前消费 `/btw`，但也暴露第二个边界：C 的前台真实任务轮确认了
  guidance 后，在 cooperative yield 进入后台续跑时只恢复原 goal/workspace，没有恢复已经提交的 guidance；
  D 因首轮主动把补充要求写入进度清单而偶然保住，C 的最终实现过程则没有持续携带三项要求。
- 对照 会话运行时 将 pending user input 记录进同一 turn history，以及 通道运行时 等待 steering message 写入
  transcript 后才确认 delivery，本地候选保留“一次新输入”语义，同时把已确认 guidance 作为精确 task-id
  绑定的 committed context 投影到每个后台 continuation。它不会进入普通聊天、其他 task 或下一次请求。
- 若前台在确认前崩溃或让出，后台 request id 虽已变化，仍会用 durable task id 读取原 request guidance
  inbox，成功进入模型 prompt 后再确认。整个归属链只读 request/task id 与 delivered ledger，不解析补充文字。

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

- `/goal` 每个 continuation turn 除了精确 task link 和子代理树，还直接获得同一 `task_id` 的
  `task_progress_summary` 与现有 task compact refs。前一轮记录的整体进展、下一步、开放项和近期完成项
  会进入下一轮，不再只靠重新读目录恢复工作现场。
- task compact rollup 将根任务进度与 child 状态一起写入 `work_state_snapshot.json` 和
  `continue_packet.json`。派工种下的 child 进度项只按精确 `run_id + canonical DONE` 自动闭合；
  `integrate-and-verify` 仍由主代理根据真实整合与测试事实更新，不成为系统验收硬门。
- `task_progress select` 成功结果同时返回 task 状态、是否复用原 workspace、匹配 goal 状态和是否已安排
  continuation。普通回复仍由模型生成，但模型不应在结构化事实显示原目标已恢复后再次向用户索要任务。
- 前台达到 cooperative-yield 安全点时，零工具表达轮除“任务继续、用户可继续聊天”外，还获得本轮成功/
  失败动作数、当前 task-progress 总览、下一步和开放项数量。它仍由同一个 LLM 自然措辞，但不得向用户
  提及 JSON、结构化信息、facts、数据包或系统提示，只报告任务本身的真实进展。

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
