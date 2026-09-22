# LLM_GUIDE

这份文档是给 LLM/AI 开发者读的项目入口指南。

**开工前必须读，收工后必须改。**

---

## 铁律

1. **开工前**：读 `docs/ROADMAP.md`，确认你要做的事在列表里，了解它的"解决问题"和当前状态。
2. **收工后**：更新 `docs/ROADMAP.md`（改状态或删除已完成条目），把已落地的功能写入 `docs/COMPLETED.md`。
3. **每个功能必须写"解决问题"**：不能只罗列模块名和 workflow 名，必须说明它解决哪个用户痛点、系统风险或交付缺口。
4. **不改设计文档就不能改设计**：如果实现方向与 DESIGN_LEDGER.md 有冲突，先更新设计文档再写代码。
5. **每次汇报必须带"建议下一步"**：父代理、子代理、其他线程和交接文档都要说明推荐后续动作、并行边界和风险守门点。
6. **自然语言不当机器事实源**：用户 prompt、模型 summary、报告正文、guidance、角色描述只能做沟通和软引导；状态、权限、验收、恢复、派工和产物归属必须来自结构化字段、refs、工具结果、显式配置或文件系统事实。
7. **主链路优先，兜底最后**：主链路没跑顺前不要加 fallback、旧路径兼容、影子入口、只转发 facade 或“还能跑”的旁路；确定不用的旧兼容要删。
8. **复用已有底层协议**：会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导统一使用本项目既有 owner/thread/task 事实源；长期记忆、人格、多用户调度和 IM 适配各守模块边界，不为同一概念再建一套状态机。

---

## 当前运行边界（验收状态看 STATUS）

第 4 步本地源码已接通安装、配置、实际启用、普通工具组合、停用释放、重新启用及卸载。
卸载使用释放返回的完整记录做原锁 CAS；原成功结果持久化并严格读回后，才消费退出证明和回收无人引用的旧包。
原准备仍在执行时保留安装与环境；同包重装保留其包，旧请求不控制新安装，UNKNOWN 不因后来清理成功被改写。
公开目录 v3 派生原提交安装引用，避免同包卸载重装后旧目录再次有效；安装表和原操作历史不新增权威副本。
显式 `/plugins@插件ID` 已本地接到原 HostCommand/ToolExecutor/MCP；工具参数和宿主选择分别绑定，业务用户不借管理员资格。
审批在原执行区间等待，明确批准一次后复查固定代次；重复提交只读原结果，单次连接退出与业务结果分别报告。
TUI/Gateway 的交互审批运输及完整多 TUI 装卸仍待完成；本片未推送部署、未新增实际 TUI，不进入第 5 步。

本地实际 `/plugins enable` 复用原 HostCommand/ToolExecutor：计划先领取，准备环境，完整核对候选 MCP 目录，确认候选退出后发布 active。
新运行由原 Registry 按 core 注入的可信 owner 接入同代工具；各权限视图独立投影，构造和可用性检查不启动插件。
私有设置只经子进程环境交付，插件自述 effect 不降低原审批；关闭与迟到登记双向复查，旧代理不转投新连接。
完整 session 证明保存在原 termination.cleanup，迟到 host 不能擦除；命令退出状态、退出码和 child 回执保持。
旧连接或排队拒绝的未发送事实进入原操作账，真正已发送或缺失退出证明的 UNKNOWN 不改报成功；见 [启用组合](docs/design/PLUGIN_ACTIVATION.md)。

本地 `/plugins disable` 已接原 HostCommand/ToolExecutor：先撤销安装表中的固定代次，再关闭原 enable attempt。
原准备任务资源与共享 MCP 按各自身份冻结，锁外清理；准备清理明确包含终态记录以核对仍可能存活的 host。
清理未确认保持 UNKNOWN；未释放的原激活保持 revoked。释放/消费和重新启用见本节首段，卸载、显式业务命令及实际多 TUI 尚未完成。
原 operation 引用只按可信 owner 精确反查首次管理链，资源核对按原集合语义，不通过列表顺序或当前 attempt 猜身份。

原 host 已有显式 stdio 通道，三路字节直接继承给 child；日志模式默认不变，stdio 绑定 launcher 寿命。
启动信封当前 v5，session 当前 v4；旧 v2/v3 原版本更新/恢复。共享激活排除业务查询、任务停止和普通终态裁剪。
固定激活已接原托管 MCP 启动与实际发送；字段本身仍不授予执行权。完整管理装卸未开放，见 [托管管道](docs/design/MANAGED_PROCESS_STDIO.md)。

安装表本地源码已升 v3：同一原计划预留、发布和撤销，旧代读回不换绑；配置和重新准备须等原激活清理确认。
撤销只关闭原表中的执行权，不等待准备阶段的 quota，不表示 OS 资源已退出；原插件锁与提交读回保持。
显式 v1/v2 迁移、stdio 资源归属与启停接线见 [激活权威](docs/design/PLUGIN_ACTIVATION.md)，完整装卸尚未发布验收。

MCP 本地源码已区分临时 disconnect 与永久 stop；每次请求和目录发现固定 transport，关闭后不能经 prepare 重连。
原进程树清理未确认不替换连接；可用性不提前回收组长，读线程自行关闭管道。连接合同见 [MCP 生命周期](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。
这不代替唯一插件安装表的持久激活/撤销和真实 TUI 装卸验收。
模型工作片冻结权限时直接读取 `user_space/owner_access.py`，不得从 core 恢复旧私有 helper；主任务与继承子代理的目录墙仍按原裁决。

长等待、后台进程完成通知与缓存诊断复用既有进程/会话账本，见 `docs/design/LONG_RUNNING_EXECUTION.md`。
子代理明确停止按固定执行归属清理后台/PTY；独立 runner 复用原心跳转交精确 attempt 中断。
宿主可能启动父级接续或新轮，launch/PID/出生标识不能证明整棵 OS 树独占；宿主退出单独只读核对。
runner 的未完成不等于失败；显式结束原因与当前状态、成功标志一致且无明确失败时，清当前错误投影，保留原尝试历史。
后台收尾看当前子树及未读邮箱，不用工作片开始时的旧阶段永久抑制最终回复；编辑匹配冲突要求读回文件。
后台本地缺模型等待该会话配置恢复，不按固定时间热重试；普通错误仍走原冷却，进程内策略不拥有持久状态。
后台进程持久地址读取宿主冻结的 canonical owner home，不能随 Full Access 的路径墙变化而迁移。
后台主/子工具审批共用 `agent_tool_approval.py` 的原账本和 TUI FIFO；归属校验读 `tool_approval_scope.py`。
子代理身份先读 canonical run；父会话任务关联只是展示投影，不能把孩子或孙代理误当成主代理 claim。
此修复已发布并同版部署双机，实际子/孙审批与原调用接续已验；报告质量和长任务失败仍见 STATUS。
同参拒绝携带已发布部署：宿主列表跨同一 child attempt 的 Goal 续轮和活动回合 Compact 传递，批准不随之扩大。
实际 TUI 70 已验证 Goal 续轮同参不再弹窗；其后嵌套 Shell 后台启动与前台超时遗留进程已发布并同版部署双机，实际 TUI 78/79 复现及 TUI 75 普通对照已验。
Shell 字面程序语法归 shell_syntax.py；前台管道结束前保留组长，终止仍沿 process_registry 核对出生标识与独立组成员。
Shell 回执、截断说明与展示行数共用采集正文的 LF 口径；末尾换行不额外算一行，原始输出和采集完整性保持独立。修复已同版部署，实际 TUI 80/82 的回执与展示已验；模型报告错误仍见 STATUS。
无生产调用的旧 verifier integrity 模块已删除；验证事实入口统一读 verification/runtime.py，文件完整性矩阵不证明报告正确。
旧 `structured-repair` 诊断场景已从源码移除，runner 自然结束不再解析结果块；不得恢复专用 JSON 修复回合。相邻重试场景现归 runner_retry_case.py，既有验收失败仍保留。
main 绑定当前选定任务的有效 claim；换轮失效，Goal paused 不取消当前审批，不把接收方续租当批准。
同任务续做先绑定 canonical run/attempt 再准备归档，request 只代表当前消息；目录准备失败关闭本次新 attempt。
普通 Shell 与交互 PTY 共用 `parse_shell_command`；删除解析入口时必须同时核对两条执行链。
资源访问身份和执行归属分别定义在 `tooling/process_scope.py`；PTY 已直接使用，不能拿访问回退补齐任务身份。
源码中的 direct/local 中断只停止精确回合，保留插话及独立资源；任务资源停止已接主绑定和固定子树，发布与实际验收见 STATUS。
后台记录 Store 当前 v3 区分任务与共享激活，旧 v2 原版本读改写；原 CAS、同目录 redo 与公共 directory_lock 保持，锁名和顺序不变。
读写与裁剪先恢复固定批次；提交后异常不能当作未发生。源码启动已沿唯一 v2 预留、host/child 绑定和交接链；
启动方只在交接前持有取消权，独立 host 持续限制日志。Registry 每次读取原 Store，明确 session 停止消费冻结实例。
执行权关闭共用 `runtime_db/run_cancellation.py`，取消 UNKNOWN 保留原锁，pending 条件在同一事务核对。
Gateway 持久主任务沿 Goal→task→请求锁关闭原权限并冻结主资源；后台旧片在创建 attempt 前检查中断，不能被恢复复活。
`conversation/task_resources.py` 组合主绑定及固定子树，`subagents/cancellation.py` 关闭原子权限并准备清单；
`tooling/process_resource_stop.py` 只清理冻结实例，后台确认、PTY 异步请求和 runner 退出分开。
direct/local 的消息句柄由 worker 与命令端共享，core 在模型前发布实际身份；Compact 在原任务锁内重新核对，旧句柄不控制新 job。
无持久任务热请求先在原 T 锁关闭发布并读取绑定，再释放 T、取 task 锁；已晋升或身份不可读不能降级猜测清理。
整树清单在原 Goal/task/creation 锁内固定，异步清理不再查询 agent 或当前孩子；DONE/FAILED/UNKNOWN 保留业务历史。
已发布同版双机，实际 TUI 138—142 分项验证主后台、并行孩子、PTY、孙代理和另一会话隔离；137 原失败保留。
子代理创建、换轮、旧轮放弃和控制预留沿原 creation guard；`subagents/coordination.py` 只管理同线程嵌套持锁。
guidance 的 admission 在 creation 之外，启动探测与执行放 creation 锁外。
managed 后台排队绑定原 pending ID，经宿主 argv、DispatchParams 和 runner 到 DB 精确激活；缺失不补 current。
启动标记由原 lifecycle 服务执行条件 mutation，runner_start.py 只负责准入与核对；CLI 不越层导入领域实现。worker 先激活再发布携带 attempt 的 session，旧心跳不能覆盖新轮。
旧配置投影也须在激活后核对原 attempt 再写；创建回执复读 canonical，不能依赖控制函数原地修改旧对象。
插话重放按排序 turn→批次修复→回执 fresh pending→准确 DB 预留，仅接续本条消息；源码竞态与实际覆盖分别记录。
无数据库模式已补原 canonical 启动接纳：非空 ID 绑定唯一 launch，消费与 RUNNING 同次 mutation，旧保存不能回滚。
停止也撤销业务终态上的新预留，显式恢复重新领取；普通保存只可回收同一准确身份。累计源码全仓、严格 gate 与安装版所测控制链通过；完整计数和未实测旁支见 TESTS。

SSE delta 原样保留，OpenAI 工具参数生成有独立进度；
派工不会永久禁止主代理本地工作，用户明确限制仍保留；复制按最新代次和实际通道结果反馈。
工具日志预览不代替有界读取正文，文件尾部、版本和分页参数须完整送入下一轮；
Shell 不设人为命令字符上限，安全、权限、时间和输出预算仍沿原合同执行。

普通 assistant 的原生思考也须跨轮回放，
成功响应仅有思考也不是空传输；零工具有界续跑前先追加原生内容，不能丢失再重采样。
请求拒绝不等同密钥错误；前台/背景展示交接只按宿主 request ID，不按相同文本删除消息。
长选区的 OSC 52 长度预算不影响 native/tmux stdin；未知上游 400 不以轮换 session header 自动重试。
内部 runtime_fact 同样区分请求拒绝与配置错误；typed 不可重试事实不能被错误正文覆盖。全选与复制绑定当前视口。

本入口只保留现行规则与导航；当前设计、已实现内容和开放问题分别见 `DESIGN_LEDGER.md`、
`docs/COMPLETED.md` 和 `STATUS.md`。旧轮次的测试结果不替代当前发布验收。
插件参数已共用不可变声明、词法、绑定、帮助与补全，核心自由正文保持原协议；参数与补全修复已同版部署，实际验收及模型质量边界见 STATUS。
自动补全不能在完整命令后擅自追加可选旗标；显式 Tab 负责进一步发现，接受候选与 Enter 提交分开。
插件命令错误和静态帮助在 CLI/HTTP 入口结束，不能进入旧控制执行器或普通模型队列；完整装卸仍未发布。
显式管理请求的本地源码已沿原 RuntimeDB 原子登记独立 pending 运行；请求重送不换代，终态只读原操作。
创建树统一归 `runtime_db/run_creation.py`，冻结绑定读 `host_commands.py`；本地管理执行与只读查询由 `host_command_execution.py` 接原执行器，见 [宿主命令合同](docs/design/HOST_COMMAND_EXECUTION.md)。
本地 HTTP/direct 安装复用原管理授权、路径权限和配额，只保存默认停用包；后续启停组合见首段，完整装卸与实际多 TUI 仍待完成。
独立环境内部准备器已本地实现，见 `plugin_environment.py`：固定地址、原配额非阻塞准入、本地 wheel 闭包和原取消链。
环境准备已改为原 operation 的固定计划与原 ProcessSessionStore：计划先领取，候选后写入，运行中核对原 holder/代数/锁。
准备进程显式绑定 launcher 寿命与同一 monotonic 期限；普通长期后台默认不变，当前启动信封 v5 要求同版模式、寿命和激活字段。
创建 venv 不代表启用接线完成；禁止扫描目录代替原安装权威，禁止在启用前运行已装插件的 Python 自检。
配置已在本地接到原管理执行链：`/plugins configure <插件> --file <JSON>` 完整替换停用插件设置，使用原 schema 校验，不补默认值。
唯一安装表当前 v3 保存配置、激活、版本及回执；旧 v1/v2 在实际修改时显式迁移，查询只读。目录 v3 携带安装版本与原提交派生引用，私有值不进入公开目录或工具账。
新目录拒绝旧协议，客户端和 Gateway 发布时必须同版；目录本身不是执行权或资源清理证明。
`enable_plugins=false` 拒绝新安装，原管理员查询不受开关或来源文件消失影响；断连保留原请求和未知结果，不能自动重送。
未启动占位取消保留原输入、幂等与资源声明，损坏结果原文不洗成空账；严格回读拒绝损坏或未知版本，源码验证与已部署停止验收分开。
宿主目录与提交版本已发布同版双机：`plugin_command_catalog.v1` 只是只读声明，原 owner 解析仍唯一；无冷用户初始化。
TUI 自动补全只读缓存，显式 Tab/命令才请求宿主；首次选择的版本在参数补全后也不能被刷新覆盖，过期不自动重放。
Gateway 模式须读宿主目录，plain 持有完整 Agent 也不能回退本地；发布及实际 TUI 进度看 STATUS。

模块结构调整及 Jev 试验先读 [可维护性评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)：
后端、后台工具策略、上下文、历史准备、单片执行、提交/投递、Gateway 和存储已按职责归位，验收及剩余质量问题见 STATUS。

Jev 的最新产品方向与独立开发顺序读 [可选决策模型计划](docs/design/DECISION_MODEL_INTEGRATION.md)，P1—P5 已授权独立实施；
本地 P1-A/B 已有原模型目录 v3、decision 用途、生成隔离和原生 decide 适配器；完整清单见 [执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)。
严格 HTTP 与协议联合 216 项本地验证通过；可选调用的有界等待/准入/取消/账本、设置及业务消费尚未接通。
决策 TUI 用量按用户约定只增加输入 token、输出预留、不显示价格，不能新增独立统计账。
默认关闭、短总期限、失败沿原流程、配置/账本复用，不加入当前插件重构 Goal，也不改变普通主模型慢流合同。
时间可调，用户设置与 agent 按用户指令代操作共用原配置服务；首版即支持开关、读回和作用范围，不依赖决策服务在线。
Computer Use 的依赖与当前桌面验收须单独确认。
后续结构整理按可维护性评估中的“下一轮结构整理顺序（待实施）”执行合并计划：先明确依赖与后台调度边界，
再经公共命令、最小装卸、TUI 使用、并发故障验收贯通插件，之后继续子代理、工具循环和剩余 TUI。
Audit/摄取重构已移出本轮；新增第 10 步制作约 10 个自有简易插件并做组合验收，详见 [样本计划](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)。
TUI 插件只提交声明式展示，读取有作用域的快照/订阅；所有订阅随停用撤销，不直接执行插件 Python。
后台供应退避切片已验；唤醒/Goal 路由已分离并同版部署，353 项相关回归及 TUI 120—123 所测框架链路通过。长 Goal 暂停续采/Compact、并行普通任务、三子代理及定时投递已验，模型交付失败与未覆盖分路单列；租约/恢复已发布部署，324 项回归和独立复核通过；稳定资源换代误标脏的既有缺陷也已修复并同包部署，186 项相关回归通过。TUI 130 的原请求有序恢复、133 的取消后续代保留 STABLE、原终端复用与退出已验，第 2 步本轮框架范围收口；交付失败和实际覆盖边界见 STATUS。插件详细合同读 [可装卸插件方案](docs/design/PLUGIN_LIFECYCLE.md)。
当前基线与通用修复已发布并同版部署；用户已确认框架功能与模型交付质量分开记录，第 1 步框架基线收口，第 2 步进度策略首片已验，供应状态次片已迁入 `background_supply_backoff.py` 并部署。TUI 114—119 覆盖正常链路、定时工作的真实连接失败与自动续接、冷却期间另一前台任务及原配置回切。故障仅作用于专用本地代理，系统网络未改；完整覆盖边界见 TESTS。进度策略内部真机未命中和模型交付失败保留，框架缺陷、必测覆盖缺失或未明归因仍阻断相关步骤。
插件接线核对见 [迁移约束](docs/design/PLUGIN_LIFECYCLE.md#第-1-步接线核对与迁移约束)：沿已有 handler 快照与执行器补生命周期边界，不能把目录删除或冻结 availability 当成撤销；该能力尚未实现。
前版 TUI 84/85 再验仍有生成程序和模型判断失败，TUI 86 普通对照通过；退出原因以实际宿主回执/日志为据，不能从孩子 DONE 或模型猜测推断程序被回收。
子代理完成事件的声明窗口事实经中性合同传给活动回合和后台完成清单；只保留发布时数值，不改终态或据此重派。已同版部署，TUI 89 的后台原生上下文保留原值与终态；本轮完整验收见 STATUS。
首批插件支持显式本地包与只读示例，隔离和撤销必须完整；在线安装、更新/回退另批实施，不能将待做动作显示为可用。
其中候选功能及 slash 名称只用于说明插件内容，不能当作现有命令或已验能力。
社区插件调查只核对公开说明与包声明，运行兼容性未验；DSH Web/Cordis 插件不能视作可直接安装的 Python/TUI 扩展。
当前建设自有插件接口，外部项目只参考交互机制；安装后的配置、启用、发现、调用和撤销须一起设计，不做 DSH 兼容层。
插件入口统一为 `/plugins` 管理与 `/plugins@插件ID` 调用；公共 `command_catalog.py` 已统一名称、别名和核心尾部语法，参数声明驱动绑定、帮助与补全。帮助及错误在公共入口结束，不进入模型或旧停止分支；宿主 owner 目录与提交版本已接通，实际插件贡献及装卸未开放，验收边界看 STATUS。
后台目标状态处理读 `background_goal.py`，只依赖原 Goal/任务/时钟领域和精确能力；地址选择读 `background_routing.py`，只接收线程、owner 路径及属性的只读回调。来源消费、执行租约和能力预扫仍由 runtime 编排，旧 GoalMixin 和旧路由方法不再保留。
后台执行 claim 的领取、最终准入与结算现读 `background_claim.py`，每次权威恢复检查读 `background_recovery.py`；runtime 绑定原状态查询、来源退休和失败记账能力。共享心跳仍在 `run_claim.py`，三种租约不合并；已同包部署双机，恢复与竞争的实际覆盖见 TESTS。
第 3 步公共命令、参数、资源停止和宿主目录均已发布部署，本轮框架范围收口，实际分项复验及模型交付失败见 TESTS；第 4 步静态包与安装事实已进入本地开发，第 5—10 步待做。现有启动插件不代表已经支持 TUI 热装卸。
本地提交复核与自动化测试收口见评估文档的“本地提交前复核”；不要将定向重跑写成全仓再次通过。
后端公共合同读 `backends/base.py`，传输读 `http.py`，协议读 `openai_chat.py` / `anthropic.py` / `responses.py`，
构造及缺配置判据读 `factory.py`；旧 base 文件不再承载协议实现。
三种协议的探针诊断与 HTTP/OAuth 传输共用 `provider_headers.endpoint_parts`；保留代理前缀和完整接口，不能重复追加路径。
后台有界投影读 `conversation/background_context.py`，原生历史种子读 `background_history_seed.py`；
后者与负责展示快照的 `background_history.py` 职责不同。准备模块不拥有调度或投递，读取错误仍显式失败。
单工作片执行读 `background_execution.py`，每次 Compact 后按最新线程准备参数，沿原 store 保存原生历史；
执行结果用具名字段交接，模型作用域仍由准备层冻结。技术续跑只读判据统一在 `turn_end.py`。
后台交付读 `background_delivery.py`：外发、canonical 追加和整封冻结独立于调度器；
任务状态在原抑制位置通过只读回调查询，重投沿原身份与元数据，不再次调用模型。
Gateway 流式出口读 `gateway_parts/stream_writer.py`，公开投影和审批交互分别由 `stream_events.py`、
`stream_approval.py` 承担；恢复直接向原队列 chunk 写终态。Compact 携带读 `conversation/compact_carry.py`，
只有纯计算共用，mailbox 释放仍在原运行器。本次独立重构 Goal 不包含 auth 与 Jev，Gateway 与存储组合均已落地。
Gateway 请求编排读 `request_execution.py`，上下文准备、持久绑定、历史提交与输入渲染分别读
`request_context.py`、`request_binding.py`、`request_history.py`、`request_prompt.py`；旧导入不留转发。
前后台完整行窗口统一在 `conversation/history_projection.py`，保留 metadata 与同一窗口，不用展示摘要代替原生历史。
模块搬移须联测上下文、Compact、repair、停止和重启恢复；锁、持久 schema、路径和原提交顺序不随模块迁移。
存储用量访问 `store.model_usage`，实现读 `conversation/store_usage.py`；通过原线程原子更新保存显示，账本仍只存原事件。
Goal 计时访问 `store.goal_clock`，共享对象持有操作与基线；JSONL 原语读 `store_io.py`，旧方法与导入不留转发。
线程、消息、任务关联与 Audit 分别访问 `store.threads` / `messages` / `tasks` / `audits`，
实现见 `store_threads.py`、`store_messages.py`、`store_tasks.py`、`store_audits.py`。
线程组件持有新会话默认模型解析器；消息只依赖线程校验和原子更新；任务终态显式关闭进度策略。
Audit 共用任务账本、命名锁和任务锁；原 TaskStore 的自由函数转发方法已删除。
插话通过 `store.guidance` 入队、认领和读取；回执规则读 `store_guidance_records.py`，
账本与投影读 `store_guidance_ledger.py`，模型提交与消费确认分别读 `store_guidance_submission.py`、
`store_guidance_acknowledgements.py`，终态及失效尝试恢复读 `store_guidance_recovery.py`。
调用方直接使用 `guidance.ledger` / `submissions` / `acknowledgements` / `recovery`，不保留旧 Store 方法。
聚合 Store 已无领域继承；Gateway 外锁、回合锁、回执锁及批次先提交后修投影的顺序保持，新版验收见 STATUS。
公共路径通过 `store.storage` 访问，实现见 `store_layout.py`；读取侧投影在 `storage.indexes`，实现见 `store_index.py`。
根目录、原子更新目标和索引仍沿同一原文件；旧基础类已删除，路径方法与旧字段不留转发，唤醒回执归唤醒领域。
执行归属通过 `store.claims` 的领取、读取、续租和终态接口访问；实现读 `store_claims.py`，恢复绑定和租约 TTL 不随模块迁移。
目标、观察、唤醒和进度分别通过 `store.goals` / `observations` / `wakes` / `progress` 访问，
对应实现为 `store_goals.py`、`store_observations.py`、`store_wakes.py`、`store_progress.py`；旧 Store 方法不留转发。
Goal 显式接收原共享时钟；唤醒接收观察确认能力，发布顺序不能拆开。旧账 GC 仍在组装入口先策略后租约。
通用 JSON 对象读取及尽力删除原语归 `store_io.py`；错误口径与原持久迁移规则保持。
首次 `/goal` 沿控制回执传递 workspace，复用 `gateway_parts/workspace_scope.py` 校验后仅初始化空 cwd；
已有线程目录不被控制命令重定向。显式目录签入回执 v3 摘要，旧版不得携带未签名目录。

- 每台机器一个 Gateway，多个 TUI 是独立客户端/会话。wheel 使用 non-editable 独立 runtime，
  Gateway 与默认 TUI 入口必须同版；检查 executable、module.__file__、安装位置和实际配置，保留回滚。
- 常规真实模型验收默认官网 MiniMax-M2.7；检查实际 provider/端点，不按同名模型推断。
  本轮视觉任务授权使用官方 MiniMax-M3，复用 M2.7 私有密钥引用并切模型名；真实图片输入仍从 TUI 进入，日常默认模型不静默改动。
  用户明确指定的其它协议/模型单独验证；私有凭据不进仓库、不输出，不静默切用户日常模型。
  新增四组对照已授权：my-agent 的官方 M2.7/OpenCode Flash，加 Codex、Free-Code 的官方 M2.7；
  四组真实调用可用且本轮对照已收口；my-agent 两模型同题短任务通过，Flash 核心长题通过但报告有保留项，M2.7 长题在三个框架均未全过。用户确认分开记录后 Goal 恢复 active；框架通过不改写交付失败，新切片继续做多 TUI 和长任务验收。
  控制变量、准备失败和验收边界见 TESTS.md，不据短题关闭长任务失败或仅按胜负归因。
- 产品名与发布库是 my-agent；品牌清理不搬迁检出、owner home、真实 tmux 或任务数据。
- `/model` 管理 owner 私有 provider/model v2；获取目录、短测必须显式操作。保存/编辑不调用模型。
  软件不预填模型/协议/端点；未配置仍可打开设置，真实调用明确提示 `/model`，不自动回退。
  新工作片冻结模型/端点/密钥/容量/请求头整组配置，子孙继承创建时引用，显式 model 只解析本 owner 配置。
  Auth 支持显式订阅设备码登录与通用 OAuth 参数；令牌仍在 owner 私有 provider，不能跨用户共享。
  Embedding 只管理目录用途，不能选作主子模型。详见 `docs/design/TUI_MODEL_PROFILES.md`、`docs/design/MODEL_OAUTH.md`。
- `/permissions` / F4 使用 owner tool_policy.json 的 ask、auto、full-access；Full Access 仅可信管理员。
  工具执行线程继承工作片 Context；角色、工具可用性、Full Access 都不增加用户未要求的工作目标。
  capability grant 与具体工具批准是不同事实；SOUL 专用确认、owner 墙、灾难保护仍有效。
- 普通主子代理默认在 canonical owner home 工作；tasks/output/work 只作整理建议，不形成目录锁。
  执行 cwd 与内部 owner_runs_dir 分离，后台恢复不能继承 daemon 的 cwd。用户明确外部 cwd 仍经权限门。
  文件整理/命名共用 home_context_enabled 与 workspace_task_path_template 软提示，不靠标题猜运行身份。
- 每个 owner/thread 只有一份 canonical transcript；task link、child wake、展示投影不得建立第二份模型历史。
  本地 TUI 展示通道与 owner provider 分开；宿主路径保留时，后续内部编号遮蔽也不能改写路径片段。
  新消息只追加，普通轮不改旧缓存前缀。任务切换不删除历史/记忆，真正摘要替换只在 Compact。
  详见 `docs/design/CONVERSATION_CONTEXT_DESIGN.md`。
- main/child/grandchild 各自用 ConversationThread 的 checkpoint + generation CAS 提交 Compact。
  失败/停止不推进代次，不把普通窗口化计成压缩。源历史/精确工具账保留；已退休前缀只能随已提交摘要回收。
  空摘要、失败熔断、重放和取消以当前实现与该模块文档为准，不新增旁路摘要或独立计数。
- Context 是本线程模型 preflight 压力，不是累计计费。压缩开始的实际窗口由压缩模块提供，
  先保存同代数值再通知 TUI；成功提交清旧数字。显示遥测、计数和进度不得进入模型输入/缓存前缀。
  ModelCallLedger 是成本唯一权威；缓存命中以 provider usage 为准，不能用估算或比例推测账单。
  TUI 统计条的本次模型轮、当轮工具、最近缓存与当前代理会话累计各有独立口径，见 `docs/design/TUI_DESIGN.md`。
  `ConversationThread.model_metrics` 仅为有界显示副本；完整/精简模型上下文均排除它，不能据此调度、判断完成或收费。
  成功、异常和取消共用结算；`usage_scope_id` 是累计容器代次，source 切换不重计，重启/重建另计；旧账不静默改写。
- 大窗口切小窗口用有预算的连续分段摘要；覆盖所有来源后才提交，typed overflow 可缩小请求，
  网络/认证错误不伪装成超窗。observed_tool_paths 仅是有界查找提示，不是权限或文件存在证据。
  能容纳的单次请求保留原缓存面；分段采用无执行工具的摘要角色。原文锚点在固定预算内优先保留用户原话，
  助手长结论不能挤掉短用户要求；超预算仍有明确省略，原 transcript 不删除，不承诺摘要无损。
- 原生请求保持稳定 system/tools、已提交摘要/历史、当前 user、append-only native IR、动态事实尾部顺序。
  一个 user 只出现一次；模型/工具快照必须与真实请求一致。Provider system 是授权/验证软指导的唯一正文，
  不能逐工具重复长规则。实际 native Schema 必须与冻结工具快照相同。
  中断、异常和后台静默让出都保留本轮原生历史，空正文事实不冒充公开回复；后台每片有独立宿主回合号，
  原生信封与公开 final 同号去重，外发重投沿原冻结身份。缺工具结果只标记效果未知，不从屏幕补造事实。
- 连接/首事件/流间隔超时分开，健康慢流没有隐式总墙钟；停止贯穿请求和退避。
  响应头等待被取消时，公共 open 边界按结构化中断收口连接清理异常；无中断仍抛原错，不能增加取消重试。
  TUI 已入队的持久请求沿原回执等待，Gateway 瞬时重启不能被前端误报成该任务未执行；不自动重发。
  Gateway chat 客户端的不活跃等待使用单调时钟，系统校时不能使活跃请求提前超时；只有本请求活动可续租。
  每模型可显式设置 `model_queue_wait_seconds`，默认 0，只额外延长首事件等待，不增加流静默或输出预算。
  明确 HTTP 400 不因缺少解释自动重试；429、5xx、网络瞬断仍走 typed 有界恢复。
- 采样 top_p 是可选配置：普通端点默认省略，模型级覆盖与连接同快照、同缓存键。
  三种接口的温度统一由显式开关控制，默认不覆盖提供方；单模型填写温度自动启用，请求级显式覆盖保留。
  已核对的精确官方/工具运行时 V4 Flash Chat 采用 0.95；用户显式温度不被删除。
  不按任意模型名或代理猜默认，不以调整采样代替 400 根因诊断，详见 `docs/design/TUI_MODEL_PROFILES.md`。
- 工具操作身份、owner、run/task/parent/root 和副作用结果均读取结构化事实。
  原生轮次只新增本批执行事实，不反复复制近期账本；全轮核验与原始工具历史保留，旧缓存前缀不改。
  成功重复按完整原始结果摘要观察并定间隔软提醒；后台查询另用宿主稳定进展摘要，不把耗时当进展。
  该摘要不能参与动作拒绝、任意 Python 的只读分类或任务完成裁决；沉默的进程不因此被强杀。
  普通后台记忆策展避让本 Gateway 的同端点活动工作片，pending/游标保留；pre_compact 不互等。
  后台请求预算必须传到底层，超时取消旧传输，旧线程尚未退出不叠加重试。
  重复门自身的未执行拒绝不是新结果，不清空或挤掉原观测；完整拒绝仍归档并返回模型。
  拒绝正文携带原计数和换路建议，不新增完成门或慢流时限；非文本读取说明能力边界，不自动转图。
  明确零副作用失败可交模型修参；部分写入/执行效果未知仍保持 UNKNOWN，不自动重放。
  Shell/controlled_exec 是非交互批处理（stdin=DEVNULL），交互用独立 PTY；后台句柄不是 PTY 句柄。
- 普通子代理直接创建并自动运行；角色/权限快照决定是否可递归，不能解析 goal 扩权。
  正常进度靠直属生命周期事件；list_agents 只供按需查看，不轮询或手工推动。
  list_agents 从同一快照提供紧凑状态与 read_order；不暴露内部恢复路径，长结果用原归档逻辑引用恢复。
  创建不自动让出；主子孙均可继续独立工作，任一新结果可交给直属父级，不等全树结束。
  子代理状态投影不再另发父级等待指令；各层保留自己的分工，协调者只加载当前角色正文和角色索引。
  真实下级成果按引用整合，不要求协调者亲手重写；确实没有可推进的独立工作时仍正常等待。
  阶段长等待按 exact attempt 给诊断，慢流不按总耗时强杀；提醒不改权限或重跑未知副作用。
  详见 `docs/design/SUBAGENT_PARALLEL_EXECUTION.md`，真实组合的覆盖边界另列。
  用户插话/停止/查看走同一 owner-scoped 结构化控制协议。详见 `docs/modules/subagent/SUBAGENT_RUNBOOK.md`。
- 派工以显式 covers 绑定原 Todo，output_files 可选且不授予额外权限；共享项目目录不代表冲突。
  同一 operation 重放回原回执，普通不同创建不能因 IO 路径相同被合并。依赖/独占写集由模型合理分工，
  不能用自然语言猜依赖、自动生成目录锁或自动派整批 QA/repair。
- Todo 是软计划，不是完成验收；普通 final 不因未勾完而被挡或暗中续跑。只有显式 /goal 和既有
  child 等待、审批、UNKNOWN 保留各自生命周期。UI 清单读 display_plan 的 exact generation/revision；
  历史页只恢复正文，不能用旧 Todo 覆盖当前计划。
  Todo/覆盖清单按 ID 部分更新时，缺省状态保持原值，新项才默认 pending；更正开关不补造状态。
  已完成项仍可补写 notes 和 evidence，不能返回成功却吞掉验证备注；状态/结果更正仍显式声明。
- 每个代理最多一个未结束 Goal；主子各自归属，普通派工可不附目标，Todo 可选。方向键选 Goal、Enter 编辑，
  Ctrl+S 保存、Ctrl+G 放弃。主代理 Esc 中断当前轮，active Goal 沿原 wake 安全续接；
  `/goal pause` 只关闭目标自动续跑，保留当前执行和独立资源；`/stop` 明确停止任务，组合中断与资源收回。
  Goal 清除只移除目标；有无 Goal 或目标 paused 都不把 Esc 变成资源停止。普通消息不隐式恢复 Goal。
  子代理视角标明的“停止此代理”保留其明确资源停止语义。
  后台普通插话由精确 running claim 验证，复用原持久回执；合法新 attempt 与 TaskRun 重开同事务提交，旧关闭事件保留。
  同任务追问只更换消息 request ID；主执行入口将 run/attempt 成对绑定到真实数据库身份，工具不能拿新消息编号冒充原 run。
  内容版本冲突不覆盖草稿，保存不隐式恢复暂停目标；新中断行为的真实 TUI 组合验收仍见 STATUS。
  Goal 的持久 task 输入编号不是一条普通用户消息；子代理返回先核对精确 Goal 归属，其余请求仍查原历史，不能伪造消息补齐。
  Goal 编号不是当前运行状态；只有精确归属且仍为 active 才可承诺续跑。历史多目标不自动合并或激活，
  并行分工仍有独立开放问题，不能以单 Goal 和编辑器验收代替整项关闭。
- 模型不再请求工具就结束本轮；turn_end.reason 不证明项目完成。失败原因、未验证范围需模型如实说明，
  编译/旧版本成功不能代替新版启动和业务验证；不能拿无响应、空日志猜成沙箱限制。
- 用户可见 commentary、thinking、tool、final 以 typed block/message/request ID 同步主子页面与 canonical
  display 账；display 不进入模型/Compact/Memory。分页和实时游标分开，Ctrl+O 不切换代理身份。
  Working 只由真实活动驱动，Todo 未勾完不能维持动画；流关闭不能判断任务/工具成功。
- 思考结束默认折叠，正文/final 完整显示；空思考占位可撤下，完整思考保持原顺序，不能在 final 后重放。
  每个主子页面独立滚动锚点，手动上翻不追尾，回到底部或发送消息才恢复跟随；滚轮每格一行。
- 长期记忆只在本 owner 维护，USER/AGENTS 与普通 memory 可自主更新；SOUL 修改仍需用户本人确认，
  不允许文件工具绕过。Skill 逐轮冻结索引、按需读正文；自学习默认关，仅候选可自动生成，正式 Skill 要确认。
- 生产包不得带开发验收 harness、tests、运行数据或废弃源码。包边界、import 边界与定向测试都要过；
  不通过增加 baseline、skip/xfail 或删除真实失败证据“清绿”。完整发布门见 AGENTS.md。

---

子代理交接补充：自然 final 与结构化 final 共用工具产物 registry 的精确引用，不恢复“模型不输出 JSON
就没有产物”的旧分支。声明与工具共用 execution_cwd，不把内部 run/output 当业务目录，不自动搬运文件。

## 项目结构速览

```
my-agent/                          ← 项目根目录
├── LLM_GUIDE.md                   ← 【你正在读的文件】LLM 入口
├── DESIGN_LEDGER.md               ← 设计台账（所有设计决策的来源）
├── STATUS.md                      ← 当前状态、测试基线、推荐下一步
├── docs/design/GATEWAY_DESIGN.md  ← Gateway 架构设计
├── MEMORY_BACKLOG.md              ← 记忆系统痛点和设计原则
├── DISCUSSION_BACKLOG.md          ← 系统问题讨论
├── docs/
│   ├── ROADMAP.md                 ← 【必读】待做/进行中功能清单
│   ├── COMPLETED.md               ← 【必读】已落地功能清单
│   ├── design/                    ← 模块设计文档
│   │   ├── README.md              ← 模块设计文档索引
│   │   └── CONVERSATION_CONTEXT_DESIGN.md
│   └── modules/                   ← 模块四件套（discussion/progress/purpose/structure）
│       ├── README.md
│       ├── subagent/
│       ├── memory/
│       ├── gateway/
│       └── live-lab/
├── agent_py_agent/                ← Python 包源码
│   ├── README.md                  ← 包级使用说明
│   ├── __main__.py                ← CLI 入口
│   ├── agent/                     ← 核心 agent 代码
│   │   ├── DIRECTORY_GUIDE.md     ← 【必读】目录职责地图
│   │   ├── agent_core/            ← 主代理编排
│   │   ├── backends/              ← 模型后端适配
│   │   ├── capability/            ← 能力路由
│   │   ├── gateway_parts/         ← Gateway 文件协议
│   │   ├── io/                    ← 底层 I/O 原语
│   │   ├── local_storage/         ← SQLite 本地事实源
│   │   ├── memory_store/          ← 记忆存储
│   │   ├── memory_archive/        ← 记忆冷归档
│   │   ├── memory_routing/        ← 长期规则路由
│   │   ├── prompting_parts/       ← Prompt 构造
│   │   ├── settings/              ← 配置
│   │   ├── subagents/             ← 子代理领域
│   │   ├── subagent_workflows/    ← Workflow 模板
│   │   └── tooling/               ← 工具实现
│   ├── cli/                       ← CLI 子命令
│   ├── config/                    ← YAML 配置文件
│   ├── data/                      ← 运行时数据和 prompt 模板
│   ├── tests/                     ← 测试
│   └── prompts/                   ← Prompt 模板
└── scripts/                       ← 工具脚本
```

---

## 开工前：必读清单

按顺序读，每个文件解决一个问题：

| 顺序 | 文件 | 解决什么问题 |
|------|------|-------------|
| 1 | `LLM_GUIDE.md` | 你在哪、该读什么、怎么改 |
| 2 | `docs/ROADMAP.md` | 要做的事在不在列表里、当前状态 |
| 3 | `DESIGN_LEDGER.md` | 设计决策来源、是否有冲突的设计原则 |
| 4 | `agent/DIRECTORY_GUIDE.md` | 代码放哪里、边界是什么 |
| 5 | `docs/design/` 对应模块 | 模块级详细设计（如果改动涉及特定模块） |
| 6 | `docs/modules/<module>/02-progress.md` | 模块最近推进了什么 |
| 7 | `docs/modules/<module>/04-structure.md` | 模块结构和核心文件 |

如果改动涉及测试：
- 读 `TESTS.md` 了解测试策略
- 读 `TEST_CHECKLIST.md` 了解收口检查项

如果改动涉及 subagent/capability/runner：
- 读 `docs/modules/subagent/SUBAGENT_RUNBOOK.md` 了解运行协议

---

## 收工后：必改清单

### 1. 更新 `docs/ROADMAP.md`

- 如果你完成了某个条目：把状态改为"已落地"，或直接删除条目（已移入 COMPLETED.md）。
- 如果你在做某个条目但没做完：更新"当前进展"和"待做"。
- 如果你发现了新问题或新需求：在 ROADMAP.md 末尾新增条目，必须写"解决问题"。
- 如果你发现设计冲突：先更新 DESIGN_LEDGER.md，再更新 ROADMAP.md。

### 2. 更新 `docs/COMPLETED.md`

- 新增一条记录，包含：解决问题、落地内容、验证方式。
- 不要写太长，每条 10-20 行足够。

### 3. 更新 `DESIGN_LEDGER.md`（如果涉及设计决策）

- 新增条目写清楚：日期、状态、摘要、已落地、后续方向。
- 超过 100 行的细节拆到 `docs/design/<module>.md`。

### 4. 更新 `STATUS.md`（如果改动影响测试基线或可用能力）

- 更新测试状态（通过数量）。
- 更新"当前可用能力"或"当前主要限制"。
- 更新"最新恢复入口"（如果改动影响恢复链路）。

### 5. 更新模块四件套（如果改动涉及特定模块）

- `docs/modules/<module>/02-progress.md`：记录本次推进。
- `docs/modules/<module>/04-structure.md`：如果结构变了。

### 6. 写清本轮汇报的下一步

- 最终回复、handoff、子代理报告都要包含 `建议下一步`。
- 建议要能直接指导下一位开发者行动：先做什么、能不能并行、不要碰哪些用户本地改动、需要跑哪些验证。
- 如果已经完成到可暂停，也要明确建议是等待评审、合并、观察 CI，还是进入下一阶段。

---

## 编码规范

### 注释格式（两层注释）

```python
# LLM: technical summary of the contract, side effects and invariants.
# 函数用途: 人能看懂的用途、调用时机和修改注意事项。
def example(...):
    ...
```

- 每个功能代码里的 module / class / function / method 都要有这类双层注释。
- module 使用 `模块用途:`，class 使用 `类用途:`，function 和 method 使用 `函数用途:`。
- class / def 有装饰器时，注释放在第一行装饰器上方。
- `LLM:` 给后续模型读，写清 contract / invariant / side effects / caller expectations。
- `模块用途:`、`函数用途:` 或 `类用途:` 给人读，用大白话说明用途、调用入口、修改时要检查什么。
- 有副作用的函数必须写清：会不会写文件、调用 API、启动进程、改状态。
- 测试函数不强求长注释。
- 批量补注释后必须跑 `compileall`、`ruff` 和 `scripts/check_code_size.py`；注释不计入实现行数，但语法位置和装饰器位置必须正确。

### 测试要求

- 新增测试用 `test_*.py` 或 `test_` 前缀，`run_tests.py` 会自动发现。
- 完整冒烟默认调用真实 API，echo/fake backend 只用于纯函数定位。
- 冒烟测试必须隔离 workspace，不污染开发仓库。
- 测试通过后更新 `STATUS.md` 的测试数量。

### 代码放置决策

按这个顺序问：

1. 外部协议客户端？→ `backends/` 或 `clients/`
2. 主代理流程编排？→ `agent_core/`
3. 子代理领域规则？→ `subagents/`
4. 工具实现或安全边界？→ `tooling/`
5. Gateway 文件协议？→ `gateway_parts/`
6. 本地事实源持久化？→ `local_storage/`
7. 记忆存储？→ `memory_store/`
8. Prompt 上下文构造？→ `prompting_parts/`
9. 配置 schema？→ `settings/`
10. 底层无业务含义的 I/O？→ `io/`

都不是？先写清楚变化原因，再决定是否需要新目录。不要塞进 `utils/common/shared`。

---

## 文档体系

### 设计层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `DESIGN_LEDGER.md` | 设计决策主台账，只放摘要和导航 | 每次设计决策变更 |
| `docs/design/*.md` | 模块级详细设计 | 模块设计变更时 |
| `docs/design/GATEWAY_DESIGN.md` | Gateway 专项设计 | Gateway 架构变更时 |
| `*_BACKLOG.md` | 痛点收集和设计原则 | 发现新痛点时 |

### 状态层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `STATUS.md` | 当前状态、测试基线、推荐下一步 | 每次测试基线或能力变更 |
| `docs/ROADMAP.md` | 待做/进行中功能清单 | 开工前读后、收工后改 |
| `docs/COMPLETED.md` | 已落地功能清单 | 功能完成时 |

### 运行层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `SUBAGENT_RUNBOOK.md` | subagent/capability/runner 运行协议 | 主链路变更时 |
| `TESTS.md` | 测试策略 | 测试策略变更时 |
| `TEST_CHECKLIST.md` | 收口检查项 | 新增检查项时 |
| `CLI_REFERENCE.md` | CLI 命令参考 | 新增/变更命令时 |
| `CODEBASE_TREE.md` | 代码树说明 | 结构变更时 |

### 模块四件套（docs/modules/<module>/）

| 文件 | 用途 |
|------|------|
| `01-discussion.md` | 灵感和讨论 |
| `02-progress.md` | 推进、解决的问题和测试 |
| `03-purpose.md` | 初心和设计想法 |
| `04-structure.md` | 结构树、核心文件、数据流 |

同步门：`scripts/check_doc_sync.py` 会检查 covered module 的文档是否与代码同步。

---

## 设计原则

从 DESIGN_LEDGER.md 提炼的核心约束：

1. **递归协作者模型**：主代理、子代理和孙代理共用同一轮模型/工具循环；差异只来自结构化父子身份、权限上界和工作区。
2. **自然完成**：模型不再请求工具时结束本轮；宿主只记录 `turn_end.reason`，不在回复后追加机器质量验收。
3. **事实与质量分开**：路径、权限、工具终态和产物存在性仍是结构化事实；“做得够不够好”由模型/用户继续复核，不设通用硬门。
4. **patch 不自动应用**：patch 审核器只审核状态，不自动应用 diff（除非有独立审核链路）。
5. **自学习必须用户确认**：学习候选必须经过用户确认才能变成正式 skill。
6. **执行模式以入口合同为准**：TUI 正常委派会实际执行；显式 dry-run 的管理入口只做预览，不能把旧管理命令默认值推广到所有任务。
7. **删除与覆盖受权限和副作用合同控制**：不能对用户宣称全产品“不删文件”；高风险目标需明确授权和精确路径，失败不伪装成功。
8. **文件第一事实源**：机器判断必须读 JSON/JSONL，Markdown 台账不是事实源。
9. **每个功能写"解决问题"**：不能只罗列模块名。
10. **通道故障 ≠ 任务失败**：runtime/session/adapter 故障不等于任务本身失败。

---

## 常见场景

### 我要加一个新功能

1. 读 `docs/ROADMAP.md`，确认是否已有相关条目。
2. 读 `DESIGN_LEDGER.md`，确认没有设计冲突。
3. 读 `agent/DIRECTORY_GUIDE.md`，确认代码放哪里。
4. 写代码、写测试。
5. 先跑相关定向测试，再做真实 TUI 验收；全仓 pytest 频率遵守 AGENTS.md，不因新功能机械重复全仓。
6. 更新 `docs/ROADMAP.md`（改状态）和 `docs/COMPLETED.md`（新增记录）。
7. 如果涉及设计决策，更新 `DESIGN_LEDGER.md`。

### 我要修一个 bug

1. 读相关代码和测试。
2. 修 bug、补测试。
3. 跑相关定向测试并复验原真实 TUI 失败样本；不能把定向通过写成端到端通过。
4. 如果 bug 揭示了设计问题，更新 `DESIGN_LEDGER.md`。

### 我要重构代码

1. 读 `agent/DIRECTORY_GUIDE.md`，确认边界。
2. 读 `DESIGN_LEDGER.md` "代码体检与后续拆分计划"条目。
3. 只移动一个低耦合区域，同步迁移调用点，不新增旧入口转发壳。
4. 跑直接调用方和相邻模块定向测试，远端提交前执行 AGENTS.md 全部严格 gate。
5. 不在同一轮同时改行为和大移动文件。

### 我不确定该不该改

1. 读 `docs/ROADMAP.md`，看优先级。
2. 读 `DESIGN_LEDGER.md`，看设计原则。
3. 读 `STATUS.md` "当前主要限制"，看是否在限制列表里。
4. 如果都不确定，在 `DISCUSSION_BACKLOG.md` 里记录问题，等确认后再动。

本地包与安装事实见 [插件包合同](docs/design/PLUGIN_PACKAGES.md)：命令 JSON 读取统一归 command_declarations，
严格 JSON 与目录锁共用公共原语；归档读取不安装、不导入实现。安装 Store 沿 canonical owner 插件目录，默认停用，先包后表。
原请求回执与版本同次保存，清理异常不能覆盖已提交或未知事实；管理已有本地接线，但候选或安装记录仍不代表已授权工具。
管理权限、原操作链和配置已有本地接线，独立环境已有内部准备器，安装表激活 CAS 已本地实现；原 stdio 资源归属和精确撤销仍待接通，不以开发合同测试代替真实 TUI 装卸验收。
