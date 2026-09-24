# 决策模型 P1—P5：暂停与完整接手报告

## 0. 先读此处

> **2026-09-25 更新**：本文件是 09-23 暂停时的历史快照，最新状态、证据与建议下一步见 [决策模型最终交接](DECISION_MODEL_FINAL_HANDOFF.md)。

- 日期：2026-09-23。用户明确要求“停下来，写交接，让另一个 agent 接替”，并要求写清完整 Goal。
- 应用 Goal 已调用暂停接口并返回 `paused`；不是完成，也不是失败阻塞。当前任务不再开发、测试业务、调用模型或部署。
- 原开发子代理 `compact_source_lifetime_design` 已中断；其余审查/基线子代理已完成。停止时没有未提交的生产代码。
- 停止时 HEAD：`3855e5a67f72bbdce473e56ee5a71dcda785018f`，工作区干净。本次交接仅新增/更新文档；最终文档提交可通过 Git 查看。
- 分支：`codex/decision-model-integration`。工作区为 Codex worktrees 下的 `decision-model-plan/my-agent-dsh`，使用 `git worktree list` 确認准确位置；不要在原主工作区直接续写。
- 本任务线程 ID：`01a0bc61-771e-7021-87f3-995d6734420e`。
- **完整范围仍为 P1—P5、18 项；01—11按各自本地证据完成，12—18未整项完成。11/18是项目数，不是工时百分比。**
- **最近准备实施的12.4第二片2a，停下时只有设计、基线和共享归属确认，没有开始生产实现。** 不要寻找或假定存在已完成的 `history_seed.py` / source seed 新接口。
- 接手代理仅在用户明确让其继续时恢复工作；不要因看见旧文档中的active/“正在做”就自动续跑旧代理。

## 1. 应用 Goal 原文与用户后续确认

### 1.1 原应用 Goal 原文

在独立工作区按 docs/design/DECISION_MODEL_INTEGRATION.md 实施 my-agent 可选决策模型接入。先与“模块重构”任务按当前 HEAD、脏文件、精确文件/函数和验收环境对齐，保持其插件审批与 TUI/Gateway 工作不受干扰。P1：复用 owner 模型配置和既有授权/配置服务，增加 decision 模型用途与 Jev 原生适配；用户与 my-agent 共用可调时间、总开关、逐点关闭/观察/应用、读取/修改/恢复继承/有效值读回；默认关闭，建议前台单次 2 秒、阶段累计 4 秒、后台 4 秒，均可调整；排队/连接/读取/解析共享绝对期限，默认零重试，错误立即采用原方案，取消与迟到结果隔离，资源有界，复用唯一调用账本、费用和配额。P2：接通子代理创建前模型选择与现有 Curator 提取前临时标注，保留显式模型/继承、幂等、权限与记忆游标权威。P3：接召回后重排；待插件接口稳定且归属明确后接 Skill/tool 推荐，不复制注册表或绕过撤销。P4：完成原设置入口及 agent 代操作的实际使用体验、窗口/稳定缓存/费用/故障与多会话组合验证。后续召回前判断、记忆写入建议和有预算自动调参仅按各点独立证据逐步实施，不默认全开，不扩大用户权限。每片先合同/假服务/replay/定向回归，再在已具备明确配置和费用预算时做少量真实 TUI 验收；普通模型保持官方 MiniMax-M2.7，不静默改用户日常设置。复杂并发、取消和状态边界可由 Astra 最高档子代理独立复核，常规工作用高档并由主代理整合。同步设计/配置/测试/结构文档，保留真实失败证据；只处理本线变更，不代提交另一任务文件；部署、重启、共享入口集成事前同步。最终交付可审查实现、验证证据、剩余边界和后续建议；当前未具备真实 Jev 凭据/预算或共享接口时不得声称实测完成。

### 1.2 必须同时遵守的后续要求

原文是历史快照，以下用户后续明确要求优先，不能只照旧Goal遗漏：

1. **全部P1到最后都要做，P5不能缩成“以后再说”。** 完整检查单附在本报告后半部分，唯一持续更新TODO仍是 [DECISION_MODEL_GOAL.md](DECISION_MODEL_GOAL.md)。
2. 决策模型是区别于执行LLM的一类模型；复用原模型目录、配置/授权/账本/插件快照/记忆流程，不安装整个jev-harness，不内嵌第二套agent。
3. 用户与my-agent均能按用户意愿修改总开关、逐点off/observe/apply及时间；默认关闭。前台单次2秒、阶段累计4秒、后台4秒只是可调默认值。
4. 超时、断网、没额度、协议错误、取消、关闭或迟到建议不能拖垮基础agent。默认零重试，期限贯穿排队/连接/读取/解析；晚结果隔离、残留资源有界。
5. 选项应区分成功选择、need_data、not_needed、no_match、abstain和请求失败。资料不足不由模型文字猜成授权或成功。
6. 子代理模型选择由主代理及宿主自动完成；不能让用户逐个选择、确认子代理模型或手动补决策资料，避免破坏自动化。
7. **不做决策价格/计费功能。** 原Goal出现“费用”是早期表述；当前显示只沿原用量行额外显示决策输入token，输出位置留白以备未来，未知不能补零。供应商原usage可保留，时间/次数/输入token实验预算仍有意义，本地决策模型也必须适用。
8. 真实测试已获Jev凭据与充分授权，用户说“测试到完全没问题为止”，不再等待请求次数审批。但要按证据有目的地测，不能无限重跑。密钥已在隔离私有配置，不放报告、仓库或日志输出。
9. 普通真实模型用官方MiniMax-M2.7；可让Jev对子代理选择官方MiniMax-M3及OpenCode deepseek-v4-flash。**OpenCode只用deepseek-v4-flash，MiniMax两者均走官方原生入口。** 不静默改用户日常模型。
10. 普通子任务用Astra high或GPT-6 Sol high/xhigh；复杂并发/取消/状态审查可Astra max，最高指max，**不是ultra**。
11. 用户允许并行开发和测试机192.168.1.9；修改共享入口、部署和重启仍须与另一工作线精确协调，不把旧协调当无限授权。
12. 保持代码轻量、优雅、可读、可扩展；从已有通用实现提取复用，不为单任务增加专项合同、重复注册表或第二状态权威。

### 1.3 最终完成判定

- 全部P1—P5要求逐项有实现和匹配范围的验收；每项关闭等价、失败隔离、来源/权限及实际消费链正确。
- 源码、离线合同、真实接口、实际安装版TUI和模型质量分别报告。不得拿fake通过、显式手动切模型或局部内存指标冒充更大范围通过。
- 完成共享集成及相应严格本地gate，部署必须另协调；文档和交接同步。
- 当前远未满足全部完成条件；接手者不能因为12.4或首批链路完成就把整个Goal标complete。

## 2. 必读入口与现有实现

依次读：
1. `AGENTS.md`、`LLM_GUIDE.md`、`docs/WORKSTREAMS.md`。
2. [完整设计](../design/DECISION_MODEL_INTEGRATION.md) 与 [唯一Goal/TODO](DECISION_MODEL_GOAL.md)。
3. [容量与Compact审计](DECISION_MODEL_CONTEXT_AUDIT.md)：最新source首片、宿主基线及剩余边界。
4. [真实验收](DECISION_MODEL_REAL_VALIDATION.md)、`TESTS.md`。
5. 接手具体模块时读对应合同、模块进度/结构与focused tests，不全面重扫所有历史文档。

核心接缝（相对仓库）：
- 原生协议：`agent_py_agent/agent/backends/{decision_protocol,typesafe_decision,typesafe_decision_wire}.py`。
- 决策服务/预算/账本：`conversation/{decision_service,decision_policy,decision_model_call,decision_experiment}.py`。
- 设置：`settings/decision_settings*.py`、`decision_experiment*.py`、原 `user_config`。
- 子代理：`agent_core/subagent/model_selection.py`、`agent_core/orchestration/decision_subagent.py`。
- 记忆：`memory_store/{decision_curator,decision_curator_relation,decision_recall}.py`。
- 能力：`capability/{decision_candidates,decision_recommendation}.py`。
- 主模型/实验/规划的分片证据见下方TODO及对应HANDOFF，不凭文件存在判断整项完成。

## 3. 当前18项进度快照

| 顺序 | 状态 | 具体交付 | 完成时用户能得到什么 / 验收点 |
| --- | --- | --- | --- |
| 01 / P1-A | ✅ 已完成（本地） | 决策模型配置与普通 LLM 分开 | 可保存 decision 配置，主/子 LLM 不会误选；提交 `17530b518` |
| 02 / P1-B | ✅ 已完成（本地） | Jev 原生协议与短期限 HTTP | 不安装 harness，能发原生请求；坏响应/超时拒绝；提交 `4e33f7593` |
| 03 / P1-C/D | ✅ 已完成（组件） | 有界等待、准确取消、资源保留 | 超时即返回，旧请求未退出不堆积，给普通 LLM 留名额；组件已联合验证并通过本地严格 gate |
| 04 / P1-E | ✅ 已完成（本地服务） | 一套设置服务 | 总开关、逐点模式、可调秒数、恢复继承及 CAS 读回；原 user_config 已接通；完整菜单属于 11 |
| 05 / P1-F | ✅ 已完成（本地组件） | 原账本与用量 | 终态不复活、真实 worker 精确保留、快照补账；同一行显示决策输入，输出留白、无价格、未知不补零；真实调用接线归 06 |
| 06 / P1-C/D/G | ✅ 已完成（本地服务） | 第一条可用决策链 | 原配置→本地 HTTP→原账本→活动 TUI 用量行组合通过；冷却、在途关闭及写锁争用已验，见 P1-F/G 交接 |
| 07 / P2-A/B | ✅ 已完成（本地业务接缝） | 子代理创建前选模型 | 已接建议与显式优先/幂等合同；最终必须由宿主自动核验并选定模型，无逐子代理用户操作；完整容量证明及真实异模派工归 12/13，尚未验收 |
| 08 / P2-C/D/E/F | ✅ 已完成（本地业务） | 记忆整理前置标注 | 原 Curator 临时建议与提取/提交/游标组合通过；关闭、非选择、失败和 lease 头寸对照见 Curator 交接 |
| 09 / P3-A/D | ✅ 已完成（本地业务） | 召回后重排与结果复用 | 在原权限/来源内排序，关键材料不丢，失败恢复原顺序；复用不另建记忆库 |
| 10 / P3-B/C | ✅ 已完成（本地业务） | Skill/tool/插件能力推荐与实际减量 | 原prompt/native schema减量、搜索恢复、插件撤销、HTTP超时及在途关闭已验 |
| 11 / P4-A/B/C | ✅ 已完成（本地及一条真实中文链） | 原设置入口和 agent 代操作接线 | 可新增/编辑/测试/开关/调时间；普通 user owner 原工具可见性、可信线程与 CAS 预览已修，真实中文 read→thread patch 成功、事务回执含生效值并准确回复；其它 owner/故障组合仍归 13 |
| 12 / P3-E、P4-D/F | 🔄 进行中 | 窗口、缓存与并发故障验收 | Jev两层容量门与目录代次已本地通过；原生计量/主子采用153项、三宿主展示接续及相关回归236项通过；同轮不重决策、新轮重置、后台真实身份已验；12.1/2/3/5/6/7已验，剩12.4完整大历史来源与媒体集成 |
| 13 / P4-E/G | 🔄 进行中 | 真实 Jev + MiniMax TUI 验收 | 隔离 Gateway/官方 M2.7、子代理官方 M3 与受限候选 OpenCode DeepSeek、普通中文配置 read→patch 都有成功样本；主会话异模、多 owner 和故障矩阵待验。累计 Jev HTTP 65 次 |
| 14 / P5-A/B | 🔄 进行中（A/B 首片） | 召回前判断与记忆写入建议 | B 的短样本正式记忆关系提示已用真实Jev验12对，后续提取仅本地替身；A 的默认关闭补充查询已接原正式上下文，136项离线回归通过且受控漏召回可只追加缺失事实；隔离Jev两轮建议均未在词面基线外新增事实，真实收益未证，普通召回不能静默跳过，均未验收 |
| 15 / P5-C | 🔄 进行中（两首片） | 其余决策点逐个接通 | 已归档 web_fetch 阅读顺序与现有 Todo 优先级软建议接入，均有隔离Jev成功/回退证据但实际业务收益待验；质量提示已审原证据/收口边界，动作候选缺可信观察 ID，自学习尚无 Skill 提案确认链，三点仍待实现 |
| 16 / P5-D | 🔄 进行中（采用本地片及真实保留样本） | 主会话自动选模型 | 原线程版本及 Gateway 观察已接；完整请求验证、发送前 CAS 和失败保留的 fake HTTP 片已通过。隔离真实三轮分别超时、need_data、选当前 M2.7，均安全完成；尚无真实跨模型采用、容量和历史验收 |
| 17 / P5-E/F/G/H | 🔄 进行中（恢复及 E1 原语） | 有预算的自测与调参 | 原设置恢复原语已通过；E1 已保存有界授权信封、原账本串行预留并做 356 项组合回归。缺用户授权入口、可靠输入上界及实际发送硬门，实验联网仍关闭；可信收益与自动应用仍待做 |
| 18 / 最终集成 | 🔄 进行中（插件基线本地合并待验） | 集成、文档和完整交接 | 早期回归修复 Jev 窗口、task_local、旧测试和错误码；导入守卫零发现。隔离分支已合入插件基线 f04ec3a42，迁移唯一公共取消原语，交叉九文件298项、决策685项通过；全仓19,869通过、8个短期限HTTP/探测测试失败，后续已拆分首事件/idle测试预算，五文件91 passed、1既有xpassed；web_fetch历史失败根因仍开放，未当作全仓gate通过；后续第7步和原仓库TUI仍待对齐 |

## 4. 最近已提交的工作及验证边界

| 提交 | 实际内容 | 证据 / 未覆盖 |
| --- | --- | --- |
| `3855e5a67` | 三宿主生命周期设计和基线文档 | 无2a生产实现 |
| `53d45f6c1` | child不展示正文时，不再全读历史并计算展示窗口 | 先1 fail/1 pass，修复后三文件31 passed；只是renderer，不是seed或整链释放 |
| `464df65c1` | scoped选中正文→摘要的固定地址重放 | 26文件530 passed；Ruff/doc/strict size hard=0/diff/登记新文件后clean-package通过；未部署 |
| `099da9f2d` | 12.7安装版真实Compact/显式跨模型缓存记录 | 对应旧生产包5e5122b04，不覆盖新source代码 |
| `5e5122b04` | owner授权的4个后台fake Store夹具适配 | 4 failed/158 passed→整文件162 passed，业务断言不改 |
| `bb60be21c` | 原生历史隔离副本复用、主线小JSON有界编码修复、IR夹具字段 | 三文件72 passed、IR文件29 passed，不能累加为全仓通过 |
| `eb9efc129` | checkpoint链逐行读取、旧摘要释放 | 9文件98 passed，精确覆盖ID仍O(N) |
| `970c0b195` / `935475f8f` | 12.6跨窗并发 / 12.5输出预留完整容量 | 本地组合123 / 187 passed，真实质量另验 |

### 4.1 source首片实现

新增 `conversation/message_replay.py`、`compact_message_source.py` 和对应两测试文件。固定EOF、dev/inode、行地址/完整hash；初次两遍整段校验，后续重放所选行校验；晚追加下一次读取才可见。来源、分区、native/orphan清扫、原JSON/token公式、分段器与普通checkpoint/CAS共用原路径。取消、短路、异常显式关闭读取器。

相同128行、4,195,620字符的真实文件→load→原分段→CAS，旧峰值9,683,703 bytes，新1,448,802；正文翻倍新1,532,904。95/189段、完整JSON SHA256及精确IDs保持。仅Python tracemalloc，非RSS。最大单行、必要发送材料、媒体后缀、轻量ID索引仍有成本。

尚未处理：三宿主seed/params/frozen/回调的正文驻留；无scope与/context旧全载入口；landmark先收集各行至多1200字符再取窗口；O(N)身份与覆盖元数据。canonical仍依赖append-only合同，不承诺抵御最终复验与CAS之间任意外部改写。

## 5. 接手后的最近一项：12.4第二片

### 5.1 已确定、未实施的2a方案

采用**显式source字段+准备解析入口**，不用磁盘Sequence伪装旧tuple/provider list。

- `ConversationHistorySeed` 具体messages/canonical_messages与source严格二选一。
- source只引用同一次冻结的canonical地址及已冻结的投影规则；原文件、范围/view和CAS仍唯一权威。
- 三宿主复用原单行过滤、终态工具折叠、native信封与scope规则；不能另抄一套选择算法，不能重放时重读当前权限而扩大范围。
- 只在原native/text准备边界解析成具体值；下游capture、ToolLoopRequestInput、纯projector、运行时provider list原位更新不改合同。
- native源已由原native_history隔离，避免第二次深拷贝；旧concrete seed保持原隔离规则。
- 读取失败不能变空历史；None、已知为空、未准备/未知必须分清。
- **2a只证明seed/宿主早期准备阶段改善，不证明摘要期间旧params/frozen已释放。**

已商定文件清单（停下时尚未编辑）：
- `conversation/models.py`：source及互斥校验。
- `conversation/history_projection.py`：提取复用原单行规则，冻结地址范围。
- `conversation/agent_thread.py`：复用原fold投影生成source；已提交renderer小修须保留。
- `conversation/background_history_seed.py`：已裁决地址复用，非预选入口保留原scope算法。
- `gateway_parts/request_context.py`、`request_prompt.py`：显式history source与seed/display解析。
- `gateway_compact_recovery.py`：候选source。
- 拟新增 `conversation/history_seed.py`：只读投影载体与明确text/native解析。
- 两个共享函数见第7节。
- 拟新增 `test_conversation_history_seed.py`、`test_host_history_seed_lifetime.py`，相邻seed字段断言按新显式合同适配，不能删除完整内容断言。

后台 `load_context_bundle → _task_scoped_operational_context → prepare_context_payload` 还有messages及deepcopy驻留。2a不顺手扩成该范围已完成；必须单列，后续按证据处理。

### 5.2 2b必须继续解决的难点

- `PreparedCompactRecovery.select` 的params、frozen、render_params、host_state、partial/闭包，及外层运行帧都可能保留旧正文。
- `next_tool_loop_model_response` 入口params在prepare返回后仍活在调用栈；只在select内replace/del局部不会释放其provider list。
- 初始 `view.is_candidate=False` 仍要求完整旧请求计量。不能为了释放把旧历史改为空/None，然后把假空投影当原请求通过。
- 保留自动noop、未知模态拒绝、两候选回用、当前要求/schema/输出预留、失败不提交及候选与首实际payload相等。
- 不能把IO藏入ToolLoopRequestInput或纯projector；完整初始预检尚未有界时，必须报告该峰值，不称“必要实际出站成本”。
- 2b精确接口尚未设计完成。不要把口头方向当已审实现。

### 5.3 已冻结基线及复测方法

seed-only：真实JSONL→scoped loader→三宿主原seed，4195620字符；source峰约0.27MB，seed准备峰Gateway8,979,675 / child8,584,530 / background8,968,172 bytes；完整canonical JSON hash及128行相等。

完整链固定53d45f6c1：128×32768字符，真实prepare、Compact分段/预算、writer/CAS、provider builder到首HTTP边界；只有摘要返回和最终HTTP返回为fake，无网络。observer不保存大请求，临时MY_AGENT_HOME隔离。

| 宿主 | 首payload前峰值bytes | 摘要期最高驻留bytes | 摘要调用数 | 精确覆盖IDs |
| --- | ---: | ---: | ---: | ---: |
| child | 10932180 | 10272498 | 96 | 130 |
| Gateway | 29807947 | 9558548 | 47 | 128 |
| background | 11393412 | 10730361 | 96 | 130 |

三宿主prepare及业务payload均一次，原任务标记保持；fixture历史/预算不同，不能横向排名。Gateway峰在摘要入口前。3 passed只证明链路/完整IDs和测量成功，不表示内存目标通过。

后台初版成功判断过宽，现脚本已改为 `runtime_status=="ok"` 且 `model_response=="材料核对完成。"`，单节点1 passed确认。单节点新进程峰12,183,643、驻留11,523,190；与三节点预热不同，不能混比。原三节点日志保留不覆盖。后续用修正脚本、相同运行方式、相同宿主做前后对照。

## 6. 尚未完成的后续要求

- **12**：剩12.4（其余12.1/2/3/5/6/7已按限定范围验收）。做完2a、2b也要按完整条目复核媒体、旧来源入口、三宿主与真实安装版的缺失证据，不能自动勾选。
- **13**：真实故障矩阵、多owner、第四轮selection_changed原因与质量/延迟/实际采用率；已有样本不能外推统计效果。
- **14**：召回前补充查询真实收益未证；记忆关系建议首片有12对Jev样本，后续提取/晋升仍需业务组合。用户明确查历史不能跳过。
- **15**：web_fetch阅读顺序和Todo优先级首片已有；质量提示、DOM/OCR/动作候选、自学习筛选仍未完成。动作缺可信观察ID，自学习缺Skill提案确认链，必须补通用底层，不专项硬编码。
- **16**：主会话自动选择已有本地采用链；真实仅超时、need_data或保留M2.7，**尚无真实不同模型自动采用**。手动/model跨模型不能代替此项。
- **17**：已有授权信封/预留及CAS恢复原语；缺实际用户授权入口、可靠输入上界、发送前硬预算和可信对照/自动应用，实验联网仍失败关闭。人工后续修改优先，撤销停止未来试验，恢复不重放未知副作用。
- **18**：与另一线最新代码精确集成、完整交接及严格gate。历史全仓19,869 passed/8个短期限HTTP失败，后续测试预算修订五文件91 passed/1既有xpassed；web_fetch历史5秒超时根因仍未关闭。不称全仓绿色，不重复无依据跑全仓。

## 7. 与另一工作线的边界

另一任务名称必须使用原名 **“模块重构”**，线程ID `01a0b9b8-7beb-7922-a1e3-6a21a24980d8`。可读其紧凑状态并按明确授权继续协调，不代提交对方文件。

停止前对方明确转交本独立分支的2a机械接线，仅：
1. `agent_core/runtime/loop_support.py::_native_provider_history_messages`。
2. `agent_core/_tool_loop_service.py::_text_conversation_history_section`。

其它native plan/commit、live-tool checkpoint来源/CAS、active_turn_compact、process-index输出投影等仍归对方。尤其不要改 `LiveToolCompactCheckpointRequest`、`LiveToolCompactCommitRequest`、`write_live_tool_compact_checkpoint`、`committed_live_tool_compact_source_ids`、`model_visible_active_turn_tool_calls`。

**最新交接通知：对方也按用户要求暂停了Goal。** 对方自报已发布main `66a598cf3`；.10测试机切 `39a2c2de`，本机仅安装未切；TUI230任务完成待核账，228只打开没提交业务。这是对方通知，不是本线部署验收。对方交接在 `~/.my-agent/releases/step8-39a2c2de/HANDOFF_TO_NEXT_AGENT.md` 与 `GOAL_HANDOFF.md`。新接手者应重新核对最新HEAD、dirty、负责人及精确函数范围，旧协调不自动扩大到新任务。

本分支不要直接整体移植对方新候选，也不要在原主工作区继续开发。当前无部署窗口、无待批准操作。

## 8. 真实测试与远端收尾

- 用户授权独立测试机192.168.1.9；SSH已有别名 `openeuler`（root与既有identity），直接IP默认本机用户会认证失败，不需要改账户或密钥。
- 最近安装验收目录：`/root/decision-acceptance-20260923/5e5122b04`，独立venv/HOME，端口8431。旧319004926目录保留。
- 真实安装生产版本仅5e5122b04，**未包含464df65c1或53d45f6c1**。
- 最近七条业务全部done/completed；M2.7两轮→原/compact generation0→1→续聊，on4保留当前模型，原菜单显式切官方M3/OpenCode DeepSeek且完成工具轮。
- 本线累计真实Jev HTTP **65次**；本轮停止前没有新增真实调用。
- M2.7实际端点 `api.minimaxi.com/anthropic/v1/messages`，M3同官方入口；DeepSeek为 `opencode.ai/zen/go/v1/chat/completions`；Jev为 `api.typesafe.ai/v1/systemone`。
- 回执缓存字段按实际报告/缺报区分；没有跨供应商共享缓存或净token收益结论。主/摘要/决策分别统计。
- 最后收尾证据：原隔离设置owner revision20→21恢复decision关闭；TUI exit0；Gateway PID3163135已退出；pending/processing为0；8420/8431无监听；业务fixture未改；HTTP26个均结束。此为最近一次观察，接手者启动前须再核对，不自动重启。
- cleanup与真实证据见服务器该目录 `artifacts/` 以及 [真实验收](DECISION_MODEL_REAL_VALIDATION.md)。
- 无待处理远端进程/TTY handle；最后模型/测试命令均已终止或完成。
- 不展示/复制真实API key到仓库或报告。凭据存在不等于当前仍有效；需要真实验收时只读确认配置并遵守原隔离方式。

## 9. 本地证据文件与执行说明

以下/tmp文件是本机仓外材料，可能随清理消失；接手应及时保留，不把它们当另一持久状态权威。必要结论已归档在仓库文档。

| 文件 | 用途 |
| --- | --- |
| `/tmp/decision-compact-source-lifetime-handoff.md` | source首片精确15生产/6测试文件和26文件命令 |
| `/tmp/decision-compact-source-lifetime-design.md` | source两片设计；早期状态可能过时，以本报告为准 |
| `/tmp/decision-host-history-consumer-audit.md` | 三宿主、tuple/深拷贝/纯投影/原位list消费清单 |
| `/tmp/decision_host_seed_baseline.py` / `decision_host_seed_baseline_20260923.log` | seed-only测量 |
| `/tmp/decision_host_history_baseline.py` | 已收紧后台成功断言的完整链测量脚本 |
| `/tmp/decision_host_history_baseline_53d45f6c1.log` | 三节点原始基线 |
| `/tmp/decision_host_history_background_success_53d45f6c1.log` | 后台更强成功判定单独证明 |
| `/tmp/decision_source_focused_final_20260923.log` | 530 passed |
| `/tmp/decision_source_lifetime_red_20260923.log` / `decision_source_lifetime_final_isolated_20260923.log` | source内存红绿 |
| `/tmp/decision_child_render_red_20260923.log` / `decision_child_render_green_20260923.log` | renderer红绿31项 |
| `/tmp/decision-cache-acceptance-5e5122b04/` | 真实缓存旁观脚本与脱敏报告；不代表新代码实测 |

本线一直使用原主checkout的 `.venv/bin/python` / `.venv/bin/ruff`，但所有仓库命令显式workdir为独立工作区。按 `git worktree list` 定位解释器，不在原工作区写代码。真实SimpleAgent测试必须pytest隔离或显式临时MY_AGENT_HOME，避免日常用户目录。

常规只跑改动直接相关focused tests。远端提交/合并前按AGENTS完整本地严格gate：focused pytest、全目录Ruff、doc sync、strict code-size、diff、clean-package。未达到约10000行且无额外要求，不反复全仓pytest。新文件未登记时clean-package会报UNTRACKED_FILE，需审查后纳入Git，不忽略守卫。尺寸基线不能为过测放宽；生成报告恢复策略先核对当前owner。

## 10. 完整P1—P5检查单快照

以下取自停止时唯一Goal文件，保留原勾选及未完成项；与第3节“本地切片完成”口径不同，未勾选的端到端要求仍须逐项验收。

## 总目标与完成条件

在 my-agent 原生模型体系中加入可选决策模型，保留完整基础能力。用户和 my-agent 按用户意愿共用设置，
能调整时间、选择决策模型、逐点开启/观察/关闭；超时、没额度和错误不拖垮原执行、工具或记忆流程。
复用原配置、授权、取消、资源、调用账本、子代理、记忆和上下文机制，不迁入整个外部 agent/harness。
P1—P5 全部逐项实现并验收，文档与真实限制同步，才可以关闭整个 Goal。
未具备凭据、请求测试授权、接口稳定点或测试环境的项目必须明确留待验证，不能用 fake 结果计为真实通过。
质量不足的接入点继续默认关闭，失败证据保留；不得为宣称成功放宽权限、遗失必要材料或制造效果统计。

## P1：配置、协议、时限与故障隔离

- [x] P1-A：原 provider/model schema 增加 decision 用途与 Jev 协议；显式持久迁移，原模型 ID/凭据/选择保持；主子执行模型不能误选 decision。仅本地源码/合同，不表示已能发起决策请求。
- [x] P1-B：Jev 请求与各题结果的严格协议校验，候选 ID、版本、来源和 usage 保留；无依赖也能启动基础产品。
- [x] P1-C（本地）：复用请求预算、取消和传输，单次/阶段时间可调；默认前台 2/4 秒、后台 4 秒，完整请求总期限、默认零重试。
- [x] P1-D（本地）：错误即时保留原方案；精确请求取消、迟到结果失效、未退出资源有界；冷却及恢复不禁用主模型、不新增后台守护。
- [x] P1-E：共用读取、字段级修改、恢复继承及有效值读回；主开关、逐点模式、时间覆盖、版本冲突、临时/长期范围已本地实现；原 user_config 已接，完整菜单归 P4。
- [x] P1-F（本地）：同一 ModelCallLedger 与并发准入组件接线；决策不进入 USD 价格估算或 owner/run 成本累计，失败、取消、未知输入用量不报零；状态查询不泄露密钥，不改稳定模型前缀。
- [x] P1-G（本地）：关闭等价、配置双入口、真实慢 HTTP 假服务、错误类型、竞态与资源回收的本地定向验收。

退出条件：最小链有真实可调用入口；开启正常、关闭等价、短期限失败和不可用配置均有证据，不能只有孤立协议类型。

## P2：子代理模型选择与记忆前置标注

- [x] P2-A（本地）：子代理创建前一次决策；显式模型优先、父子孙继承保持、批量共用阶段预算、单项失败保留原选择。
- [ ] P2-B：宿主在主代理派工时自动完成授权/模态/工具/窗口客观核验、候选与最终配置冻结；重复请求和恢复不重新选择或重复创建，不要求用户逐子代理确认、选模型或补决策资料。
- [x] P2-C：原 Curator 批次提取前增加临时标签和优先级，保留原始材料与来源；不建立另一后台代理或记忆库。
- [ ] P2-D：各点 off/observe/apply；need_data、not_needed、no_match、abstain 和请求失败分开，局部结果独立处理。
- [x] P2-E：原记忆提取、验证、晋升、未解决集合与游标不由决策输出直接控制；失效建议不落盘。
- [ ] P2-F：关闭/成功/超时/额度/取消/恢复对照，继承与幂等、原始批次完整性、候选引用和输入 token 归属验收。

退出条件：两个真实业务入口可配置、可观察、可关闭，失败实际沿原路径执行，定向和实际场景证据分开保存。

## P3：召回与能力推荐

- [x] P3-A（本地）：原召回后候选重排/筛选，保持 owner 和来源权限、必要材料及原文可恢复；故障回原排序和原预算。
- [x] P3-B（本地）：Skill/tool 索引推荐，沿现有快照和按需读取；实际执行仍有正确 schema/正文，推荐不扩权。
- [x] P3-C（本地）：插件接口稳定后接能力推荐，目录/激活变化复查，旧建议不能复活已撤销工具；不复制 Registry。
- [x] P3-D（本轮复用）：结果复用先依原 operation/run；有明确收益时增加有界进程缓存，权限/输入/模型/策略版本变化失效。
- [ ] P3-E：稳定前缀、工具集合变化、跨模型上下文与输入 token 对照；缓存命中以实际 usage 为准。

退出条件：不因推荐隐藏必要工具或丢失关键记忆，插件故障/停用与普通核心任务并行互不越界。

## P4：完整交互与端到端验收

- [x] P4-A（本地交互）：原模型设置入口完整支持 decision 新增、编辑、选择、测试、模式、时间覆盖及恢复继承；保存不隐式发模型请求。
- [x] P4-B（一条真实中文链）：用户普通中文要求经主模型转换为宿主配置动作；可信 user owner 已自主 read→thread patch，原事务回执与持久文件可核。更广的 owner/故障组合归 P4-F/G。
- [x] P4-C（本地范围/运输）：不同 owner、不同 TUI、进行中修改和关闭、服务失效仍可操作配置；明确设置值与实际有效值。
- [ ] P4-D：250K/1M 等不同窗口、token 计数差异、输出/推理预留、工具历史完整、既有 Compact 与稳定缓存验证。
- [ ] P4-E：先本地合同/fake/replay，再少量真实 Jev + 官方 MiniMax-M2.7 实际 TUI；核对真实 provider/端点及原账本。
- [ ] P4-F：正常、慢响应、断网、硬额度、取消、关闭、服务恢复、批量派工、Curator 与插件并行的组合证据。
- [ ] P4-G：中文质量、延迟分布、关键遗漏、实际采用率与全任务输入 token/请求次数报告；功能链和模型判断质量分别结论，不估算决策价格。

展示约定（用户明确）：现有用量行沿用 LLM 风格，额外显示决策输入 token；不显示价格，输出位置预留且当前留白。
这不删除供应商原 usage 或内部预算事实；缺少用量仍是未知，不能为了展示补零。

退出条件：直接用户操作和 agent 代操作均可复现；缺少真实凭据/预算/环境时不宣称完成实测。

## P5：全部后续接入点与受控自动调整

- [ ] P5-A：召回前判断，保留用户显式查历史要求；不确定或故障时沿原召回。
- [ ] P5-B：记忆候选分类/合并/写入建议，经原验证与晋升；不能直接删除、覆盖人格或将分数变成事实。
- [ ] P5-C：检索/外部材料排序、规划派工建议、DOM/OCR/工具动作候选、交付质量提示、自学习候选筛选逐点接线与独立开关。
- [ ] P5-D：主会话自动模型选择仅在用户授权候选范围和新工作片边界生效；原历史、上下文与进行中调用保持正确。
- [ ] P5-E：有范围、有时间/请求次数/输入 token 预算的对照试验，复用原任务与调用记录；记录基线、实际结果、来源和配置版本。
- [ ] P5-F：基于证据提出设置差异；用户授权范围内可自动应用，人工后续修改优先，版本冲突不覆盖，撤销停止未来试验。
- [ ] P5-G：恢复前值使用原配置服务及并发校验，不回滚已执行业务、不重放未知副作用；授权外变更不应用。
- [ ] P5-H：每个接入点关闭等价、故障隔离、输入/消费关系和收益边界验收；没有收益的能力不默认开启。

退出条件：P5 不再作为“以后再说”的总待办；各点有实现和验证结论，自主试验/调参有明确授权及可撤销的预算边界。

## 最终集成与交接

- [ ] 每片同步中文配置、双层注释、设计、树与模块测试文档，删除不再使用的中间实现。
- [ ] 与插件线精确集成，不覆盖其文件或文档；共享入口只有一个实施者，独立 worktree 本地验证先行。
- [ ] 远端提交/合并前通过 focused pytest、Ruff、doc sync、strict code-size、diff、clean-package；全仓频率遵守 AGENTS.md。
- [ ] 如需部署/重启，提前与插件线同步目标和验收窗口，保留回滚证据，同机保持一个 Gateway。
- [ ] 汇总源码、离线、真实接口、实际 TUI、模型质量各自证据与限制，不混为整体通过。
- [ ] 最终按 HANDOFF_TEMPLATE.md 给出文件、验证、风险和建议下一步；所有必做项完成才把应用 Goal 标为 complete。

## 11. 可直接交给下一位agent的接手指令

> 接手my-agent可选决策模型完整P1—P5目标。先读本报告及DECISION_MODEL_GOAL.md，进入codex/decision-model-integration独立工作区，核对HEAD和dirty；不要写原主工作区。用户已让旧代理停止，其Goal为paused，你仅在用户本次明确委派后继续。当前01—11本地切片完成、12—18未整项完成；优先12.4第二片2a（显式source seed，严格互斥，原native/text边界解析），其生产尚未开始。已有完整基线和两个共享函数的旧范围确认，先与“模块重构”的接手者复核最新归属。之后完成2b旧请求释放及后续13—18，不能缩减P5。决策默认关闭、失败不影响原执行；不计价格，只显示输入token；普通真实模型官方M2.7，M3官方，OpenCode仅deepseek-v4-flash。保护纯投影、scope、完整历史、原CAS、媒体、权限和两候选回用。常规用高档子代理，复杂边界Astra max，不用ultra。每片定向验证、同步文档，真实验收用隔离.9并先协调；不把局部通过冒充全Goal完成。

## 建议下一步

由用户指定的新agent先完成工作区及共享归属核对，再接2a；可让独立代理复核来源/关闭/范围与测量，生产保持单owner。随后沿相同基线解决2b，不在旧Goal暂停期间自动重启开发或远端服务。本代理到交接文档完成即停止。
