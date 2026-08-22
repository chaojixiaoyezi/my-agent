# TEST CHECKLIST

- [x] 工具运行时改动相关 focused tests 通过（Schema/runtime/protocol/policy/executor/ledger/output/concurrency/cancel/compact 矩阵）；其他并行模块仍按各自条目验收。
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
  covers 继续可见并按 canonical child status 打标。
  main 等待 child 时按 typed status 显示“等待 N 个子代理”，最终答复进入普通 assistant transcript；
  `7c052f2` 第 1 条真机任务已暴露布局/Todo 问题，新候选 147 项定向回归已过，仍需 `.7`
  单 Gateway + 下一条原样长任务 TUI 验收后勾选。
- [x] `.7` 单 Gateway 真实 TUI 已观察两个 child 从等待启动推进到一次 attempt `DONE`，固定活动区展示
  状态、动作和耗时，真实文件内容正确；测试者未向主代理或 child 发送推动消息。
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
- [ ] `task_progress` 仅为软账本：open 项不触发普通任务自动续跑、不阻止自然 final；新项要求稳定
  `id/title/status`，模型旧 pending 不能覆盖 canonical child DONE。
- [ ] OPEN capability request 不能被普通 completed 收尾覆盖为 DONE；直属父级 grant/deny 后必须续跑
  同一 run，取消/接管终态不得复活。根、子、孙只能 guidance/cancel/resolve 自己的直属 child；
  模型 cancel 回执不得夹带整树状态，schema 不暴露 dry_run/kill_process 运维参数。
- [ ] 普通 child 逐层继承父级结构化 workspace 上界；`output_files` 记录明确交付目标与冲突锁，但不能
  扩大父级权限。裸相对路径按可信 cwd 解析，只有显式 `output/...`、`work/...` 进入 task 内部目录；
  `/root` 启动的真实任务必须把 `abc/` 交付到 `/root/abc`。后台续跑必须绑定 exact task，不能复用同
  thread 旧任务的 main run/attempt。
- [ ] child 的父级共享 read/search 预览只来自当前 tool loop archive；当前轮没有读取时不得复用上一任务
  agent cache，真实 execution context 不得出现无关旧 task 路径。
- [ ] local/unmanaged child 继承 workspace root 时不再被完全同路径的默认 home deny 误拦；
  更窄凭据/用户目录 deny 与远程 owner home 围栏必须保留。
- [ ] TUI `/stop` 在前台 turn 运行/提交时精确绑定 turn id；前台让出但当前 conversation
  仍有唯一 live background task 时也可停止，不得因 TUI 本地 `is_running=false` 拒绝发送。
- [ ] 同批 child 的 `output_files/output_refs` 互相重叠时，整批拒绝且要求重分路径重试；
  不得误报为等待既有 run。只有结构化 `existing_run_id` 非空时才等直属生命周期事件，
  两种回执都不得改变用户原始约束。
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
- [ ] 真实测试中主代理和独立子代理 compact 后能继续工作；至少一条链连续发生多代 compact，近期完整回合、工具事实、任务状态和产物引用不丢，且没有重做已经成功的副作用。
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
