# LLM_GUIDE

mc2-e11c（2026-10-08，本地实现、待3a复核）：Curator消息与审计只读定位沿有界进程缓存，失效同源完整扫描，
无schema/配置/权限/追加或游标推进变化。键为绝对路径+ID，文件指纹与锚行复核；缓存不是授权或历史防篡改事实。
结构与上界见memory/04-structure，新旧矩阵、扫描量、性能及变异见TESTS；真实生产/Gateway/跨平台未验证。

personafreeze（2026-10-08，本地实现、待 07 复核）：默认只冻结线程人格三份文件，安全行差异走动态尾巴；
快照在首次/内容失效或已提交压缩/所选档案协议变化后的准备刷新；坏内容同锁重建只warn原因，截断target仅报新增。唯一路径与边界见 CONVERSATION_CONTEXT_DESIGN，
测试见 TESTS 首节；真实 Gateway/渠道/供应商缓存收益未验证，不推送、不部署。

w1 第 2 步（2026-10-08，`worker/w1-method-carry`，本地已实现、待 3a 非作者复核）：
主会话方法沿用统一经 `capability/method_carry.py`，隐藏线程账本只记成功 get；逐 run 名单冻结、首次使用稳定显示、
Compact 后入口与资料重读清单、Skill 简单带回及 using/remove 已接真实控制/业务入口。现读开关默认 true，预算仍缓存，原权限/pins 不变。
最终无变异新增/原失败/完整 guards9 242 项通过，十处变异均有效；广回归 18 项在真正基线仍失败，不称联合全绿。
本次 scope 回归和自引尺寸报告准备错误已修并复验。第 1 步沙箱外通过为 3a 任务书提供的外部证据；
第 2 步真实 TUI/飞书、MiniMax、Linux 与生产仍未验证。详见 TESTS 首节和设计第 4 节；下段保留第 1 步交付时点。

w2 integprep（2026-10-08，待 3a 集成复核）：指定 w1 第 1 步已并入；实验臂沿原用户能力路径写真正隔离文件开关，正常/异常原子恢复原字节及权限（含只读）或删除新文件，预算仍缓存。原 false/true 的假 ABC 与 C 仅缓存变异已校准；整合回归保留三个真正基线复现的 Seatbelt 权限拒绝，详见 TESTS，不宣称全绿。
用户 10-07 已决定不开选包、靠目录和点名，入口与会话沿用去重延期；3a 的 selfix 真跑成绩与本整合版本未验边界分别记录在设计第 5 节和 TESTS，不外推生产完成。

w2 selfix（2026-10-07，本地修复，待集成/真模型复跑）：一次选包不再共用推荐限数，只按独立输入预算与模型窗口收候选；
预算不足复用原 router 打分稳定排序，不筛零分，推荐、默认关闭、入口读取与子代理合同不变。离线证据和未验边界见 TESTS，正式复跑由 3a 做。

w1 第 1 步（2026-10-07，本地已实现、待 3a 复核）：能力现读开关的唯一名单与读取函数在
`capability/self_install_switches.py`；选包主/子入口和参数中心已同源，前端目录直接读后端 effect。
两个自动装回执、预算缓存、授权与首请求资格不变；第 2 步会话沿用未做。测试/变异/基线失败与未验证边界见 TESTS 首节，
实际落点见 `docs/design/SKILL_PACK_SELECTION.md` 第 3 节；不据本地结果宣称真实 Gateway/TUI/飞书或生产已验证。

M1 B5第5段叠到17j（2026-10-04，b5s5，`worker/m1-b5-on17j`，接b5r `65f47f2c6`，本地已实施、整体WIP待复审）：
三方应用源 `ff5d761d9..d517a8b7b`，保B4真实handler事件；所有实际review生成决定、排除精确批准跳门，原归档信封→唯一runtime ledger写入→B6展示已经组合转正，不手插账本。
六条初审小项已落实，I4逐字段比门引用与参数哈希但保新调用身份；外五/内四身份与写边界安全退化已注释。最终版本27文件563通过，完整11文件guards187；指定六+三及新增六变异有效。无交互混合门已有deny时账本保留deny、不误计无法审批，详见TESTS。
`_tool_event_context`/prompt装配仍保持17j，b4g由3a先挑；真实宿主启用/沙箱、TUI/IM/Gateway、Linux全量和9b安全终审未验证，不部署。本段覆盖下方“第5待叠”的旧状态，旧段只记原时点。

M1 B5 搬到17j（2026-10-04，b5r，`worker/m1-b5-on17j`，基线 `ebe621d87`，迁移已实施、整体WIP）：
第1–4段来源为 `git diff b6ede99e0 ff5d761d9`，三方应用后手合执行器、注册表、Gateway共用池和文档；常数目录产品脚本重生899项。
B5保持宿主decide后、原审批前收紧；B4只观察真正开始的handler。registry保留17j唯一动态审批读取，sol2两处agent.config读取不代修。
rm-guard拒绝删除零执行/零工具事件，允许更新执行并发成对事件；账本实读strict xfail等第5段，不手插行。
联合697项中678通过、13失败、5准备错误、1 strict xfail；18项在真正17j基线相同失败，宿主启动/启用未验成。命令与限制见TESTS。
ds2第5段迁移线为 `worker/m1-b5-seg5-on-s3`，3a后叠；不等它、不改B7、不部署，不以隔离传输替身冒充真实Gateway/TUI/IM验收。下方历史段保留原时点。

M1 B9（原 M5，2026-10-03，m1b9，`worker/sol2-m5`，已实现，待 be 复审）：用户想做 my-agent 插件时读取
内置 `write-my-agent-plugin` 正文，再按需读模板与参考。工具/面板 v6 已有；两语言模板采用 B1 的 v8 文件入口，
正文只允许 prompt_submitted，full 收紧只列精确工具名且 effects 为空，network 默认 false。握手双能力位、观察空回执，
裁决仅 allow_as_is/ask/deny，不能改参数或降低宿主审批。B7 前生产启用返回 plugin_events_disabled；仅 local/main、
强制沙箱、默认断网和收窄读是 B7 的前置合同，不是本模板 stdio 已验证的保障。模型只交包，不安装、启用、取码或代填。
v8 用源码文件构建器，旧 v1–v5 wheel 才走 wheel 构建器；只构建受信授权源码，中间文件留工作区，缺入口如实报告。
当前测试及未验边界见 TESTS；宿主安装启用、隔离与真实客户端由 3a 后续复核。
M1 B5 第4段（2026-10-04，m1b5，接第3段提交471b7b4fc，整体WIP）：
精确批准只读宿主write_boundary.approved_actions，四调用字段加当前插件/version/activation/gate均匹配才跳门；auto不依赖host approval_applied。主/子真实隔离消费者批准一次后，新身份再问，等待中换代的ask/deny不能贴已应用批准。
GateCall.approved_gate_refs为宿主复制引用，GateReview.approval_applied=True是无协议请求的批准跳门投影，ds2账本不得把它当征询；tighten_plugin_decision与PluginToolGate.merge参数未变。十四相关+完整十守卫539通过，六本段变异有效；第5由ds2做，完整安全复审、B7及实际TUI/IM/Gateway验收仍未完成。下方为历史时点。

M1 B5 第3段主/子与 I4（2026-10-04，m1b5，接57f465dca，本地隔离验证、整体WIP）：
真实执行器/B2池→原轮审批→canonical主claim/child attempt→notices/所属用户决定已覆盖四自动批准情形；子审批冻结execution_attempt_id，恢复换轮的旧记录无效，不另建批准源。
I4用真实请求处理/重排入口与假供应商、停机故障注入验证重新征询；489项含当前完整十守卫通过，不等同真重启/TUI/IM。
第4精确批准重跑仍待做，当前批准后可能重新ask；第5账本由3a改派ds2独立树并行。本线不做第5，不改B7，不部署WIP。下方记录保留原时点。

M1 B5 第3段执行端（2026-10-03，m1b5，接c70c895a6，部署窗口WIP）：
稳定JSON引用附原binding，不改旧ID；前缀同源清洗，仅本次/拒绝；Gateway固定选项，五点先守门。拒绝/无人审批码登记permission不可重试；full带arguments_truncated，样例另派。
隔离Gateway链及十一相关文件+完整十守卫首次468通过；第3段真实主子/I4未验，第4精确重跑/第5账本未做，不可上线。3a外部228通过已解除原启用失败待办，历史原时点记录保留；部署窗口先停本轮。

M1 B5 第 3 段（2026-10-03，m1b5，接 `6ec0fc7f3`，消费端部分接线、整体 WIP）：
四审批入口与自主提供者先用合同层单一 plugin_gate_required；插件请求不复用/写入会话或长期批准，两个等待入口独立防自动模式绕过。
执行端尚未附加引用，审批前缀、I4、无人审批回执、第 4–5 段仍未做，不把隔离标记请求单测当完整链验收。
外部 guards9 曾有十四文件导致本树缺一个新文件、收集失败；收尾清单已回到十文件且无缺失，完整复跑 172 项通过。此前失败保留，不改其他分支或真实 owner。

M1 B5 ds10 修正（2026-10-03，接 `3771f67ca`，第 2 段组件已修、整体 WIP）：
非 ok 构造统一宁严 ask，revoked 合并剔除；直接 GateReply 与解码共用八十字单行清洗，审批前缀不得另写清洗。
第 3 段须点名并守住 Gateway permission_bridge 等待轮询、主/子请求与等待、自主提供者，不能只改两处请求入口。
24 条有效业务红测修后组件三文件 129 通过；第 3–5 段未实施，原启用准备 13 失败仍待 3a 外部核实，不能上线。

M1 B5 第 2 段（2026-10-03，m1b5，`worker/m1-b5-tool-gate`，接合同头 `b531c5fab`，组件已实施、整体 WIP）：
宿主 decide 后接共用池征询，registry 默认 model，两真实宿主构造显式 host_command；只消费宿主字段，不采用参数来源。
排队/启动/回答/追新共享总预算，握手与失败宁严 ask；池关闭不等于停用，撤销以安装快照校准。
超时配置从 B7 移属 B5，默认 2000、范围 200–10000，显式参数边界，模型 user_config 拒绝并保留 PARAMETER_BOUNDARY。
当前组件 105 项和完整 guards9 172 项通过；第 3–5 段审批防自动批准、精确重跑和账本仍未接线，不能视作完整 B5。
后续非作者会话交叉初审、9b 安全终审、3a 沙箱外复验；下列旧阶段记录保留原时点，不覆盖本段进展。

M1 B5 备审补充（2026-10-03，3a 转述 ae，合同已纳入，代码待第 2–4 段）：审批不能只改 Gateway，`conversation/agent_tool_approval.py` 的子/后台入口也须在缓存、长期授权前共用 `plugin_gate_required`，等待自主轮询同样守门；批准不写会话/长期授权。
宿主来源只在 `plugin_management`、`plugin_invocation` 的真实构造点显式设置，registry 与 foundation 自检默认 model；`plugin_gate_ref` 是 JSON 字符串，算好旧 `permission_id` 后附加，收紧门自行读原 `approved_actions` 精确核对。详见 M1 设计第 8/13 节与 TESTS 待实施矩阵，不将文档修订当链路已实现。

M1 B5（2026-10-03，m1b5，`worker/m1-b5-tool-gate`，功能提交 `9cf60731d`，第 1 段已实现，整体 WIP，待 be、ae 复审）：
`plugin_events/tool_gate.py` 仅提供纯匹配、严格协议/安全投影和只能更严的稳定合并，原 `ToolCall` 与安装表仍是权威；63 项组件合同及五个内存变异已有证据。
第 2–5 段的共用池、来源、执行/审批、防自动批准、精确重跑与账本尚未接线，不能视作生产收紧已生效。
full 投影保持合法 JSON 对象、总量限 4000 字符；先去全部内部键再统一脱敏，消息去换行/控制符并限 80 字符。
继续时按 M1 设计第 8/9/13/21 节与本轮 TESTS 续做，不改 B7 前 v8 拒绝门，不自己建第二连接池，不读真实 owner 或碰生产 Gateway。
老格式插件权限第一段（2026-10-03，opp，WIP）：3a 第 6.1 节五项裁定已写入设计稿。
`plugin_permissions/` 只做 R/W/N/E 静态授权、有限路径身份、固定代次记录、同源文字和 B7 构造器请求；不得复制 OS 规则。
老格式插件权限第二段（2026-10-03，opp，WIP）：第一段 ds4 初审可继续（3a 转述）；本段未做非作者初审，不可部署。
老格式插件启用回归尾补（2026-10-04，opp4，WIP）：第三段本身经9b终审通过是3a/9b外部证据，不替本次改写用例验收。
真实启用用例明确测试wide，经完整confirm_command与authorization_id确认，v6事实取runtime；不改产品默认true，不删真实入口或改为替身通过。
补准备前零prepare和撤旧前安装CAS零retire用例，self.*刷新后旧码失效只能重新预览；进入_enable用的是用户最后确认的授权。
B7第四段必须每次启动复核事实与固定permission_json一致，覆盖候选/业务/面板/重连；本次只列验收、不接OS规则。
整系列等正式接线后同批17j，赶不上整体17k；本机失败/跳过及两版基线对照见TESTS，真实链/OS/渠道未验证，不降低默认分两次上线。
老格式插件权限第三段（2026-10-04，opp，WIP）：第一段 ds4 可继续；第二段 ds1 五项清单通过但 C10 两例必须改（均3a转述），本段保留旧确认首句并追加完整新段、原测试不改。本段新代码未做非作者/9b，不可部署。
`plugin_permissions/` 做 R/W/N/E 授权、有限路径身份、原确认运输、单一模式判定/撤旧代和独立回滚导出；不得复制 B7 OS 规则。
原安装表 v4，仅显式旧 v3 已启用激活迁移一次性兼容，查询不写盘，缺字段不是豁免；新授权纳入原激活摘要，同代不能换授权。
`plugin_legacy_sandbox_default=true` 是管理员边界项，模型 PARAMETER_BOUNDARY；原管理上下文/启用执行器真正消费，开走 restricted，关仍完整确认，既有 restricted 不降级；B7 未接拒候选，不是隔离证据。
v4 表对 17i 不可读，会 fail-closed；切旧前用新运行时 `scripts/export_plugin_installations_v3.py` 显式独立导出并保留原 v4 备份，损失权限/兼容事实与旧宽权限恢复必须确认，见设计稿6.4。
执行确认刷新根/程序/解释器，准备与候选启动/发现/清理后发布前复验原固定授权；旧代报告存在不是提交事实，必须核原表同撤旧子提交 release。两处 retirement_operation 独立计算有意由完整目标相等守卫核对。B7 合入后再统一业务/面板与候选；32根原客户端/IM/HTTP handler完整运输合同不是实际 TUI/飞书送达证明。
v7 当次输入读墙另片后续；D 仅旧激活已知兼容边界，G2b 服务端强制/真实 OS/TUI/飞书/生产回滚尚未验证。本段八处独立红绿变异、merge-base复核与测试边界见 TESTS；非作者本段初审后交9b。

M1 B1（2026-10-03，m1b1，`worker/m1-b1`，两条裁定与 ae 意见已实施，ae 复审通过，并入 step17i）：v8 只扩 v6 文件入口的静态声明，
事件/收紧/网络/强制沙箱要求进入原启用确认码；旧 v1–v7 固定序列化字节不变，不另开 Python 轮子订阅路径。
B7 就绪前 B1 先在确认码之前以 `plugin_events_disabled` 拒绝全部 v8 启用，不生成运行时或候选计划，但安装允许。B7 再将暂时拒绝及计划排除换成真正总开关、local/main 与强制沙箱；`sandbox=required` 不是已经隔离的证据。
3a 2026-10-03 裁定：观察 text 仅提示提交，工具开始只能 none；参数只经精确工具的 full 收紧门，不新增 `events[].tools`。
v8 允许只贡献事件或收紧门，不用添加工具/面板；v1–v7 原规则及 v8 必须有订阅的门不变。
ae 复审修订的合同 125 项、常数目录测试 11 项及 --check 877 项一致已有本轮证据；原变异经 ae 确认，新增关闭门变异能抓到。
ae 沙箱外基线 `274cedb1e` 和 B1 头 `bf520e911` 的旧启用文件均 35 passed，六失败来自 my-agent 命令沙箱环境（3a 转述）；本线未亲自外部复验。收尾结果见 TESTS，ae 一并复审，3a 集成后再验 B7 真实链，不据此部署整期 M1。

Anthropic 预算边界（2026-10-02，sol2，本地已实施、待集成）：空区间 `[1024, max_tokens-1024]` 不发 thinking，
出站与回执共用 reasoning_control 的冻结预算/原因裁决，不抬高 cap。配置回执用工厂同源的常规输出上限，
无上限说明只写条件，不把省略字段当作服务商已关闭思考。十一文件 494 项与三项变异有组件证据，详见 TESTS；
真实供应商、实际 TUI/IM、生产 Gateway 未验证。下一步 3a 审阅合入并核真实请求，不由本线部署或改生产配置。

C8/C9（2026-10-02，sol56，本地已实现并完成审查尾补、待集成/真实入口核对）：`/recover <处置> <编号>` 只认 opaque ID，
空或纯空白 thread 在事务前按越界拒绝；非空写入口必须在同一事务复核 thread、未关 TaskRun 和 current unknown，同范围重号拒绝；不带编号的唯一目标旧行为保持。
`/recover owner` 只给完整可信 local/main 管理员，空 thread 历史 unknown 的查看/预览不读正文，确认码绑定完整
`(target_id, attempt_id)` 集合；集合变化零写入，同码重送只读批次回执。共享 unknown→recovered CAS、静止规则和
TaskRun 树不变；部分批次成功按已提交成功数返回 ok，同码重放无论全成或部分成都先标明“该确认已处理过”。临时库三文件 30 项、原五个加返工三个及尾补两个变异已有组件证据；真实 Gateway/TUI/飞书和生产历史处置未验证，
集成后先用隔离 owner 复核查看→预览→集合变化拒绝→确认→重复确认，不要直接拿生产 unknown 做首验。

C7（2026-10-02，sol，已上线 step17a，main de222698b，2026-10-02）：用户智能程度固定八档，唯一换算表在 backends/reasoning_control；
菜单/命令/schema 不再另写档位白名单，发送和回执共用声明筛选。线程/child 保留用户值，投影与发送按候选模型降档。
不要为 Responses 借用 Chat 容量投影或按型号猜支持。三个变异有组件证据，旧 Gateway 控制失败保持在 TESTS；
真实模型、终端与 IM 客户端未验证，建议由 3a 集成后核 xhigh/ultra 出站值及降档回执。

J10（2026-10-02，sol2，已上线 step17a，main de222698b，2026-10-02）：`delivery_quality` 沿原 `_optional_result_hints` 消费成功写入的 stale/last_verification_id，
核对同 run/task 较早焦点；只有多个 stale 才选择，保留总焦点 2—12 界限。两入口共用本轮参数集合逐记录一次，
去重不是工具幂等或完成状态，不持久化；observe 不追加、子代理及已收口不触发、来源/设置/期限变化丢弃。
定向 208、guards9 168 项与三项变异有离线证据，真实 Jev/采用由 be 集成后复测，不增加完成门或强制续跑，详见 TESTS。

J11（2026-10-02，sol56，已上线 step17a，main de222698b，2026-10-02；真实验收仍待做）：主会话与子代理首轮选模共用结构化输入模态判定。
只认 Gateway `input_media`、冻结 canonical media 块与档案 `input_modalities`；不读普通正文、不从文件名或模型名猜能力。
image/video 候选须显式声明支持；全无兼容候选保留原模型并记结构化提示；纯文本兼容旧档案，未知历史仍为
`history_modality_unknown`。本地聚焦 205+18 项、三个变异、guards9 168 项和全部静态门禁已验，size_diff 新增 0；命令见 TESTS。
真实 Gateway/TUI/Decision/收费模型及真实带图自动换模未验证，后续由 3a 集成后按主会话与 child 各复核一例。

P12（2026-10-01，sol，已上线 step17a，main de222698b，2026-10-02）：Curator 的固定引用只走原模型目录解析与后端工厂，默认空值仍沿 owner 选择。
`memory_curator_model_profile` 是安全边界，模型 set/reset/revert 不可改；只在可信管理员用户 `/settings` 的同步作用域允许此键，
actor 标签不是授权。失效保留原未配置失败码、退避及运行账诊断，不回退聊天凭据；旧两覆盖键不保留兼容转换。
定向 235、守卫 167 项与三个变异有组件证据，恢复回归 99 项通过；真实模型、Gateway、终端与 IM 客户端未验证，不能写成生产可用。
后续由 3a 集成部署，绑定实际 deepseek-v4-flash 档案并复核重启与渠道显示；多 owner 须遵守原共享授权。详见 TESTS 和记忆模块文档。

C14 M-B1（2026-10-01，sol2，已上线 step17a，main de222698b，2026-10-02；宿主启用链仍需沙箱外复核）：`plugins/shuohao-novel-gates/` 提供五阶段只读门，
同源声明、逐次读取上下文、原样来源/NUL 与无工作区日志有仓库证据。缺依赖门 skipped，不把 passed 当全部已验。
宿主结果分层 JSON 解码；本机宿主启用失败、嵌套 Seatbelt 跳过仍保留，由 3a 沙箱外复验；真实 TUI/模型未验证。
没有新增配置或宿主通道；后续修改需联测 `test_shuohao_novel_gates.py`、74+20 向量和固定来源摘要，详见 TESTS 与插件 README。
收尾补充（2026-10-02，3a）：只跑 M-B1/跨语言样例等直接相关文件与 guards9，不再执行全仓或无文件列表的-x。
跨语言样例 2 passed、2 failed，均在启用确认处失败；本机限制交外部复核，未变输入的有效证据复用。

C14 第一批 M-A1 + M-A2（2026-10-01，sol56，已上线 step17a，main de222698b，2026-10-02）：`drama-media-shell` 已迁入固定 MIT 上游的
四个离线提示词检查器和 fixture-only 生产作业流程。工作区只走 SDK 0.2.0 逐次上下文，作业记录只进插件私有目录；
包内明确没有付费供应商适配器、Remotion 或限用途小说样例，fixture 不算真实生成。实际包/MCP 聚焦 9 项已验；真实 TUI、
真实模型和正式部署未验证。本轮未启动 Gateway、未改用户配置。后续先审查许可/来源与付费代码排除，再在集成版做原生验收。

C10（2026-10-01，sol，已上线 step17a，main de222698b，2026-10-02）：IM 的 `/plugins` 和 `/plugins@<插件ID>` 已复用 TUI
插件服务；管理仅可信管理员，含可执行程序的启用保留原预览与 `--confirm`。定向 155 项、架构守卫
166 项、导入边界零条与三个变异已验；真实 IM 收发未验证。本轮没有新配置、Gateway 启停或部署。
后续由集成者核对组合后在真实普通用户、管理员私聊和群聊验收；详见 TESTS 与 Gateway 模块文档。

当前能力包验收：固定 27 次执行完成，业务 16/27、原资源执行 12/18；原失败保持，Goal active。归因文档已到 3fd3cff4c，当前十二步骤与七组完整缺项见[唯一 TODO](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md#唯一-todo)及[收口审计](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#收口审计与七组未覆盖范围2026-09-28)。Mac 两版记录已核，Linux 两版未部署；容器源码测试不代替 wheel 发布。C22 的 65536 只证明机制，历史较大窗口提交不能补算当前能力包生产规模。建议下一步先由 Claude 集成缺项文档；只读可并行，产品／发布归 Claude，私有运行归 root，本轮不新开真实模型或 owner 用例。

以下阶段记录保留原时点，其“0/27／待开始”不覆盖当前结果。

固定 `8ef68c5fd` 上的 C22 已完成 G02 修复复验：同一请求四代自动 Compact，第二代后继续包 get、完整 source_ref 原样复制并实际执行 A/B 原程序；pins、输入和配置保持。65536 受控窗口的机制分项通过，交接内容仍有错误，默认窗口自然长任务未覆盖；最终仍 0/27。详见[本轮分项](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c22固定校准后的连续压缩与原资源执行2026-09-28)。

最新 C23 在固定330b、原262144窗口完成一次原生三助手复验：一次派工、同代包授权、两次后台续接、父完整读回及汇总保存已验；全文消费与资料准确性失败，未确认新宿主缺陷。正式315的23867项作者全仓与五静态gate已核，与实际330b执行分别记账。Goal active，下一步固定正式运行字节后执行最终27次及发布核对。只读审阅可并行，私有运行归root、产品与主线发布归Claude；详见[C23证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c23三助手后台续接与交接2026-09-28)。以下候选与准备记录按原时点保留，旧业务失败不改判。

历史结构化补证 v2 已核出同一 M2.7/200000 请求连续第 1–5 代提交；主 owner 77 次提交中，17 次手动精确关联、60 次触发来源未知。控制记录完整性未证，不能用未匹配或 `forced` 推断自动触发；缺当前包 pins 和跨代资源执行，不能替代 C21。见[复核边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#历史-compact-证据复核2026-09-28)。

当前私有验证已切到固定 `f7851cec84f33626554cfd039694fb5cec1effd5`。C20 在官方 M2.7 的独立 131072 窗口、新 CAP06 中完成一次普通交接任务：包选择、同代读取、原脚本同字节物化并执行、输入与配置保持已验；250.573 秒自然终态，峰值上下文估算 91718，低于 117964 触发点，Compact 为 0。连续压缩及压缩后执行仍未覆盖，交接说明存在字段和引用范围误述，最终 0/27 不变。详见[本轮分项](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c20固定f785的128k原生验证2026-09-28)。

以下候选按各自原时点保留，不把旧段中的“当前”“待交接”当作最新运行状态。

当前私有固定cb40cc4a8已含成员路径纠错，119项及严格gate通过。C19三助手并行、两名授权孩子当前包方法/模板get与父级读回已验；第三子读旧源码，父声称汇总未保存，整体交接未过，不能扩成三子当前包全通过。未命中参数恢复/父resolve，未触发Compact。后续按通用机制与模型交付限制分别记录，最终0/27保持，四项收口见[唯一TODO](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md#唯一-todo)。

当前开发切片是C17通用能力包验收：固定51dac1b81已由Claude组合发布并双机部署step13w，本线核对23639项全仓通过、0失败及本地严格gate；线上CI未作为验收来源。私有环境也已精确安装同版，发布与最终功能收口分别记录。Goal仍active，当前缺项以[唯一执行清单](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)为准。

能力包按用户2026-09-27澄清交付通用功能，领域只作样例。C17的65k对照第1代Compact提交后同代读取通过，第2代51342超过49152而失败；Claude负责容量计量和修复。默认262144窗口G04已完成原资源同字节复制与实际执行，但交接内容有字段及限制错误，且本轮未触发Compact；普通G03空选通过但漏负责人。最终27次允许范围验收尚未启动，不把这些开发分项拼成完整通过。详见[当前验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17固定发布与原生开发验收2026-09-27)。

以下各候选按原时点保留；其中“尚未安装”“待部署”等历史措辞不代表当前状态。

A0.3.0已完成141项组件验证、本地严格gate及原938 Gateway的原生热更新。普通A02复验已终态：新人物方法get成功，但原checker被错误物化为JSON信封而执行失败，模型简化自检漏掉来源关系和内容矛盾，独立审阅判业务未通过。原资源完整返回不等于全文实际入模或正确采用；新字面诊断未成功执行。旧文件/配置保持，保留集0/36；下一步先落实已有b496宿主修复到私有运行组合再验，main由Claude独占。SHT-26及持物因果仍未迁入，详见[本轮验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a030原生开发复验2026-09-27)。

第十二候选修复直接展示工具结果的双重归档引用：模型临时视图省略本次物理自归档ref，沿原逻辑锚点读取，canonical/业务引用及外置摘要选择保持。普通定向25项通过，真实模型采用待验，私有宿主仍938；A0.2.2语义失败不因此关闭。详见[本轮验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十二候选模型引用投影2026-09-27)。

A0.2.2构建、本地严格gate及原生热更新已完成；普通A02开发复验终态，业务质量仍失败。workflow完整送达，三分表未get，checker预览的错误归档引用未恢复全文；模型使用与包同字节的旧轮checker副本，自行修引用并完整回读最终稿，结构通过后仍遗漏场次数说明和人物动作矛盾。不能称本轮三分表完整采用、source_ref物化或语义改进通过。详见[本轮原生证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a022原生开发复验2026-09-27)，保留集0/36保持。

第十候选已将固定791与A方法提交8645组合为5ac8c8789，再吸收原作者c71回执修复；新冻结仅服务与其测试两文件变化，12文件343项通过，独立复核关闭原P2。修复前929项和A包65项保留为各自阶段证据，不相加；组合尚未切换私有运行环境。详见[本轮证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c71回执修复与a021独立方法复验2026-09-27)。

A0.2.1已在既有938唯一Gateway上完成原生停用/更新/启用，配置和709个旧canonical文件保持。新CAP01只提交一次原A02需求；模型实际get三份新审阅方法，按原工具回执自行修参数/引用/时长，原checker字节一致并最终结构通过（7镜、3场、60秒）。业务仍失败：SH05放入口袋后SH06直接手持，最终自检却称接续一致；读取方法、结构通过与语义审阅有效性分别记账。该证据属于938+A0.2.1开发复验，最终保留集仍0/36。

能力包三片已实现：原子文件候选的默认关闭语法反馈、B0.1.3交接检查、子代理已授权入口首次准备。
写入诊断不变成完成门；子入口去重须沿原first-request资格，不能凭空历史猜首次或另建采用账。
文件片215项、B交接183项通过并已独立窄复核；诊断异常不再翻转写入事实，路径别名不再误复用快照。
子入口显式选模后漏入口的组合缺口已修并独立复核；claim提取早返回helper后，新冻结源码的35文件977项再次通过，本地严格gate全部通过，线上CI未作为验收来源。
固定main `558eb65df`已完成本地组合；两份直接测试独立36项通过，组合后48文件1301项及六项本地严格gate通过，前后源码指纹一致。固定`938d04aae`已精确安装私有环境，1422成员与源码/wheel一致，B0.1.3原生热更新通过。
第九候选五席复验已结束：B01业务通过，其余四席质量失败。JSON错误回执后自行修复、四孩子首请求同代入口已验；孩子仍未主动get私有方法。X01实际使用AST相同但字节不同的B检查器副本，不能称原样物化链通过；长任务42镜525秒与最终48镜600秒矛盾。下一步先核对包内审阅方法和原closeout事实链，禁止为此添加任务专项硬门。详见[复验矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第九候选普通业务复验2026-09-27)。
Compact固定8419计量修复与e6a46文档已组合，十文件140项及静态严格门通过；上述普通任务不替代独立Compact验收。

能力包第八候选先吸收固定main `54f24ab94`，两处独立复核发现的Jev会话统计/Compact错误范围已由原实施方修复；再组合`04c339eb9`分段请求修复，相关12文件289项通过，与前轮分别记账。
A0.2.0增加v2镜头来源/新增/未知声明，B0.1.2补完整模板与制作交接；新组合69文件1661 passed、9 skipped及严格门通过。固定`f6f93e4f3`已精确安装到私有环境，1417包成员与源码/wheel一致；A/B六次原生管理写成功，六席质量复验已结束：B01/C02在冻结范围通过，A02/X01/E01/L01失败；客观格式问题与来源语义分层处理。
[固定上游覆盖清单](docs/design/CAPABILITY_SOURCE_COVERAGE.md)区分全部入口索引、深读边界及未迁移功能，不给出未经验证的内化百分比。
原X01/E01来源语义与交接质量失败保留。F01第二轮因测试观察器错误未触发控制；观察器v2已离线校准，不能补做已结束任务冒充通过。
F01第三轮实际get后停止、停用、同任务恢复及旧source_ref物化拒绝已验；恢复后未再次get，不能补算读取拒绝。原任务自然终态后B已重新启用，旧pin/选择标记保持；新六席开发集仍只各发一次原冻结需求。
ZERO01全卸载核心CSV及精确重装原生通过；当前保留例各0/3，详见[唯一Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

Compact生成事实已按[设计交接](docs/design/COMPACT_GENERATION_FACTS.md)由Claude实施并交付固定main；能力包线只组合接线与独立复核，不同时修改Compact/Jev。新组合原生验收与实施方自己的真实记录分开。

能力包时长补片只修改A/B包内检查与方法，不新增宿主硬门；四文件142项和同一Gateway原生热更新通过，新版六席已终态但质量仍未全过。
结构通过不代表来源语义或创作质量；包脚本由模型执行，宿主尚无原脚本执行身份绑定，不能宣称宿主已验证。
长任务父派工省略包授权、显式选错Shell目录和未调用原检查器须如实归因，不能自动补授权、修路径或改产物来补验收。

能力包第七候选的唯一准备接缝位于真实主业务首轮build/capture之前，默认开关关闭；回执、纯渲染和已提交恢复候选不进入。
开发时分别检查TaskLink一次领取、辅助调用账、入口只读准入和原请求载荷；权限不得靠“已选择”扩大，取消不得降为普通warning。
本候选31文件901项与本地严格gate通过，精确安装后六席官方M2.7入口/方法及空选已验；短剧业务质量仍未全过，先修包层检查。
开启时纯问答也可能保留普通任务，Goal不变；默认关闭仍沿原晋升。跟踪[唯一Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

能力包候选6的537项及严格gate通过并精确安装。官方M2.7原会话再次Compact生成自然摘要，原文件/pins保持；旧机械回退原因、方法续用仍未验证。此前原脚本引用、两级孙代理同代读取、新客户端续原Goal身份及缺运行依赖的如实交付有独立证据，临时夹具已原生卸载。12个保留例仅首轮完成，自然采用与交付质量未达标；已对齐一次结构化选择/宿主入口加载，正在组合固定main后分片实现。详见[唯一 TODO](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)和[合同](docs/design/CAPABILITY_PACKS.md)；尚未发布。

决策模型P1—P5已由用户指定的接手代理于2026-09-23恢复推进；原代理的停止基线与已验/未验范围见[完整交接报告](docs/tasks/DECISION_MODEL_TAKEOVER_HANDOFF.md)，接手后的Goal、并行边界和当前一步（先吸收main再做12.4第二片）见[执行Goal接手记录](docs/tasks/DECISION_MODEL_GOAL.md#2026-09-23-接手记录与当前-goal)。

child历史说明在不展示正文时不再提前读取完整来源或计算展示窗口，保留原线程说明及核验；三文件31项通过。三宿主seed物化峰值已定位，后续延后/释放尚未实施，12.4未完成。

12.4来源到摘要生命周期首片已本地验收：选中消息保存同源地址视图，原生摘要按原JSON和token口径重放，不常驻整份正文/provider数组。26文件530项通过、独立末审无新确定缺陷；约4.2M字符旧峰值9.68MB降至约1.45MB，翻倍来源约1.53MB，完整hash与覆盖ID保持。三宿主旧请求与无scope入口仍待处理，12.4及总清单11/18不变。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#选中正文生命周期方案2026-09-23首片本地验收)。

12.7固定5e5122b04安装候选的19个变更测试文件613 passed、1既有xpassed，非pytest严格守卫通过。同一原生TUI已验M2.7两轮→原/compact提交→续聊、on4建议保留及显式切换官方M3/OpenCode DeepSeek的真实工具轮和缓存回执；七条业务全部完成，Jev累计65次，隔离设置已恢复且Gateway已停。12.7按已验组合收口，12剩12.4，总清单11/18不变；真实自动异模仍未完成。见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

此前4项后台Compact测试接口缺口已按owner授权收口：先复现4 failed/158 passed，再补fake Store的include_messages及临时canonical消息域，整文件162 passed。原业务断言及生产路径不改；旧全仓八项历史问题另列，第12.4及11/18仍未完成。见[验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4检查点读取已本地改为逐行校验：不再同时保留全账本及全部旧摘要，原writer/CAS和覆盖规则不变。9文件98项通过；约6.5MB账本的测试峰值由26.35MB降至0.69/1.23MB（orphan/已提交链）。选中消息正文及覆盖ID仍驻留，12.4和11/18不提前完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.6本地组合已完成：250K/1M近窗口完整输入、同Agent双会话采用/保留隔离、未知协议/模态及连接变更校准失效；六文件123项通过。消息扫描Unicode空白与数值溢出兼容修复五文件145项通过。12.4全链来源及12.7真实缓存仍开放，11/18不变。见[本地验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.5本地组合已完成：三宿主完整候选保留当前要求、工具schema及输出cap，容量不足不提交；Responses未知cap单列。十文件187项通过，增强断言后新14项复验通过（重叠不累加）。当时12.4/12.6/12.7仍开放；12.6最新状态见首段，18项清单11/18不变。见[容量验收](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

主线最小移植闭包已核对：扫描、等值估算、摘要窗口三组在固定主线临时副本通过97项及相邻139项；无Jev配置依赖，尚未合入或发布。scoped来源/覆盖必须另按合同闭包接入，11/18及12.4不变。见[移植交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要分段本地15文件316项通过：复用原循环顺序读取JSON字符、消费后释放窗口；共享估算器改流式累计且数值保持。修复提示纳入预算，发送及来源EOF后复查取消；writer/CAS不变。全链仍有原消息/覆盖驻留，11/18不变。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4保留历史完整投影已本地实现：Gateway、后台和child的Compact来源/候选不再套普通字符窗，完整材料统一进入原容量门；普通展示保持原规则。73项联合及416项相邻回归通过（含重叠，不累加），整项12.4及11/18不变。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4固定来源范围筛选已本地实现：完整尾界内两遍校验后只保留范围内未覆盖正文；后台先读任务事实，recent_limit=0也不提前全载正文。12文件326项通过，另4项主线独占测试的旧Store签名尚待集成适配，不能称整体gate通过；ID索引、未压正文和覆盖链仍非完全有界，11/18不变。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4消息扫描底座已本地实现：原前向页可冻结完整尾界并限制页字节，幂等追加改逐行完整校验、拒绝未完成尾行，153项定向回归通过。Compact全量来源/覆盖链仍待接入，不按整项完成；见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

集成安装版319004926已在独立测试机完成决策关闭/2秒超时/4秒返回三轮原生TUI，主任务均完成；真实缓存有回执但总输入未节省。全线Jev HTTP 57次，11/18清单不变；设置已恢复关闭、候选Gateway已停，详见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

旧8项短期限失败已完成阶段审计及测试层修订，五文件91 passed、1既有xpassed；生产期限未变。web_fetch历史5秒超时仍缺根因证据，不能视为全仓通过，详见[TESTS](TESTS.md)。

决策模型第12.4媒体整合本地1419项联合与严格gate通过：普通媒体沿原模型发送，未知模态不自动切模型或提交强制Compact；摘要覆盖只到完整文字前缀，原生媒体后缀保留。原媒体M3验收不替代集成版证据；11/18和旧全仓八项失败状态不变，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

当前十步 Goal 的第 4、5 步框架必测项已收口，正在进入第 6 步插件故障和长任务；按[唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单) 的工作队列推进。第 5 步源码 `689e83e85` 已推送 main、同一 wheel 双机部署，原生 TUI168—173 的管理、中文业务、显式短／长分页、重连、粘贴及交互证据见 TESTS。旧 TUI164 的插件工具链成功、模型产物表情和行尾失真，交付失败仍保留并归第 8 步通用核验；第 6—10 步连续长任务和组合验收尚未完成。
第8步精确Compact来源及耐久清理事实已本地组合：48文件1212 passed／24项既有xfail；末审发现的未知尾部候选编号冲突已红转绿，四文件159 passed。正在严格发布检查，未推送部署或启动新版真实TUI。
显示缓存沿不可变发布快照与稳定前缀复用，不逐帧扫描旧历史生成版本键；来源、顺序、宽度和模式变化保持原失效/锚点语义。真实 fd 开发已自然结束并在同一长历史续聊成功，最终补充片的证据和未验证边界见 TESTS。

真实 fd 开发任务用于长对话验收，合成大行数仅定位存储边界。TUI 可见绘制最多 20 Hz、动画 4 Hz，同会话原生 TUI 对照已验证 CPU 降低与按键响应；不会丢弃原始事件或思考，见 TESTS。

TUI 的无活动等待窗不再成为任务失败：原页按同一请求/游标退避观察到 canonical terminal，退出只停止本地观察。官网 M2.7 已复现旧缺陷并通过修复对照和客户端暂停恢复；plain 有限等待保持，见 TESTS。

新增 TUI 图片/视频输入沿 owner 私有内容引用、Gateway ask 和原生消息传递；官网 M3 已识别图片与视频并在重连后继续读取。官网 M2.7 已完成 100 独立身份/50 执行槽真模型验收，替代前轮假模型的验收结论。默认环境仍未切换，见 [媒体合同](docs/design/TUI_INPUT_MEDIA.md) 与 TESTS。

TUI 资源与状态连接修复已完成独立性能线分层验收：稳定块复用已净化内容、客户端退出统一停止轮询、长历史与原文按页读取、近期显示索引有界、空闲 owner 实例按在途和持久事实回收。100 身份/50 执行槽使用真实 Gateway 与假模型分层验收，官方 MiniMax 的真实 TUI 单列；当前原 checkout 和用户默认运行环境尚未切换。边界见 [资源合同](docs/design/TUI_RESOURCE_LIFETIME.md)。

当前十步 Goal 的第 1—6 步框架必测项已收口，第 7 步子代理结果链前四片已从独立工作树应用到原 checkout，组合定向回归、严格 gate 与 wheel 边界检查通过，已推送远端 main，尚待默认 Gateway 同版部署与新版真实 TUI；按[唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单) 继续。第 5 步源码 `689e83e85` 已推送 main、同一 wheel 双机部署；第 6 步沿同一运行包完成插件故障与长任务的多 TUI 验收，未改产品代码。原生 TUI192 的 600 页插件原返回全部正确，但模型最终文件有 306 页金额错误，框架与内容结论须分开读 TESTS。并行的 TUI／历史修复由官方 MiniMax-M2.7 原生 TUI 验过阅读与插话边界，并已随本轮快进推送远端 main，尚未切换既定双机默认 Gateway；旧 TUI164 的表情和行尾失真及其它报告错误仍保留给后续通用交付核验。

第8步结束收口与工具并发段已组合，11文件283 passed／20项既有xfail；取消、审批、记账和延后裁决顺序保持。Compact逐调用来源仍在修复，未发布部署。

第8步工具事实与唯一循环入口已组合，16文件349 passed／20项既有xfail；原状态、effect、身份及执行顺序保持，恢复保留同源清理事实。未推送部署或开始新版原生TUI。

第8步本地候选已完成模型采纳和请求周期两片，并移植有界消息扫描及等值token估算；18文件456 passed、24项既有xfail。模型采纳与请求周期、A/B底座已集成本地主线50cd9e7a7／57baa13cb，源码tree与验收候选逐项一致；未推送新包、未切双机Gateway，第8步原生TUI矩阵尚未开始。

第 1—7 步本轮框架范围已收口；当前进行第 8.2 首片及 Compact 移植边界核对。业务交付失败和未覆盖分支仍单列 TESTS，不能称所有任务通过。
子代理状态／投影／持久收口／父通知拆分及第 7.7 前台清理修复已发布双机；
TUI210／213 核对本机回收与原退出码，214 核对 Linux 默认 bwrap 路径，215 核对三孩子并行与核心隔离。
未覆盖的 Full Access／Windows 清理、模型内容错误、旧测试准备失误继续保留 TESTS，不扩大验收结论。

第 7.8 修复已由隔离候选 `979ed5c76` 集成为 `fc17def5f`：能力事件唤醒后，读取当前会话任务的 completed 状态，
允许原模型最终回复进入既有交付链；未完成的能力事件仍静默，取消／中断、空载荷和幂等规则不变。
不改 BLOCKED 孩子的运行账关闭合同、不放宽权限、不补写旧 TUI212 回复。主线三文件 348 项相关测试及 Ruff、文档同步、严格尺寸、diff、clean-package 均已通过；
源码已随6c5fe6f36推送main；同一wheel（SHA256前缀930f59f6）双机各1,274文件逐项一致，默认入口和唯一Gateway已切换，回滚副本保留。
TUI216正式授权链与218三孩子对照已核账完成；219未实际申请却报告待授权，按场景未命中与模型报告错误保留。
TUI224已实际命中能力事件completed分支并公开最终回复；测试机217又暴露grant碰到旧片仍活、随后孩子PENDING却未续跑，原账与准确宿主已核对。7.9的三处来源修复634d12daf已集成为911a53245：当前canonical投影、精确attempt退出后接续、wake窄字段持久化；候选17个交错用例及9文件236项通过／1项既有skip、严格gate通过。主线授权、worker与后台交付三文件组合通过，Ruff、doc sync、strict-size、diff、clean-package通过；源码已随67bb7817e推送main，同一wheel（SHA256前缀83975aff）双机各1,274文件核对一致，默认入口和Gateway已切新版；TUI223三孩子核心对照通过，222仅覆盖前台接手，224后台能力完成交付通过；220同run续接和最终产物正确，但出现两条后台收尾回复，已确认旧handled BLOCKED通知被待处理快照重新消费，列入7.10修复；221测试机正常续跑与最终交付通过、无重复后台final。线上CI未有本次运行记录，未作为验收来源。

第7.10修复随7280c5b3e发布main，同一wheel（SHA256前缀c3f1242f）双机各1,274文件一致，默认入口与唯一Gateway同版。六文件304项、相邻三文件61项及严格gate通过。新版原生TUI225—227均仅派出回执加一条最终回复，任务／attempt结束、无锁、准确宿主退出；227实际命中旧attempt已结算而宿主未退出期间的grant，随后同run第三attempt执行，且能力事件completed最终回复公开。7.9／7.10本轮框架验收收口；7.10原瞬时快照交错由确定性红转绿用例证明，不把本轮无重复扩大为该交错必然命中。225最终摘要被孩子错误覆盖、226缺汇总合计、227检查程序及能力工具选择问题保留为业务失败／限制。线上CI无运行记录，未作为验收来源。

这份文档是给 LLM/AI 开发者读的项目入口指南。

**开工前必须读，收工后必须改。**

当前十步重构按 [具体 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单) 推进；每次汇报完成编号、当前编号、阻塞和下一交付，本地实现与真实 TUI 验收分开。
旧第 4 步的 4.6 曾细分为七项可验收交付，完成结果见 TESTS；后续按唯一 TODO 的实际依赖推进，不重复审阅或无依据重跑全仓。

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

插件只读上下文沿 registry 的实际 cwd 和权限逐次冻结，经固定 MCP 连接能力协商后放到 `_meta`；
普通外部 MCP 不收到路径，业务参数和安装设置不变。纯协议及路径裁决见[上下文合同](docs/design/PLUGIN_WORKSPACE_CONTEXT.md)，
SDK 与 workspace-peek 已完成本地标准构建、独立 MCP 和原宿主完整装卸组件验证；已通过发布验收并同包部署双机，TUI154—157 的实际验收正在进行，组件测试不能代替实际 TUI。

第 4 步本地源码已接通安装、配置、实际启用、普通工具组合、停用释放、重新启用及卸载。
卸载使用释放返回的完整记录做原锁 CAS；原成功结果持久化并严格读回后，才消费退出证明和回收无人引用的旧包。
原准备仍在执行时保留安装与环境；同包重装保留其包，旧请求不控制新安装，UNKNOWN 不因后来清理成功被改写。
公开目录 v3 派生原提交安装引用，避免同包卸载重装后旧目录再次有效；安装表和原操作历史不新增权威副本。
显式 `/plugins@插件ID` 已本地接到原 HostCommand/ToolExecutor/MCP；工具参数和宿主选择分别绑定，业务用户不借管理员资格。
审批在原执行区间等待，明确批准一次后复查固定代次；重复提交只读原结果，单次连接退出与业务结果分别报告。
TUI/Gateway 的独立命令审批已本地接通，复用原 FIFO 与文件桥；规范 owner 由原服务端明确返回，不由客户端猜测。
断连只取消本命令，结果不明仍查询原编号；完整多 TUI 装卸尚待验收，未推送部署，不进入第 5 步。

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
本地已有 decision 用途、生成隔离和原生 decide 适配器；原目录当前 v4、会话 v10，旧数据显式迁移。
完整清单见 [执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)；子代理选择、Curator 标注和召回重排已本地接通，原设置菜单、原运输与原生探测已本地接通，隔离真实模型/TUI 部分已验，安装版尚未验。
第 12 项原生预检已共用出站清扫和 ToolChoice；`projected_model_context_components` 只计量冻结投影，宿主准备及校准留在外层。三宿主同轮展示由原参数携带，后台实际身份走原发布回传；清除不重决策，新业务轮重置。旧 v2 观测不能套用新口径；完整 Compact 恢复输入接线和跨模型缓存仍按执行 Goal 验收。
transcript Compact 的触发和候选接受已复用普通请求的已知输出预留；恢复目标不能绕过容量门，未知cap不补猜。尚未接入完整恢复输入的宿主仍沿旧会话估算，不能据此宣称12.4/12.5整项已验。
原 PromptRenderInput 保留不可变注入片段；Gateway overflow已在原render/select接入完整恢复请求，只替换历史/操作证据/代次，原CAS后同次生成。只读来源的defer不是force=False；摘要失败不得进入普通业务瞬时重试，停止同时检查真实run token。child overflow现已复用公共恢复器接入；初次/手动入口的完整容量仍待接入，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
后台已有一次性事实准备与纯渲染入口，历史结果携带同次冻结的任务范围；原进度写账不在候选间重跑。后台恢复已接公共完整候选计量，detached/窄审计不能直接继承全局新摘要，见容量审计末节。
Compact新候选写v3，在同一链明确scope、summary base和精确覆盖；局部CAS可保留全线程投影。工具覆盖沿原run/attempt/模型turn/call身份，旧未知不猜不隐藏。后台现将实际应用的摘要视图沿原参数链交给种子与工具过滤，detached从原文按范围/覆盖重建，narrow仅继承本活动轮；Gateway/child现从原canonical loader绑定同一scope/view，后台也复用该loader；后台transcript与carried活动归档恢复候选在原CAS前计量、同次发送。Gateway/child无transcript的carried恢复也已改为先准备、完整计量、再CAS；混合来源在同一摘要候选同时读取消息和工具投影，单次CAS后发送获选材料。初次/手动及真实native工具IR组合仍待接，不把本地切片算12.4整项通过。
严格 HTTP 与协议联合 216 项本地验证通过；后续有界等待/精确取消/原准入及 Curator 迁移联合 315 项通过。
共用决策设置与原 user_config 已接 read/patch/reset、owner/thread CAS、有效值读回；失效配置不能阻止关闭。
原账本终态不再被迟到回调复活，用途分区和字段来源保留至原累计容器，worker 可准确保留未退出调用。
原用量快照以范围和内容去重；同一 TUI 行已增加决策输入，输出留白，部分缺报显示未知。决策请求不做 USD 价格估算或 owner/run 成本累计，本地决策模型无需价格配置；原调用账仍保留真实请求状态和用量。
实际决策服务的策略/冷却、原 HTTP/账本及活动用量行已本地组合接通，短决策不等待持久显示写锁。
原设置菜单已接通，能力推荐已在原工作片准备处接线；原Skill快照/Registry仍唯一，展示选择不能当作授权或已加载正文。
关闭/观察/失败保持原展示；成功时短名单沿原动态段和native工具展示进入模型，省略项仍由原搜索找回。
真实Jev已开始并修复独立选题与百分位概率舍入，脱敏失败样本已replay；不能把接口小样本当作完整业务或已安装TUI可用。
Jev官方总64k与state加最长题32k现有发送前保守容量门，超量回基础方案；它使用UTF-8字节上界，不冒充供应商token计数。
子代理决策候选范围可按原模型配置 ID 经 owner/thread 设置缩小；空数组保留原全部授权目录，配置变化使旧建议失效，官方 MiniMax 来源须查连接而非名字。
子代理实际选模是主代理派工后的自动链：Jev 建议、宿主首轮请求与候选能力核验、采用或保留继承模型、创建执行，不要求用户逐子代理确认或选模型；pending 建议不能被当作已切换。
原私有模型目录当前 v5、共享发布 v2 增加随机持久代次和原锁 guard；模型/凭据/OAuth/发布变更令旧建议失效，普通读取不写。此[目录代次片](docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)已定向验证，仍要由首发送链真正消费，未知事实自动保留继承模型。
隔离官方 MiniMax-M2.7 与 Gateway TUI 已跑通普通会话和三子代理保留原模型；完整子代理窗口/缓存与 M3/DeepSeek 真实切换仍待验，记录见`docs/tasks/DECISION_MODEL_REAL_VALIDATION.md`。
Curator 已本地消费用户后台临时标注，完整材料/提取/验证/游标仍沿原链；后台原 run 不冒用前台会话。
P5-B 新增独立 `curator_relation` 首片：仅本批完整消息与有版本的短 long-term 正文可获临时关系提示；原候选/晋升不由 Jev 直接控制，见 [P5-B 交接](docs/tasks/DECISION_MODEL_P5B_HANDOFF.md)。task_local/control_plane 及 owner 关闭记忆时已不扫描正式库；普通会话召回前跳过尚未开放。[P5-A 审计](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)要求未来只做不删原结果的补充检索，先有可信查询候选和无副作用候选检索接缝。
P5-C 的 `external_material_order` 首片已本地接通，默认关闭：原 web_fetch 多页归档后只追加页序建议，原结果、引用、归档和工具权限不变；隔离真实Jev已验非选择保留及一次自动追加2→3→1，页面源为本地受控材料，其它检索/规划点未验，见 [P5-C 交接](docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。
P5-D 主会话自动选模已完成[只读合同审计](docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md)及[原线程选择版本首片](docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md)：显式同值选择也递增版本，旧线程不伪造手动事件；Gateway 准确车道、首请求容量、跨模型历史兼容和实际自动采用仍待实现，不把版本合同当作切换通过。
P5-E/F/G/H 的[只读审计](docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md)已明确原 Goal/调用账与验证证据的复用边界；原设置服务增加同次 `set/unset`、完整双层 CAS 的内部 `restore` 基础原语，默认行为不变。此后 `/experiment` 授权、经验输入上界与发送硬门已合入；E2 对照记录（只写 Gateway 请求记录）、F1 只读证据评估与 `/experiment apply` 授权内经原设置 CAS 的一次性晋升在本地分支待审，见 [E1 交接第三片](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。P5-G 自动收尾与 H 收益验收未做，不能把恢复原语或本地晋升当成整项完成。
缺数据等非选择结果分别保留，额外等待被原 lease 头寸限制；子代理选择、召回及真实模型验收继续。
消费者在刷新候选后统一复核 `decision_outcome_is_current`，不能把服务返回时的 may_apply 当作永久有效授权。
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
稳定 key 的唤醒发布由 `store_wake_publication.py` 在原 dedupe 锁内先冻结完整信号／观察，再安装原文件；
失败由调用方重试原发布，查询纯读。通用 handled 后可新发，runner 完成显式保留 handled；v1 在写入口显式迁移，
坏账不清空、无 key 不承诺重试幂等，不新增后台恢复扫描。此修复本地验收及未覆盖项见[发布交接](docs/tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md)。
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
  TUI 统计条的本轮模型轮、当轮工具、总缓存（会话累计命中率）、累计会话、决策开关、输出、速度各有独立口径，见 `docs/design/TUI_DESIGN.md`。
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
  不允许文件工具绕过。Skill 逐轮冻结索引、按需读正文；自学习开启时主代理任务完成后在后台自动总结 Skill（只写 `skills/learned/`，经自动闸门、留账可回滚），见 docs/design/SKILL_AUTO_SUMMARY.md。
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
5. **自学习经自动闸门发布**：用户 2026-09-26 决定不逐条确认；自学 Skill 只写自学目录、必须通过重名/脱敏/解析/安全扫描闸门、只改自己生成且用户没改过的 Skill，全程留账可回滚。
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

决策模型12.4首次准备切片：三宿主共享完整请求预检，压缩早于自动选模与模型拒绝回退基线。手动来源和历史估算语义已对齐；定向组合及真实测试边界见TESTS和容量审计。

决策模型Compact恢复的原生工具来源正在本地联验：原IR与archive共用一次分区和原writer/CAS，严格失败不得截断材料后取得覆盖。外层溢出重跑的原生IR传递仍未完成，证据见TESTS及决策模型容量审计末节。

决策模型12.4外层overflow桥接已本地实现，当前联合收口：原生IR按同宿主逻辑回合携带，UserTurn沿原插话包ID过滤，释放异常保存完成事实，旧私有tool_round标记不跨循环。准备权限/前缀不继承；taskless后台不补任务属性。第12.4整体、媒体、超大历史、真实缓存及旧全仓八项失败仍开放；见TESTS首节。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
新增 TUI 图片/视频输入沿 owner 私有内容引用、Gateway ask 和原生消息传递；官网 M3 已识别图片与视频并在重连后继续读取。官网 M2.7 已完成 100 独立身份/50 执行槽真模型验收，替代前轮假模型的验收结论。默认环境仍未切换，见 [媒体合同](docs/design/TUI_INPUT_MEDIA.md) 与 TESTS。

决策实验晋升提示 J6（2026-10-01，已上线 step17a，main de222698b，2026-10-02）：只从新写入的 applied 回执生成提示，沿原 host_notice 队列、当轮流、canonical final
和 IM DeliveryService 送达，不开第二条通道；原 promotion 是唯一幂等账。回执冻结规则与 promotion_id，设置撤销走 /model 本会话逐字段恢复继承。
隔离链路、TUI renderer 与三项变异有证据，真实终端/飞书收信未验证；改动时联测晋升、host_notices 与 adapter_manager，见 TESTS 的 J6 节。
