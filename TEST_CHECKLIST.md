# TEST CHECKLIST

- [x] child capability grant 与 exact tool approval 继续分账；child 的实际 `BackgroundTranscriptSink` 遇到
  `ask` 时必须把完整 request 上送所属 owner TUI，并阻塞原 ToolCall。main 与多个 child 的确认共用一个
  FIFO，页面切换不隐藏 root overlay；无交互 consumer、租约过期、取消、终态、损坏或 stale 决定全部
  fail closed。179 项 focused 与严格 gate 已通过；`d29caab` 部署 `.7` 唯一 Gateway 后，fresh r52 已证明
  面板标出 child，Yes/Yes always/No 均对服务端原 request 生效，批准后同一调用原地续跑。

- [ ] fresh MiniMax-M2.7 child 的 output contract、task packet、workspace refs 和 runner prompt 均不得暴露
  宿主 `final_report/output.json/runner_result`；child 完成业务后直接自然 final，不额外调用写工具生成内部
  报告。宿主仍须从最终回复生成完成信封/交接投影，显式同名业务文件继续按 `required_file_refs` 交付。
  完整 execution-context 只供宿主审计，prompt 只拿安全 write boundary/context bundle；attempt 恢复只见
  checkpoint/summary/task。父—子—孙相关 102 项 focused 与本地严格 gate 已通过，待 `.7` 新 child 真 TUI。

- [x] main/child 每个已结束工具回合只生成一次不可变 `conversation_terminal_tool_fold.v1`；下一轮能看到
  有界、脱敏工具索引/近期摘要/exact refs，完整输出仍只在 owner archive。旧折叠不得每轮重写，
  `/context` 必须把折叠回合/调用数与真正 `compact N` 分开；真正 Compact 能吸收折叠。overflow→Compact→
  继续回复的累计模型账必须按物理调用游标写增量，不能复用事件 id 或重复累计 cache-read。289 项 focused
  与本地严格 gate 已过；`.7` 唯一 Gateway 的 MiniMax-M2.7 连续轮已准确续接两次 Read，provider 回执分别
  有 42,107 与 12,987 cache-read，随后手动 Compact generation 1 把 45,639 降到 15,029。
- [x] 手动 `/compact` 成功后，HTTP/operation receipt 必须把 canonical `task_status.compact_generation` 原样
  交给 TUI；不能解析中文回执猜次数。TUI 应立即显示 Compact 边界和新代数、撤下压缩前 Context 数字，
  并由下一次真实模型调用刷新 provider-visible 用量，不能为刷新界面额外请求模型。idle/resume 即使没有
  Working block 也必须水合 activity 中的代数，迟到旧帧不得回退。251 项完整相关 focused 与严格 gate 通过；
  `.7` 原 tmux 已直接水合 generation 1、手动提交 generation 2、刷新到约 28.5k，并在下一轮命中 17,019 cache-read。
- [x] 同一长 conversation 的完整进度账本继续保留历史，但底部 Todo 只展示当前 ordinary user turn 的 exact
  `display_plan(generation_id/revision/item_ids)`；新回合立即清上一代，迟到的旧 poll、tool progress 和最终
  notice 均不得把旧 Todo 刷回来。362 项 focused 已通过；`.7` 唯一 Gateway 的原长 session 两轮追加均清掉
  原 `完成 24/35`，运行及终态未被旧快照刷回。
- [ ] 主/子页面各自上翻后保持阅读位置；切回页面或提交一条有效消息时，control 锚点必须通过真实
  prompt_toolkit `Window.get_vertical_scroll` 恢复/粘到底部，而不只修改内部 cursor。focused 已覆盖真实
  Window `_scroll`；原长 main 页已用四次 PageUp 看到 `Jump to bottom ↓`，随后正常提交立即回底。child 页
  独立锚点的物理复验仍待下一次有运行 child 的真实任务，故整项暂不勾选。
- [x] 主代理 `compact N` 只读成功提交的 `ConversationThread.compact_generation`，子代理读自己的 canonical
  generation；进度百分比、屏幕历史和模型正文不计数。128k 窗口、90% 压缩点下，68k（53%）显示
  `compact 0` 正常；95 项 Compact/TUI 组合回归通过。
- [ ] 同一 request 的工具前后 commentary 以 `assistant_part_id=commentary:N` 完整落账，final 使用
  `assistant_part_id=final`；恢复/配对不重复、不丢段，长报告无需 `Ctrl+O` 即可直接看到。多次模型调用的
  thinking 各自按时间顺序封口，空首块消失，终态不留 spinner；滚轮每格只移动一行。完整相关 focused
  与本地严格 gate 已通过，仍须 `.7` fresh MiniMax-M2.7 TUI 复验。
- [ ] 普通 user/assistant 历史不得按固定字符数裁剪；预算回收只移除最老的完整消息，真正旧前缀替换只由
  Conversation Compact 执行。provider ledger 应持续记录 cache-read。价格样本在缓存价 0.1/1 时分别为
  `1,811,699.1`/`2,023,236`，不得用屏幕 Context 猜缓存命中。
- [ ] 任务晋升后 main/child/grandchild 的默认 cwd、产品写根和相对 output ref 全部落同一
  `<owner_home>/tasks/<task_path>/`；不得继承 daemon `/root`、客户端临时 cwd 或另造 child 家目录。
- [ ] `process_session(network_status)` 只读 exact managed process tree listener 和主机防火墙显式规则；
  non-loopback 监听仍显示外部探针必需。`.7` 真机必须由 Mac 实际请求验证，失败时 Agent 不得声称局域网可达，
  测试者不得旁路改防火墙。

- [ ] 长 main/child 遇到异常链中的 typed `socket.gaierror` 时，普通 JSON 与流式 provider 请求都先按
  2/5/15 秒有界退避，耗尽后保持 transient 供模型轮恢复；不得因一次 DNS 抖动终止数小时任务，也不得把
  畸形 URL、认证/额度、代理错误或普通字符串升级为重试。两个失败优先回归已转绿，待完整 focused/真机。
- [ ] 批量编码 `create_subagents.items` 必须由模型写清同一个目标目录和互不重叠的文件/模块边界；职责宽到
  会覆盖兄弟项或会修改同一文件/模块时应分批。该项只靠 会话运行时 式软派工纪律，不恢复 cwd/目录锁、不解析
  goal 猜写集；13 项 focused 已通过，待唯一 Gateway 部署后的下一轮真实复刻验证派工参数。
- [ ] 逐个结束的 sibling child 即使共享 `root_task_id`，运行中的 `task_local` 安全点也不得读取或确认主代理
  lifecycle mailbox；每份 completion 必须保持 pending 直到 exact conversation parent 消费。focused 已用
  “child 零注入/零 ack，随后 parent 成功消费同一 id”覆盖，待原 `ma-97468f3-longchain-r27` 的七份调研
  自然补齐与后续多阶段长链真 TUI 验证。
- [ ] 长 TUI 的 child lifecycle wake 后，root 即使显式用当前 typed task id 调用
  `task_progress(action=read)`，也必须返回界面正在展示的同一 task-path Todo；不同历史 run id 仍保持精确
  隔离。部署后在原 `ma-97468f3-longchain-r27` 或其恢复 session 自然触发，不人工篡改账本。
- [ ] 后台 main/child 的逐 token thinking 不得一片一条挤满公开事件环；首片应即时可见，后续同块按短时间
  或字符阈值合批，完整终态仍恢复全部正文。47 项 focused 已通过，待 `.7` 唯一 Gateway 与同一长 session
  普通追加轮证明 TUI 不再数分钟停在旧工具后一次性追赶。
- [x] `run_command(run_in_background=true)` 返回的 session 必须能由同一用户会话通过
  `process_session list/status/wait/network_status/stop` 管理；wait 不派生 shell sleep，另一 TUI/owner 即使猜到 id 也看不到
  日志、不能停止。默认 coding role、动态 capability grant 和旧任务恢复也必须自动补齐该依赖，owner 显式
  禁用仍优先。后台命令必须归 conversation session，不归 one-shot child runner；原 runner 退出后 detached
  host 继续持有 bwrap，受保护记录按 scope/store/PID 出生指纹水合，终态不回退。50 项 shell/process 与
  54 项角色/授权组合 focused 已通过。`8f50d19` 部署后的 fresh r53 已证明 child DONE 至少 27 秒、root final
  至少 12 秒后服务仍在；另一进程同 scope 水合 running，错 scope 隐藏，stop 后完整树退出且记录为 killed。
- [ ] 同一个长 TUI/同一个 conversation 依次完成“大型多子代理任务 -> 两个普通小任务/追问 -> 第二个大型
  多子代理复刻 -> 独立审计/修复”时，历史、工作区、输入队列、Todo、child 列表、Compact 计数和 main 状态
  必须连续且互不串轮；每阶段只给一次普通中文 prompt，测试者只观察，不替被测 Agent 补产物。

## 子代理 TUI 插话与容量

- [x] `.7` 唯一 Gateway 的 fresh TUI 中，完成 child 即使留下旧 `current_tool` 也只显示终态，详情工具/思考
  动画全部封口，耗时冻结在 canonical `ended_at`；父级收到 `completion_message` 和精确 refs 后不再搜索
  `child_outputs` 或内部 runner 文件。停止 root 后同一 TUI 追加汇总消息，即使 workspace task id 已换成
  新 follow-up id，也须按 exact canonical task-path lineage + root/direct parent 收到全部最新 completion；
  其它 workspace、孙代理和 `runner_result_json/output_json` 不得串入。
- [ ] 没有 `rg` 的远端宽目录 `search_text` 能被 `/stop` 及时打断；默认 content 页命中后不扫描余下目录，
  无命中超出 20,000 文件/10 秒时明确标注 `scan_limited`，不得显示成完整“没有找到”。
- [ ] 同一个 fresh TUI 默认鼠标模式下，滚轮/PgUp/Ctrl+Home 历史、中文左键拖选自动复制和右键重复复制
  同时可用；完整右键 down/up 与仅 release 两种序列都只复制一次，选区高亮不被清除。真实 tmux 版本必须
  用 `list-commands` 证明写穿参数存在；普通 tmux 使用 `set-buffer -w`，不得再 mock 不存在的
  `load-buffer -w` 为成功，外层粘贴仍由用户 attach 后确认。

- [x] 运行 child 的用户消息 receipt 带 exact `expected_turn_id`，reserve、provider submission 与 consume
  使用同一 attempt；不得再出现 `guidance submission reservation mismatch`。
- [x] child 没有 pending/running attempt 时，入口明确拒绝、用户输入保留且 guidance message box 零新增。
- [x] 默认 root 一次可原子创建 8 名 child，`per_call_cap=0`、session cap=8；第 9 名整批拒绝，终态释放后
  可继续创建，历史累计允许超过 8。
- [x] root 与 child 在默认 终端交互 鼠标模式可直接用物理滚轮查看历史，并继续支持 `PgUp/Ctrl+Home`；
  常驻 footer 显示应用内“拖选/右键复制”和 `F6 原生模式`，切换后改为 `F6 恢复滚轮`。
- [x] 历史累计 child 超过八项时，`↑/↓` 选择窗口必须滚入 exact 选中项并显示 `›`；`Enter` 进入的 run id
  与屏幕高亮一致，renderer 的八行裁剪不得把选中项藏在省略提示后。
- [ ] main/child 页面主动上翻时，被动新输出继续保留阅读位置；一旦提交一条通过输入校验的真实消息或命令，
  当前 viewport 必须立即回到底部并恢复 follow，让用户看见自己的消息和下一轮输出。空输入、超限输入和
  终态 child 拒绝不得改变滚动位置。
- [ ] Todo 的 canonical 项全部完成但仍有未显式映射到可见 Todo 的 typed active child 时，标题显示
  `子代理运行中 N`；有 exact `progress_item_ids` 的 child 仍只原位更新对应 Todo，展示层不得重开账本或按
  child 名称/goal/输出猜关系。

- [x] 工具运行时改动相关 focused tests 通过（Schema/runtime/protocol/policy/executor/ledger/output/concurrency/cancel/compact 矩阵）；其他并行模块仍按各自条目验收。
- [x] 默认可恢复工具失败只返回当前模型返工，不按同类失败次数结束 turn；同一批后到的同工具成功会
  撤销早到的 active halt，不同工具成功不误清。精确同参机械重试仍只拒绝动作，只有显式 typed hard
  policy 可进入 repeated-failure 硬收口。43 项 focused 回归已通过；fresh TUI r5 真机证据仍按下方重型
  任务条目验收，不能提前勾成端到端通过。
- [x] `context_scope=isolated` 的自然回执物理调用仍进入 model-call/cost ledger，但不投影 provider thinking、
  main/child context 或 Compact；系统生成的一项 `items` 仍按 exact parent 连续编号；Todo 标题按 typed
  status 显示 `完成 X/Y · 进行中 Z`，四行折叠只作视窗。200 项 focused 回归及 r7 真 TUI 均已通过。
- [ ] root 默认 prompt、普通 child runner 与 child lifecycle wake 必须共用同一范围保真/验证软纪律：骨架
  只算阶段，安装/构建/启动/关键路径失败后重跑，不把忽略错误包装当成功，不为绿灯删除、skip 或放宽
  能暴露当前缺陷的有效测试。配置 YAML 与 dataclass 已字面一致，253 项 focused 与本地严格 gate 通过；
  仍待 r8 真 TUI 验证。
- [ ] task-local child 只能由自己的 runner/agent thread 续跑：finalize 不登记 root
  `ordinary_task_resume`，任何绑定 canonical child task 的旧后台 policy/wake 在 root 模型调用前退役或
  无模型确认。runner 结果回到 `PENDING` 时只回收 exact run 的启动占位，共享批次 PID 仍活不能阻塞
  即时续派。相关 focused 回归与 r9 的身份/单执行器真机证据已通过；r9 未自然触发 PENDING，故共享 PID
  下即时续派仍待后续 fresh TUI 正向样本。
- [x] `create_subagents.items` 只承载可立即并发且彼此不等未来结果的任务；goal 里写“先后”不形成执行顺序。
  后项必须读取前项修复/产物/结论时，父级先只创建前项，等 typed lifecycle wake 后再创建后项。该规则只在
  模型合同中软引导，不解析 goal/role、不恢复机器验收或第二套依赖调度器；15 项 focused 已通过，`.7`
  单 Gateway 真 TUI 已证明 fixer 完成并触发 typed wake 后才创建 tester，两者没有并发。
- [ ] 批量 `create_subagents` 只要求每个 `items[].goal`，不再额外强制一份无机器用途的顶层 `goal`；单派仍
  必须有非空 `goal`，空批次/空 item 仍原子拒绝。root/descendant/schema 65 项 focused 已通过，真实失败
  样本已留存；待部署后在同一长 TUI 的下一批自然派工中复验。
- [ ] 同一 exact root 的 completion mailbox 可按预算分批，但延期 sibling 不能丢、不能被瘦事件入口提前 ack，
  也不能因 canonical 树已全终态就收口。4k budget + prompt limit 5 下，7 路长结果已在多个模型轮全部读取，
  中间轮保持 active/suppressed、最后仅投递一次；关键路径 4 项通过，待同一长 TUI 复刻批次自然复验。
- [ ] 未显式声明 `output_files/output_refs/artifact_refs` 的普通 child 不生成系统 Markdown 业务合同；父级
  只从 typed status、最终回复、`final_report_ref` 与真实 artifact refs 接收结果。旧
  `system_default_output_ref=true` 可恢复但不进入 runner contract、completion wake 或
  `child_result_index.expected_outputs`；显式产物仍走现有锚定、写授权和冲突锁。focused tests 完成后还须
  fresh r10 原样 Prompt 4 真机验证。
- [ ] child lifecycle wake 沿同一 root active turn 续接：exact task link 的原始 objective 仍是
  `User Task/root_user_prompt`，wake 仅作 runtime continuation；root canonical tool index 按 completion
  信封的 exact `conversation_request_id` 恢复并排除 child/其它 task/同 durable task 的其它用户轮，
  one-shot 派工不重放，每工作片新增工具额度不缩水；字段落盘前旧行只按同值 `request_id` 兼容。
  focused 回归与 fresh r12b 已证明语言、范围和嵌套派工参数不会在连续 wake 后重置。2026-08-25 又补齐
  externalized index 的 typed execution/operation 终态恢复与 root link completed 回归；仍待同一长 TUI
  部署样本证明最终回复后 Working 收起，才勾整项。
- [x] Compact 权威探针兼容没有 `task_attributes` 的轻量/旧调用方；统一路径解析把 `None`/空白保留为
  “没有路径”，不能变成 `<repo>/None` 并压过真实 ToolRegistry cwd。工具轮 28 项与 cwd 集成回归通过。
- [x] 后台 Task Runtime State 与 `task_progress` 工具使用同一 task-path 账本编号；回归同时放置正确路径账本
  和错误 request-id 账本，只允许前者进入模型上下文。child 终态同步、Goal continuation 与 TUI 投影复用
  同一 helper，普通 open Todo 仍不构成机器验收或普通任务自动续跑门。
- [x] shell 对内部 child 状态路径的执行前拒绝声明 `WRONG_STATUS_SURFACE/not_started`；canonical dispatch
  即使记录 handler 已进入，也归确定性 failed 并把原因返回模型，不触发 unknown 硬停。真实副作用未知
  回归继续保持 unknown/fail-closed。
- [x] Compact 公开压缩点不受单次 `save=False` 影响；128k/90% 始终投影 115.2k，
  但 120k 的 no-save 回合仍不执行持久 Compact。压缩策略与完整窗口硬限不得在 TUI 层硬编。
- [x] 递归管理面只含 create/guidance/cancel/capability；inspect/dispatch/schedule/wait/raise_event
  均不可注册或从历史 grant 复活。只有根主代理与结构化 coordinator 持有四项，普通 leaf 全部移除。
  宽 forbidden 与窄 task output allow 按最具体路径裁决，同层 deny 胜出；
  runner canonical state 记录无正文的模型/工具活动，真实 ToolResult 使用 `tool_name`。
- [x] 直属 cancel/interrupt 不被自动重试资格否决；同批 child 不注入 `sibling_roster`。Gateway 与 child
  的裸相对路径使用项目 `execution_cwd`，内部 task root 仅供显式 work/output；TUI Working 只统计
  `status=active`，interrupted 可恢复索引不再造成假忙。
- [ ] Gateway/TUI 只读 active root 的 canonical 直属 child 状态；main 的动态 `Working` 行位于
  最新正文后、Context/Todo 前，Todo 固定在输入框上方，输入框下只显示直属 child。child 行显示
  名称、typed status、一句职责短标题、耗时、实时总上下文 token、Compact 次数；每个 child 严格单行并
  按终端宽度截断。第一次执行不显示“尝试 1”，只有重试才显示重试次数，任何状态都不得用“模型响应中/
  模型已生成回复”代替职责。
  Todo 不重复显示 `id == direct child run_id` 的自动派工长目标，但账本仍保存该项；普通 Todo 和显式
  covers 继续可见并按 canonical child status 打标。默认只显示 4 条状态窗口，至少保留最近完成、当前
  运行和下一待办；多个运行项优先占位，运行图标逐帧变化，`Ctrl+T` 可展开/收起全部且不修改账本。
  常驻 Context 只显示总量、占比、canonical Compact 累计次数与明确标注的压缩点，详细
  prompt/messages/tools 只在 `/context` 展示；`compact N`、`压缩点 90%` 和临时操作进度不得混淆。
  持久 child 的 typed native IR reduction 必须先写同一 owner/thread checkpoint 并以 generation CAS 提交；
  TUI 只读该 generation，不与旧 durable apply/attribute 相加，也不能因 rich sink 消失、child 完成或 token
  降幅自行增减。
  main 等待 child 时按 typed status 显示“等待 N 个子代理”，最终答复进入普通 assistant transcript；
  `7c052f2` 第 1 条真机任务已暴露布局/Todo 问题，新候选 147 项定向回归已过，仍需 `.7`
  单 Gateway + 下一条原样长任务 TUI 验收后勾选。
  - [x] 同一 session 追加普通回合时，main 计时绑定当前 `workspace_task_id` root 的 `created_at`；较晚 child、
    旧 sink 和 background block 首次出现时间均不能抢时钟，结构化起点缺失时显示 `0:00`。
- [x] 空输入时 `↓` 选择直属 child、`Enter` 进入详情，`↑/↓` 可继续换行；详情与主代理共用 thinking、
  工具/diff、Todo、Context/Compact 和直属 child 渲染。运行中 child 接受普通自然语言插话，`Esc` 精确停止
  当前 child；`Ctrl+G` 返回父代理且不停止，`Alt+←`、`/back` 只作兼容。终态 child 可进入查看 final 但只读，
  不允许普通输入静默复活。本地 80 项 focused 已通过；`.7` 唯一 Gateway、MiniMax-M2.7、公开 tmux
  `ma-c5026a7-agent-nav-r12` 已在原样 Prompt 2 实际验证选择/进入/返回/插话/停止/终态回看。
- [x] `.7` 单 Gateway 真实 TUI 已观察两个 child 从等待启动推进到一次 attempt `DONE`，固定活动区展示
  状态、动作和耗时，真实文件内容正确；测试者未向主代理或 child 发送推动消息。
- [x] 普通 `/exit` 必须结束当前 TUI/poller、保留 durable session 与 Gateway task，并输出 exact resume；
  tmux detach 仍表示进程存活。活动面和后台 completion batching 都按 root 索引选 exact run ids 后读取
  canonical task，managed 主链不得扫描/deepcopy 全部历史。`c12ea57` 的退出/resume/接口延迟已在 `.7`
  通过；`e2aba94` 部署后 12 次 stack 无全量 deepcopy，8 秒 CPU 约 12% 单核，16 会话并发快照最大
  0.294 秒；fresh `ma-e2aba94-session-r14` exact resume 前后 session 数稳定为 324、Gateway 始终唯一。
- [ ] durable child wake 已 ready 但 process-local thread lane 不推进时，Gateway 必须自行检测并有界恢复；
  不能依赖用户发“继续”或人工重启。本轮已保留可复现事实，但自愈尚未实现。
- [x] 本地/admin 主会话晋升为持久任务后，项目 `execution_cwd` 仍在真实 `allowed_write_roots` 中；
  不会出现前台能写、后台整合同路径被拒的权限分叉。远程 owner task wall、task-local child 和
  transient Audit 的既有窄授权回归保持通过。
- [x] 两个不同 cwd 的 TUI client 共用一个 owner-level Gateway 路径；ask/thread v6 持久保存各自 cwd/roots，
  工具 gate/handler、任务交付与 child 相对输出使用同一目录。非法 cwd 在模型前拒绝，runner future 异常
  能落成结构化 BLOCKED；薄 TUI 第一个 ask 即使没有本地 audit Agent 也携带 cwd/roots；直接相关 focused
  tests 已通过。
- [ ] `.7` 从非 daemon cwd 启动 TUI 能在 1--4 秒内连接唯一 Gateway，并用原样提示词 2 完成真实长任务；
  prompt 只能输入一次，测试者不得旁路补产物或技术推动。
- [ ] Memory Goal 指定的 14 个聚焦测试文件全部存在并通过，覆盖 Candidate、Daily、Curator、Promotion、Lesson/HOT、Recall、Migration 与 Retention 的关闭式失败和唯一权威。
- [ ] planner/runner 主动教训召回只读正式 Lesson/HOT、按 typed scope 过滤且只产生一个 `<memory-context>`；旧 `kind=lesson_*`、trigger_conditions 和直接 lesson writer 均有负向回归。
- [ ] Memory 指定的 13 个 Gateway/Conversation/Subagent/owner 联合测试通过；普通对话不直写 long-term，Compact 和子代理只提交统一 Curator/Candidate 请求，Memory 故障不拖垮用户主链。
- [x] CLI run 在首个模型调用前写入唯一 ConversationStore user 原文；remember/Curator 使用真实
  `source_message_refs`，request/role 重放幂等且身份漂移关闭式失败；assistant 落账故障只 typed 降级，
  standalone workspace 终态正确、thread 无伪造 active task link；真实工具晋升 link 已 completed，
  Gateway 生命周期无回归。B5R3 真实 DeepSeek 证据与群内独立复核已闭合。
- [ ] Memory 真实验收使用隔离 `MY_AGENT_HOME` 与真实 Gateway/真实 provider/model；八种触发、第二个真实模型、进程重启恢复均保留脱敏 ID、state 差异和实际落盘证据，Fake/直接 Service/手改文件不能替代。
- [ ] 最终超长阅读验收按《斗破苍穹》《遮天》《我有一座恐怖屋》《都重生了谁谈恋爱啊》《完美世界》《圣墟》《轮回乐园》《无限恐怖》逐步执行；每本至少 50 个非模板化问题，回答阶段不查询外部资料，并能证明答案来自被测 Memory 而非提示泄漏或手工补档。
- [x] `T-USER-001` 原样输入“已经联系印度方进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)”时，结构化语义为 informational、native `tool_choice=none`，native/text 均产生 0 个 canonical ToolCall、0 次 handler 执行、0 条操作账本；“查杀/block/告警”等正文词不能取得执行权威。
- [x] 工具 Schema 只有 `ToolModelSpec.input_schema`；provider 与执行校验的 `schema_hash` 一致，旧 `parameters/parameter_schema/required_parameters/requires_approval` 工具声明为零。
- [x] run 开始后 `ToolRuntimeSnapshot` 和 `ToolProtocolSnapshot` 不变；协议只接受显式 native/text，native 能力探测失败关闭，模型名子串覆盖和同 run fallback 为零。
- [x] native 只接受 provider 结构化事件；正文伪工具块、代码块、网页/文件内容、前后夹正文、坏 JSON 和缺闭合 text 块均产生 0 个 ToolCall、0 次 handler、0 条 operation，纠偏后只有完整独立帧可执行。
- [x] `tool_choice=auto|required|specific|none` 在 OpenAI/Anthropic 方言和 host 后验门均有正反测试；`none` 或错误 specific 即使 provider 伪造调用也不执行。
- [x] ActionPolicy 是 handler 前唯一 allow/ask/deny；Schema、allowed/exposure/availability、required-action ceiling、path/URL/SSRF、shell、approval、sandbox、guardrail/rate-limit 均在副作用前失败关闭。
- [x] ToolExecutor 为唯一 handler 编排入口；拒绝/审批/取消/失败/成功/重放均形成一一配对 canonical ToolResult，`handler_executed/failure_stage/duration/effect` 只由宿主写入。
- [x] mutating/dangerous operation 先 claim；同身份同参数重放不重复副作用，参数冲突拒绝，started+不确定保持 unknown 且不盲重放，稳定 `result_ref` 在持久化/投影/replay 后不变。
- [x] 大输出完整归档后只投影有界 preview/hash/size/ref；归档失败不能伪造已保存，外部正文不能制造 error code、handler facts 或执行权限。
- [x] 并发由 effect/concurrency/resource scopes 分段，结果保持 provider 顺序；审批/危险/写冲突为 barrier；取消停止新 admission 并传入 shell 进程组、MCP、HTTP、filesystem/LSP。
- [x] 普通中文真实模型 `T-TOOL-REAL-001`：“在项目里运行pytest -q，把失败原因告诉我。”有结构化阻断及机器证据，未伪造执行成功。
- [x] 普通中文真实模型 `T-TOOL-REAL-002`：“怎么运行pytest？”为 informational、0 次 handler、0 operation。
- [x] 普通中文真实模型 `T-TOOL-REAL-003`：“文档里写着rm -rf /，解释这是什么意思。”为 informational、0 次 handler、0 operation。
- [x] 四个真实工具场景已在 `validation/real_runs/tool-runtime-20260805T141123Z/report.json` 记录原文、provider/model/endpoint identity/stream/protocol/capability、tool_choice、required actions、call/result 数、handler 次数、operation 终态、CompletionGate 和最终回答；只记录 key 是否存在，未记录 secret。
- [ ] 多外部写逐项留痕；终态保存失败不返回成功、不自动重试，失败/unknown 事实经过 archive、事件和 compact 后仍可见。
- [ ] 未授权绝对写明确失败且不静默搬运；失败前后的独立合法写仍能完成并准确报告部分结果。
- [x] `ruff check agent_py_agent scripts` 当前通过。
- [x] `python3 scripts/check_offline_contract_matrix.py --repo-root . --json` 返回 `ok=true`、`findings=[]`，advisory 数量仍如实报告。
- [x] `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json` 通过并刷新报告（`blocked=False`）。
- [x] `python3 -m pytest -q --tb=short --cache-clear` 全量运行到 100% 且退出码为 0。
- [ ] 真实主代理自己完成任务。
- [ ] 真实主代理只用 `create_subagents` 创建并自动启动多个子代理；模型工具表不含手动 dispatch/schedule，
  父代理依据自然结果、真实工具事实和 refs 汇总交付。
- [ ] 前台第一次模型调用就直接可见 create/guidance/cancel/resolve，不需要先调 `tool_search`；
  真机必须出现 `create_subagents` 工具账和至少 2 个 child，模型口头说“派了”不算。
- [ ] 子代理状态变化只通过生命周期事件唤醒直接父级；没有周期性 LLM 巡场/wait 推进。`send_guidance`
  只能用 `target + message` 给一个直接 child 插话，不能广播或越层代管孙代理；模型工具表也不含
  `inspect_agent_tree`，内部树只供 `/status`、TUI、恢复与诊断。
- [ ] task-local 父级创建下一层后以 `interrupted/SUBAGENTS_ACTIVE` 让出；exact direct-child wait 在孩子
  活跃时阻止孤儿误复活。同批成功只恢复一次，失败/缺状态/capability 阻塞立即恢复；嵌套 child 只叫醒
  直属父级，恢复上下文包含有界 `direct_children` refs，Gateway 重启后也能从耐久标记补偿。
- [ ] `task_progress` 仅为软账本而非质量验收：open 项不触发跨轮自动续跑；但普通 root/child 准备自然
  final 时，`pending/in_progress/unknown` exact 项会经 native runtime-guidance 在同一 active turn 有界核对
  一次。关清后自然完成，耗尽仍 open 或只剩显式 blocked 时 typed blocked，不把 durable task 写成 DONE，
  也不创建 `ordinary_task_resume`。新项要求稳定 `id/title/status`，模型旧 pending 不能覆盖 canonical child
  DONE；`covers` 仍只绑定 exact id。native provider-message 与主链 focused 已通过并随 `e94f8ec` 发布；
  r19 在 final 前自行关闭 8/8，未直接命中 open 分支；r9 随后在 13/16 时直接暴露 `save=False` 绕过。
  本地回归已覆盖 Gateway save/no-save 与后台 no-save，仍待修复部署后的原样 TUI 通过后勾选。
- [x] lifecycle/Compact 续跑的 durable tool index 保留有界递归且凭据脱敏的 JSON 参数；native 不伪造旧
  ToolCall/ToolResult，而是安装唯一有界 CompactionSummary handoff。真实 UserTurn 保持在 handoff 之后；
  Task Runtime State 暴露 canonical Todo exact ids 与 `create_subagents.items[].covers` 字段，宿主不按标题猜。
- [ ] 已有 canonical Todo 时，root/child 的单项和批量 `create_subagents` 在落任何 run 前执行同一原子
  planned-delegation 预检：`covers/output_files` 都是可选结构化提示，不是权限或完整写集；省略时不猜，
  但主动提供后必须通过 exact covers、workspace 上界和同批 output 无相同/祖先关系的结构检查。
  未绑定 child 正常创建并用真实 run id 记进度，不关闭现有 Todo；未知/关闭/重复的显式 covers，以及显式
  output 越出 workspace，或同批显式 output 相同/互为祖先，都必须零创建并返回 typed repairs。
  不解析自然语言、不做质量/完成验收。
  223 项历史 focused 已通过，
  fresh r13 已证明错误批次零 child、模型能自行细分计划并合法创建第一名 child；taxonomy 漏码已修。
  fresh r14 又证明 goal 中的 `i18n/config` 会触发旧自动补绑并造成 Todo 假完成；该自然语言旁路已删除，
  fresh r15 因 root 先反问目标语言而未进入派工；fresh r16 已证明自主默认和 lifecycle wake，但抓到 child
  越过直接 goal；fresh r17 又抓到 mandatory covers 使 Git 返工 child 错绑 GUI Todo。当前回归要求 child
  只以直接父级 goal 为边界，并支持可选 exact covers；fresh r18 再验证不扩做兄弟项、不拿无关 id 顶替。
- [x] 根默认 `system_prompt` 在持续执行纪律前包含 会话运行时 assumptions-first 软边界：安全可逆的次要选择采用
  合理默认并继续；只有任何假设都会实质偏离、越权或产生不可逆风险时才问一个短问题。YAML 与 dataclass
  逐字一致，文本不含项目名/语言专项，也不解析问句或写机器状态。
- [x] 发布 YAML 与 dataclass 的默认 system prompt 文本完全一致，不再因测试机省略配置而回落到 Go 专项
  骨架；主/子/wake 共用 会话运行时 式持续完成软纪律，且没有重新引入 final 解析、Todo 自动续轮或机器质量验收。
- [x] 同一 exact parent 下，系统生成的单个补派、批量补派和递归 child 名称沿历史 sibling 连续编号；
  显式自定义名称不改，机器身份仍只认 run_id。
- [ ] OPEN capability request 不能被普通 completed 收尾覆盖为 DONE；直属父级 grant/deny 后必须续跑
  同一 run，取消/接管终态不得复活。根、子、孙只能 guidance/cancel/resolve 自己的直属 child；
  模型 cancel 回执不得夹带整树状态，schema 不暴露 dry_run/kill_process 运维参数。
- [ ] 普通 child 逐层继承父级结构化 workspace 上界；`output_files` 只记录明确交付目标与验证线索，不能
  扩大父级权限。裸相对路径按可信 cwd 解析，只有显式 `output/...`、`work/...` 进入 task 内部目录；
  直接 child 即使省略 `output_files`，也必须继承 active conversation 的 host-validated cwd/runtime roots，
  不能退回 Gateway daemon 仓库。`/root` 启动的真实任务必须把 `abc/` 交付到 `/root/abc`。后台续跑必须
  绑定 exact task，不能复用同 thread 旧任务的 main run/attempt。
- [ ] child 的父级共享 read/search 预览只来自当前 tool loop archive；当前轮没有读取时不得复用上一任务
  agent cache，真实 execution context 不得出现无关旧 task 路径。
- [ ] local/unmanaged child 继承 workspace root 时不再被完全同路径的默认 home deny 误拦；
  更窄凭据/用户目录 deny 与远程 owner home 围栏必须保留。
- [ ] TUI `/stop` 在前台 turn 运行/提交时精确绑定 turn id；前台让出但当前 conversation
  仍有唯一 live background task 时也可停止，不得因 TUI 本地 `is_running=false` 拒绝发送。
- [ ] 既有同级/父子 child 的 `output_files/output_refs` 不产生文件所有权、持久 workspace 租约或动态
  `locked_files`；但同一次 `create_subagents.items` 主动提供的 `output_files` 若完全相同或互为祖先/子路径，
  必须整批 `not_started` 并返回冲突 item/path 供模型缩窄或分批。未声明写集不猜，越出父 workspace 仍拒绝。
- [ ] 直属 coding child、递归 leaf 与所有可写内置角色的工具快照必须含
  `write_file/edit_file/apply_patch`，并由同一 canonical 成员源驱动授权、写围栏、路径 gate 和进度投影；父级
  显式缺少 `edit_file` 时后代不得扩权。`apply_patch` 未命中回显有界 expected lines 且不写文件，部署后真
  TUI 需证明 worker 能实际调用 `edit_file`，不再因空格失配退回整文件覆盖循环。
- [ ] child 完成事件在后台轮开始时只采样一次；采样后才创建的 DONE wake 保持 pending 并另开新轮。
  新鲜终态轮的自然回复不等待 root task status 充当第二验收器。
- [ ] 单 Gateway 内后台车道按 `owner + durable thread_id` 隔离；同 thread 继续由 run claim
  单飞，同 owner 的另一条长 TUI/policy 回合不得阻塞 child 完成 wake。全局和单 owner
  并发上限必须可配置，超出保留持久队列而不丢失。
- [ ] 主 run/current attempt 为 `unknown` 时 wake、observation、policy 原样保留且零模型调用、零自动重挂；
  同线程新任务不被旧阻塞项占满 limit。人工核对恢复后原事件继续，Gateway 不再刷 loop error。
- [ ] Web 服务等长期命令只用 `run_command(run_in_background=true)` 启动并返回受管 session；shell `&` /
  `nohup ... &` 在执行前明确拒绝且可修正重试，最终必须从另一条命令核验 0.0.0.0 监听与局域网访问。
- [ ] 主代理、子代理、Gateway 与 TUI 对六类 `turn_end` 映射一致；普通完成不读取 acceptance/verification，
  历史兼容字段不进入当前 prompt、context bundle、父级摘要或启动前检查。
- [ ] TUI 在活动轮显示唯一的主代理工作状态和持续 thinking 增量；前台 turn 让出而 canonical active task
  仍非零时，正文末尾的 main `Working` 与输入框下的直属 child 状态/职责/耗时/实时上下文 token/Compact 继续更新，成功查询
  归零才收起，查询失败不误清零。用户位于
  页底时自动跟随，主动上翻后不抢滚动，回到底部后恢复跟随；Compact 显示 typed 百分比并在完成/失败后
  正确收口。
- [ ] 四个 `TESTS.md` 原样重型任务全部通过真实 `MiniMax-M2.7` TUI 顺序验收；每次启动、切换或输入前已先
  向用户报告测试对象、`192.0.2.7`、tmux session 名称和可直接 attach 的完整命令，且全程只有一个 Gateway。
- [x] fresh Prompt 4 r18 的后续派工复用 canonical Todo；只有事实确实匹配时才带 exact covers，额外返工
  可不绑定并显示真实 child 行。已关闭项返工若仍要映射，先以 `correction=true` 重开原 id，绝不拿下一个
  open 兄弟 id 顶替。可选 output hint 若提供则只落当前 cwd，不把模型预报当完整写集或权限。每名 child
  只完成直接父级 goal，不替兄弟扩做；root 保持原语言、完整范围和“主代理不得写功能代码”边界。r18
  未再出现 r17 的强制错绑；新的 5/8 假收口单列为 r19 同轮停止核对验收。
- [x] fresh Prompt 4 r19 在固定 lazygit commit 和唯一 Gateway 上只输入一次原样用户 prompt；测试者未改
  产物、未追加推动消息。5 名 child 全部自然 DONE，第五名上下文 113.8k 时真实 Compact 一次并继续；root
  执行 112 个生成测试，但独立产物 TUI 白屏，不能算完整复刻。root 在 final 前已主动关闭 8/8 Todo，故这
  一项只证明 r19 测试已完成，不冒充 open-Todo 停止钩子的直接真机证据。
- [ ] 下一条原样重型任务若自然留下 canonical open Todo，TUI/日志应证明模型在同一 active turn 收到 exact
  核对并继续，或将真实阻塞写为 blocked 后如实汇报；durable root 不能在 open Todo 下变成 DONE。不得
  人工篡改账本、追加技术提示或用玩具 prompt 专门诱发。r9 已作为修复前失败基线保留：13/16 关闭时
  后台 no-save 假完成；修复后样本必须与它分开记录。
- [ ] 真实测试中 main/child/grandchild 各自沿独立 `agent_thread_id` Compact 后能继续工作；至少一条 child
  链连续发生多代 generation，近期完整回合、工具事实、任务状态和产物引用不丢，且没有重做已经成功的
  副作用。持久 native IR 裁剪与 transcript 压缩都只推进该 thread generation；presentation/no-save 临时事件
  和旧 apply ledger 不计入。Compact provider 请求必须由真实任务 user 开头、synthetic 摘要 user 结尾；真机
  checkpoint summary 要包含任务、进展、路径和待办，不能只是最后工具动作的普通续写。child 使用工作工具时
  不得覆盖父 `conversation_thread_id` 或重绑父 conversation task。r19 已补到 child 单代正样本：113.8k
  触发后 `compact 0 -> 1`、39.3k 继续完成；main、孙代理和连续多代仍未因此提前勾选。
- [ ] 真实 IM 双用户验证 compact/memory/旧聊天检索不串 owner 或 chat，结束后恢复生产 compact 阈值。
- [ ] `/verbose on/full/off` 只改变当前 thread，不进入 transcript/guidance/模型；进度发送不触发任务重做，最终回复仍能送达。
- [ ] CLI 与真实 IM 的 `/status`、`/btw <内容>`、`/stop`、`/goal ...`、`/verbose ...` 都由统一系统命令入口处理；任何未知 `/XXXX` fail-closed，不进入普通队列、transcript 或模型。
- [ ] 已有 linked live turn 时 `/btw` 只注入该 turn、不发第二个 wake，也不泄漏到下一任务；`/stop` 不判断聊天/任务，直接按当前窗口的精确 request id 打断模型读取、清掉未消费 steer、停止子树，不停止 Gateway，也不影响其他用户会话。
- [ ] `/stop` 后 transcript、compact、memory 和 task workspace 保留；用户后续自然说“继续”时，模型用精确 task id 重开原现场，不创建第二个任务目录。
- [ ] 同一 TUI/IM thread 的普通任务自然 `completed` 后，下一条消息在首个
  `promotes_task` 工具处建立新 successor id，但必须继承同一 canonical task root；
  旧 link 保持终态，模型不得因为相对路径失败而搜索、复制旧产物到新空目录。
- [ ] `/goal` 每 thread 只允许一个未结束目标，pause/resume/edit/clear 保留正确任务身份；active goal 的 `/stop` 只暂停，complete/blocked 仅由精确 scoped 工具写入。
- [ ] `/audit` 只在显式前缀激活，guarantee/window 沿子代理结构化继承；普通 prompt、goal、summary 中的 `/audit` 文字不激活 watch 保证。
- [ ] 同一 Agent 的前台聊天和后台续跑并发时，prompt、request id、task workspace 和 tool-loop params 不串；已销毁 Agent 不留下可被 object-id 复用的旧状态。
- [ ] 远程 user/group owner 只能访问自己 home 与 `~/.my-agent/shared/`；其他 owner、根模板和旧顶层私有目录在 full mode 下也拒绝。
- [ ] 模型可自主决定子代理数量；本批/任务/owner/全局任一上限不足时整批拒绝，不静默截断或部分创建。
- [ ] 普通任务由模型自然收口；显式产物不存在时工具和 artifact refs 必须如实报告缺失，但宿主不得另建
  机器质量验收状态或用旧 verification 阻断模型结束。
- [ ] 输出目录符合当前 task workspace / 用户指定目录规则。
- [ ] 最终 Linux 容器运行 sandbox probe 退出 0；没有用 `privileged` 或宿主级 `SYS_ADMIN` 绕过。
- [x] `check_clean_package.py --mode worktree .` 已如实阻断 2171 项保留的未跟踪协作/运行文件；新建 wheel 和 sdist 均通过 artifact 模式。
- [ ] 默认容器安装的透明 `my-agent` 只挂当前 workspace 和持久 home；`--host` 没被误当生产路径。
