# COMPLETED

## 2026-09-07 R197 HTTP 断流分类（本地完成，真实验收未完成）

- 解决问题：IncompleteRead 不是 OSError，长子任务遇到 HTTP 半截响应会绕过退避、裸抛失败。
- 共享 JSON/GET/SSE/open 入口按异常类型归一，复用现有预算；中断优先，超时阶段保留，不采纳半截工具参数。
- 9 个新增用例先在原实现失败，修复后连同网络/流式/原生工具相关 146 项通过。尚未把定向结果当真实 TUI 通过。

## 2026-09-07 R196 超时结果与恢复提示（本地完成，产品闭环未完成）

- 删除按普通调用次数放行 UNKNOWN 的旧逻辑与按 TOOL_TIMEOUT 声称清理成功的特例，运行与恢复采用同一事实。
- 按 会话运行时 exec 的退出/输出路径，将进程终止回执和部分输出带回 shell；已知终止失败与未知结果分账，禁止同 operation 重放。
- 前置 deadline、后台停止和 Gateway 专用错误提示同步处理；352 个唯一定向用例通过，真实 TUI 仍待新版复验。

## 2026-09-07 R195 删除虚假的子代理重试数字（本地完成，真实客户端待验）

- 解决问题：coordinator 正常等待下级后继续执行，attempts 增加被误显示为失败重试。
- 对照 终端交互 AgentLine，底部只投影真实状态、职责、耗时、上下文和 Compact；不改 runner、预算或停止规则。
- 定向回归先复现失败，修后验证等待/运行/成功/失败四种状态及数字保留。测试机原递归任务是复验入口，尚未部署。

## R191 运行状态分段与预检查共用投影（已部署，来源增量样本通过）

- 解决问题：一个状态变化重复整包输入，预检查又与真实发送形态不一致，加快上下文增长和压缩。
- 动态字段携带宿主来源，IR 只追加变化；删除 ever-seen 集合，保留状态返回旧值，Compact 按来源保留最新值。
- 预检查和发送共用纯投影，普通历史前缀不变，取消/CAS 回滚不留另一份去重状态；260 focused 通过。
- R191 真 TUI 同提示词首轮主代理 0 代 final，完整批量追加到 4 代后 final；真实请求不变记忆只发送一次。
  首轮质量不同，费用不标等效比较通过；R190 原子代理第 96 代 DONE 也不表示完整复刻无缺陷。

## 2026-09-05 R190 摘要覆盖的旧运行快照回收（本地切片）

- 解决问题：旧运行状态不随旧工具退休，逐步挤满 Compact 最低水位，长任务反复进行很浅的整理。
- 复用工具整对回收的摘要覆盖参数，只释放连续退休前缀的旧 RuntimeFactsTurn，最新状态与 UserTurn 保留；
  普通请求、权限、原始账本不改，事务失败完整恢复。
- 141 focused 通过，含 5 项失败先行证据。未部署、真实成本未验，不能算长任务封板。

## 2026-09-05 R189 过程流换代与最终消息接线（已部署，原 TUI 切片通过）

- 解决 Gateway 换进程后旧 TUI 大游标吞新输出：过程流携带独立身份，只有不同流才能重基序号；
  canonical 消息仍用持久字节位置，冷 owner 不暖启动、失败不确认、不清空用户输入和既有历史。
- 子代理 final 从原消息库带 thread/message ID，修正遗漏参数的旧调用，移除正文拼接的 legacy 去重键。
- 228 focused / 1 原有 skipped；客户端 PID 不变跨唯一 Gateway 重启，新流思考/工具/Compact 实时接续。
  B 已结束 child 页面可见原 final，Ctrl+O 展开与 Ctrl+G 返回正确；不提前关闭未提交过程持久化等剩余 P1。

## 2026-09-05 R186 已结束后台回合的恢复验收补充

- 原 B session 自然 final 后退出再恢复，191 个过程块、最终回复顺序正确，Ctrl+O 可展开，Working 收起。
  只回看前后消息/用量文件 hash 一致、行数 210/51、Compact 39；未结束回合归档和长历史分页不在通过范围。

## 2026-09-05 R188 后台工作目录残留修复（已部署，原 TUI 路径通过）

- 解决问题：主代理等完 child 后，使用内部 runs 记录目录解析用户项目相对路径。去掉后台 task-link
  恢复对 cwd/权限根的两项赋值，统一使用可信 thread cwd 或 owner home；内部归档引用保留。
- 184 focused / 2 原有 xfailed 和严格 gate 通过；默认 home、显式家内项目、工具及派工视角一致。
  R188 wheel `e0ff3033...789a6` 包含 R187 长 child 修复；唯一 Gateway PID 2415915 下，原 B root
  29 次相对 tasks 文件操作成功，原 HANDOVER 直接修改，已知服务原件和 18778 停止状态保持。
  两个原 session 恢复、child 列表仍为 5/3；长任务超过旧 Compact 上限、自然完成后恢复与新游标问题仍开放。

## 2026-09-05 R187 子代理 Compact 执行循环修复（本地切片）

- 解决问题：长回合有进展却因累计 8 次压缩被判失败，后续清理再把失败改成停止。
  删除次数硬终止，保留正式代次、原身份、停止和真实失败；被动父级清理不改写已有终态。
- 98 focused 与严格检查通过，超过旧上限后的真实 MiniMax TUI 完成验收仍在 ROADMAP，未宣称封板。

## 2026-09-05 R186 已提交后台工作片快照（代码已部署，长 TUI 验收仍进行中）

- 解决问题：旧后台过程在恢复 final 后重放到末尾。终态公开块快照与 canonical final 一起保存，
  完整恢复后按精确工作片身份重基易失环；实时补帧、历史恢复使用原 block ID，不改变模型历史或缓存。
- 330 focused / 2 原有 xfailed 与严格检查通过；`.10` wheel `eb5a8e04...ab3f`、唯一 Gateway PID 2344284，
  B 原 session 新任务已确认 5 次真实 MiniMax-M2.7 调用、cache-read 92,256。三个 child 自然完成，
  主代理正在整合。最终回复后的 exact resume 尚未完成，不能把本实现记录当作 P1 全量通过。
- A 原 root/child4 在顺序升级后以同一 run/thread 恢复，只换执行 attempt，没有新增替代 run。
  Gateway 提交前过程持久化、Compact 工作片让出和长历史分页仍开放。

## 2026-09-05 R185 canonical 后台正文切片（不代表完整历史验收完成）

- 解决问题：后台最终回复被另存到 notices，恢复时用不同 ID 再显示。删除第二份写入/读取链，改为同一
  canonical message 有界页和恢复末行游标；同一 final 保持相同 block，内容相同的不同消息各自保留。
- 363 focused / 2 原有 xfailed 与严格检查通过；`.10` B 多子代理续作产生真实 final，自动显示且 Working
  收起。原服务 app.py SHA 与空文件仍保留。再次恢复却仍有旧过程环重放到 final 之后，该分支失败，
  完整快照与缓冲排序继续在 ROADMAP/BUG-111 跟进，不能据此宣布 P1 封板。

## 2026-09-05 R184 家目录边界与文件软整理

- 解决问题：同一用户的旧任务目录被当成独立权限区，续作触发 task 回绑失败，正确路径还会被改写。
  删除这批执行门、参数/patch 正文重写、output/work 魔法映射、由产物路径推导写根以及额外命名 LLM。
- 普通主/子代理默认在自己的 owner home 工作；任务命名、资料/源码/临时文件/成果分类和旧工作续作由
  同一稳定内置指南说明。参考 通道运行时 bootstrap 工作区说明、长期助手 SOUL/AGENTS 加载和 会话运行时 cwd/权限根分离。
  新宿主归档写 runs/日期/身份哈希，旧记录与用户产品原位保留；owner、SOUL、control-plane、exact Audit 不软化。
- 532 项 focused、本地严格 gate 和 `.10` 单 Gateway / MiniMax-M2.7 原会话 TUI 通过本切片验收：
  两份旧目录原文件反复直接修改，三个普通 child 的实际 cwd 均为自己 home，Compact 后继续原会话。
  详见 FT-158/160；未跑全仓 pytest，未声称真实 TUI 覆盖每一个拒绝分支。
- 多子代理工具任务另暴露模型误改样本、报告失真 BUG-115；后续均由原 TUI 自行恢复已知文件并保持服务停止。
  这不等于模型质量问题已根治，也不关闭完整过程归档、旧 notice 重复和剩余 P1 验收。

## 2026-09-05 R182 exact resume 等待统一 readiness

- 薄 Gateway TUI 不再先同步请求历史；在可见 preflight 内顺序连接、恢复同 owner/session、启动 worker。
  历史失败显式结束，用户已退出后的迟到结果不重启界面。本地/普通终端模式、权限与模型请求拼装不变。
- 141 项 focused 与严格门通过；`.10` 两个 R178 原 session 在唯一 Gateway 冷启动过程中均成功恢复，首条
  输入/思考/工具可回看，模型调用不增加。随后两路普通中文追问均自然 final，长会话另正常推进 Compact 3。
- 完整归档、恢复后旧 notice 重复与跨历史任务 rebind 的新失败分账，P1 未封板；不以本启动修复替代它们。

## 2026-09-05 R181 长会话显示不再重复截取

- 显示层删除 `max_turns` 二次分组截取；53 条原始消息虽只有 5 条用户任务，但无独立 request id 的后台
  commentary 形成 34 个显示组，不能再用 20 回合预览上限截掉开头。读取层仍保持原来的有界窗口。
- `.10` 原 `ma-r180-110-long-resume` Ctrl+Home 已看到首条八项目调研的用户输入、思考、工具；回看调用
  仍 61 / Compact 2，没有重派或模型请求。相关 115 项 focused 与严格门通过；完整归档/分页仍独立未完成。

## 2026-09-05 R180 已持久化历史的类型恢复

- Gateway 与本地恢复分别传递问答预览和 canonical display-only 事件；原先丢失的思考、commentary、工具与
  final 复用正常 reducer 显示。无内部 user 注入/签名/参数透传，不回灌模型，不重新排队或调用工具。
- `.10` `ma-r179-110-process-resume` 首轮历史 Ctrl+Home/思考 Ctrl+O 真实可见，恢复前后均 36 次模型调用；
  后续普通追问自然完成。`ma-r180-110-long-resume` 恢复五阶段原 session，main/child 代次 2/5，进入和返回
  原已完成 child 正确，没有新增回看模型调用。
- 186 项 focused、Ruff、doc sync、strict size、diff、源码/候选 wheel clean-package 均通过，未跑全仓 pytest。
  本切片不代表 Compact 前完整过程、无限分页和 child 有界环之外的历史已经恢复；这些 P1 边界仍开放。

## 2026-09-05 R179 后台网络证据与停止清理

- 原 firewalld helper 保留原生退出码，查询失败不再说成关闭；逐 zone/port 返回明确放行、未明确放行或未知，
  部分放行不再冒充全部放行。不新增网络请求、规则写入、模型调用或任务质量判定。
- `.10` 的 R179 wheel 已通过真实 MiniMax TUI：恢复原交班服务 session 后，实际 network_status 查询
  public/18778 得到 rc=1、not_explicitly_allowed，明确区分本机监听与外部 LAN 未验证。
- `gwreq-1788585936-8e660f0e75484386bc731523185c2b30` 经 TUI 停止原进程；受管记录 killed/-15、listener
  PID 2221390 消失、18778 不再监听，唯一 Gateway 继续运行。此前重复启动的失败进程也均已退出。
- focused 覆盖超时、授权错误、252 未运行、zone 缺失、全部/部分/未放行；异常分支尚未做 TUI 故障注入。
  模型此前两次漏查 firewalld 的诊断质量问题保留为 BUG-109，不冒充由此自动消失。

## 2026-09-05 R177 TUI 终态补帧与返回父层即时校准

- final 或 Working 归零后，既有即时 invalidate 之外只登记一枚 one-shot terminal frame；刷新线程消费后立即
  静止。`.10` `ma-r177-110-tui-final-nav` 第一轮自主派 5 名 child，收齐后用户无按键即看到完整最终回复，
  Working 同帧撤下。
- 同一会话第二轮形成 main→coordinator→researcher。孙代理详情自然终态后，Ctrl+G 返回父页约 0.2 秒首帧
  已显示“已完成”；Enter 回看仍命中同一 run 与历史，再返回 root 时 coordinator 和两名直属 researcher
  状态、最终回复均正确。
- 父页即时校准只使用详情页已接收且 `updated_at` 不旧的 typed row，下一 Gateway 全量轮询仍是权威；不改
  active count、Todo、Goal、selection、history、Compact、缓存、取消或 wake。相关 focused 与静态门通过。

## 2026-09-05 R176 child/grandchild Compact 中断与父级恢复

- `.10` 单 Gateway + MiniMax-M2.7 两路真实 TUI 分别在直属 child 和 depth-2 grandchild 的 Compact
  summarizing 阶段按 Esc；两份候选均以 `candidate_discarded` 结束，没有推进 generation/checkpoint，也没有
  执行半份摘要。
- 直属 child 取消后 main 约 8.4 秒恢复，五名兄弟继续并最终收口；孙代理取消后 exact coordinator 约 3.4 秒
  开始下一 attempt，随后 coordinator/main 自然 DONE。另一次 Compact 已提交后的停止保留已提交 generation，
  作为 post-commit 对照，不与 pre-commit 回滚混写。
- 这关闭了 R154/R169/R170 留下的分层故障门：主代理、child、grandchild 现在各有自然 Compact 成功、压后续跑
  与中断原子边界证据；算法共享不再被错误当成层级运行链已经自动通过。

## 2026-09-04 R170 递归派工容量与孙代理 Compact 真机闭环

- 根与任意递归层级现共用唯一 session/owner/task/per-call 容量计算和 owner-local 创建事务；超限批次原子
  `not_started`，不静默截断或部分落盘。120 项 focused 与 `.10` 真 TUI
  `ma-r170-110-nested-capacity-r2` 均证明 `requested=4/available=1` 时原批次零创建。
- 独立 TUI `ma-r170-110-grandchild-compact-r2` 形成 main→coordinator→5 名孙代理的真实三层树；depth-2
  `subagent-1788527190-94bc6171` 自然 Compact `120,065→69,952`、generation 1，压后继续工作并 DONE。
  五名孙代理完成后 coordinator、main 逐层自动唤醒和 final，TUI 插话、动画、checkpoint、lineage 与
  MiniMax-M2.7 用量账均有结构化证据。Compact 中途取消仍按 main/child/grandchild 分层留在测试清单。

## 2026-09-04 R169 Transcript Compact 缓存面统一

- Gateway 前后台、手动 `/compact` 与每层 agent thread 复用普通模型轮的 stable prompt、system、工具 Schema
  和 canonical provider messages，只在末尾追加摘要要求；auxiliary 摘要不执行模型返回的工具调用。
- `.10` 单 Gateway 已分别取得 main 自然/手动、直属 child 和 depth-2 grandchild 的 MiniMax-M2.7 真 TUI
  证据；三层都提交自己的 generation、显示动画、保留 provider cache-read 并在压缩后继续工作。故障中途取消
  仍按层级单列测试，不能由自然成功推断。

## 2026-09-04 R167/R168 Shell 前像存储与工具归档分型

- shell 前像已从用户项目树迁到 owner `data/artifact_backups`，使用 opaque hash blob、原子写入和严格
  owner/symlink 边界；无变化内容立即清理，真实改动的旧内容继续由 registry ref 恢复。
- canonical `tool_output` archive 现在带结构化角色和 shell exclusion，不再被每条后续命令当用户产物递归
  复制。ToolOperation 成功/失败结算后统一通知回收 crash-window manifest；UNKNOWN 继续保留供恢复核对。
- `.10` 单 Gateway、MiniMax-M2.7 真 TUI `ma-r168-110-u381-artifact-linear` 已输出 5,000 行、修改并复核
  真实交付物；完整工具回执仍可读取，项目内无内部备份，`operation_manifests=0`、`backup_blobs=0`。
  受管后台 shell 的退出后复核仍是后续独立工作，不包含在本项完成范围。

## 2026-09-04 R166 Compact 缓存面与真实可达水位

- live Compact 的资格预估和最终提交改用同一个 native IR 删除语义，恢复目标不再被误当第二道硬门；
  provider 有效摘要即使没有达到优选 60%，只要低于真实 90% trigger 也能正式记代并继续原 attempt。
- 摘要辅助调用保持 会话运行时 的完整历史顺序和 终端交互 的稳定缓存前缀，只在末尾追加 Compact 指令；调用
  不安装工具 handler、不进入工具循环，异常工具块走有界 typed fallback，独立用量完整进入 provider 账本。
- `.10` 真 TUI `ma-r166-110-compact-cache` 中 main 提交两代、child 提交一代后均继续原任务，8 名 child
  和 main 全部收口；main/child 状态行显示 canonical generation，最终报告存在且直接显示。196 项相关
  focused、Ruff、doc sync、strict code-size、diff 与 clean-package 通过。

## 2026-09-04 R163/R165 Compact 正式记代与历史任务续作收口

- R163 `.10` 单 Gateway + MiniMax-M2.7 长 TUI 证明 child provider overflow 会先提交自己的
  ConversationThread：DeepSeek child generation 1 为 `118,383→71,087`，8 名 child 全部 DONE。main 五次
  Compact 的 47 个 live-tool source id 全不重叠，generation 严格 0→5，最终报告和 Working 自然收口。
  会话运行时/终端交互 对照确认 60% 是优选目标、低于真实 trigger 即可提交；浅压缩成本作为新 ROADMAP 项保留。
- R165 让普通新回合看到同 owner/thread 最近四个 completed task-path 候选，但不自动选择 cwd 或授予写权；
  exact successor 成功后只删除身份精确匹配、完全未修改的系统占位脚手架，task link 审计记录仍保留。
- 同一真 TUI 依次完成星河日志分析器、无关云尺目录体检器、再自然续作星河，定向测试分别 23/17/26 项
  通过。第三轮业务文件只落原星河目录，continuation completed、占位 link superseded，物理 task 目录保持
  两个；主 Compact 1→2→4，三轮最终回复都直接显示。相关本地 focused 通过，`.10` 仍只有一个 Gateway。

## 2026-09-03 R156–R161 Full Access 设备与历史任务绝对地址

- Full Access 的根 bind 后重新 `--dev-bind /dev /dev`，恢复管理员已授权环境里的真实设备语义；WorkspaceOnly
  设备最小集、owner 墙和网络边界不变。`.10` 单 Gateway + MiniMax-M2.7 真 TUI 已用常规 pytest 验证。
- Gateway completion 保留 exact conversation runtime，`session_search` 只从终态 typed Gateway 记录投影
  task_ref；宽检索优先合法历史引用，模型复用的 owner-relative/absolute 地址由独立 canonical owner home
  还原，权限仍由后置统一门裁决。
- R161 将 owner-home 晋升 rebase 收窄为占位 cwd 重定向，不再吞掉 canonical `<owner>/tasks/...` 历史绝对地址。
  206 项 focused 通过；`.10` 真 TUI `ma-r161-110-history-address` 完成 A/B 后自然续作 A，原目录测试从 23 增至
  30 项，无嵌套 `tasks/` 或第三份业务项目，并自然提交 Compact generation 1。占位壳保留和 Todo 拒绝后的
  展示一致性是另两项 P1，不影响本项地址/设备合同完成结论。

- 2026-09-02 完成普通新任务与旧项目续作的工作区分流：终态普通 task 只保留为
  状态/导航历史，不再隐式选中下一回合 cwd；exact request、active task 和未结束 Goal
  仍保持原路径。显式写入旧项目时，统一工具入口按 canonical `task_path` 合并同项目的
  多代执行，选最新 terminal link 建 successor 并清理影子占位。focused 与本机
  MiniMax-M2.7 真 TUI `ma-r155-local-workspace-routing` 均通过：无关 CSV 项目新建目录，
  Markdown 报告续作则回到旧文本项目，request/thread/successor/path 精确一致。这解决
  长期助手把所有事粘在一个目录，或明确续作却留下空影子任务的双向问题。

## 2026-09-02 R154 主代理 Compact 中断原子边界

- 三条 Compact 路径使用同一个只读中断合同；CAS 前的用户停止恢复未提交内存状态、丢弃候选且不增加失败，
  CAS 成功后则保留已提交代次。该语义对齐 会话运行时 可取消 compact task，并适配 my-agent 的 checkpoint/CAS。
- focused 覆盖摘要前后、原生 IR 改写后、checkpoint 后和预取消；本机真 TUI
  `ma-r154-local-compact-cancel` 在 active-turn 摘要 20% 时 Esc 后保持 compact 4/failure 0，并能继续原任务。
- 本项完成的是主代理路径；child/grandchild 自然真机停止仍保留在 TEST_CHECKLIST，不以单测冒充完成。

## 2026-09-02 R151 child 详细历史中的层级返回

- transcript modal 现在也注册 Ctrl+G/Alt+Left，并复用唯一 `navigation.back()`；不再要求用户先 Ctrl+O 收起
  详情。代理层级和 transcript 展开态保持正交，Esc 的精确停止语义不变。
- 79 项 focused 通过；`ma-r151-local-child-detail-back` 真实恢复、进入 child、展开、一次返回 root、再收起
  详情均通过，root 的历史、child roster 与 Compact 代次未丢。

## 2026-09-02 R150 Compact 动画 operation 隔离

- TUI 不再把持久 generation 当成动画身份：同 operation 内进度单调，不同 operation 即便复用同一待提交
  generation 也各自从真实 started 阶段开始；旧候选迟到事件不能覆盖当前 fallback。
- 85 项 Compact/Gateway/background focused 与 Ruff 通过。R150 真 TUI 从 exact session id 恢复 R149 全历史，
  手动 `/compact` 显示动画并在提交后把 generation/计数从 3 精确推进到 4；随后的记忆追问准确。
- 本项只改展示 reducer，不改变摘要、checkpoint/CAS、缓存或计费。自然发生的 live→transcript fallback 仍保留
  为后续压力真测，不用合成事件冒充 provider 已真实走过该分支。

## 2026-09-02 R149 子代理详情投影尺寸硬门

- 把超长 `apply_agent_view` 拆为校验后的 immutable projection、锁内名册/终稿登记和活动区数据生成；展示
  Runtime 仍无执行权限，Gateway typed payload 仍是唯一身份与终态来源。
- 78 项 navigation/input/PTY 回归、Ruff 与 strict code-size 通过，生产 hard finding 从 1 降为 0。
- `ma-r149-local-child-navigation` 真 TUI 已验证运行中 child 的完整 prompt、思考、工具、独立历史和返回。
  详细 transcript 中 Ctrl+G 需先退出 modal 的组合键缺口单列 ROADMAP/BUG-102，不属于本项完成范围。

## 2026-09-02 R147/R148 TUI 消息层级与 TaskRun 收口

- TUI 思考标签、正文和 Ctrl+O 提示统一使用 terminal dim；用户消息保留深色独立底并加 bold。终端交互
  提供的是 `dimColor` 语义而非固定灰色值，本机真实 TUI 的 ANSI 输出已证明用户为 SGR 1、思考为 SGR 2。
- 会话 TaskRun 不再依赖一轮内临时完成属性。持久 task link 终态与 exact AgentRun 整树终态共同授权唯一 CAS，
  root/child 竞态和 Gateway 启动崩溃窗口都能幂等补齐，UNKNOWN 工具操作不受影响。
- R147 52 项 renderer focused 通过；R148 相关 117 项 focused 通过。`ma-r148-local-goal-taskrun` 真 `/goal`
  完成后，Goal 工具、root AgentRun、TaskRun/唯一 close event 三方一致。测试机同 wheel 部署仍是后续硬门。

## 2026-09-02 R143 模型前置失败不再留下幽灵运行

- 解决已绑定 RuntimeDB attempt 后、正式模型循环前的 capability probe/上下文准备异常没有进入既有 closeout，
  从而让 TUI 已空闲而 run/attempt 永久显示运行中的问题。
- `_run_once_with_params` 现在以一个统一异常边界覆盖 prompt/skill scope、工具准备、上下文装配、provider
  probe、模型循环与 finalization；复用既有 `_settle_main_agent_run_exception`，普通异常落 `failed`、用户中断
  落 `cancelled`，不新增兼容旁路或自然语言判定。
- 对照 会话运行时 `会话运行时-rs/core/src/tasks/mod.rs`：任务从统一 spawn 点调用 `on_task_finished`，意外错误同样先摘除
  active task 再结束 turn。my-agent 保留自己的 RuntimeDB CAS，只适配同一个生命周期边界。
- 本机 wheel `4648d4c8a1d35bffd4c53e4f859c5e1ec1ba582d9c894249a459c7e01bf35c24` 真 TUI
  `ma-r143-local-preflight-closeout` 通过：缺密钥轮的 run/attempt 均 failed 且 ended_at 非零；恢复配置后的
  下一轮自然 done 并完成实现、测试和端到端验证。相关 focused 34 项与扩展 39 项全绿。

## 2026-09-01 R129--R133 Gateway 活跃回合与子代理等待跨重启

- 解决 processing request 已按 exact id 重排、工具结果也完整落账时，启动 stale-attempt 调和仍先把旧
  main run/attempt 标成 unknown，第二 worker 又被 generic fail-closed 门挡住的问题。普通 unknown 合同不
  放宽；只有同一 `gateway_active_turn_recovery.v1` 同时通过 task+run 双身份、current unknown、全部已启动
  工具终态与 matching durable archive、无 DIRTY/MUTATING resource 核对，旧 attempt 才转 recovered 并释放
  exact execution lock。CLAIMED 且 handler 未启动可在同事务取消，其它不确定继续人工处理。
- R129 `.10` 真 TUI 保留失败基线：请求、任务目录和 15 条工具归档都成功重排，但 generic unknown 门在模型
  前拒绝第二 attempt。R130 `ma-r130-110-main-active-restart` 部署 wheel
  `bb42ce82392bc63d6a9ddf1df2415eceb34007f5116972a2317101118769578c` 后，旧 generation 的 21 个 terminal
  operation 被逐项核对并记为 recorded；同 request/thread/task/run 的 generation 2 只执行 5 个后续操作，
  18 个 unittest 通过并自然 final，零已结算写入重放。
- R131 `ma-r131-110-direct-child-wait-restart` 在 main 已以 `SUBAGENTS_ACTIVE` 等待唯一直属 child 时重启；
  原 child `subagent-1788302139-230579bf`、generation 1、runner PID 167884 均未复制，child 完成后新 Gateway
  自动唤醒原 main generation 2，34 个 unittest 与最终回复自然完成。
- R133 `ma-r133-110-coordinator-wait-restart` 形成精确 root→1 coordinator→2 workers；coordinator generation
  1 以 `SUBAGENTS_ACTIVE` 让出后重启唯一 Gateway。两名 worker 保持原 run/generation 完成，原 coordinator
  generation 2 集成，原 main generation 2 汇报，91 个 unittest 全通过。R132 额外 coordinator 的模型偏差
  保留为负样本，没有用它冒充等待重启门。
- 提交 `e4bb658` 前直接相关 180 项 focused、PyCompile、Ruff、doc sync、strict code-size、diff 与
  clean-package 全绿；变更远低于 10,000 行，按约定未跑全仓 pytest。部署和三次顺序重启全程都只有一个
  8420 listener，配置与欢迎页为 MiniMax-M2.7。

## 2026-09-01 R124--R128 直属父级 capability 授权与危险 ToolCall 分账

- 解决 child 申请父级已经拥有的工具时，main 因当前模型可见清单不完整而误拒绝、语义 Router 又抢先把
  OPEN 申请结成 GAP、grant 后 MCP 工具只落账却不进入下一 runner 的问题。创建时由 Tool Gateway 冻结直属
  父级 immutable `ToolRuntimeSnapshot`；嵌套父级读取 exact execution context，模型文字不能伪造或扩大。
- 普通/MCP 申请字段共用结构化规范器：完整普通名优先；MCP 短名只有在父快照中唯一末段精确命中时才规范为
  `mcp__server__tool`，未知或重名保持原值并在 hard grant gate fail closed。父级可授予全部 exact 工具时，
  CapabilityRouter 保持 OPEN，交给直属父级 grant/deny，不再抢先关 GAP。
- capability grant 把普通/MCP 名同时并入同一 child 的 durable `allowed_tools` 和下一不可变 provider 快照，
  但具体危险 ToolCall 继续走原有用户审批。grant/deny 是父级控制裁决，不再让普通用户理解内部 run/tool；
  实际桌面操作仍必须按 exact call 单独确认。
- R128 `.10` 单 Gateway、MiniMax-M2.7 真 TUI `ma-r128-110-main-capability-approval` 通过：直属 main 自主
  grant `capreq-1788293189-ab042cc0`，原 child `subagent-1788293169-474f3c3b` 生成第二 runner session 后继续；
  `take_screenshot_with_ocr` 另经 `approval:053e0842c70dd2439b6ed55d` 一次性批准，真实返回窗口、屏幕尺寸和
  OCR 文本。最终 wheel SHA-256 为 `54c992e9d949ae957e05eb4afba5a06a2672b66c34a271f59e5c94d7f3db22cf`。
- 部署前 capability/create/route 组合 194 项通过；提交前扩大到 capability/direct-parent/approval/Gateway/
  background runtime 共 401 个 selected case，395 passed、6 个既有 expected xfail、0 failed。R128 同时核对
  canonical request、grant、两片 runner session、runtime gate、operation/audit 与结果 ref。本轮远低于
  10,000 行，按约定未跑全仓 pytest。

## 2026-09-01 R123 Computer Use 开源执行器与真 TUI 闭环

- 解决 my-agent 没有系统桌面控制能力、又不应自造截图/OCR/鼠标键盘引擎的问题：官方 extra 固定复用
  MIT/PyPI `computer-control-mcp==0.3.13`，底座只做结构化装配，并复用既有 MCP、Tool Gateway、审批、取消、
  operation 和 owner 墙。上游缺失的滚轮只调用同一 PyAutoGUI `scroll()`，没有复制执行器。
- 默认关闭；只有 `local/main + full-access` 注入。普通 owner 的真实 TUI 完全看不到能力；浏览器页面仍优先
  Browser。普通 MCP 默认延迟披露，Computer Use 通过 deployment-owned `catalog_category=computer_use`
  首轮直接可见，修复 MiniMax-M2.7 因无法消费原生 Tool Search 引用而误选终端的问题。
- `.10` `ma-r123-110-u376-computer-use` 已真实完成窗口激活、截图/OCR、滚动找验证码、点击、ASCII 输入、
  Enter 和 OCR 后置验证；`ma-r123-110-u377-computer-stop` 证明 MCP 运行调用可由 `/stop` 中断；
  `ma-r123-110-u378-computer-isolation` 证明普通 owner 隔离。全程只有 1 Gateway + 1 MCP 子进程。
- commit `8eab29c` wheel SHA-256 为 `c6b1fa3d3d4c302984d2c4a5cd333bd0768f22c8becc64995b787ebacf20822e`；
  107 项相关合同、Ruff、doc sync、strict code-size、diff 与 clean-package 通过。本轮远低于 10,000 行，未跑全仓 pytest。

## 2026-09-01 R121 Goal 固定状态、思考折叠与后续轮连续性

- `/goal ...` 在 durable outbox 成功后原样留在 TUI 历史；固定面板 Goal 排在 child 前，方向键可选，Enter
  展开 exact objective/状态/token，Ctrl+G 收起。显示块不进入模型队列，也不生成第二个控制请求。
- live thinking 实时展示；结束后默认折成灰色摘要，Ctrl+O 进入详细历史、再按返回。正文不会随 thinking
  隐藏。普通 turn 不再由旧 `ordinary_task_resume` 暗中续跑，只有显式 Goal 持有持续生命周期。
- 真 TUI 先发现 active Goal 吞 final，再发现空 `task_path` 被误判为工作区损坏；分别按 会话运行时 normal turn 与
  thread overlay 语义修复。非空但真实丢失的 sticky workspace 仍 fail closed。
- `.10` `ma-r121-110-u374-goal-thinking` 最终复验通过，canonical history 精确保存 user/final；唯一 Gateway
  为 `ma-gateway-r121-final2-110-r3`，MiniMax-M2.7，wheel SHA-256 `9bb53f3226013534ef3e64a9cd5e192fd941dc682647baff11b1d929785d8d34`。
- 相关 focused、Gateway 会话上下文整文件、PyCompile、Ruff、doc sync、strict code-size、diff 和
  clean-package 全绿；累计 4,769 行，按约定未跑全仓 pytest。

## 2026-08-31 R118 父级能力裁决、durable wake 与生产递归角色

- `resolve_capability_requests` 的 grant/deny 现在都是直属父级在自身 authority 上限内的 mutating 控制；
  direct-parent、owner、workspace/write roots 与可用工具继续硬拦越界，具体危险调用的用户审批仍单独保留。
- Gateway scheduler 不再于提交 ready thread 前同步重复 orphan 全量扫描；唯一 reconciler 继续恢复死亡 runner，
  非 Gateway scheduler 保留 inline 兜底。u328 证明直属 child 完成后 main 自动恢复，u331 证明两名 depth-2
  researcher 收齐后 coordinator 与 main 逐层自动恢复并 final，全程没有测试者催办。
- 标准 wheel 现在强制携带 builtin role catalog，distribution boundary 会在缺失时失败；同时把 owner-root helper
  下沉到 user-space 低层，消除 Gateway 对 agent-core 的反向依赖。最终 wheel SHA-256 为
  `0f8f97e8b439bcf718ca4acbc1bb207aa7199b53fe055bfe9ed4ef35d28cd40b`。
- 当前 `.10` 仍只有一个 `ma-gateway-0f6ac38-110` / MiniMax-M2.7 / 8420 listener。focused、Ruff、doc sync、
  strict code-size、diff、source/wheel clean-package 均通过；本轮远低于 10,000 行，按约定未重复跑全仓 pytest。

## 2026-08-31 R116/R117 provider 上下文观察与 TUI 后台 final 持久化

- provider context estimator 不再只靠本地 token 近似：成功调用把数值观察以稳定请求指纹和 Compact
  generation CAS 写入 exact ConversationThread。动态消息不会破坏指纹；不匹配、缺 usage 与过期观察均
  保守回退。R116 u323 的立即追问命中 `32,418` cache-read / `8,061` input，主代理保持 compact 0；u324
  自然越线后只 Compact 一次并继续，关闭旧版本同类任务频繁重压的成本缺口。
- 本地 TUI 的 delivery channel 明确属于 transcript-capable channel。后台 main 的 commentary/final 与前台
  assistant 一样先幂等提交 ConversationStore，`background_notice` 只投影“有新消息”，不能代替模型历史。
  重试按 exact wake id/part id 去重，未知外部 route 保持 fail closed。
- R117b u326 在发追问前直接核对 raw thread：schema v8、11 条 messages、6 条后台 commentary、1 条后台
  final；追问随后准确召回两个 child 报告、marker 与 357 行总报告。此前 u325 因旧 editable venv 导入
  schema v7，保留为部署负样本。
- 最终部署改用独立普通-wheel runtime venv；u327 的两名 child 交付 245/198 行报告，main 交付 290 行整合
  报告。追问前 raw messages 已有 4 commentary + 1 background final，零工具追问准确召回所有行数和 marker。
  当前 8420 只有 `ma-gateway-efa90d1-110-runtime-venv`，实际 import 位于该 venv site-packages。
- 相关 background runtime、delivery、notice、conversation history 与 provider observation focused 已通过；
  本轮生产/测试代码增减低于 10,000 行，按约定不重复全仓 pytest。

## 2026-08-30 R114t/R114r/R114q/R114n/R114v 长会话底座收口

- pending conversation 不再把 owner 根冒充工作 cwd；首个工作工具固定 canonical task root 后 main/child/
  grandchild 共用该根。`.10` fresh u313 六 child 同根，owner-path 拒绝为 0，后续续作与手动 Compact 仍能召回。
- 跨后台工作片的 active-turn archive 只能经 ConversationThread checkpoint/CAS 被摘要替换；u314 八 child
  完成、三代真实 Compact、367 行总报告与 3.17M provider cache-read 均有账，普通恢复不再暗中缩短上下文。
- 60% recovery target 已同时作用于 transcript/live-tool；u314 每代 source replacement 均低于目标，后续
  增长来自新工作，不是同一候选贴线重压。完整 raw archive、Todo、child、产物和 final 保留。
- 后台 main 保留 owner-scoped Skill/Memory/Persona 工具；u316 自然完成 Skill、四 child、受管服务、两分钟
  复查和三项 USER 偏好写入，SOUL 与其它 owner 未改。
- systemd/launchd Gateway 服务 cwd 改为 `<MY_AGENT_HOME>/service-cwd`，不再绑定安装时源码 checkout。
  wheel `41f752d...` 已从 `/root`/site-packages 启动 `.10` 唯一 8420 Gateway；相关 focused 与本轮全部变更
  测试文件通过。连续 8 代后的公平 yield 仍列 R114u 真机观察，不在本完成项提前勾选。

## 2026-08-30 R114s Persona 写入限频 owner 隔离

- 解决同一 Gateway 的进程级 Persona 写入时间窗让不同 owner 互相消耗额度的问题；限频现在按 canonical
  owner home 分桶，过期 bucket 有界清理，同 owner 仍保留 30 秒 3 次保护。
- `PERSONA_UPDATE_RATE_LIMITED` 已进入统一错误 taxonomy，拒绝明确为 `effect_outcome=not_started` 并支持
  退避，不再被工具运行层升级为 UNKNOWN/DIRTY。
- 38 项 focused 与 Ruff 通过；`.10` 单 Gateway 的两个 fresh owner 各三次真实写入全部成功，磁盘无串写，
  两个同 owner 新 TUI 均在零读取工具情况下准确召回各自三项。

- 2026-08-30 完成 R113 本地底座候选：会话运行时/终端交互 式 Todo 实时更新与回复前核对被压成每轮
  Workspace Context 的短纪律，替换旧说明且继续受现有开关控制；宿主仍不自动打勾、不追加隐藏模型轮、
  不以 open Todo 拦截 final。Conversation Compact 现在优先保留 provider typed error code，并沿
  transcript/live-tool、Gateway rich chunk、TUI reducer 到红色终态块显示；失败不推进 generation，原上下文
  保留。相关 9 文件组合 264 项 focused 通过；R112 fresh 已证明八个 child covers 项持久追平到 8/9，
  同时保留 root 最后一项未主动关闭和一次 provider Compact 失败作为发布后复验基线。

- 2026-08-30 完成 R109 子代理 typed lifecycle 与详情首次视口闭环。R108 累计基线先提交为 `3379c21`；
  R109 修复提交为 `a166c7d`。对照 会话运行时 typed agent status/feed 与 终端交互 loading spinner 后，child 只按
  exact phase 显示排队、启动、等待首事件、运行、等待下级和终态；首次详情从 canonical goal 顶部打开，
  返回/重进恢复独立视口。87 项 TUI focused 与静态门通过。`.10` 原样八项目调研真 TUI 中，8 名 child 从
  排队转运行并全部终态，轻量运行时/deepseek-harness 各自然 Compact 1 次；fresh resume
  `ma-a166c7d-110-u272-child-first-view` 无需 `Ctrl+Home` 即显示完整 prompt，回切保持位置，main 自动恢复并
  直接显示 final，终态无 Working。产物为 8 份分报告和 1 份 16,651-byte 横向总报告；部署仅替换两个纯
  TUI 客户端文件，没有重启或复制 Gateway，PID `3699956`/8420 始终唯一。本轮未推送远端。

- 2026-08-30 完成 R108 Compact 错误合同和严格门收尾：六个已在控制面使用的 typed
  Compact code 已全部进入恢复分类表；相邻 176 项回归、Ruff、doc sync、strict code-size、
  import-boundary、diff 与 clean-package 全过。`AUDIT-02` 按用户要求保留 strict xfail，未改
  `/audit` 产品逻辑。`.10` 顺序部署后仍只有 `ma-gateway-r108-110` 一个 Gateway，
  PID `3694064`，MiniMax-M2.7。fresh TUI 中 `/compact` 进度条可见，成功提交
  `generation 1` 和 `11,595 → 9,186 tokens`，旧历史保留；客户端正常 `/exit`。本轮未提交、
  未推送远端。

- 2026-08-30 完成 R106 14 路单 Gateway 功能矩阵与 R107 递归工作区闭环。R106 长会话在同一 thread 内生成
  25 份技术笔记（合计 11,832 行）和 422 行总报告，真实发生 7 次 Compact；会话、权限、Todo/child、Goal、
  Schedule/Watch、Skill、Memory、capability、child 详情、控制与大输出搜索均取得 TUI/结构化证据。R106
  孙代理路径错位失败样本未搬运或补写；R107 fresh TUI 中 coordinator 和两名 depth-2 child 共用唯一
  canonical task root，交付 363/331/122 行三份可由父级直接读取的文件，Gateway 零路径拒绝。R107 仍只有
  PID `3690272` 监听 8420，模型为 MiniMax-M2.7；本轮未提交、未推送。

- 2026-08-30 完成本地 R106 两项底座候选：owner-scoped Shell 成功/失败统一携带隔离视图事实，避免模型把
  沙箱内 `/root` 成功扩大成宿主副作用；新增 `/sessions` 只读列出当前 owner 最近会话与精确 resume 命令。
  对照 会话运行时 bwrap 结构化 roots、会话运行时 resume picker 与 终端交互 ResumeTask；sandbox focused 26 passed，
  chat/TUI focused 72 项通过。R106/R107 已补真实 TUI 正证；未提交、未推送。

- 2026-08-29 完成 exact child 停止单调性、异步受理与工具清单单一快照收口：旧 runner 保存/heartbeat 不再
  把已取消 child 写回 RUNNING；直接 Esc 和模型 `cancel_subagents` 都只终止指定 attempt，兄弟继续运行。
  HTTP 先同步鉴权和幂等受理，再由唯一后台线程执行 canonical cancel，r56 真 TUI 从按键到 accepted 约
  6.06 秒、到 terminal 约 9.62 秒，未再出现假“无法确认”。另修复远程 `owner_type=user` 被错误拿去匹配
  agent role，导致 `list_tools` 为 0 的分裂事实；r57 MiniMax-M2.7 真 TUI 只调用一次 `list_tools`，返回
  30 visible/30 executable 并准确列全，root-only、内部与环境不可用工具仍隐藏。相关本地组合 74 项、远端
  定向 12 项通过；完整问题、测试、操作失误与未覆盖边界见 TUI 审计。本轮未提交、未推送。

- 2026-08-29 完成双用户同 Gateway 文件/Shell 隔离的真实反证与错误语义收口：u10/u11 自己读写成功、跨
  owner 文件和 cwd 拒绝、命令正文绝对路径由 bwrap 隐藏且宿主无越界文件。Shell 失败或 stderr 现在向模型
  投影 owner-only 可见范围，避免把沙箱内 `ENOENT` 误报为宿主目录不存在；model spec 同时禁止用
  `; echo $?` 遮蔽失败。fresh MiniMax-M2.7 TUI request 返回真实 `COMMAND_FAILED/return_code=1` 并正确
  说明证据边界；本地/远端 49 项 focused 与静态检查通过。该完成项不宣称能解析任意 shell 正文，也不改变
  OS sandbox、owner policy 或网络范围。

- 2026-08-29 完成顽固旧代码瘦身与首轮真 TUI 总验收：物理删除旧 acceptance/coverage/closeout、无人调用的
  恢复与子代理投影旁路，运行合同归位后 import-boundary finding 为 0；当前工作树累计 234 文件、
  +6,787/-10,618 行。会话运行时 式同轮工具 drain、exact direct-child 当前快照、终态 summary/Todo 投影和
  process 灰色展示均已落地。`.7` 单 Gateway、MiniMax-M2.7 的 r34/r35 两次原样八路调研都完成：r35
  8 个 child 全部 DONE，1 个 child 在 113.4k 后 Compact 到 63.7k/`compact 1`，main 读取八份报告、写出
  321 行/10,888 bytes 横向报告并自然最终回复；终屏 Working/Todo 收起而 child roster 保留。r36 又证明
  从不同 cwd 启动的同一 WorkspaceOnly owner 不再分裂 durable runtime home；原样超级玛丽 Prompt 2 的
  5/5 child 自然 DONE，产物 3,120 行，主代理只整合和汇报。唯一一次全仓诊断暴露的问题已按来源 focused
  复测；Ruff、doc sync、strict code-size、diff、import-boundary 与临时索引 clean-package 严格门通过。
  测试机缺浏览器 `libgbm.so.1`，因此游戏交互仍记 PARTIAL；本轮未提交、未推送远端。

- 2026-08-28 完成第一轮顽固旧代码瘦身：物理删除零生产调用的 `agent/acceptance/`、runtime DB
  acceptance mixin、三轴 `closeout_task_run`、废弃 SecretStore 及只验证这些死实现的测试；新 runtime.db
  只让 Task 保存长期身份，把执行终态留在 TaskRun/AgentRun/Attempt，旧库多余表列保持惰性而不破坏性
  DROP。`agent_core` 包初始化同步改成无副作用，composition root 改为直接导入具体模块，修掉删除旧
  re-export 后暴露的循环导入。首批净删约 2.9k 行；runtime DB、子代理 thread/Compact focused 回归已通过，
  最终产品验收按新最低标准继续使用单 Gateway、MiniMax-M2.7 真 TUI。

- 2026-08-28 `cc4764e` + `c8a2ece` 完成 child Compact 执行权与 Todo 额外计数收口：authoritative
  `task_local` 的 typed `context_overflow` 只作为同一 active attempt 的内联 Compact 边界，不再由通用
  `agent.run()` 提前结案；其它终态、取消、失败与普通可恢复返回的收口不变。Todo 标题只把未映射当前
  清单项的活动 child 写成“另有 N 个子代理运行中”，不冒充 roster 总数。相关 focused 与本地严格 gate
  全绿并推送；`.10` 唯一 Gateway 的 fresh `ma-110-c8a2ece-compact-r1` 一次派出 8 名 child，真实显示
  “另有 8 个”，工具运行时 child 在同一 `attempt-1787877717-487f5d43` 提交 generation 1 后又成功执行
  `run_command` 并自然 `DONE`，fresh task 中 `TOOL_AUTHORITY_CONTEXT_MISSING` 为 0。

- 2026-08-28 完成 Compact 有效代次与终态续作收口：live-tool 候选只有在完整 provider-visible 请求低于
  同一触发线后才 checkpoint/CAS/发布；旧会话前缀已超线或摘要候选仍超线时不再虚增 generation，分别
  交给 transcript Compact 或原样回滚。canonical task 进入不可复活终态时立即停用 exact task progress
  policy，scheduler 只作旧账本/竞态兜底。同步把旧后台测试从诊断 prompt 迁到真实 native messages/tools
  出站面；316 项相关 focused（2 项既有 xfail）与本地严格 gate 全绿。`.10`
  `ma-110-native-cache-r2` 的 generation 5 已真实提交 `148,304 → 14,145`，随后普通追问命中 31,600
  cache-read 且旧终态 policy 已 disabled；真实验收完成。

- 2026-08-27 `2ca6e6c` 完成普通主代理与递归 child 的跨回合 canonical messages 缓存前缀：Gateway/child
  ConversationStore 先冻结 committed Compact summary 与完整消息边界尾部，runtime 再按已结束历史、当前
  user、本轮工具 IR 的时间顺序发送；记忆召回、推荐、工作区、wake 和执行事实只放动态尾。当前 user 的
  typed prompt 副本不再重复发送或重复计 token，text/native 也不再各自读取一遍历史。相关 12 个测试文件
  的扩大 focused 通过（保留既有 xfail），Ruff、doc sync、strict code-size、diff 和 wheel 边界通过。
  `.10` 原 `ma-110-dispatch-cache-r1` 长会话部署后两次自然追加分别命中 24,168/24,388 cache-read；第二次
  只有 1 个物理调用且正确续接上一排序。该完成项只表示缓存/历史底座已进真实主链，不把 6/8 调研内容质量
  或仍运行中的 `.7` 慢模型长任务冒充完成。

- 2026-08-27 补齐 OpenAI-compatible 后端的统一 thinking observer 合同：首次本地模型真 TUI 的 typed
  `on_thinking_delta` 参数错误已先红后绿；Chat Completions 的 `reasoning_content` 现在与正文分流，
  在正文/工具或流结束处只封口一次，工具调用续轮只回放白名单思考字段，未知扩展不进 canonical IR。
  10 个 backend/stream/TUI 相关文件 207 项与本地严格 gate 全绿；候选已部署 `.7` 唯一 Gateway。
  fresh `ma-cache-long-local-r64` 首段真实命中 40,960 cached token，证明本地 KV 缓存链可用；其后长任务
  因一个 child 600 秒流超时、另一个未结束而由 TUI `/stop`，功能失败与成本下界继续留在 ROADMAP，
  不写成完整任务通过。测试机唯一 Gateway 已恢复 MiniMax-M2.7；重启后复用 r62 长会话只读核对旧报告，
  4 秒开始思考且 6 次真实调用命中 138,444 cache-read，证明模型、历史和缓存续接正常。

- 2026-08-27 完成 Anthropic-compatible 原生 prompt 的稳定 system/动态 user 分层：新增 typed
  `CacheStructuredPrompt`，完整字符串仍供归档、token 统计、text/关闭缓存路径使用；Anthropic native 只把
  System、Owner Scope、Persona/Prompt Files/Skill 索引和文本工具目录放进顶层 system 缓存块，记忆、时间、
  Conversation、当前任务和执行事实不裁剪、不摘要。140 项 prompt/cache/native IR/Compact focused 通过，
  `.7` 唯一 Gateway 的 `ma-cache-probe-minimax-r60` 真回合从 25,192 全输入收敛为 5,959 普通 input +
  19,300 cache-read、0 cache-write；按输入 5、缓存 0.1/1，样本成本分别下降约 74.8%/61.0%。长多子代理
  my-agent 本地/MiniMax 与 会话运行时/终端交互 MiniMax 矩阵作为下一阶段验收继续进行。

- 2026-08-27 已部署把立即终态折叠升级为 `conversation_terminal_tool_fold.v2`：同一 main/child 回合
  一次固定写入 hot-tail、cold-fold 和 typed deadline，默认 300 秒内保留较完整热尾，过期后才切短折叠；
  V1 持久行恒按 cold 兼容，Compact 代数和 owner archive 不变。`search_text` 同时明确为本地连续字面/正则
  搜索，并用 `local_text_search.v1` 区分完整 no-match 与扫描不全。32 项组合 focused、20 项最小定向与
  MiniMax-M2.7 三例 tool-choice 通过；本地模型精确 A/B 证明热尾在缓存价 0.1/1 时分别比立即 cold 省约
  68.7%/32.7%。`.7` fresh `ma-hotfold-search-v2-r59` 已用 MiniMax-M2.7 完成真实本地 no-match 和同 thread
  普通追问，四个 typed 字段均可准确续接；项目树不再产生 `.chat_history`。deadline 后 selector 已实际
  切为 cold-fold，同一 TUI 仍准确复述唯一串与全部字段。本机两条 API 路径在约 310 秒仍命中 12,288-token
  固定前缀块；本轮尚未提交或推送，更长时效和不同 provider 校准仍留在 ROADMAP。

- 2026-08-27 `ae3fd1e` 修复流式 thinking 在 final 后复制：Anthropic collector 在原
  `content_block_stop` 调用同一观察器的 typed `complete`，同一物理调用不再由 response fallback 重放；
  TUI 对旧 Gateway 的迟到完整终态只消费一次。完整 focused 与本地严格 gate 通过，已推送并部署 `.7`
  唯一 Gateway PID `1416524`。原 `ma-r55-terminal-sticky-retest` 恢复后的真实 MiniMax-M2.7 请求只有一个
  `assistant_thinking`，它严格早于正文；屏幕最底稳定块是 assistant final。

- 2026-08-27 `1e91935`、`1b75762`、`1e4c64d` 完成长 TUI 正文/思考时序、完整消息边界、单行滚轮、
  canonical task root 与终态 sticky cwd 首采样绑定。完整 focused 与本地严格 gate 通过，已推送并部署 `.7`
  唯一 Gateway。fresh r55 的 5 名 child 自然完成、main 自动接棒、final 直接显示；同一 thread 三个独立
  completed request 精确复用一个 task path，后续轮首个列表、审批、bwrap 和 Python cwd 均在原 task root，
  localhost 返回真实游戏 HTML。Mac 对 `192.0.2.7:8765` 仍失败且模型只报未验证，因此 LAN 可达性、
  Gateway 约 30 秒冷启动和 main 的协调软纪律不在此完成项内。

- 2026-08-26 `8f50d19` 将显式后台命令从 one-shot main/child runner 改归 owner conversation session：
  detached host 持有原 bwrap 与 `--die-with-parent`，owner 沙箱外 `managed_process_session.v1` 保存 exact
  scope、store root、PID 出生指纹和单调终态；登记失败先回收，runner 退出后仍守日志上限，主机崩溃不
  自动重放。140 项直接相关回归与本地严格 gate 通过，已推送并部署 `.7` 唯一 Gateway。fresh
  `ma-evidence-r53-child-process-session` 在 child DONE 至少 27 秒、root final 至少 12 秒后仍返回 r53/200；
  另一进程水合 running，错 scope 隐藏，stop 关闭 host/bwrap/8769 并持久化 killed。

- 2026-08-26 `814cc3b` 让空输入 `Down` 先读取当前 main/child viewport 的 typed `follow`：离尾时复用
  唯一 `end()` 返回最新消息，已贴底时才继续 child selection/history；输入有字的视觉折行行为不变，也不
  解析 `N new messages` 文案。145 项相关 TUI 回归与本地严格 gate 通过，已推送并快进部署 `.7` 唯一
  Gateway。fresh `ma-scroll-r38-resume` 在 3.5 秒内恢复复杂会话；主视图真实证明
  `Ctrl+Home → Down 回底 → Down 选择 child`，终态 child 也证明 `Ctrl+Home → Down` 只回自己的尾部。

- 2026-08-26 `8c11eab` 将 Anthropic `input_json_delta` 的脱敏工具名、阶段、累计字符数和耗时接入
  Gateway rich/后台 main/child 与 TUI transient block；半截 JSON、正文、命令、路径和凭据不离开 parser，
  也不提前创建 ToolCall、执行 handler 或改变 Compact/任务状态。8 个相关文件 195 项与本地严格 gate
  通过，已推送并部署 `.7` 唯一 Gateway。fresh `ma-tool-progress-r37-mario` 真实显示
  515 chars/5s → 3.0k chars/29s，55s 后完整 `create_subagents` 卡片创建 5 个 child，临时行自动删除。

- 2026-08-26 `df95d27` 收口 Anthropic-compatible native 首轮空历史身份：`None` 只表示 text 请求，
  `[]` 保留 native empty history，使稳定 prompt/工具从第一次 Agent 调用进入唯一 copy-on-write
  `cache_control` 投影。9 个相关文件 193 项与本地严格 gate 通过，已推送并部署 `.7` 唯一 Gateway。
  fresh `ma-cache-firstturn-r34` 首请求真实写 25,999、读 12,313 cache tokens；同 thread 两个后续回合
  又读 37,234 与 46,972。MiniMax-M2.7、单 Gateway、canonical compact 0 均由结构化状态确认。

- 2026-08-25 对照 会话运行时 child `last_agent_message` 交接主链，执行 child 的可见合同已只保留用户/父级显式
  业务产物；宿主 `final_report/output.json/runner_result` 从 output contract、task packet、workspace refs 和
  旧 bundle prompt 全部移除，自然 final 后再由宿主一次收口。完整 execution-context 留作宿主审计，模型
  只收安全 write boundary/context bundle，恢复只见 checkpoint/summary/task；显式同名业务文件仍以
  `required_file_refs` 为权威。102 项 focused 与本地严格 gate 已通过；`.7` fresh child 仍列在 ROADMAP。

- 2026-08-25 `f901645` + `ff94d61` 对齐 会话运行时 typed Compact/TokenCount 与 终端交互 post-compact state：手动
  `/compact` 回执携带 canonical generation，TUI 立即撤下压缩前 Context；idle/resume 无 Working block 时仍
  从 activity 水合代数，迟到旧帧不能回退。251 项完整相关 focused 与严格 gate 通过并部署 `.7` 唯一 Gateway。
  原 `ma-97468f3-longchain-r27` 已直接恢复 `compact 1`，手动 generation 2 为 15,328→14,246，下一真实轮显示
  约 28.5k/128k；再一普通轮命中 17,019 provider cache-read、仅 3,254 uncached input，证明新前缀可继续复用。

- 2026-08-25 `7b14e34` 已把普通 main/child 回合的 canonical 工具 archive 一次折成不可变、脱敏、6k 默认上限
  的 `conversation_terminal_tool_fold.v1`；公开正文不变，下一轮历史只追加同一 fold，真正 Compact 才摘要。
  overflow→Compact→继续调用的累计 provider usage 同时按物理游标原子转为增量，cache-read/cache-write 不再
  重复累计。289 项 focused 与严格 gate 通过并部署 `.7` 唯一 Gateway；原长 tmux 两轮真实证明 2 次 Read
  可跨回合准确续接，provider cache-read 分别为 42,107 与 12,987，手动 Compact generation 1 成功吸收
  98 条消息。其成功后 TUI 仍显示旧 `compact 0` 的二层显示同步已转入 ROADMAP 候选。

- 2026-08-25 `4425618` 把完整 task-path 进度账本与当前 conversation request 的 TUI Todo 分层：宿主
  `display_plan` 携带 exact generation/revision/item ids，迟到旧快照不能复活上一阶段清单；主/子 transcript
  的 typed anchor 通过 prompt_toolkit `Window.get_vertical_scroll` 落到真实窗口。362 项 focused（2 xfail）、
  95 项 Compact/TUI 组合和本地严格 gate 通过，已推送并部署 `.7` 唯一 Gateway。原长 tmux
  `ma-97468f3-longchain-r27` 两轮普通追加均清掉旧 24/35 Todo，PageUp 离底显示跳底提示，Enter 立即回底，
  终态 Working 收起；46%–53% 低于 90% 压缩线，主代理 `compact 0` 与权威 thread 一致。

- 2026-08-25 `8e6948d` 已把 会话运行时 的“同批只放可立即并行的独立 sidecar”写入唯一
  `create_subagents` 模型合同并部署 `.7` 单 Gateway。长 TUI `ma-97468f3-longchain-r27` 真实证明修复 child
  完成后才由 typed wake 创建独立 tester：12:50 与 2:27 两段没有重叠、均一次 attempt、无需用户发送
  “继续”，最终活动归零。该能力仍是软派工纪律，不新增依赖状态机或机器验收。

本文件不再保存历史流水。当前完成项以 git 历史和模块 `02-progress.md` 为准。

最近收口重点：

- 2026-08-25 收口 `4106025` 真 TUI 暴露的第二层 active-turn 身份错位：durable task id 只定位 root
  workspace，child canonical attributes/completion wake/工具索引统一携带 exact
  `conversation_request_id`，后台只恢复该 turn 的工具事实；字段落盘前的旧行仅由同值 request id 精确
  兼容。模型把 exact run id 拼入过期 task 目录时，`read_file` 只经同 owner canonical agent projection
  找回唯一 `final_report.md` 并重过权限门，其它内部状态不跳转。5 个 focused 文件 206 项结果为 204 passed、
  2 个既有 xfailed；本地严格 gate 全通过，真 TUI 复验仍留在 ROADMAP。

- 2026-08-25 收口 child 完成后 root 已回复但 TUI 永久 Working 的底层事实断点：工具输出首次归档现把
  host-owned `tool_execution/tool_operation` 以有界白名单写入大小输出 index，background carried record
  恢复 succeeded operation，仍不把 `ok=true` 或正文猜成副作用成功；精确宿主 `final_report.md` 作为完成
  信封交接叶子可由 `read_file` 读取，其它 state/list/shell 状态面继续拒绝。5 个 focused 文件共 234 项，
  结果 232 passed、2 个既有 xfailed；Ruff、doc-sync、strict code-size、diff 与 clean-package 全通过，改动
  远低于 10,000 行，未跑全仓。`.7` 单 Gateway 真 TUI 复验仍按 ROADMAP 执行。

- 2026-08-25 `999a621` 把 `subagent-completion.v1` 接入普通 Gateway follow-up，并按同 thread、非 detached
  task link 的 exact canonical task path 建立 workspace lineage，避免新 follow-up task id 切断原 root
  child 结果。119 项 focused 与本地严格 gate 通过，已推送并部署 `.7` 唯一 Gateway。原长 session tmux
  `ma-41d5a4a-terminal-fix-r26` 的普通中文续轮收到十份 completion、直接完成八项目整合且零目录搜索；
  后续两条小任务仍复用同一 session 且零工具调用。内部 `final_report_ref` 的安全可读投影仍留在 ROADMAP。

- 2026-08-25 `41d5a4a` 对照 会话运行时 terminal child 完成消息收口三处底座：统一根/递归父级的
  `subagent-completion.v1` 最终回复与 refs，隐藏内部 runner 文件；`search_text` Python fallback 流式返回并
  受 20,000 文件/10 秒与 cancellation 约束；TUI 终态优先关闭陈旧工具动画、冻结耗时，并兼容 SSH/tmux
  只转发右键 release 的复制序列。200 项 focused 与本地严格 gate 通过，已推送并部署 `.7` 唯一 Gateway。
  tmux `ma-41d5a4a-terminal-fix-r26` 已证明主/子历史、终态显示和 Esc 停止；该现场另发现的普通前台续轮
  completion 漏接已由上面的 `999a621` 收口。

- 2026-08-24 `2bf4602` 修复运行中 child 插话“HTTP 接受后立即消失、模型只在 thinking 里理人”的合同
  错位：Gateway 接受只表示 `queued/pending`，child TUI 继续显示 exact pending；provider 成功消费后才以
  `active_turn_input_consumed(client_message_ids)` 按 FIFO 提升为正式用户历史。child 系统提示要求先用普通
  assistant 回答真实用户再继续原任务。148 项相关 focused 与本地严格 gate 通过，已推送、部署 `.7`
  唯一 Gateway。tmux `ma-2bf4602-child-chat-r25` 中连续两条中文同时排队，随后顺序入历史、获得一条分别
  回答两问的正文，紧接着继续 `web_fetch`；`Ctrl+O` 未离开 child 视角。

- 2026-08-24 `53498c1` 修复唯一 Gateway 被多 TUI 轮询拖到 6.68 GB 后遭 OOM kill、fresh TUI 只显示
  “正在连接 Gateway”便退出的问题：按 会话运行时 固定执行者/容量 128 背压原则，将 thread-per-request 换成
  16 个复用 daemon worker、128 总在途上限和 typed 503；所有短轮询响应关闭 keep-alive，防单客户端占满
  工位。多用户边界另对照 通道运行时 的入口限流与 长期助手 的 session lease，继续由鉴权后的每用户/同会话
  准入及 owner round-robin 守公平，不在 socket 层相信身份头。40 项 focused 和本地严格 gate 通过，已
  推送部署 `.7` 唯一 Gateway。fresh tmux `ma-53498c1-http-pool-r24` 约 1 秒进入 TUI，MiniMax-M2.7 真调用
  成功；9 个 TUI 自然轮询只生成 `gateway-http_0..4`，旧 `process_request_thread` 为 0，RSS 约 132 MB
  稳定。Gateway tmux 为 `ma-gateway-53498c1`。

- 2026-08-24 `0da26f0` 修复“历史存在但物理滚轮看不到”的默认交互回归：按 终端交互 恢复启动即开启
  mouse tracking，wheel/离底/follow 与应用内复制共用现有 typed viewport；F6 只作原生复制逃生口，footer
  随模式变化。155 项 focused 与本地严格 gate 通过，已推送部署 `.7`。tmux
  `ma-0da26f0-终端交互-history-r23` 中 root/child 默认滚轮均看到完整历史，中文拖选和右键复制完整写入
  tmux buffer；未发送模型消息或改被测产物。

- 2026-08-24 `7e2ffb6` 修复子代理名册“游标已到隐藏项但屏幕仍画前八项”：renderer 现在从同一
  canonical ordered roster 裁出包含 exact `selected_run_id` 的八行窗口，不新增滚动状态、不改 Enter
  裁决或 child 状态。65 项 focused 与本地严格 gate 通过，已部署 `.7` 且未重启唯一 Gateway；tmux
  `ma-7e2ffb6-roster-r21` 连按九次 `↓` 显示 coordinator-9 高亮，Enter 进入同名详情，返回向上后
  researcher-8 与前八项恢复。未发送模型消息，未改被测产物。

- 2026-08-24 `6d33228` 修复 TUI 给运行 child 插话会触发
  `guidance submission reservation mismatch` 的底座错误：Gateway 入账前绑定 exact pending/running
  AgentAttempt，缺少活跃片时拒绝且不落消息，ConversationStore 严格门未放宽。默认容量从会话 6、单次 4、
  runner 4 收口为会话 8、单次 0、runner 8；root/child 常驻 footer 明示 PgUp/Ctrl+Home 与 F6。429 项
  focused 和本地严格 gate 通过，已推送、部署 `.7` 唯一 Gateway。fresh
  `ma-6d33228-p3-guidance8-r20` 中八名 researcher 同时 RUNNING；researcher-1 中文插话 receipt 最终
  consumed、child 继续工作，root/child 历史及 F6 滚轮均通过。任务产物未被测试者修改。

- 2026-08-24 `d14549c` 完成代理视角独立滚动状态：main/child/grandchild 的普通与详细 viewport 分别保存
  follow、cursor 与未读基线，返回已有页面恢复原锚点，跨页选区清除；原生复制模式返回父级时明确提示
  `PgUp/Ctrl+Home` 与 `F6` 取舍。128 项 focused 和本地严格 gate 通过，已推送并部署到 `.7` 唯一
  Gateway。tmux `ma-d14549c-scroll-r18` exact resume 同一 Prompt 2 会话、不重发任务，实按证明 main 与
  worker-1 的首屏锚点可双向恢复，F6 后滚轮能翻到旧工具调用，再次 F6 可回原生复制。

- 2026-08-24 `91a1c3d` 完成子代理详情的完整消息页接线：详情首条读取 canonical `task.goal`，typed child
  sink 不再因自身非 callable 丢失工具卡和工具前过程说明，`Ctrl+O` 冻结当前 active runtime。218 项 focused
  和本地严格 gate 通过，已推送并部署到 `.7` 唯一 Gateway。原样 Prompt 2 的 tmux
  `ma-91a1c3d-child-full-r17` 实际显示完整派工、灰色 thinking/process、工具与代码内容，并证明展开不跳 root；
  三名 child 均产生配对工具事件。被测游戏任务仍在运行，不计入本完成项。

- 2026-08-24 `c12ea57` + `e2aba94` 完成 TUI 进程、durable session 与 Gateway task 分层，以及单 Gateway
  活动/readiness 降载。普通 `/exit` 结束客户端/poller，保留 session 和后台任务并打印 exact resume；tmux
  detach 仍明确保持进程。TUI 活动与后台 completion batching 均先按 root 索引选 exact run ids，再重读
  canonical task；无关历史损坏不再阻塞当前 root，当前树损坏仍 fail closed。`.7` 真机 exact resume 前后
  session 数均为 324，TUI PID 正常退出、Gateway 始终一个；8 秒 CPU 为约 12% 单核，12 次 stack 采样未再
  出现全量 `list_runs_report/deepcopy`，16 会话并发快照最慢 0.294 秒。181 项 focused 跑到 100%
  （179 passed、2 xfailed），本地严格
  gate 通过；未跑全仓 pytest，因改动远低于 10,000 行。

- 2026-08-24 `c5026a7` 完成 TUI 子代理可进入视图和 owner-scoped 共用控制面：空输入 `↓/Enter` 进入，
  `Ctrl+G` 只返回，`Esc` 精确停止当前 child；运行 child 接受幂等普通中文 guidance，终态 child 保留历史/final
  但只读。child 的公开 thinking/tool/diff 使用有界跨进程事件流，状态与权限仍由 canonical 账本裁决。
  focused 严格门通过并部署到 `.7` 唯一 Gateway；原样 Prompt 2 的 `ma-c5026a7-agent-nav-r12` 已实际覆盖
  多行选择、运行/停止/完成详情、插话、返回和停止，模型为 MiniMax-M2.7。

- 2026-08-24 `fb75b68` 已删除 会话运行时 没有的 `SUBAGENT_ACTIVE_LINEAGE_EXISTS` 硬门、冲突扫描与 Audit
  专项绕过；同一 canonical parent 可分批创建不同 child，重复工作仍由 typed idempotency/work-scope
  复用，单次 4、会话 6、active-turn claim 与 owner 权限墙保持不变。派工 focused suite 和本地严格 gate
  通过，代码已推送并部署到 `.7` 唯一 Gateway。fresh r9 首批 3 名自然 DONE、一次 child Compact 后，
  background main 成功用一个 items 调用创建第二批 3 名；独立 repeated-call+active-sibling 分支当前证据
  仍是直接回归，等待自然 TUI 样本。

- 2026-08-24 `80386ac` 已把 会话运行时 派工后 no-duplicate-work 规则集中到
  `orchestration/coordinator_policy.py`，root 工具说明、递归 coordinator runner、角色模板与
  `coordinator_execution_scope.v1` 创建回执不再互相矛盾；这只是模型软职责，不恢复机器验收或写工具硬拦。
  同批新增后台 main 的 1024 条易失 typed 事件环与独立整数游标，显式思考、工具结果、终端交互 风格
  `Update/Write/Bash`、折叠和红蓝 diff 复用原 TUI reducer/renderer，最终回复仍走持久 notice。
  218 项 focused 与本地严格 gate 已通过，代码已推送并部署到 `.7` 唯一 Gateway；
  fresh r7 已显示蓝色工具标题、层级输出、折叠提示和固定 Todo/child 区。该轮新暴露的
  max-token child 续跑断链不算在本完成项内，修复候选与复验见 STATUS/ROADMAP。

- 2026-08-23 本地候选已把普通 `workspace:*` 退出跨 run 持久 operation 锁，同时删除
  `output_files/output_refs` 的创建前所有权拒绝和活跃 child 动态 `locked_files` 投影。
  owner 分库/分目录、远程 owner home、write boundary、sandbox、operation 幂等/replay 与逻辑锁保留。
  过期父目录锁不再拦截新 handler 的主链回归已通过；推送、`.7` 部署和真实 TUI 对照尚待完成。

- 2026-08-23 `e94f8ec` 曾部署普通计划的同轮停止核对；该方案已在 2026-08-28 按用户决定退役，因为它会
  用 open Todo 覆盖模型 plain final 并增加隐藏 provider 调用。历史 Prompt 4 r19、112 passed 和白屏证据
  继续保留为任务样本，但 stop-nudge 不再是当前产品能力；现行语义见
  `docs/modules/delivery/01-closeout.md` 与 `docs/ROADMAP.md`“普通计划的 会话运行时 式自然结束”。
- 2026-08-23 Prompt 4 r18 证明可选 covers 后 root 能继续多批真实编码，但在 canonical Todo 仅完成 5/8、
  构建/测试仍 pending 且机器无 Rust/Cargo 时，普通 final 仍把 durable task 写成 DONE。本地切片已按 会话运行时
  stop hook 增加同一 active turn 的一次结构化清单核对；耗尽后只会 typed blocked，不另起自动续轮，也不
  扫描代码、测试或产物。原生指引转发、主链集成和状态保持的 focused 回归已通过；发布与 fresh r19 见
  STATUS/ROADMAP。
- 2026-08-23 `4c3a59d` 部署后的 Prompt 4 r17 证明骨架/TUI child 的直接 goal 边界改善；但 Git child 写成
  cwd 根部第二棵 Rust 树后，root 返工时被 mandatory covers 逼着把新 Git child 绑定到下一 open GUI
  Todo `4`，造成确定性假进度。现场已 `/stop`；当前切片把 covers 改为可选 exact 映射：省略时用真实
  child run id 记进度，提供的错/关/重复 id 仍原子拒绝；返工原项先以 `correction=true` 重开。发布和
  fresh r18 见 STATUS/ROADMAP。
- 2026-08-23 `b7005a8` 部署后的 Prompt 4 r16 已证明 root 会安全自主选语言、创建 7 项 Todo、按 typed
  repair 补 exact `covers`，并在首名 child DONE 后自然派第二名；但首名骨架 child 声明 6 个文件却额外
  写 11 个文件，其中两个与第二名职责重叠。会话运行时 spawn 不要求预报完整写集，现场也证明模型自报不完整；
  当时切片保留 mandatory covers，把 `output_files` 降为可选交付/冲突提示，并让 child 只把直接父级 `goal`
  当完整工作边界。该改动不解析 goal、不恢复机器验收；发布和 fresh r17 见 STATUS/ROADMAP。
- 2026-08-23 Prompt 4 r15 暴露根代理会把用户已授权的安全次要选择退回用户；相同 MiniMax-M2.7 的
  会话运行时 对照也在探索后输出了语言/范围/测试菜单，均违背 会话运行时 Default 源码。当前切片把源码中的
  assumptions-first 语义适配到根默认 `system_prompt` 前部：合理默认后继续，只有实质偏离、越权或不可逆
  风险才问一个短问题。它不含项目/语言专项，不解析问句，不恢复机器验收；发布和 fresh r16 见
  STATUS/ROADMAP。
- 2026-08-23 Prompt 4 r14 证明 `c5cc7c2` 下主代理能在 child DONE 后自然醒来并创建第二批，同时抓到旧
  goal-id 自动补绑把目录名 `i18n/config` 当作 Todo id、造成 3 项假完成。当前本地切片删除这条自然语言
  决策旁路和 `covers_auto_bound` 投影；当时计划内派工继续只认显式 `covers`，漏填由同一 typed repair 返回
  模型。新增同名目录负向回归已通过，发布与 fresh r15 见 STATUS/ROADMAP。
- 2026-08-22 `611ee55` 已部署，Prompt 4 r13 真 TUI 证明计划内派工原子合同生效：5 批重复 covers 均零
  child，模型自行细分计划后创建了 exact covers + cwd 内精确写入集合的 child；该 child 实际产出 1,296
  行 Rust、失败后继续并自然唤醒 root。r13 同时抓到新报码漏进 taxonomy、外层显示 `UNKNOWN_ERROR`；
  当前本地小修把它归为可修参数的 orchestration 错误，不增加专项循环。发布与 r14 见 STATUS/ROADMAP。
- 2026-08-22 Prompt 4 r12b 已证明 `5f1f485` 的 native active-turn handoff 保留原目标、语言和嵌套工具
  参数；同时抓到计划内派工仍可漏 `covers`、把兄弟目录藏在 goal 后先创建再权限阻塞。当前本地实现把
  exact Todo 绑定、直接编码 child 的结构化写入集合和父级 workspace 上界合并为创建前原子合同；失败整批
  `not_started` 并返回 typed repairs，同批父子路径也算冲突。该合同不解析自然语言、不判断质量/完成，
  capability 也不能扩到兄弟目录。发布与 fresh r13 结论以 STATUS/ROADMAP 为准。
- 2026-08-22 `a190378` 已完成并部署 root lifecycle wake 的 exact objective/task/path 与工具索引恢复；fresh
  r11 证明语言和任务范围不再随 child 完成漂移。本地后续切片又收口了索引中嵌套 Todo/派工参数被清空、
  native provider 看不到 carried 工具轨迹的问题：参数以有界递归且凭据脱敏的 JSON 持久化，跨进程续跑
  使用同一 IR 的 `CompactionSummary` handoff，Task Runtime State 给出 exact Todo id 和 `items[].covers`
  延续合同。focused 回归已通过；发布与 r12 真机结论以 STATUS/ROADMAP 为准。
- 2026-08-22 `0c6c916` 已推送并部署，Prompt 4 r10 真 TUI 证明未声明产物的 child 不再收到 synthetic
  Markdown 业务合同，typed status、最终回复和系统报告可以独立完成交接。r10 同时形成新的底座失败样本：
  completion wake 把 synthetic 文案当成新 User Task，导致 root 从 Rust 漂移到 Go。原始任务未丢盘；当前
  工作树已按 会话运行时 active-turn history 完成 exact task objective 与 root tool archive 的续接实现，发布与
  r11 真机结果以 STATUS/ROADMAP 为准。
- 2026-08-22 Prompt 4 r9 已在真实 TUI 证明 `93e18f6` 的 child 身份所有权：5 名 child 均使用 exact
  thread/parent，未出现 child `ordinary_task_resume`、root/child 混合身份或双执行器。r9 同时暴露
  系统默认 Markdown 被 runner 冒充“用户业务产物”，让编码 child 只写分析报告。当前本地候选已按 会话运行时
  spawn/final-message watcher 删除新任务的假文件合同，并让历史标记只留在 durable 迁移层，不进入模型
  可见 contract、完成通知或 expected outputs；真实显式输出规则不变。发布与 r10 见 STATUS/ROADMAP。
- 2026-08-22 Prompt 4 r8 暴露 child 断点被 root 后台错误接管，以及共享批次 PID 阻塞单个 PENDING
  child 重试。当前本地候选已对照 会话运行时 的独立 child ThreadId/session 与精确父级 watcher：task-local
  finalize 不再登记 `ordinary_task_resume`；绑定 canonical child task 的后台来源在 root 模型前退役或
  无模型确认；每次 runner 结果回到 PENDING 都只释放 exact run 的启动占位，再沿既有 runner auto-start
  续派。focused 回归、推送与部署已完成；r9 结论以 `STATUS.md` / `docs/ROADMAP.md` 为准。
- 2026-08-22 Prompt 4 r7 已在真实 TUI 证明 r6 的四项修复：isolated 用户回执不再泄露 thinking 或覆盖
  main context，单项补派连续编号为 worker-6/7，Todo 显示真实完成/运行数且四行只作视窗。该轮同时暴露
  新的通用候选：main 把完整任务降成欢迎页空壳，安装/启动失败后删除有效失败测试再误报完成。当前实现
  已对照 会话运行时 持续到端到端解决与运行测试验证的 prompt，把委派范围保真、失败入口重跑和禁止为绿灯
  削弱有效测试并入 root/child/lifecycle wake 共用软纪律；不新增机器质量验收器。发布与 r8 状态以
  `STATUS.md` / `docs/ROADMAP.md` 为准。
- 2026-08-22 Prompt 4 r6 的真实 TUI 样本推动四个通用底层候选：`context_scope=isolated` 的用户回执轮
  继续进入模型成本/调用账本，但不再公开私有 thinking 或覆盖主任务 context；系统生成的一项 `items`
  返工也沿 exact parent 的历史 sibling 序号，显式自定义名保持不变；Todo 标题显示 typed
  `完成 X/Y · 进行中 Z`，默认四行仅是可展开视窗。实现对照 会话运行时 active-turn reasoning 与 终端交互
  `TaskListV2`，200 项直接相关 focused 回归已通过；推送、部署与 r7 真机结论仍以 ROADMAP/STATUS 为准。
- 2026-08-22 本地候选已把 Prompt 4 r5 的“明知未完成仍 final”收口为 会话运行时 式模型执行纪律，而不是恢复
  吃过多次亏的宿主质量验收：发布 YAML 与 dataclass 共用同一个通用默认 prompt，删除 Go 专项骨架；
  主代理、子代理和 lifecycle wake 共用“已知缺口且仍可推进就继续”的软提示。`task_progress` open 项只
  返回非阻断 guidance，`covers` exact id 现在同时绑定普通 Todo 与 coverage，并在 canonical child DONE
  后打钩；单个、批量和递归 child 共用连续显示编号。直接相关 focused 回归已通过，真机状态仍以
  `STATUS.md` 和 `docs/ROADMAP.md` 为准。
- 2026-08-22 `680e209` 已完成并部署 main/child/grandchild 统一 Conversation Compact 账本：每个 delegated run 在创建
  或旧任务首次恢复时物化独立 `agent_thread_id`，每个 attempt 幂等落 user/assistant；轮前 transcript 与
  运行中 native IR 都复用同一 owner/thread checkpoint-before-CAS 主链。live-tool 来源保存精确 ToolCall ID，
  不推进 transcript cursor，只累计 `compact_source_tool_pairs`；provider overflow 也不再账外丢 20%。
  摘要/checkpoint/CAS 失败恢复原 IR 并进入同一失败熔断。task-local 权限与 Memory 隔离保留，旧
  apply/continue 不再由正式 child runner 生成；TUI/Web/SQLite 只读 child thread generation。大型项目真机已
  证明 generation/checkpoint/窗口下降与继续运行生效；同时发现并在本地修正摘要请求顺序：真实任务 user
  在前、native history 居中、synthetic Compact user 指令最后，对齐 会话运行时，避免 MiniMax 普通续写末尾动作。
- 2026-08-22 本地底座已把 Compact 的“配置压缩点”与“单次回合是否允许持久
  apply”分开：presentation/no-save 回合不再把 Context 行的 90% 错写成 100%；真实
  preflight 仍保持 save=False 不压缩落盘，并由完整模型窗口守最后硬限。回归同时锁定
  `115.2k/128k` 的展示及 `120k` no-save 不误触发。
- 2026-08-22 本地底座已修复后台主代理读取第二本空进度账的问题：task-path 指纹算法集中到
  `runtime/task_identity.py`，工具写入、Task Runtime State、child seed 终态同步、Goal 续跑和 TUI 投影
  使用同一 ledger id。负向回归证明 request-id 下的旧账不会覆盖真账。普通 Todo 仍只作模型可见工作笔记，
  没有恢复机器验收或普通任务自动续跑；部署和正式 Prompt 3 真机结论仍以 STATUS/ROADMAP 为准。
- 2026-08-22 本地 TUI 已把 Todo 默认摘要收为固定四条状态窗口：最近完成、当前运行和下一待办优先，
  运行项复用 Working 动画，`Ctrl+T` 只展开/收起完整 canonical 清单。常驻 Context 显示总量、窗口占比、
  主 ConversationThread 已提交 Compact 次数和明确命名的自动压缩点；协议相关 prompt/messages/tools
  分类留给 `/context`。该轮曾用 exact-run 数字投影补过渡期次数；这条过渡现已被上方独立 child
  ConversationThread generation 主链删除。部署/真机状态仍以 `STATUS.md`、`docs/ROADMAP.md` 为准。
- 2026-08-21 `714c0c8` 已把单一后台计数扩展为 终端交互/模型助手 Code 式的固定子代理
  活动区：Gateway 从 active task link 与 canonical run 账本生成有界直属 child 投影，输入框附近原位
  显示名称、状态、职责短标题、耗时和 attempts。它不进 transcript，不暴露工具输出/路径/权限，也不参与
  完成、重试或验收。代码已推送并部署 `.7` 单 Gateway；两个真实 child 分别在 46 秒、52 秒一次 attempt
  `DONE`，活动区完整显示状态变化并在主代理自动汇总后收起。父级 wake 在旧进程卡 process-local lane、
  优雅重启后立即恢复的尾项仍留在 ROADMAP，未冒充为全部生命周期验收通过。
- 2026-08-21 当前本地切片按 `d928d77` 真机失败样本收口四处底层语义：普通相对交付路径继承当前可信
  cwd，显式 output/work 才进入 task 内部目录；child 共享阅读包不再跨任务缓存；只有根主代理与结构化
  coordinator 持有 create/guidance/cancel/resolve，普通 leaf 无下级管理工具；TUI 从 canonical active
  task count 显示可移除的后台 Working。188 项定向回归和本地严格 gate 已通过；这里不代表已推送、部署
  或真机交付通过，当前状态仍以 ROADMAP/STATUS 为准。
- 2026-08-21 本地实现已补上递归直属事件链：task-local 父代理创建下一层后以
  `interrupted/SUBAGENTS_ACTIVE` 让出，精确 child ids 的耐久标记会阻止孤儿器误复活；同批成功收齐只
  恢复一次，失败或 capability 阻塞立即恢复，孙代理不再越级唤醒根会话。父级新工作片获得有界
  `direct_children` 状态和结果 refs，不需要 inspect/wait/shell sleep。旧 `task_progress` 自动 continuation
  也已删除，软清单不再决定续跑或完成。这里只表示本地实现完成，发布和真机结论仍以 ROADMAP/STATUS
  为准。
- 2026-08-21 本地底座已把 Gateway 后台整合从 owner 级 single-flight 收细为
  `owner + durable thread_id` 车道。同会话仍共用既有持久 run claim 单飞，不同 TUI/会话按 owner
  公平、在全局池和 `background_threads_per_owner` 两层显式上限内并发。真实 scheduler
  回归已证明同 `local/main` 的一条模型回合挂起时，另一条会话仍能入模。
  这解决的是“旧窗口占住用户后台通道，新任务 child 全完成也叫不醒主代理”；
  `.7` 真机交付结论仍以 ROADMAP/STATUS 为准。
- 2026-08-21 本地实现已把主代理与任意层级子代理收敛为同一递归关系：模型只有统一
  `create_subagents` 创建入口，创建后由宿主自动启动；手动 `dispatch_subagents` / child scheduler 工具已从
  模型控制面删除，废弃 dispatch action envelope 也已移除；命令行 dispatch 仅作为宿主后台进程入口，
  普通用户无需调用。主代理、子代理与 Gateway 共用六类 `turn_end`，普通完成不再依赖机器质量验收、
  `acceptance_checks`、`VERIFIED` 或专用结果包装。TUI 同批补齐 Working 动画、条件式页底跟随、持续 thinking
  增量和 Compact 百分比。当前只代表本地实现完成，发布与 `.7` 真机状态以 `docs/ROADMAP.md`、`STATUS.md`
  为准。
- 同日递归控制面继续按 会话运行时 收口：删除 create 后周期性 LLM 巡场和 wait helper；`send_guidance` 只允许
  当前代理给一个直接 child 发送 `target + message`，用户插入主代理仍走 active turn。后台续跑身份改为
  task-bound，TUI 后台 notice 改为立即/每秒独立块，批量 child 的 `output_files` 获得完整嵌套 schema。
  这解决了同 thread 旧任务串权和后台更新丢失；随后普通 child 工作区继承又消除了“父级能写、child
  却必须重复声明权限”的断层。真实 8080 交付仍在 ROADMAP，未提前标记通过。
- 同日首轮 `.7` 原样任务定位并修复 capability 断链：OPEN 请求现在优先于 provider 的普通 completed
  收尾，child 会保持 `BLOCKED`；直属父级 grant/deny 后原 run 自动回到 `PENDING`，由同一生命周期链
  续跑，不再需要模型巡检或推动。普通 child 逐层继承父级结构化工作区上界，`output_files` 回归交付身份
  与冲突锁职责。未注册的 `InspectAgentTreeTool`、Schema、公开导出和专用测试已删除；内部状态投影继续
  服务 `/status`、TUI 与恢复。当前只代表本地实现和定向回归完成，真机结论仍以 ROADMAP 为准。
- 同日 `e321483` 真机失败样本证明已注册工具不等于模型首轮看得到：默认延迟整个
  `orchestration` 造成 0 child 且主代理自写。本地配置已按 会话运行时 multi-agent v2 改为递归代理控制
  首轮直出，并以默认 `model_visible_specs()` 回归锁定；本项只表示本地修正完成，真机复验尚未通过。
- 同日 `c2c0235` 真机让 4 个 child 真正自动启动，并定位出“进程活着但像挂死”的两层根因。本地已按
  会话运行时 最具体路径规则修复宽 forbidden 误伤窄 task output allow，真实 ToolResult 活动改读
  `tool_name`，runner 离散模型/工具阶段会写有界短状态；旧 `raise_event` 模型工具、Schema、注册、实现和
  专用测试已删除，内部 observation/wake 生命周期服务保留。活跃内置 Skill 的过期调用说明也已清理，
  并新增退休工具名扫描回归。这里只表示本地实现与严格 gate 已通过，发布与真机重跑见 ROADMAP。
- TUI 灰色层级已按 终端交互 的容器级 `dimColor` 语义修正：思考 Markdown 的普通文字、粗体、代码和
  链接以及 `Ctrl+O` 折叠提示都由最终浅灰 role 接管，不再只染括号或行前缀。鼠标左键拖选松手继续
  自动复制。历史记录中“tmux 使用 `load-buffer -w` 已写穿”的结论已被 3.3a 真命令表推翻；当前正确的
  `set-buffer -w` 候选仍在 ROADMAP，故本项只保留灰色层级和应用内选区为已完成。
  `167c98d5` 已推送并部署到用户更新后的 `.7` 测试机；本地/远端 focused 105 项、单 Gateway、多 TUI、
  MiniMax-M2.7、真实回复、ANSI 灰色层级与 tmux 中文写穿已验。外层系统剪贴板粘贴仍以用户手动验收为准，
  详细证据见 `STATUS.md`。
- 用户随后确认全屏 mouse tracking 下右键不会出现宿主菜单；正文和输入框已新增“已有选区时右键按下直接
  复制”，配对 release 不重复复制且不清高亮。本地六文件 focused 107 项通过；部署和宿主系统剪贴板
  粘贴仍在 ROADMAP，未提前写成完成。
- EXEC-44 写后验证新鲜度已由 aiohttp→Go 真机任务闭环：最后一次源码复制后，被测 Agent 自主重新
  build、运行 29 项行为测试并完成 HTTP 200 E2E，随后才输出 final。验证事实只来自 canonical
  `handler_details` 与 durable event；read/search 不清 stale，Go/Cargo manifest 分类不读项目名或 prompt。
  证据保存在 `.13:/root/tui-parity-evidence/rich-transcript-20260818/aiohttp-closeout/`，测试者未旁路修改产物。
- SANDBOX-02 已由 Tornado→Go 真机任务闭环：约 122MB 构建缓存/临时文件留在 canonical
  `work/.sandbox-tmp`，约 240KB 最终 output 未发现 `.sandbox-tmp/.cache/.gocache/gomodcache`。这证明
  `task_work_dir` 优先 sandbox root 与 `TMPDIR/XDG_CACHE_HOME=/tmp` 的通用修复生效，不依赖 Go/Tornado
  特判。旧 Click→Go 的 137MB 污染证据继续保留，不改写历史。
- 模型调用账本现把“最多 128 条明细”和“完整 request/run 累计数”分开：logical turn、物理 model
  attempt、provider HTTP attempt/retry 与终态分布在明细裁剪后仍准确，且不保存 prompt、response、key。
  现同一累计容器还保存 provider input/output/cache-read/cache-write token，无 usage 时保存标记明确的估算；
  `AgentRunResult`、Gateway result 和 runtime fact 可直接用于单任务成本对比，不依赖全局 metrics 差值。
  超限 focused 回归、本地/`.13` Ruff 与测试通过；旧任务 response 中的 128 已更正为 retained-detail 下限。
- 终端交互 TUI 可观察行为复刻已在 `my-agent` 以 Python/prompt_toolkit 单一 typed 状态链完成：
  typed journal/reducer、stable/active blocks、Markdown/code/diff、spinner/tool/permission、输入/history/
  search/completion/paste/queue、scroll/transcript/mouse/resize、interrupt/exit 和 canonical history resume
  均已接通，旧字符串 lexer/transcript/stream 路径已删除。85 项矩阵结案为 38 `VERIFIED`、38
  `MAPPED_VERIFIED`、9 `NOT_APPLICABLE`。`192.0.2.13:/root/my-agent` 最终部署 70 个文件、删除
  5 个废弃文件并保留回滚包；MiniMax-M2.7 Gateway/TUI 健康，secret 实值扫描 0 命中。详细任务见
  `docs/tasks/completed/TASK-20260818-终端交互-tui-parity.md`。
- 后续四路真机观察已重开其中 C17：旧版运行中普通 Enter 实际等待为下一回合，且 queue preview 会随
  transcript 滚走。该回归不改写本条历史验收事实；当前修复与 `.13` 复验状态以 `docs/ROADMAP.md`、
  `STATUS.md` 和 parity matrix 的 `IMPLEMENTED` 行为准，完成前不得继续引用旧 EV-QUEUE 宣称已通过。
- 用户后续体验指出的软折输入 Up/Down 与不可点击 unseen pill 已按 终端交互 的视觉行/回尾行为补齐；
  `/context` 读取自动 compact 同一估算，`/compact [instructions]` 复用唯一 checkpoint/CAS 主链，自动
  compact 继续按 90% 阈值在每轮前运行。`/effort` 入口只报告后端真实能力；当前 MiniMax-M2.7 没有可调
  effort 参数，设置会明确拒绝而不伪造生效。
- `.13` 真实普通问候、`/context` 与 `/compact` 已把 generation 从 0 推进到 1 并复查 summary/pending；
  同一问候还发现本地 owner 没有主动通道却暴露 `send_message`。当前以工具自身 availability 在每轮 snapshot
  前核对结构化通道事实，修复前 2 次失败调用、修复后 0 次；本地和远端 12 项消息工具 focused 回归通过。
- Fiber→TypeScript 最终请求为 188 次 logical/model/provider attempt、0 retry、186 工具轮和 `done/ok`。
  但产物仅 1,093 功能源码行，约为原版 27,354 行的 4.0%；18 项 Jest 直跑虽过，仍有 open-handle 警告、
  stub/no-op 中间件和大量公开能力缺失，因此只算可运行核心子集，不算完整等价复刻。
- 用户截图追补的鼠标松开后选区失控、终端兔耳少女头像、逐轮 commentary、可折叠 provider thinking、
  红蓝行号 diff、写入预览和命令 stdout/stderr/退出码已在 `.13` 真机可见。首个 Click→Go 请求因旧
  `ECONNREFUSED` 分类失败，修复双层有界退避后，独立续作以 197 个工具轮完成 3,021 行 Go 源码和
  可执行文件；当时 response 的 128 是账本明细上限，不再表述为精确调用总数。`[no test files]` 仍不能
  说成自动测试覆盖，旧缓存污染由随后 Tornado→Go 真实任务完成通用复验。
- 工具运行时已收敛为唯一链：`required_actions -> ToolRuntimeSnapshot -> ToolChoice ->
  provider adapter -> canonical ToolCall -> ActionPolicy -> ToolExecutor -> operation/reconcile -> canonical
  ToolResult -> settlement -> CompletionGate`。旧重复 Schema/审批/effect/执行入口、native 正文提升、
  protocol-v2、parser/JSON repair 和陈旧测试已删除。focused/full/static/package 门已通过；
  MiniMax-M2.7 四条原始普通中文真实验收证据在
  `validation/real_runs/tool-runtime-20260805T141123Z/report.json`。macOS 无 owner-scoped `bwrap` 的执行请求
  按合同阻断，没有降级到宿主执行。详细任务见
  `docs/tasks/completed/TASK-20260804-1913-tool-runtime-unification.md`。
- `/audit` 高频判读没有新增专用 Agent/Memory/Compact 路线，而是在现有 durable spool 主链加入
  时间、条数和按模型上下文计算的数据量三条件合批；自动判完即逐条签收，低风险原文不再进入长命
  子代理重复判。正常记录不截断，单条自身超窗才产生显式头尾模型视图，完整原文与逐条结论可由
  owner-scoped `source_ref/ack_id` 复查；安全上限已经扣除稳定提示词、请求信封和输出余量。spool
  只在完整落盘记录边界组批，避免 48+1 形成一次单条补判。1.10 正式 CLI 五路 10 分钟实测对
  3,000/3,000 条完成逐条结论并保留完整可追溯原文；但 20 条隐藏真样本只命中 14 条、48 条 hit 中
  34 条误报，机制通过不等于模型质量通过。部署后 smoke 还发现积压长日志的 HTTP 整页会超过 1MB；
  现在只按 typed 过大错误缩小完整记录页数，最少一条，单条仍过大才显式失败。最终发布状态仍以
  产品事实页为准。
- 主代理“虚报全部完成”不再通过自然语言分类或普通任务完成硬门处理：参考 终端交互 的结构化核验
  提醒，持久 `task_progress` 仍有 open item 时丢弃第一版 plain final，并在原工具循环中给同一模型
  `open_count` 软核对；下一轮仍可读/更新清单或继续工作，提醒随后移除。同一工具进展段不重复提示，
  但提醒后若产生了新的真实工具动作，后续收口可再核对一次；没有新增工具动作时允许普通任务结束。只有
  显式持久 `/goal` 的 open plan 保持 `unfinished` 并续跑。终态普通 task 与 sticky cwd 也已解耦，
  下一次工作在同一目录创建新的执行 task id；子代理工具严格继承父 run 并只能减权，模型/provider
  物理重试进入同一线程安全账本。发布和真机状态以产品事实页为准。
- 当前 turn 副作用事实已从同一 canonical tool archive/operation store 生成内部
  `operation_verification.v1` 与不含内部 ID/路径/参数的公开投影，贯穿 CLI、Gateway、HTTP、
  transcript、后台任务、历史索引和 compact。1.10 双 owner 真测中，MiniMax 一次零工具调用却声称
  完成，程序精确标为 `status=none`；同会话纠正后才出现四条 succeeded operation。最终两个 owner
  的临时 Memory 均清理、两条真实飞书发送 receipt 均为 sent；自由正文仍不是执行权威，不新增
  自然语言分类器。
- Compact 的可读摘要与操作事实已分栏：真实 MiniMax 反例会把 `remember/list` 错总结成“成功删除”，
  所以 `conversation_thread.v4` 在同一 cursor CAS 中另存有界 `compact_operation_evidence`，后续轮
  在摘要之后消费程序证据。相同反例续问已正确回答未删除；无中文关键词、无第二套会话。
- Memory 第二批恢复场景不再作为未开工路线项：route 冲突/缺权威文件、损坏或缺失恢复包、Gateway、
  subagent 与 local doctor 的既有主链测试已联合复验。长期 Memory 写入是锁内原子 write-through，
  没有 长期助手 外部异步 provider 的 pending queue，因此不复制 `pre-compact flush` 或第二个
  memory provider。只被自身测试调用、可绕过统一 Memory/配额/来源合同直接改 HOT/lesson 的
  `home_memory_notes` 旧写入口与测试已删除；`memory-hot.md` 只保留为用户/管理员现有提示文件。
- 工具结果不再由 live、compact、恢复和子代理共享链各自处理。`ToolRuntimePolicy.output_policy` 的最低输出信任与脱敏策略在
  Registry 执行结果上形成唯一投影，handler 只可收紧；外部网页/浏览器/MCP/视觉/watch 和
  `work/blobs/tool_outputs/` 归档正文统一按不可信数据进入模型，完整正文仍留 owner-scoped artifact，
  prompt 只保留脱敏有界 preview 和恢复引用。JSON/纯文本归档再经 `read_artifact/read_file/search_text`
  读取时继续继承来源，不以扩展名或正文关键词判定。MCP 的重复凭据正则已删除并复用统一 redactor。
  本地 Qwen、MiniMax 和两个真实飞书客户端链路均已验证；真客户端发现的 request 级消息幂等键碰撞也已
  在统一投递入口修复为 per-logical-message 稳定身份，不增加飞书专用分支。
- 多外部写继续使用唯一 operation store，没有新增通用 Saga 或自动回滚。权威 completion 保存失败时，
  provider 的成功回报会降级为 unknown 并阻止盲重做；operation/effect 状态贯穿归档、控制面、模型恢复
  和 compact。显式绝对路径的旧 escape-relocate 兼容链及专属死代码已删除，目标只能按原路径明确成功
  或被统一写边界拒绝。本地 Qwen 基础 CLI、MiniMax 长链/极端 CLI、完整本地门禁和 1.10 双 owner
  Feishu scope/真实出站均通过；新的桌面客户端入站因 macOS 锁屏未冒充完成，精确边界见产品事实页。
- 工具漏参不再由各 handler 或主循环分散补救：`ToolRuntimePolicy.input_policy` 逐字段声明安全默认值或 Registry 可信上下文
  binding，统一入口在 Schema/effect/path/审批前补入并写脱敏 `source/source_ref`。Schema `default`
  注解本身不获得执行权，显式模型字段不被覆盖，其余必填参数仍精确失败。`run_command`、PTY start 和
  `read_artifact` 已迁移，旧 cwd 末端补参和 artifact scope 特判删除；发布与真机证据以产品事实页为准。
- 工具参数合同已从“模型 Schema、拍平 required/type、MCP 投影、handler 各管一段”收敛为一条主链：
  `ToolModelSpec.input_schema` 同时驱动 provider 与副作用前运行门；只做无歧义强类型纠正，完整检查嵌套、
  枚举、范围和额外字段，并返回不含原值的 JSON 路径问题。外层信封与工具参数已明确分层，修复了
  `kind/run_id/status/metadata/artifact_refs` 既是正式参数却被旧协议名单跳过或误判的缺陷。MCP 不再
  压平 Schema，畸形/未支持断言在注册时 fail-closed；旧 unknown-field、MCP protocol-field 过滤和拍平
  required/type 支路已删除。本地 8899 与 MiniMax-M2.7 均已在隔离目录完成真实
  `PATH_NOT_FOUND -> 替代读取 -> write_file` 恢复链；完整 pytest、静态/合同门和干净 wheel
  发布门同轮通过。
- 会话运行时 式当前 turn 引导已经接入：`/btw` 绑定精确当前执行，与 `/stop`、完成共用迁移锁和 active CAS；
  辅助回执无权消费 active-turn input。输入在模型成功接收后幂等写入唯一 thread transcript，compact 续轮用
  typed carrier 保留，不再复制成 task guidance/history。再次发布与 1.10 产物复验前不把 `/btw` 列为稳定完成。
- 前台 request 归档与 durable task 后台接管之间的窄窗不再等同“任务切换”：linked request 的 retired
  状态会继续核对同 thread 当前 TaskRun 并发布幂等 wake；mismatch 和不可读状态仍 fail-closed。该候选
  已有同任务交接接受与真实切换拒绝回归，待 1.10 发布复验。
- 普通用户正文统一收口到模型表达：除显式控制命令外，聊天、派工回执、进度和最终回复都由 LLM 根据
  结构化事实撰写；统一出口只按机器可判定的空正文、真实工具调用和 bracket/XML/native 内部协议拒绝，
  不再用中英文正则猜“完成”、ETA 或大小语义，也不使用固定“正在运行”兜底。任务是否完成只认 typed
  runtime event；开放进度和未聚合子代理不能提前关根任务。
- 1.10 MiniMax M2.7 两个 Feishu-scoped 合成用户最终实测通过：A 精确 5 子任务、`/btw` 后完成，B 精确
  4 子任务、`/stop` 后取消且重启不复活；迟到投递为 0，A/B 记忆口令隔离，最终 `failures=[]`。该证据
  是服务器侧真实 owner/channel/conversation 链，不等同真实 Feishu 客户端入站；37.3/50.7 秒首次自然
  回执延迟仍按产品事实页列为性能缺口。
- 普通任务的派工/等待/完成正文继续由 LLM 生成；回执短轮已剥离旧工具历史，最终完成新增不可变
  `delivery_snapshot`。与快照一致的原模型摘要直接保留；有冲突时才带 draft 进入修订短轮。系统不会
  回退到“任务正在处理”等模板；内部协议、真实工具调用或空正文会重试一次，仍不合格则抑制正文。
  ETA、完成与大小由结构化 facts 约束表达，但用户文字不反向裁决任务状态。
- 普通任务不依赖用户输入 `/goal` 才能自主派工。当前工作树已加入前台安全 quantum：精确绑定的
  Gateway/chat task 到安全点后以同一 task/workspace 转入耐久后台续作，释放普通聊天入口；后台可按任务
  结构继续自做或创建子代理，停止/终态任务不会被该续跑策略复活。该部分已随 `047e24f7` 部署并在 1.10
  观察到两个长任务按 quantum 让出、一个任务自主派 3 个子代理、另一个任务完成 47 项测试；并行聊天的
  第二执行器缺口与尚未部署候选仍以产品事实页为准。
- 后台 claim 的 0 心跳语义、90 秒默认 TTL、同进程域死 owner 立即接管和跨 Pod fail-safe 已闭环；
  Gateway SIGTERM/SIGINT 也进入 typed stop/drain/forensics 主链。真实 1.10 发布复验仍以产品事实页为准。

- 当前 owner home 路径成为唯一默认运行路径。
- 文件大小硬门已改成报告提示。
- 多个旧转发层和历史路径模块已删除。
- owner-scoped 前后台 shell 已改为 bwrap fail-closed，并由 worker/K8s 复用真实 readiness 自检。
- 默认一键安装已进入透明容器 CLI；工作树与 wheel/tar 发布干净度使用同一结构化检查器。
- P0 收敛已完成本地验收：产品事实页、根目录 pytest、配置同步、Ruff、真实 blocker/advisory 报告语义、MCP effect 硬门、sandbox fail-closed 与未跟踪运行数据检查均已闭环；发布状态仍以 `docs/PRODUCT_FACTS.md` 为准。
- P1 主链收敛已完成本地验收：完整 import 矩阵进入 CI，生产 wheel 剥离测试/offline harness，默认 gateway/正式入口/显式插件链收口，真实 embedding 工具检索、POSIX PTY、stdio LSP 与 OpenAI native tools 已接主链；真实生态与规模承诺仍按 `docs/PRODUCT_FACTS.md` 的部分可用/实验性边界描述。
- P2 scale 主链接线已在当前工作树完成：显式 fail-closed profile、PG/ASGI/RLS、Redis 共享准入、OTLP、独立在线迁移 Job、真实 Agent worker 和 continuous-monitor proof 机制均已接线并做本机真依赖 smoke；十万用户、目标集群灰度和 24 小时真实异构来源仍未证明。
- 普通 Feishu 对话与工作主链已收口：真实 chat/topic 多轮 transcript、跨会话隔离、同会话顺序执行、结构化任务选择/提升/完成、内置默认 prompt、USER/AGENTS 自主维护与 SOUL 单独确认、首条消息不被密码 onboarding 吞掉、长任务异步可恢复回送均已落地；scale worker 复用同一执行链。
- 普通会话累计上下文已接入 owner/thread scope：复用现有 compact 阈值、token 估算和模型后端生成 thread summary，raw transcript 保留；旧聊天进入 owner-local `session_search` 索引。`/verbose off|on|full` 及 typed 工具进度复用持久化回送链，不重提任务。
- 多 IM 投递底座已在当前工作树收敛：普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`；收件上下文与回复信封分离，adapter/capabilities/target validator 统一注册，第二个 fake IM 契约无需修改投递主流程即可接入。生产第二平台与正式部署复验仍按产品事实页标注。
- 普通会话即时控制已在当前工作树收敛：CLI/Feishu 共用 `/status`、`/btw` 和 `/stop`。`/btw` 作为当前
  active turn 的真实 UserTurn 在下一安全点进入模型并幂等写回同一 thread；`/stop` 中断当前 turn 与子代理
  但保留 transcript、workspace 和 memory。旧永久注入与 `/btw-clear` 已移除；真实 1.10 复验仍按产品事实页标注。
- 前台、后台和 compact 后续轮共用同一 owner/thread 的 summary + raw tail；task link、workspace、progress、
  wake 和子代理树只提供结构化运行事实，不再形成 task-scoped transcript、平行聊天历史或第二种 compact。
- 完成回复投影已保留结构化用户摘要：模型自然结束时写出的最终说明，以及显式验收提交里的测试结果、
  主要功能和限制，都会经过统一清洗后进入回复信封与会话历史；验收权威仍是结构化产物/工具事实，
  内部协议和宿主绝对路径不会外泄。
- root 部署的 owner 路径边界已收紧：宿主 home 危险根豁免只给无 owner scope 的本地管理员，远程 Feishu owner 保留 `/root` 拒绝边界，同时继续以精确 owner home 白名单访问自己的数据；Linux root 场景已加入确定性回归。
- 普通聊天和工作共用同一持续 thread；结构化任务工具只在真实工作开始时建立 workspace/进度记录，
  text-tool envelope 统一净化，自动监督 material-delta 去空转，子代理命令日志不进入普通 transcript。
- 运行故障事实已在当前工作树加固：shell 管道失败不能假绿，UNKNOWN_ERROR 保留原始报码和脱敏输入形状，
  Gateway 意外 watch 返回非零、清理 drain 不完整另记失败；persona replace/remove 使用精确 entry ID，
  临时密码/OTP 不进入 durable memory。
- Tool Gateway 已补齐已知门禁报码到统一错误 taxonomy：命令解析、owner/path 隔离、工具协议、限流熔断、
  审批绑定、幂等和管线配置不再因漏注册降级为 `UNKNOWN_ERROR`；防漏测试按真实强制管线及其直接子门扫描，
  动态 artifact-ref finding 先归一为稳定协议码。专项回归已通过，1.10 真机复验待当前长任务自然收口。
- 工具调用运行身份不再把后台主代理的临时轮次误认成子代理：只有线程级 subagent context 或显式
  `task_local` scope 才查询子代理 canonical ledger；主代理保留持久 `root_task_id`，真实子代理 load error
  仍原样进入结构化审计。`606fe20a` 已部署 1.10，两个全新 Feishu-scoped owner 的后台工具事件零假错。
- `raise_collaboration` 已接入工具 envelope 的持久 `root_task_id`，后台主代理无需让模型补传任务身份；
  协作域报码保留为 `reported_error_code`，运行时另用已注册的 parameter/arguments/execution 控制码，
  工具输出 artifact 与 index 也同时保存两层错误事实，不再把真实原因压成单独的 `UNKNOWN_ERROR`。
- 后台最终回复不再使用唤醒轮开始时间伪装成消息完成时间；会话消息仍按 append-only 顺序读取，thread
  活跃时间只允许向前，避免长任务完成后反而把会话排序回旧时间。
- 派工自然回执的模型可见 facts 已从 runner/recorded 等内部生命周期名投影为 planned/ready/started/
  failed-to-start 用户语义；运行权威账本不变，模型仍按真实数字自由组织回复，不靠关键词拦截或固定句子。
- 后台完成事件已在当前工作树分层：成功兄弟完成信号短窗合并，部分成功只内部整合，最终/失败/阻塞/
  需决策才由父代理写普通会话并经正式投递链外呼；后台续跑严格复用原 task link 的
  goal/workspace/index 标题，不再被定时提示覆盖。旧的子代理固定完成通知旁路已经删除。
- 真机复验发现并修复 observation/wake 双写竞态：完成事件改为 wake-first 单入口，内部 wait/自动续推的
  运行中占位正文不再污染普通聊天；wake 失败仍保留 observation 兜底。
- 真机复验发现父任务 goal 仍可被 wait reason 覆盖；现已把 task identity 不变量下沉到 store 的原子
  bind 入口，保留首次 goal/workspace/created_at 并拒绝跨 thread 重绑。
- 真机双用户长任务复验暴露的批量派工与完成投递缺口已在当前工作树收口：同一模型轮里的多次
  `create_subagents` 按序全部执行并合并为一份事实回执，依赖前序结果的编排调用仍延后且返回专用错误码；
  模型臆造的 owner-home 交付路径在未获用户显式授权时归回当前任务 `output/`；后台自动续跑只有形成
  结构化最终交付时才进入普通会话，内部监督、等待和占位正文保持内部可见。
- 真机复验继续暴露“结构化完成与 findings delta 同轮出现”时完成信号被 delta 遮住；当前工作树改为
  结构化完成优先，并且 IM 回复信封只携带已投影的人话，不再把内部完成块、结论账标记或宿主路径交给
  adapter。这样既不恢复内部碎碎念，也不会在产物已验收后静默吞掉父代理最终回复。

## 2026-08-29 `.10` R86--R91 底座与真实 TUI 收口

- Owner Memory Curator 改为惰性 soft residency：历史 owner 不再由 maintenance 无工作物化，活跃 scheduler/
  wake/turn 仍为 hard residency。唯一 Gateway 初始 RSS 从约 509--521MiB 降到约 185--224MiB；R91 下
  22 路 TUI 与压力任务同时运行时约 275MiB，仍只有一个 8420 listener。
- dead execution lock 恢复改为 PID + process start token 客观判死后接管；未知旧读调用记 UNKNOWN，未知
  mutation 记 DIRTY，live/unprovable holder 继续 fail closed。
- `send_guidance` 不再向终态 child 写永不消费的假队列。R90 fresh TUI 已证明 DONE child 返回专用错误码、
  无 pending 文件且不复活。
- capability 自动授权现在以通用 `context_refresh` 结束旧不可变工具快照，并在下一 durable slice 重建工具
  router。R91 fresh U94 已完成“只读 child → 申请单文件写权限 → 自行写 1,562-byte 报告”，归档中
  `TOOL_UNAVAILABLE=0`。
- 本轮 focused 分别通过 149、201、68、86、111 和 69 项；R91 相关 Ruff、PyCompile 与 diff check 通过。
  未跑全仓 pytest，原因是当前切片远低于约 10,000 行全仓门，且用户要求减少无效全仓测试。

## 2026-08-29 R92 直属 child 结果跨后台续片保持

- 把前台 follow-up 和后台 scheduled/recovery 的 child 完成输入统一到中立
  `contracts/subagent_completion.py` 投影；每个后续工作片都从 durable observation ledger 按 exact root 与
  direct parent 重建，不再只依赖一次性 completion wake。
- 后台上下文预算现在保护已选 child identity 与完整 `final_report_ref`，只允许缩短自然语言正文和减少次要
  ref；私有 runner payload 与孙代理结果不会越级进入 root。
- focused 六文件组合通过并保留 2 个既有 xfail；R92 已部署到 `.10` 的唯一 Gateway。fresh U98 4/4 child
  完成并写出 7,448-byte 对照报告；旧失败 U85 在 `compact 1` 后也恢复整合并自然 final。
- 七路新 TUI 启动后的 30 个 TUI 合计 PSS 约 2.10GiB，Gateway PSS 约 346MiB，整机 available 约 9.7GiB；
  没有出现多 Gateway 或随窗口数成倍膨胀。`.7` 当前整机不可达，保留为环境恢复后的复测项。

## 2026-08-22 裸启动与恢复控制面收口（`.7` 已部署复验）

- 解决了普通用户运行 `my-agent` 被全机历史任务扫描和虚假 `[Y/n]` 阻塞的问题：裸启动与 `chat` 共用
  轻量 fresh-session 路径，只有 `resume <session_id>` 能恢复明确会话。
- `status` 已成为无开关、无副作用的显式投影；旧的两个启动恢复配置删除。普通 run 的 stale attempt
  由 Gateway 启动调和，subagent 由单 Gateway 周期 supervision 调和。
- 派工巡查锁已改为 POSIX `flock` / Windows `msvcrt` 内核锁。空文件、坏 JSON、旧 PID 和 PID 复用不再
  决定锁归属，进程死亡由内核自动释放；focused parser/startup/status/lock/recovery tests 通过。
- `311b8db` 与配置统一补丁 `06b84e1` 已进入远端 `main` 和 `.7`。真实裸 TUI 1 秒内显示输入框与
  `MiniMax-M2.7`，普通中文请求经同一 Gateway 返回；单监听 PID 为 `1918200`，锁元数据持有者同 PID。
- 首轮 Gateway supervision 记录 `orphans_revived=4`、`parent_closed_cancelled=21`、
  `running_reclaimed=5`；3 条恢复 RUNNING 随后自然 DONE。连续两次显式 status 均只读返回相同近期计数。

## 2026-08-22 内部状态面拒绝的副作用分类

- 解决问题：shell 在执行前拒绝模型读取内部 child 状态文件时，旧结果被误当成“命令可能已经产生未知
  副作用”，导致整个 child 回合被硬停并留在 PENDING。
- 落地内容：保留 `WRONG_STATUS_SURFACE` 安全拒绝，增加 canonical `effect_outcome=not_started`；模型
  可读取结构化原因后改用直属生命周期事件或结果引用，真正的 unknown 仍 fail-closed。
- 验证方式：ShellTool 直接回归与 authorized dispatch/operation status 集成回归均通过；远端真实 TUI
  复验留到候选部署后执行。
