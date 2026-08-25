# TEST CHECKLIST

## 子代理 TUI 插话与容量

- [ ] 运行 child 的用户消息 receipt 带 exact `expected_turn_id`，reserve、provider submission 与 consume
  使用同一 attempt；不得再出现 `guidance submission reservation mismatch`。
- [ ] child 没有 pending/running attempt 时，入口明确拒绝、用户输入保留且 guidance message box 零新增。
- [ ] 默认 root 一次可原子创建 8 名 child，`per_call_cap=0`、session cap=8；第 9 名整批拒绝，终态释放后
  可继续创建，历史累计允许超过 8。
- [ ] root 与 child 在默认原生复制模式均可用 `PgUp/Ctrl+Home` 查看历史，常驻 footer 能看到 `F6` 滚轮提示。

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
- [ ] 未显式声明 `output_files/output_refs/artifact_refs` 的普通 child 不生成系统 Markdown 业务合同；父级
  只从 typed status、最终回复、`final_report_ref` 与真实 artifact refs 接收结果。旧
  `system_default_output_ref=true` 可恢复但不进入 runner contract、completion wake 或
  `child_result_index.expected_outputs`；显式产物仍走现有锚定、写授权和冲突锁。focused tests 完成后还须
  fresh r10 原样 Prompt 4 真机验证。
- [ ] child lifecycle wake 沿同一 root active turn 续接：exact task link 的原始 objective 仍是
  `User Task/root_user_prompt`，wake 仅作 runtime continuation；root canonical tool index 按 exact
  `run_id + task_id` 恢复并排除 child/其它 task，one-shot 派工不重放，每工作片新增工具额度不缩水。
  focused 回归与 fresh r12b 已证明语言、范围和嵌套派工参数不会在连续 wake 后重置。
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
  planned-delegation 预检：`covers/output_files` 都是可选结构化提示，不是权限、完整写集或创建前置条件。
  未绑定 child 正常创建并用真实 run id 记进度，不关闭现有 Todo；未知/关闭/重复的显式 covers，以及显式
  output 越出 workspace，都必须零创建并返回 typed repairs；可选输出的同批父子路径覆盖仍走现有冲突门。
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
- [ ] 同批、同级或父子 child 的 `output_files/output_refs` 重叠时仍可创建；这些字段不产生
  文件所有权、持久 workspace 租约或动态 `locked_files`。显式越出父级 workspace 仍须在创建前拒绝。
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
