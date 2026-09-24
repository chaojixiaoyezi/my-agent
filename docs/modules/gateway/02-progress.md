# Gateway 维护状态

决策实验对照记录与授权内自动晋升（本地分支 `claude/decision-experiment-records`，待审）：只观察实验调用经原账结算后，结构化对照条目（身份、配置版本、基线/候选名单、结算视图）经能力观察出口拆出写进同一请求记录的 `experiment_records`；回合正常收尾时按结构化工具账补写实际调用工具名，停止/关闭的回合不补写。`/experiment apply skill_tool …` 另授权宿主在证据规则（≥3 可比较样本、全部 charged、短名单召回 1.0、有节省）满足时，于回合收尾在精确回合锁内经原设置 CAS 把本会话 skill_tool 改为 apply，用户后改、撤销、到期、被替换都跳过不覆盖；回执写在 `experiment_records.promotion`，已有即不重试。普通请求零 I/O、请求字节不变。详见[E1 交接第三片](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。

决策实验授权入口（本地分支 `claude/decision-experiment-send-gate`，待审）：HTTP `/ask` 与文件队列沿 `/audit … prepare` 同一任务命令机制接收 `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>`，参数冻结进排队请求的 `system_task`、模型只见任务正文；新增 `request_experiment.py` 在主轮绑定后、首个模型调用前于精确回合锁内写 `experiment_grant` 回执并调用 E1 授权原语，重放/重启不再授权，失败只提示用户、不阻断业务。发送硬门、经验输入上界与结算归决策服务和传输层，详见[E1 交接](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。

能力推荐观测写进请求记录（本地分支 `claude/decision-capability-observation`，待审）：真的发起过能力推荐决策时，结构化观测（码、版本、名称与计数，无正文）经独立 observer 追加到 `capability_presentation_observation.entries`，最多 8 条；与模型观察同一 active-turn 事务，回合终结时照原语义抛中断，其它写盘失败只放弃这一条，内存请求同步更新。原展示回调、已有键不变。

Gateway 消息文件流式读取（本地分支 `claude/decision-gateway-message-reads`，待审）：建索引、近期产物、追加与补写去重不再按行数整块物化尾部，改为与原实现逐项等价的字节有界流式读取；4.2M 字符夹具上准备期峰值 21.33→0.82MB、全程 22.65→10.03MB，每次请求三次读取约 122→19ms。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#gateway-213mb-峰值来自整块读取消息文件2026-09-24已实施待审)。

媒体会话越过压缩点（本地分支 `claude/decision-media-preflight`，.9 真实验收已通过，待审）：未压历史带图时，preflight 只守窗口硬上限，越过压缩点也不再整轮失败；越过窗口时强制恢复报 `COMPACT_REQUEST_NON_TEXT`，客户端文案说明是图片等非文本内容使压缩不可用。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

12.4来源生命周期首片仅机械兼容显式只读Sequence：`_gateway_conversation_refs`按是否给出来源启用完整投影，防止换容器后误走普通展示窗口；history_projection接受非字符串Sequence。宿主的完整请求冻结/释放尚未重构，相邻回归另行记录，不把本片当全链内存收口。12.7固定旧包的同会话压缩/显式跨模型真实缓存另有证据，详见[真实验收](../../tasks/DECISION_MODEL_REAL_VALIDATION.md)。

12.4 第二片 2a（本地分支 `claude/decision-12.4-2a`，未合入）：Gateway 上下文与恢复候选只保存只读历史来源，移除 `_gateway_conversation_refs` 和具体副本；4.2M 字符下种子准备驻留约 54KB，首次发送前峰值从 29.8MB 降到 21.3MB，摘要期峰值留给 2b。独立评审后：来源冻结投影时刻，终态折叠不随解析时间变化；删除 `_conversation_prompt_section` 生产不可达的 `include_transcript` 正文/摘要分支，原先断言该分支的两个用例改为断言生产路径（摘要只在种子历史段，操作证据只在上下文段）。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子只读来源2a2026-09-23本地)。

12.4保留历史完整投影已本地实现：Gateway、后台和child的Compact来源/候选不再套普通字符窗，完整材料统一进入原容量门；普通展示保持原规则。73项联合及416项相邻回归通过（含重叠，不累加），整项12.4及11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway Compact的原visible范围规则现编译成逐行selector，公共message_selection沿固定完整尾界两遍验证/筛选；后台通过原Store延后正文，共用同次任务范围。writer/CAS与执行身份不变。12文件326项通过，4项主线独占后台fake Store签名待集成，整体gate未通过；12.4仍开放，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

决策模型第12.4媒体整合本地1419项联合与严格gate通过：普通媒体沿原模型发送，未知模态不自动切模型或提交强制Compact；摘要覆盖只到完整文字前缀，原生媒体后缀保留。原媒体M3验收不替代集成版证据；11/18和旧全仓八项失败状态不变，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway/child的无transcript活动归档已移除先粗估提交再重新准备的旁路，统一复用完整请求候选与同次发送。混合transcript+carried在公共恢复器同时替换原历史及已标记工具交接，单次CAS封印双来源；普通摘要失败、未知IR、超量及取消不发送恢复业务。本片32文件738项与严格gate通过，准确边界见TESTS；初次/手动与真实native工具IR组合仍未完成。

Gateway/child现通过原canonical loader绑定同一Compact scope/view，真实恢复参数在CAS后取得获胜checkpoint。后台也已接公共完整请求恢复及活动归档纯投影；定向验收见TESTS。初次/手动、其它宿主活动归档和混合超大来源仍待统一，12.4保持未完成，未部署。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

Compact检查点底座已写v3，区分提交前驱与摘要基础；局部CAS保留全线程摘要/游标，新工具恢复按完整执行身份处理。后台实际选择scope并将同一摘要view交给注入和隐藏的接线尚未完成，12.4仍不关闭。

后台上下文的 `prepare_background_context` 保留原事实读取与进度对账，`render_background_context` 只消费冻结值并调用原预算器；`BackgroundHistoryProjection` 保存同次任务范围与摘要投影，纯种子投影不重读任务。完整后台Compact接线仍待作用域检查点边界闭合，不能把全局新摘要给detached或窄审计事件。

Gateway恢复协调已抽到 `agent_core/compact_request_recovery.py` 与child共用，原payload/CAS/取消证据保持；Gateway模块只负责自己的历史投影和边界事件。

- 第 12.4 项 Gateway overflow 已本地接通完整恢复请求计量：只读保留原来源，真实请求准备后生成摘要候选，原 checkpoint/CAS 成功后直接发送获选材料。两协议、工具开关、取消/代次竞争/摘要错误及后续工具轮等 16 文件联合 337 项通过；HTTP 为内存替身，未部署。子代理、后台、初次加载及手动 Compact 仍待接入，完整进度见 `docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md`。

- 主会话自动模型选择的 Stage A 已建立原线程版本事实：`model_selection_revision/source/last_explicit_revision`
  随原线程一次原子更新，显式同值选择也前进版本并终结 pending 子代理建议；旧数据全缺才归一为未知，坏字段拒绝。
  本片只提供宿主并发/恢复事实，尚未启用主会话自动采用，也不增加逐片确认或永久固定模型。
- P5-D Stage C 在后续独立片已把 Gateway 请求级建议接到主会话首次真实发送前：原完整请求与候选 provider payload 验证、目录代次→准确车道 T→线程 CAS；发送前明确拒绝才回原模型一次，HTTP 后不跨模型重发。fake HTTP 本片34项、联合302项通过，真实供应商验收待做；工程容量估计不能称为精确 token 上界，详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md`。
  详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md`；Gateway 的 lane/模型作用域重排仍待后续片。

- Jev 能力推荐的同一 Gateway 请求展示复用已核对的内存选择：原执行回调保存是否评估过和采用的 Skill/工具展示，transcript Compact 与超窗重试按当前身份、权限和连接重新核验。无效则清除本回合旧值，已评估回合不再次调用 Jev；新请求从基础面开始。122 项本地组合覆盖 DB attempt 轮换、动态名卡/schema、失效和单次建议；原溢出及更多回归仍在收口，尚非真实 TUI 验收。

TUI 观察超时不再标记业务失败：canonical terminal 优先检查，同请求/同游标退避续等，页面退出收口；plain 有限等待保留。官网 M2.7 原页迟到回复及暂停恢复对照通过，详见 TESTS。

独立资源线新增连接寿命和空闲 owner 回收：16 个 HTTP worker / 128 在途不变，socket 空闲 5 秒、排队 2 秒后明确拒绝；状态计数改为目录类型复用。请求持有 owner 精确实例租用，维护不续空闲期，60 秒空闲且无持久硬事实才退池；不改执行状态和原队列。100 身份/50 执行槽已在受限测试机用假模型验证，官方真实 TUI 单列；尚未合并默认环境，见 [资源合同](../../design/TUI_RESOURCE_LIFETIME.md)。
- 插件面板入口 `/client/plugin-panels`（第 9 步，本地开发）：可信来源检查先于读正文，owner 只由 Gateway 作用域解析，冷 owner 不加载实例；
  活动投影复用 `conversation_agent_activity` 只读结果，交给进程内唯一展示服务；服务随 HTTP server 停止关闭。合同测试见 test_gateway_plugin_panels。
  第 10 步：另附本 owner 会话列表的惰性读取函数（只在面板订阅 `sessions` 主题时调用，白名单字段，读取失败按空列表）。
- 插件宿主只读 API `/plugin-host/query`（第 10 步）：回环来源 + 按插件激活发放的令牌，只读主题白名单；服务启动时登记回环地址、停止时清空全部令牌。

插件命令流的取消原语现直接引用 `common/cancellation.py`，不再越层依赖 tooling。保持逐请求取消和原审批运输，发布前回归进行中。

- 独立插件命令的交互审批已有本地实现：原 HTTP 请求线程执行，消息流运输原审批和结果，心跳仅检测本连接离开。
  同机 TUI 使用原 GatewayPaths 及服务端规范 owner 派生审批地址，覆盖按用户隔离关闭时的 owner 映射。
  复用 StreamApproval 和原文件桥且禁用会话批准缓存；每次 Enter 的令牌不借主/子任务，断连不自动重发。
  临时 HTTP/MCP 与 TUI 控制器组件已验证，完整实际多 TUI 装卸仍待验收，未发布部署；详见 TESTS。

- 配置命令已有本地接线：HTTP/direct 共用原管理员、来源权限、宿主请求和 ToolExecutor；正文只含文件引用，值只写私有安装表。
  安装表 v2 同次保存配置与版本，旧 v1 显式迁移；目录 v2 带安装版本使旧配置请求过期。Gateway/客户端开发回归已覆盖原入口。
  查询仍只读原结果，UNKNOWN 不重跑；没有新增队列、Agent 初始化或后台进程。启用、撤销与真实 TUI 装卸尚未完成。

- 第 4 步环境准备已有内部源码与临时 venv/pip 组件验证，尚未接 Gateway 启用动作。
  原 owner 配额锁增加显式非阻塞准入，旧调用默认等待不变；环境竞争时返回配额不可用，不另建锁或后台队列。
  原安装状态不因候选目录存在而启用；激活、撤销和实际多 TUI 验收仍待完成，详见环境合同及 TESTS。

- 第 4 步管理适配已在本地接线、未发布部署：`/client/plugins` 与 ask/control 复用原管理员授权，安装进入独立原运行及唯一工具执行器。
  查询按原请求只读，不初始化冷 owner 或重跑操作；超时保留未知及查询编号，目录刷新失败不抹掉已知结果。
  模型配置和插件管理共用 `owner_conversation_store.py`；完整代理和冷入口共用 owner 路径权限裁决。独立环境、激活和撤销仍待完成。

- 宿主目录片已发布同版双机，TUI 151—153 所测框架入口通过：`/client/plugins` 和 ask/control 共用原 owner 解析，返回不可变声明及 revision。
  冷用户不创建 Agent/thread；无中间件本机与群聊 metadata 保留原规则，不重做 auth。
  旧版本或缺失业务版本明确拒绝，不自动重放，不进入原控制或队列；实际插件贡献仍为空，装卸尚未实现。

- 参数次片已发布同版双机，所测 TUI 入口已复验：`ask/control` 在原鉴权后消费公共插件静态帮助或结构化错误。
  不新增 ControlKind，不触发旧控制回执持久化、guidance、模型或普通队列；后续宿主片已接 owner 声明投影，实际插件执行身份留生命周期实现。
  483 项相关回归及严格 gate 通过；TUI 146 的 Tab→Enter、两端各 13 类命令检查通过，143 原失败及普通任务质量单列。
  TUI 150 的活动请求首秒已验证静态命令分流，原 attempt 自然完成；未进入原生正文、guidance 或 Shell。

- 插话网络重放已收紧到原 mailbox 的旧/新 turn 排序锁与准确回执：先修提交/确认批次，只有最新 pending 才预留后继。
  回应显示实际回执状态；候选 ID 与 DB current 成对 CAS，半写失败沿原 pending 重试，模型与 runner 启动留在锁外。
  文件模式准入也已实现并发布，累计源码全仓与严格 gate 通过；TUI 138—142 分项复验和未实测旁支见 TESTS。

- 当前停止源码在主 Goal/task 外层锁下短读 Gateway T，释放 T 后进入 creation→子 Goal；主/子资源在同一控制边界固定。
  异步清理只接收冻结批次，不持 agent、不重扫后来恢复的孩子；PTY 或子树准备错误仍保留其它已提交清单。
  终态孩子保留业务结果，独立 runner 沿原心跳转交原轮取消。已同版部署双机，实际主后台、孩子、PTY、孙代理与另一会话隔离已验。

- 主资源停止已发布：持久主任务按正式 task/run/attempt 关闭权限，再冻结 v2 后台清单。
  原热请求绑定过期时不向恢复轮发任务中断；停止准备与 Goal 显式恢复串行，旧后台片在绑定新 attempt 前检查中断。
  进程清理在锁外消费原清单，部分失败保留已提交回执；后台退出确认与 PTY 异步请求分开。
  该片 20 个相关文件 735 项开发回归通过；后续源码已补 direct/local 与无持久任务热请求的主链，完整子树后台资源现已在源码接通固定清单，TUI 137 原失败未关闭。
- 无持久链接的热请求在原 T 锁内读取正式运行绑定，释放 T 后关闭精确权限并冻结主资源；已晋升、会话/绑定不可读和过期代次返回未确认。
  不补建 task link 或按请求编号扫描历史 main；此旁支本轮仅有开发回归，不能借 Gateway Goal 验收声称真实命中。

- 公共命令首片已发布并同包部署：HTTP ask/control、普通文件提交和旧队列执行统一读取公共命名空间判据。
  `/plugins@` 的缺 ID、异常后缀与正文参数均明确拒绝；原请求、中断回调和旧 guidance 保持不变。
  不新增插件控制类型或执行器，187 项相关定向（含文档测试）通过；实际命名空间与普通工具链已验，TUI 137 暴露的后台资源停止缺口已修复并有新版独立复验，参数与宿主声明目录随后独立发布验收，第 3 步本轮框架范围收口；真实插件装卸与业务权限仍待后续实现。

- 同一前台请求和后台工作片跨 Compact 保留宿主的精确拒绝列表，修复重建运行参数后再次询问已拒绝调用的问题。
  与子代理 Goal/Compact 接续共用运行参数链，不新增持久表、不提升批准、不改变控制语义。
  529 项相关定向及本地严格 gate 通过；当前为本地候选，双机安装版复验尚未完成。

- 请求适配已独立上下文、绑定、历史和输入渲染，后台从会话领域共享完整历史行；原锁、持久路径和提交顺序不变。
  请求执行文件 3,630→1,182 行，110 个定义/常量逻辑一致、666 项定向通过；新版双 TUI 暂停、压缩、重连、
  70 秒原程序续采和双子代理插话路径通过。报告初次遗漏合计、错误心算和未完全修正的间隔范围保持失败记录。

- 请求编排已分离流式缓冲、事件投影及审批组件，恢复直接沿原 chunk 地址发布终态。
  前后台 Compact 携带共用纯计算，释放和提交时机保持；定向 510 passed、3 skipped、3 xfailed，
  严格尺寸 hard=0、基线不变。真实双 TUI 的子代理/插话、审批缓存、压缩重连已验；暂停后工具仍写入的
  失败已沿公共进程树终止修复并复验：暂停静止、压缩重连和真实程序续采通过，另一会话未中断。
  强制终止的 native 信封缺口保留为边界；请求适配后续拆分见上项，下一批进入存储组合。

- 后台单片执行、历史提交与交付已经分别独立；调度器保留准入、唤醒确认和退避。
  交付沿原顺序外发、canonical 提交及整封冻结，状态在原抑制位置实时读取；没有新队列或旧入口转发。
  四路真实 TUI 已覆盖返回、停止续做和 Goal 压缩重连；路径和时间戳产物错误按失败留证。
- 同任务新请求的归档先使用绑定后的 canonical run，避免 workspace 与收尾身份冲突而残留 RUNNING。
  request 保持当前消息编号；准备目录失败只结算本次新 attempt，既有 CAS、权限和未知副作用门不变。

- 后台工具策略已从 `runtime.py` 独立到纯计算模块，目录、owner/task 收紧及投递限制语义保持。
- 后台 Goal 状态处理与地址选择分别归 `background_goal.py`、`background_routing.py`；原事务、读取时机和租约顺序保持，删除混合职责 GoalMixin。353 项相关回归通过，新安装版实际 TUI 尚待验。
  现有后台运行、观察、进程测试及独立导入边界共 `201 passed`；真实多 TUI 长任务矩阵已留证，边界见可维护性评估。
- 后续上下文与历史种子拆分保留同一 canonical 历史、任务范围和失败合同；不迁移执行权、锁及投递事务。
  当前候选真实双子代理返回与插话通过，Goal 暂停、压缩、重连后恢复到 8 条唯一记录，平方合计 204。
  同轮发现首次 `/goal` 丢 TUI 工作目录，已沿控制回执与共用校验修复；355 项定向通过。
  新 TUI 的实际 pwd、线程 cwd 和产物一致；启动后替换为空目录外部链接，在创建 Goal 前被拒绝，无外部写入。

## 当前事实源

单 Gateway 管理请求、会话控制、调度与交付；多个 TUI 是独立客户端。owner/thread/task/run/attempt 必须保持显式，展示与扫描索引不拥有执行权。

## 已落地约束

- 真实多 owner TUI 补验发现第二层标识遮蔽仍改写路径中的 owner/request；已让标识投影使用同一
  私有通道事实并保留路径，独立编号继续遮蔽。最终回复/流式/历史与外部通道分别验证。

- 本地队列前台消息按宿主 `cli_chat/cli_gateway` 来源选择私有展示通道；自定义 owner provider
  继续只负责身份，不再导致完整绝对路径被砍成文件名。流式、final、repair 和落账 channel 同源，
  历史回放不二次缩短路径；HTTP/IM 和未知来源保持原脱敏，rich 开关不扩大可见性。

- 停止结果不投递空回复，但保留已经发生的原生工具往返；模型/工具循环抛错也先通过精确会话回调
  提交历史，再返回原错误。空正文异常沿正常 final/repair 去重，结果未知不能假称成功或已压缩。
  前台正常/慢模型及子代理真实 TUI 已验证 5/7/10 组中断前工具往返进入下一轮投影。
  后台静默工作片补齐 native 写入；公开 final/投递重试按独立宿主回合去重，不补造旧版本缺失历史。

- 旧会话未配置模型仅冷却仍会反复报错；已改为 typed 配置依赖等待，当前会话选择可用模型后恢复。
  普通错误保持原 30 秒单调时钟冷却，健康会话不受阻。退避独立、有界、线程安全，原持久事件和 Goal 不变。
  后端构造与恢复检查复用同一缺配置判据；384 项联合定向通过，真实 Goal 菜单选模型后原任务自动完成。
  原有 Goal 错误收口不再消费配置依赖的 wake；其它错误与显式暂停仍保持原边界。

- 主会话后台命令完成通知进入原 wake 队列；owner 硬事实发现覆盖未发布记录，Gateway 重启可补发。
  活动回合消费同一事件，不另起主代理。自然收尾补报共用队列和 claim 准入校验，回执未落盘先等待。
  新版真实 TUI 首轮结束后收到约 91 秒采样退出通知，自动读取日志并公开汇报；配置可关闭。
- 长后台片已接收完子代理结果后，开始时冻结的 child phase 不再抑制 final 或要求无事件的额外轮次；
  当前子树、未读邮箱和 Goal 状态仍生效。定向竞态覆盖，新双子代理 TUI 有最终回复和 completed 记录。

- 客户端等待补修：系统时间前跳导致 TUI 误报等待超时、后台却继续执行。轮询入口将 deadline 转为单调时钟，
  活动续租也使用同一时钟；前跳、回拨、真实无活动超时及队列租约定向验证通过。
  正常模型真实 TUI 注入客户端一小时校时跳变后，七轮任务自然完成并收到唯一终态；未修改主机时钟。

- 新中断语义及后台插话已过真实 TUI：主代理 `/interrupt` 仅中断当前轮，active Goal
  保留原任务身份并使用现有去重 wake；`/stop` 才暂停目标并回收子树。普通聊天不暗中恢复暂停目标。
  新 attempt 与关联变更使用同一转换锁；旧取消结论不能覆盖新一轮，绑定冲突不伪称历史损坏。
  后台普通输入不再只认 processing 请求；精确 running claim 可沿原 guidance/输入回执消费，裸活动链接仍不授予插话权。
  暂停后合法恢复在 attempt 换代事务重开 TaskRun，历史关闭记录保留，避免最终完成仍挂旧 cancelled。
  后台模型输入编号与原 task 一致，回执真实消费后撤下等待提示；先后完成的历史目标不触发冲突迁移。
  六路增量验收发现子代理完成信封把该 Goal task 编号当普通 user request 查询；现先验证 Goal 归属，
  排除不对应用户消息的持久编号，混合普通请求保持严格历史校验。原现场和第二个新目标真实复验已过。
  追加任务的主代理追问又暴露新消息编号误作执行 run；现主入口回填真实 run/attempt，保留消息 ID。
  真实权限门、Compact 再入、旧代次拒绝定向已过；原会话恢复 14 项测试通过，另一次等待两名孩子时
  追问可执行真实命令且孩子继续运行，未放宽权限门。

- 前后台使用同一 canonical 历史，展示摘要的 recent limit 不裁掉模型历史。
- 后台答复先落权威会话记录；模型 provider 名不能冒充消息通道。
- 请求重复、控制 outbox、父级唤醒和交付回执使用稳定身份去重。
- 就绪检查、监听地址与鉴权分开；后台扫描不能阻塞客户端输入线程。
- 模型统计通过原有有序事件和会话活动快照投影；主/子独立保存最近数字，渲染和逐 token 到达都不扫描用量文件。
- 正常回合、持久目标与依赖等待使用不同结构化语义，不因临时沉默自动宣告完成。
- 持续 Goal 的安全续跑不依赖工具次数或 Todo，统一使用去重 wake；每个命名目标有精确任务身份。
  目标回合快照区分兄弟目标，前后台 Store 共用目标时钟。单目标真实完成，多目标最终表现继续复验。
- 未配置模型时 Gateway 仍可承载设置与历史；生成入口返回 `MODEL_NOT_CONFIGURED` 并提示 `/model`，不选择占位或离线模型。`gateway_status` 只投影当前调用方模型，不再向模型展示另一份启动默认模型。

## 未关闭

执行器无结果退出、恢复积压公平性、旧目标冲突与小时级慢模型组合仍需修复或验收，见 [全局状态](../../../STATUS.md)。不要将进程存活等同于每条 runner 存活。

## 修改入口与验证

结构见 [04-structure](04-structure.md)。改生命周期或恢复时，检查运行账本、队列、wake 回执和 TUI 可见终态。最终验收按 [TESTS](../../../TESTS.md) 走真实 TUI；生产用户会话、秘密配置及私有日志不纳入仓库。

首次自动Compact已本地接公共完整请求准备：原会话加载暂缓提交，PromptBuilder冻结后先压缩再自动选模。手动Compact在车道内按全线程来源判空，回执只报告历史估算。组合验收见TESTS，真实供应商与完整IR边界仍待验。

首次恢复宿主只在请求带有会话来源时安装（2026-09-23 修复）：未绑定 thread 的 ask 此前会在 `compact_source` 为空时抛 AttributeError，导致无会话请求全部失败，Gateway 场景测试因此失败。现在与 overflow 入口共用同一判定，没有来源就不安装宿主；入站附件无效的 `INPUT_MEDIA_INVALID` 也已登记到唯一错误合同。

外层typed overflow已接同宿主原生IR carry；真实循环释放未提交插话，Gateway按ID过滤后交下一次完整准备，原ToolCall身份和完整正文保留。恢复权限和模型前缀重新准备；无可压来源显式拒绝。此片隔离联验中，实际安装版与供应商证据仍待补。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
TUI 媒体请求已接通：input_media refs 与 ask 执行选项及幂等指纹同行，worker 在 owner 解析后验证路径/大小，再进入原 native user history。官网 M3 图片、视频与重连续问通过；官网 M2.7 100 请求/50 槽全部完成。详细资源口径见 TESTS。
- 客户端错误文案新增 `COMPACT_VISION_SUMMARY_FAILED`（随图摘要本次失败，下一次压缩自动改走归档引用，不必换模型），先于通用 `COMPACT_` 前缀匹配；见 [媒体压缩策略](../../design/COMPACT_MEDIA_POLICY.md)。
- 2026-09-25：实验授权回执 `request_experiment._receipt` 的可选字段改为显式关键字参数（`authorization_id`、`code`，非空才写入），回执形状不变。原先的 `**fields` 违反架构守卫 `test_product_code_has_no_var_keyword_service_interfaces`，是全仓回归发现的。
