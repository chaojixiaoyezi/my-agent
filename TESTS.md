# 测试与发布验收

## 原则

验证证据以 `test_verification_runtime.py`、`test_verification_repository.py` 和项目命令识别测试为准，覆盖真实工具出口、写后过期及 owner/task 隔离。
离线矩阵只检查实现/测试文件存在及尺寸报告，不能当作行为验收。无生产调用的旧 verifier integrity 模块及仅检查输入字典的测试已删除，不再计入运行时覆盖。

开发反馈优先定向合同、工具替身、模型替身和脱敏回放；真实 TUI 是最终验收最低要求。测试任务由被测代理完成，测试者不能代写产物后计为通过。
子代理审批回归必须包含真实创建生命周期的父会话关联，覆盖 child/grandchild 的批准与拒绝；仅裸 manager 创建不足以代表正常 TUI 派工。
归属记录读取失败不能退回主任务批准；具体审批与 capability grant 分开核验。对应 gateway control、background approval、owner policy 和 tool round 定向测试。
真实拒绝验收必须观察到具体审批及拒绝回执，并核对 handler 未执行；默认确认模式允许的普通命令没有弹窗时，该轮只能记为未覆盖拒绝路径。
同参拒绝回归必须跨实际子代理 Goal 续轮及 Compact，不能仅在同一个工具循环参数对象上重复调用。
`test_agent_goals.py` 使用真实生命周期、权限门和工具账，替换模型与用户决定；核对第二次同参不弹窗、不同参数仍申请、不同孩子独立、handler 始终未执行。
前后台活动回合的 Compact 夹具同时检查拒绝列表保持原对象，新调用为空；这些确定性用例不抵充安装版 TUI 验收。
审批与控制并发时，先确认退出审批模态及输入回显，再提交 `/stop`，以实际控制回执核对；不能把发送按键等同命令已执行。
长采样按实际启动/退出、追加数据与时间重叠验收；测试者处理审批的延迟、模型重跑和采样跨度须分别记录，不能用同一个 PID 数字证明没有重启。
前台 Shell 超时回归包括组长先退出、后代被重新挂到系统进程、忽略 TERM 需要升级终止，以及另一进程组不受影响；回执须核对成员退出和管道排空。
嵌套 Shell 后台检查同时覆盖字面 `-c`、常见包装命令、嵌套引用、普通字符串、重定向和 heredoc；启动前拒绝不得伪记为进程已经执行。

普通需求用自然中文表达。权限、参数、隔离、状态和恢复由底座控制，不靠在提示词里写特殊限制规避缺陷。详见 [测试分层](docs/design/main-agent-contract-testing.md) 与 [测试清单](TEST_CHECKLIST.md)。

拟议结构调整、Computer Use 当前部署复验及 Jev 只读对照边界见
[可维护性评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)。后端首批拆分已完成定向验证，既有测试与发布 gate 不变；
接入协议测试与真实桌面分别留证，Jev API 尚未验收。

本轮用户指定：所有真实验收通过实际 TUI；常规任务使用官方 MiniMax-M2.7，视觉任务使用官方 MiniMax-M3。
视觉会话复用 M2.7 的私有 provider 与密钥引用，只切换模型名；核对实际请求端点、模型及图片确已送达，不因同名模型推断来源。
测试覆盖长任务矩阵，历史日志只作案例来源；任务、工具、历史和产物须由被测代理自己完成。
登录认证也从 TUI 进入，其他账号的真实模型调用不混入本轮验收。截图、账号信息和原始日志只保存在仓库外。
每批可并开多个真实 TUI，必须公布 tmux 名称和查看命令，共用一个 Gateway；重连保留原会话身份。
新增回归覆盖 Full Access 的后台进程地址、跨权限视图恢复、PTY 共享解析及文本输入 Unicode 事件。
键盘事件替身和内存事件校验不计真实桌面通过，仍须由 TUI 模型操作后读取目标应用核对。
本轮已完成后台等待、PTY、取消续做、断连重连、Goal 暂停恢复、多子代理插话与手动/自动压缩；
本机英文和中文桌面输入读回一致。登录设备码被官方端点拒绝，真实账号确认/刷新/退出未通过。
具体通过范围与失败记录见 [本轮真实矩阵](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#本轮真实-tui-验收矩阵)。

## 必测模块

发布前 HTTP 取消回归覆盖 JSON/GET/SSE：响应头尚未返回时先中断再出现连接/清理异常，必须保持中断且仅请求一次；
未中断的对照必须原样抛错。实际 socket 等待用例与确定性竞态用例同时保留，不能靠多次重跑偶然通过。

长时间运行增量矩阵见 [设计与验收](docs/design/LONG_RUNNING_EXECUTION.md)。普通验收并行运行官方 MiniMax-M2.7
TUI，共用单 Gateway；只有用户明确要求的慢模型专项才另用单路对照，定向回放和真实通过分别记账。

| 模块 | 验证要点 |
|---|---|
| 配置与模型 | 服务商协议、密钥引用、上下文容量、会话选择、用户默认、子代理继承与显式覆盖 |
| 身份与工作区 | 多用户同 Gateway、家目录隔离、管理员显式越界、工具权限与真实路径 |
| 主子代理 | 创建、插话、停止、恢复、换代、结果落账、父级唤醒、重复及乱序事件 |
| 历史与压缩 | 未压缩历史完整性、Unicode JSONL、展示分页、长输出引用、压缩计数、模型切换 |
| 工具 | 参数校验、成功/失败状态、文件读写、搜索、补丁、命令/PTY、网络、MCP |
| 记忆与技能 | owner 隔离、自主记忆维护、人格确认、索引发现与按需读取；技能选代表场景 |
| TUI | 输入回显、换行、粘贴、滚轮、复制、完整展开、到底部、主子代理视角、Todo、活动状态 |
| 调度与交付 | 普通回合与目标模式、挂起唤醒、断线、后台交付和恢复；IM 无环境时标明未测 |

## 插件装卸拟议验收（待实施）

插件方案尚未落地，本节不是通过记录。先用合同/替身/回放验证代次、权限、撤销、清理与失败状态，
再用官方 MiniMax-M2.7 的实际 TUI 共用单 Gateway 并发验收：一路插件长任务、一路正常内置任务、一路插件管理。
对应 [合并实施计划](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施) 的第 3—6 步：
命令解析、装卸链、TUI 使用和故障验收分别留证，全部通过才计首批插件可用；不能仅以补全或单次成功调用收口。
首批从显式本地包和只读示例开始；在线安装、更新/回退留后续专项，不以未实施功能阻塞后续结构拆分。
三路会话是角色安排，实际 TUI 编号与 tmux 名称须在启动后报告；每轮核对官方 provider/端点，保留装卸前后的 Gateway 进程身份。
核对未安装/已装停用/启用/卸载的有效能力与模型配置，卡死卸载时管理入口和无关任务可用、Gateway 不重启、插件不被重连复活。
版本切换、旧任务恢复、命令冲突、owner 隔离及清理失败按真实事实单列；报告实际 TUI 编号与 tmux 查看方式。
命令合同须覆盖 `/plugins` 管理与 `/plugins@ID` 调用、同源帮助/补全、停用时只读帮助、未知目标/参数、缺值、短开关组合、引号与跨平台路径；
`@` 不误触发文件补全，命令错误不转聊天，参数文本不作为 Shell 执行。此项仍为待实施矩阵。
详细矩阵见 [插件设计](docs/design/PLUGIN_LIFECYCLE.md#实施顺序与验收)，不得用现有工具测试替代这些尚未执行的验收。

合并计划第 10 步新增 [10 个简易插件与组合验收](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)，仍为待实施：
先按 3/4/3 三批验证各包的实际功能、命令/帮助/配置、启停与卸载，再并发覆盖普通任务、插件长任务和管理操作。
提供工具或模型任务的插件还须由普通中文需求触发；纯展示插件用 TUI 操作验证，不强行增加模型调用。
用量等显示遥测不进入模型请求或调度判据；事件、目录和文件读取均守 owner/run 权限。
常规调用使用官方 MiniMax-M2.7；图片文字可用本地 OCR，需要模型视觉理解时用同一密钥引用的官方 MiniMax-M3。
每个包均覆盖故障与清理，全部卸载后对照核心能力和新会话请求基线；任一失败保留原证据，不以另一个样本通过抵消。
样本只用合成文件、测试页面和专属资源，结果由被测 my-agent 自行产出；日志、截图与个人配置不进仓库。
Audit/摄取不列入本轮新增验收；共享模块既有回归按改动影响保留，不能因此删除既有功能测试。
十步逐批验收的场景、证据及进入下一步条件见 [执行 Goal 与测试矩阵](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。

## 重点定向回归入口

- runner 正常让出：`test_subagent_runner_result_state.py` 通过真实 manager 写回和读盘，联测六种结束原因、
  旧错误清理、显式失败、缺失/未知原因及状态冲突；`ok=False` 的正常让出不应填 `runner_error` 或最近错误。
  联合直属等待、恢复、结果载荷、能力授权、来源工作者和 Compact 共 237 项通过、1 项既有 xfail。
  真实 TUI 核对递归父级的 `PENDING / interrupted`、空错误字段、精确等待身份及结果触发的续跑；
  另用真实 `/stop` 验证取消不被清成普通等待，模型分工质量单独记录。
- 探针端点：`test_backends_base.py::test_probe_endpoint_matches_transport_request` 截获实际 HTTP 出口信封，
  比较能力诊断与请求 URL；覆盖 Messages、Chat、Responses 的代理前缀、版本后缀、完整接口、尾斜杠及成功/未证明能力共 36 组。
  替身只提供协议响应，不替换地址计算；与原生工具、请求作用域和 OAuth 相邻回归共 228 项通过。
  新版真实官方 MiniMax-M2.7 TUI 完成写程序、执行和读回；一次多余参数被拒后自行修正，原失败保留。
  后台会话未持久化完整探针结果，因此地址合同与真实工具链分别留证，不把替身信封称作真实抓包。
- 后台审批桥：`test_background_tool_approval.py` 联合 Gateway 代理控制、owner 权限模式、TUI 队列与后台活动测试。
  核对原始请求批准/拒绝、跨会话拒绝、claim 换轮失效、无接收方关闭式失败、缓存隔离；主审批不能扩权到子代理控制入口。
  Goal 暂停仍允许当前审批；回合中断取消等待，明确任务停止另验资源收回。真实验收从 TUI 启动 Goal，
  未批准前核对无执行，正常面板批准后读取原调用结果；拒绝及旧 PTY 续用分别留证。
  本片 428 项定向通过。真实 TUI 已分别证明暂停后批准原调用、跨 claim 读写同一 PTY 和拒绝不执行；
  程序仅启动一次、原终端正常退出，30 项实际输出与报告逐项一致。测试者没有执行任务程序或修改产物。
- 插话存储组合：联合 `test_runtime_guidance.py`、Gateway 控制与输入交付、主子恢复、Compact 和终态测试。
  故障注入直接指向提交批次组件或账本投影；保留提交后部分回执写入失败、确认重放、旧格式迁移和改绑半写入样例。
  新组件的独立导入不得加载聚合 Store 或运行执行器。实际 TUI 必须在同次短输入操作内确认完整回显并提交，
  保存提交时的 running claim、精确输入回执、模型原生输入和最终消费状态，区分忙碌插话与任务结束后的追问。
  本片定向 2,362 项通过；综合全仓 17,116 项通过、35 项 xfail、22 项跳过；该全仓结果早于本轮控制修正。
  旧 Goal 暂停后 PTY 继续不算资源停止失败，当前控制语义另行验收，不能由历史结果替代。
- PTY 生命周期补修定向覆盖 `test_pty_sessions.py`、`test_gateway_conversation_control.py`、
  `test_orchestration_cancel_subagents_tool.py` 及公共进程终止、线程取消和导入边界：
  跨模型回合资源停止、同会话不同任务、跨 owner/thread、精确 attempt、Popen 前后取消竞态、自然退出 PID 不再发信号。
  Goal 暂停/清除保留当前执行；有无 Goal 或目标 paused 时的 interrupt 都不能停止独立资源。
  真实 TUI 分开验证：暂停 Goal 后当前链及 PTY 继续、回合结束后没有 Goal 自动续跑；
  中断回合后独立 PTY 保留；明确 `/stop` 后所属资源停止，恢复原程序保留原始记录前缀。
  另一窗口同期采样用于确认隔离。不得由测试者执行采样脚本或补报告；旧暂停后静止的观测仅是旧实现记录。
  后台恢复还须验证策略、冻结快照和原生工具 Schema 均含获准的终端工具；显式配置或 owner/task 禁用仍有效。
  `test_background_main_agent_runtime.py` 联合进程/PTY 测试覆盖 Goal、子代理、定时和 Audit 唤醒目录，真实复验接续原未完成任务。
  当前两组定向分别为 444 项控制与 267 项目录/工具测试；真实子代理续采保留 19 条后采满 30 条，主代理 PTY 中断后继续并收尾。
  后者恢复轮走文件查询，未覆盖旧 PTY 句柄的后台读取；模型自设时限耗尽及报告错误均留为失败，详见 STATUS。

- 线程、消息、任务关联与 Audit 组合：核对绑定并发、Compact CAS、幂等追加、游标分页、终态不复活、
  命名工作修订及进度退休，并联测 Gateway、子代理和 Memory 的实际领域接口。
  最新定向 2,206 项通过、28 项既有 xfail、1 项 Linux 平台跳过；故障替身指向新组件，不保留旧 API。
  实际 TUI 已验暂停/压缩/重连续采、两路长命令并行、普通后续指令和用量；失败及未测范围见 STATUS。
  实际输入必须核对 canonical 用户消息与原生请求。tmux 批量文本使用括号粘贴，确认完整回显后提交；
  授权弹窗可能改变输入焦点，拟发送字符串不等于已接收。提交前任务已结束时只计后续指令，不计运行中插话。

- Goal、观察、唤醒与进度领域：联合目标编辑/预算/恢复、发布顺序、观察扫描、策略失败/退休/GC、
  插话与子代理回传测试；工具 mock 和故障 monkeypatch 直接指向所属领域，不能沿旧 Store 补导出。
  独立导入检查组件不加载 Store 或调度执行器；跨域旧账归档保持同一时间、保留期与原顺序。
  定向 1,236 项通过、28 项既有 xfail；新版双 TUI 验证约 98 秒暂停静止、压缩重连、原程序真实续采，
  并行分批生成各 2,000 条 CSV/JSONL 后程序核对全部编号、数值、中文、时间、样本与 SHA-256。
  报告误述取消原因、临时脚本目录错误与被拒绝的状态面读取分别留证，不冒充完整任务质量通过。

- 存储上下文与执行租约：联合索引、账本 GC、线程中断、后台运行、Goal 恢复与 Gateway 错误测试，
  验证原子领取/续租/终态、同任务恢复、坏账报告、惰性指纹缓存、动态能力读取与原路径。
  `initialize=False` 初始化及缺失线程/租约读回不能创建目录；组件独立导入不能加载组装入口或执行器。
  本片联合 1,064 项通过、4 项既有 xfail，追加只读打开合同一项通过；259 个原函数/方法中两处构造函数单独审阅，其余逻辑比较一致。
  新版双 TUI 验证约 95 秒暂停静止、压缩一代重连、原程序实际续采到 12 条，另一会话两路各 10 条、重叠 104.01 秒。
  租约终态、子结果唤醒、插话、目录隔离、原生投递去重与费用入账分别核对；报告的采样路叙述错误保留。

- Gateway 请求组件：联合会话上下文、Compact、前台历史、请求错误、Goal 恢复、模型选择和后台运行测试。
  历史提交、写前绑定与输入渲染的替身直接注入各职责模块，不给旧导入增加转发；错误字段和持久格式保持。
  `test_runtime_module_boundaries.py` 实际调用共享历史选择器后检查未加载 Gateway，另核对新组件不加载网络或执行器。
  本次 666 项定向通过，110 个定义/常量的搬移前后逻辑比较一致；该证据不替代新版真实 TUI 的暂停、
  压缩、重连、后台交付、插话和目录隔离验收。原始快照、差异与真实日志保留在仓库外。
  新版双 TUI 已验证暂停四条后约 111 秒静止、Compact 一代并重连、原程序实际续跑至十二条；
  另一会话两路各八条采样重叠 84.021 秒，插话与后续更新未改原 CSV。原生历史按精确 request/part 无重复。
  初次报告遗漏合计和时间心算错误保留；后续程序复核只修正部分数字，两处间隔范围仍错，不计完整任务质量通过。

- Gateway 流式边界：联合 streaming、verbose_progress、foreground_transcript、thinking_archive、
  main_activity 和恢复测试，验证迟到读取、审批缓存、事件顺序、插话、脱敏和思考归档；猴子补丁须指向
  实际组件，不通过旧请求模块导出。前后台超窗继续由现有真实 store/替身运行器验证携带、代次与取消。
  纯 carry 与流模块能独立加载，不引入执行器；真实多 TUI 仍须逐路核对模型源、输出、控制和原生账本。

- 配置为空时不得隐式选择 echo；需要本地后端的夹具必须显式配置。默认值、工作区列表、Goal revision
  和界面文案断言与当前合同同步，不能恢复已删除的兼容语义来迎合旧测试。
  `test_home_runtime_bootstrap.py` 与 `test_r103_run_reuse_no_split.py` 联合验证同任务复用 canonical run、
  新请求身份、归档终态和目录准备失败的 attempt 收口；不放宽终态断言。
  历史不可读与任务绑定冲突在唯一错误表分别登记，保留原唤醒、禁止猜身份或重放未知副作用。

- 后台交付边界：`test_background_owner_delivery_commit.py` 联合后台运行时与历史快照，验证
  未声明路线的 canonical 交付、声明但不可用时保留外发义务、整封/纯附件冻结、已发送只补本地、
  v1 载荷重投、commentary/final 去重、终态抑制和精确审计回执。独立导入不加载调度器或网络后端。
  实际 TUI 核对父子结果返回、Goal 暂停/恢复、重连后最终回复与原生历史身份；外部 IM 的失败重投
  仍由已有替身合同验证，不冒充真实 IM 发送通过。

- 后台执行边界：联合 `test_background_main_agent_runtime.py`、`test_background_history_snapshot.py`、
  `test_background_owner_delivery_commit.py`、`test_thread_model_selection.py`、`test_cli_resume_contract.py`，
  验证同片溢出重试、八次压缩公平让出、正常/取消/异常原生历史、模型冻结和技术续跑来源约束。
  独立导入检查执行模块和纯续跑判据不反向加载调度器。真实 TUI 并行覆盖父子接续、Goal 压缩恢复、
  执行中停止及新目录隔离，不能用原候选版本的通过结果代替这批执行器验收。
  本批五路真实 TUI 已留证：Goal 保留暂停前四条、压缩重连后补齐六条；受管后台进程停止后不再写入，
  续做保留前六条并补齐十二条。定时作业独立于前台 `/stop`，不能把中断等待当作取消定时任务；
  此类任务须经 TUI 工具显式清理。字段计算通过与模型写错时间、采样间隔失败分别记账。

- 后台准备边界：`test_background_context_runtime_errors.py`、`test_background_main_agent_runtime.py`、
  `test_background_owner_delivery_commit.py` 联合 Gateway 控制、上下文用量、TUI 模型统计回归。
  保持未压缩历史完整、detached 任务创建锚点与 lineage、读取失败不调用模型、展示统计不进入上下文。
  `test_runtime_module_boundaries.py` 在独立进程检查合同、策略、上下文和历史模块加载不引入调度或网络后端。
  真 TUI 增量复测子代理返回后的后台接续与 Goal/Compact；文件搬迁的单测不替代真实验收。

- 首次 Goal 目录：TUI 控制传输、HTTP 持久回执、Goal 初始化、普通请求目录和 store 绑定联合验证。
  覆盖模型菜单先建空线程、目录与 roots 同步、相对/缺失/外部/远程 owner 拒绝、已有目录不变、
  同 ID 改目录冲突及 v1/v2/v3 摘要防篡改。实际 TUI 分两路验证 owner 内目录执行与 owner 外拒绝，
  拒绝必须发生在创建 Goal 和调用模型前；不能先发送普通聊天替首次 Goal 补目录再计为通过。

- 仅思考响应：Chat/Messages 的流式与非流式不得因无正文丢弃有效思考、用量或隐藏重试；
  真空白仍报错。`test_native_tool_use_ir_messages_flow.py` 验证两次无工具续跑逐条保存、
  OpenAI 实际出站回放、下一工具轮和最终保存不重复；`test_response_decision_native_tool_use.py`
  验证坏工具修复不回放未执行工具。联合原超时探针、截断、插话及中断历史回归。
  真实抓包先检查仅思考响应是否漏入下一请求，再评价真实任务完成，不能仅凭缓存高称通过。

- 渠道失败提示：`test_tool_failure_channel_hint.py` 联合错误语义与工具执行回归，覆盖测试/编译非零、
  参数/状态/权限拒绝、取消、未知、真实网络不可用、重复回执和新回执覆盖旧失败。
  错误正文不能提升为控制码，缺 call_id 不猜新事件；关闭阈值和每工具一次不变。
  真实 TUI 核对失败码、下一次请求是否误加换渠道提示及实际排错进展；不能把提示过滤通过当作模型不再循环。

- 后台失败退避：`test_gateway_lane_retry.py`、`test_gateway_loops_resilience.py`、
  `test_background_main_wake_recall.py`、`test_model_unconfigured.py` 与会话模型选择联合验证。
  覆盖缺配置长时间不重跑、模型引用删除/恢复、精确旧会话改选、默认选择不串会话、零值冷却、
  远端拒绝不误判本地缺配置、同 owner 健康车道、跨 owner、短锁与有界回收。
  联合 `test_background_supply_backoff.py` 和 Goal 测试核对 scheduler 不提前关闭目标/消费 wake；
  真实执行错误和额度限制仍受原保护，不把原已暂停或受阻目标无条件激活。
  真 TUI 在未配置会话设置目标，再通过 /model 选模型，核对原目标恢复、唯一最终回复及原 wake；
  另一路正常任务并行，不能把手工改任务文件或替身模型当作真实恢复验收。

- 重启与持久回执：`test_tui_worker_paths.py` 验证已提交消息在 PID 暂不可见时仍读取原 terminal；
  没有终态沿既有超时返回，不再入队；未提交请求仍报告服务停止。真实 TUI 将重启与消息投递交错，
  区分队列提交、实际执行、模型 final 和前端展示，不把服务启动命令退出当作已经就绪。

- 旧计划续写与多用户路径：`test_task_progress_advisory.py` 验证精确旧账更新、缺省状态保留、跨会话、
  子代理/独立后台目标拒绝和无隐式重绑；`test_gateway_chat_conversation_context.py` 验证本地队列的
  自定义 owner、外部/未知来源、final/实时/历史路径一致。真实 TUI 用原会话追加验证笔记并索要完整路径，
  对照 native final、canonical public row 和终端画面；不能把宿主脱敏误记成模型漏答。
  路径样例必须真实含 owner/request 标识，分别覆盖绝对路径、Windows 路径和相对目录；
  仅用不含标识的示例不能检出第二层替换。外部来源不因该修复暴露完整宿主路径。

- 进度部分更新：`test_task_progress_coverage.py`、`test_task_progress_advisory.py` 与派工对账测试，
  覆盖只补备注/元数据、空状态、各规范状态、更正标记、新项默认及模型/展示一致；没有 ID 仍按参数错误返回。
  已完成项须先写入旧备注，再更新并读回新备注；只断言状态未变或空备注成功不算覆盖。
  原生 Schema 必须明确 ID 必填，不能为了部分更新把全部字段都标成可选；标题/状态仍允许按需更新。
  真实慢任务的计划状态和原工具回执并行核对，旧数据不推测重写，不以勾选进度替代产物验收。
- 显式采样：`test_provider_sampling.py` 联合三种 backend 测试，覆盖默认省略温度、显式零值/范围端点、
  单次摘要覆盖、工作片冻结与子代理继承。慢模型客户端对照严格串行，切换前检查原请求及服务端槽位退出；
  真实出站诊断只写私有测试目录，不记录认证头、不改请求协议，不把参数回放当完整 TUI 任务通过。
- 批次执行事实：`test_current_turn_execution.py`、`test_native_tool_use_ir_messages_flow.py`，覆盖
  只追加当前批次、Compact 轮号重置后的身份区分、未知副作用、批准来源、有界省略及全轮核验保留。
  连续请求逐字节保留此前缀和全部工具对，旧会话不强制清理。真实出站核对新增事实大小及实际任务进展。
- 代理树重复查询：`test_agent_tree_model_view.py` 联合工具重复观测回归，验证仅时钟/心跳变化继续计数，
  实际工具进展、终态和产物改变重新计数；原查询结果、权限和生命周期不改变，不用耗时判死。
  子代理查自己的子树时，从规范范围裁决排除自身查询活动；主代理显式查询该孩子仍保留其真实进展。
  正常模型并行验收与慢模型串行对照同时进行，不能把正常模型的轮询浪费漏记为慢模型专属问题。

- 子代理模型续派：`test_orchestration_background_dispatch.py`、`test_model_profiles.py`、
  `test_thread_model_selection.py` 及 worker/timeout 测试。覆盖 Gateway 无默认模型、父子异模型、
  child thread 改选后的恢复、并发显式注入和并行工具线程的依赖传递；旧捕获函数回放须能重现配置/连接不一致。
  真实验收区分普通父子交接与 coordinator 等待孙代理后的重新派工；没有真正产生孙代理的不计后者通过。

- 中断历史：`test_native_tool_use_ir_messages_flow.py`、`test_cli_run_conversation.py`、
  `test_gateway_chat_conversation_context.py`、`test_subagent_runtime_compact.py`、
  `test_background_main_agent_runtime.py`、`test_background_owner_delivery_commit.py` 联合验证
  原生调用/结果保留、未知副作用占位、空正文与异常不改成功、后台静默/外发失败仍留事实而不伪造送达、
  原请求幂等、同一 repair 补交。真实 TUI 用执行中 Esc 后继续，核对下一轮真实输入和已发生的工具事实；
  一路慢模型不派子代理，正常模型并行验证父/子与普通后续轮。历史旧缺口不按显示文字补造成功。

- 客户端计时：`test_gateway_client.py`、`test_gateway_admission_wait.py`、`test_tui_worker_paths.py`，
  覆盖时钟前跳/回拨、失联超时和活动租约续期。真实 TUI 可隔离替换客户端模块时钟注入跳变，
  不修改系统时钟、不影响 Gateway/模型计时；单独记录注入已发生、真实终态及任务产物，不能把替身当真实模型。
- 账号认证：`test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`，
  联合模型配置/共享目录/会话选择/原后端测试。覆盖跨 owner、冻结引用、刷新轮换、取消和退出竞态、
  私密参数保留/清除、重定向拒绝及协议复用。真实 TUI 的设备码确认另验；替身不作为实际账号权益证明。
- 用量增量：`test_model_call_ledger.py`、`test_tui_model_metrics.py`、`test_reproject_model_usage.py`，
  成功/异常/取消共用结算；累计容器重建换代，来源切换不重复算，旧账与缺报不得估算重写。
  真 TUI 中断后追加、Goal 后台交接、子代理及 Compact 必须按 provider 分项对账。
- 慢模型额外排队：模型配置与首事件估算定向测试，默认 0、按模型覆盖、无穷大/布尔/非法值拒绝。
  真实单槽并发等待、滚动输入、Esc 分开验；额外预算不能修复 schema 编译错误或输出截断。

- 工具重复恢复：`test_tool_guardrail_gate.py`、`test_tool_call_guardrail_runtime.py`，覆盖 300 次自身拒绝
  与 400 次成功调用的持续计数/提醒、不同归档引用同正文及相同预览不同尾部。
  不清计数、不挤掉原观测、真实失败/不同结果/实际写入及零阈值；拒绝经真实归档和 native 投影后仍有
  计数及换路说明。`test_tooling_filesystem.py` 验证行/字符非文本失败提示及无额外文件转换。
  真实 TUI 复验单文件动画与正常连续工具任务；没有触发重复门的真实任务只算正常链路验收。

- 模型资源与后台策展：`test_provider_request_scope.py`、`test_memory_curator_v2.py`，覆盖同端点前台
  优先、退出释放、pending/游标保留、pre_compact 屏障、request-local 预算、取消连接及旧请求未退出不重试。
  真机只开一路本地慢模型且不派子代理；官网正常模型可并行对照。缓存核对需同时查推理服务槽位日志，
  外部请求/代理别名和缓存容量不能从 TUI 百分比推断。

- 状态读取：`test_agent_tree_model_view.py`、`test_agent_tree_three_layer_status.py`、`test_orchestration_tools.py`，
  覆盖规范原状态、scope 裁决、恢复路径不外泄、实际报告与缺失报告、八节点直接可读及大树省略计数；
  与 `test_tool_context_reducer.py` 联合核对输出外置后仍保留状态和精确逻辑回读入口。
  真实 TUI 验证运行中查询、完成后交接和真实文件读取，不以最终 DONE 替代工具调用证据。
  无活动任务目录时验证当前会话过滤，显式主请求根验证 parent_id 子树；已有终态报告需实际读取。
- 思考预览：`test_tui_renderer.py` 覆盖流式折叠行数、接收字符数、无换行长段落和窄终端，
  与 `test_thinking_display_boundaries.py`、`test_tui_complete_detail.py` 联测；真实 TUI 需捕获多帧计数增长。

- 派工一致性：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`，覆盖启动/运行/终态混合快照不推导父级动作、角色正文隔离、冻结自定义角色、
  主代理保留自身分工与用户限制；`test_tool_context_reducer.py` 验证精简回执保留唯一动作建议。
  真实 TUI 分开记录父级独立工作、分层是否如实创建、活跃范围是否重复写、确实依赖结果时是否正常等待。

- 子代理交接：`test_subagent_registered_artifact_handoff.py`、`test_subagent_output_alignment.py`，
  覆盖自然/结构化结果、孙级身份、最新文件、删除、日志排除、账本链接拒绝、cwd 与相对/绝对路径一致、
  不从输出声明增权、不从内部同名文件隐式搬运。真实 TUI 另核对创建谱系与完成信封中的实际路径。
- 补丁交接：`test_artifact_registry.py`、`test_tools/test_filesystem_tools.py`，真实 handler 到归档再到自然收口，
  覆盖新增、修改、移动、删除、部分失败、同路径不同历史 artifact_id 与当前删除状态，保留权限和执行事实。

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 宿主停止：`test_subagent_process_control.py`、`test_shell_orphan_kill.py`、`test_orchestration_cancel_subagents_tool.py`。
  受控进程验证另开 session 的写入者、忽略 TERM 的后代、独立兄弟保留和无句柄退出核对；
  未确认回执不得标记已终止或触发重派。它们不替代真实 TUI：还需在子代理长命令运行时暂停，
  观察文件保持不变，再从原会话恢复，分别核对原记录前缀和实际命令续跑，不由测试者补产物。
- 父子并行：`test_direct_parent_lifecycle.py`、`test_runtime_guidance.py`、`test_subagent_activity_diagnostics.py`、
  `test_runner_session_pool.py`，覆盖逐个完成、同时释放去重、模型答复/登记等待竞态、忙父级交接、
  慢流不误杀、阶段/审批诊断、旧 attempt、进度快照覆盖、通知重试和心跳回调失败；真实 TUI 组合另列。
- 退出与积压：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`，包含 exact attempt、慢执行存活、
  未知副作用封存、超过分页窗口、消费去重和重启游标；实际模型/故障注入仍需独立 TUI 证据。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 存储组合：`test_conversation_store.py` 另覆盖两个独立实例并发提交同一用量及累计快照，核对唯一行、首次时间和费用；
  联测 `test_conversation_context_usage.py` 的代次 CAS、`test_conversation_goal_tools.py` 的共享时钟/小数余量/重启，
  以及 `test_runtime_module_boundaries.py` 的领域独立导入。旧调用、getattr 与测试替身须一并迁移，不保留旧方法转发。
  本片真实 TUI 须核对暂停期间 Goal 秒数、恢复后的原程序续做、Compact 独立用量和主子账本归属；原始证据留仓库外。
- 目标：`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、
  `test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_conversation_control.py`、`test_run_audit_terminal.py`。
  中断增量另联测 `test_tui_input.py`、`test_tui_agent_navigation.py`、`test_r103_ledger_selfheal.py`：
  空白补全、前后台插话、Esc 与明确暂停分离、同任务换代、恢复总账及历史关闭事件保留。
  后台参数构造必须走到真实回执消费，不能仅断言邮箱写入；先后完成的历史目标不得误触发并行冲突迁移。
  子代理在 Goal 后台轮创建再回报时，持久 task ID 不需要伪造 user 消息；并测 active/complete 与混合普通请求，后者真实缺失仍报错。
  覆盖默认工具可见、无工具/无 Todo 的安全续跑、审批/暂停/错误边界、旧绑定显式迁移、
  命名目标的精确回合上下文、前后台共享时钟；普通模式不得因此自动续跑。
  另覆盖每代理一个未结束目标、父子计费与权限隔离、编辑版本冲突、暂停后保存不恢复、
  子 Goal 在同一 attempt 中跨轮与 Compact 续接、独立历史不覆盖；实际草稿键盘操作仍须 TUI 验收。
  当前真实 TUI 已覆盖主子保存、放弃、编辑中停止，以及旧版本冲突保留草稿；详情与未测组合见持续目标设计。
  `test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary` 复现运行中改主目标被子代理误领，
  覆盖主/子消息隔离与提交模型、确认消费完整链路；共享 root task 不能授予父级邮箱。
- 模型：`test_model_provider_management.py`、`test_provider_sampling.py`、`test_model_unconfigured.py`；
  未配置可进设置但不发请求，发布默认值为空，用户显式选择仍保留。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 模型统计：`test_tui_model_metrics.py`，覆盖协议缓存分母、缺报、重放去重、明细裁剪、重试、主子隔离、重连和宽字符窄屏；独立压缩成功/失败均落账，绑定工作片的不重复结算；统计字段不得影响模型上下文。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

工具正文完整性：`test_tool_output_externalizer.py` 必须经过生产 `ToolExecutor` 与
`archive_tool_output_projection`，而不是仅手造完整 `ToolResult` 给 reducer；覆盖预览阈值以上的
完整文件、分页及继续游标、归档读取 JSON 和显式保留正文，同时保留大输出外置/脱敏回归。
慢模型复读验收沿原始任务和输入文件建立独立 owner/TUI，记录真实出站回执、重复调用、
产物与独立测试结果；不更改测试项目或用硬停计为通过，不并发占用慢模型。
`test_tools/test_shell_tool.py` 另从 Schema、规范执行入口及真实本地进程验证长命令，
与空输入、危险命令、owner 沙箱、超时和非零退出联测；不把旧长度拒绝当安全边界。

后台进程重复观测：`test_process_sessions.py` 回放 33 次 uptime 变化但状态/输出不变的等待，
并核对原始结果哈希、软提示频率、日志同尾增长和退出后重置；真实 TUI 单独记录模型是否采纳提示。

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

本轮慢模型只启用一路 TUI、不派子代理，优先验证长等待、流式、插话与停止；正常远端模型可多路并行。
本地缓存诊断同时核对界面最近一次比例、输入用量和推理服务实际预填充，不用延迟反推缓存，更不把缓存未命中当成上下文丢失。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

默认配置行为必须核对实际合并结果：旧安装若把完整默认 `system_prompt` 或工具延迟目录另存为显式
覆盖，仅升级 wheel 不会替换这些值。测试可在备份后移除测试配置中已确认是旧默认副本的字段，
不能直接覆盖用户定制提示。模型声明、界面 Goal、目标账本、任务绑定和最终工具结果分别取证。

## 提交前严格 gate

```bash
python3 -m pytest <直接相关测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

默认 focused tests。生产代码与测试代码累计增删约 10,000 行或明确另有要求时追加全仓 pytest；文档清理不算实施代码变动。线上 CI 未运行时如实说明，不替代本地严格 gate。

## 发布资料清理验证

注释与示例清理要比较生产 Python AST、默认配置值、协议与依赖标识。允许的人类展示字符串变化需单列；构建包检查 LICENSE/NOTICE、vendor 许可和不含秘密数据。历史重写须先备份、只改授权引用、带 lease 更新，验证发布树不变。
