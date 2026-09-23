# Ordinary Conversation Context Design

Status: implemented in the current worktree. Release and 1.10 evidence remain governed by
`docs/PRODUCT_FACTS.md`.

## Core invariant

每个普通用户或群组只有一条持续会话主链：

`owner + channel + channel_conversation_id + channel_user_id -> thread`

一个 thread 只有一份模型可见历史。聊天、问答、修改文件、派子代理、短任务、长任务、
定时唤醒和任务恢复都继续使用这份历史。任务账本、工作区、进度、wake、子代理树和产物是结构化运行事实，
可以辅助当前 turn，但不能过滤、替换、复制 transcript，也不能建立“聊天上下文”和“任务上下文”两条主链。

直属完成事件的 `service_window_incomplete` 与 `service_window_remaining_seconds` 是发布时冻结的声明窗口事实，
前台安全点、后台完成清单和预算裁剪共用中性合同筛选；两字段类型正确时成对保留，缺失或损坏时不推测补齐。
这些值不是当前倒计时，也不是采样进程运行时间；它们不改变子代理终态、Goal 或重派策略。
真实 TUI 曾在当前 wake 中携带此事实，但后续完成清单丢字段；修复已同版部署，实际后台消费在正文裁剪后仍保留原值及终态，无新增配置或执行行为。
完成正文仍可显式截短，完整报告引用必须保留；实际旧样本已核对报告存在并包含全文，不将有恢复引用的预览裁剪称为数据丢失。

IM 只完成身份映射、消息/附件接收和回复投递。Feishu、CLI、HTTP 以及未来 IM 都不得定义 compact、memory、
task 或 turn 语义；它们进入同一个 Gateway/runtime。

## Transcript and compact

显式scope的Compact只读来源现按完整LF尾界扫描两次：第一遍验证身份及创建锚点，第二遍按原范围与原检查点精确覆盖筛正文，原字节hash必须一致。后台有任务先读同次结构事实，再按范围加载正文，0展示限制仍不截断。该实现不改变摘要writer/CAS；ID位置、选中正文及覆盖链仍需内存，完整有界化尚未完成。

后续待实施：Compact已明确保留的历史须完整进入容量候选及实际发送，不能在宿主投影中再次用普通展示字符窗口悄悄删除。普通展示窗口与原范围规则保留；未知容量或完整请求过大必须由原容量合同处理。详细证据和待办见[容量审计](../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

本地文件队列的 `cli_chat/cli_gateway` 来源由提交入口写入，属于私有展示，不能把用于多用户身份的
`metadata.channel`（owner provider）当成外部投递通道。前台流式、final、延迟 repair 与保存消息的
channel 使用同一 resolver；历史回放读取落账 channel。HTTP/IM/未知来源仍沿原脱敏策略，
不因 rich transcript、角色或正文改变可见性；owner 路由与会话绑定身份保持不变。
独立的内部编号仍可遮蔽，但私有路径内的 owner/request 片段不能变成“当前空间/当前请求”；
路径策略和标识投影消费同一个通道事实，原生模型历史不改写。

中断或模型/工具循环抛错不是丢弃执行历史的理由。正常返回的停止结果以及异常栈退出前的 native IR
都进入同一个 canonical 消息信封；正文为空并单独标记 `aborted/error`，不对用户发送假回复。
Gateway 沿原 final/repair 写入，子代理绑定精确 child thread/turn，一次性 CLI 沿其原会话写入。
缺配对结果的工具调用只补 `effect_outcome=unknown` 的错误占位，不能断言已成功、未执行或已经压缩；
后续模型需核实有副作用的操作，不由宿主盲目重放。仅更正有序退出路径，不声称覆盖进程强杀、断电，
也不能恢复旧版本已经丢失的原生历史；屏幕 display checkpoint 不回灌作为机器事实。

后台工作片遵循相同规则：每次实际执行分配独立的宿主会话回合编号，任务、wake 和工具操作身份不变。
原生历史先以空正文 `assistant_part_id=native` 写入原 transcript，再按原通道投递规则保存公开 final；
两者共用该回合编号，模型投影只回放一次原生信封。静默等待、投递被抑制或异常退出不能删除工具事实。
投递重试继续使用已冻结的原 metadata，不因重试分配的新请求对象改绑原回合；空历史不新建占位行。
窄范围审计事件沿独立审计合同保留隔离，不将内部事件正文或原生历史注入普通会话。

薄 Gateway TUI 显式恢复必须在同一可见 preflight 内先等待 readiness，再后台读取授权的 owner/session
history，最后启动 worker；恢复失败不接受任务，客户端退出后迟到结果不重新启动。原会话的数据错误不得
被包装成空历史成功，连接期间也不创建新 session、Gateway 或额外重试器。

TUI 显式 resume 不再使用完整问答预览重建正文。Gateway 在同一 owner/thread 的 canonical rows 上另生成
`conversation_history_display.v1`：真实用户输入、公开 assistant 正文/思考、按 tool-use id 配对的结果。native
user 注入、签名和参数留在服务端；显示事件不回灌模型。canonical final 优先于 provider 原始末段正文。
原问答预览仍保留原调用方，缓存、Compact 和原生请求拼装不因 UI 恢复改变。当前恢复范围受已有历史读取窗口
约束；Compact 前完整过程与长会话分页仍待 P1 验收，不能凭已有 native 尾部声明完整归档已恢复。

owner-scoped `ConversationStore` 中的 raw JSONL transcript 是唯一对话事实源。thread JSON 只保存同一历史的
compact summary、精确消息/字节 cursor、generation、live checkpoint pointer、累计来源消息/工具往返、
连续失败状态和 `/verbose` 设置。compact 不删除或改写 raw 消息：旧段被摘要后，新的 user/assistant 消息
继续追加在同一文件尾部。checkpoint JSONL 保存每代候选的完整 summary、hash、`source_kind`、源消息/字节
范围或原生工具调用 ID、近期尾部、工具事实、前后 token 和前代指针；只有 thread 指向它才表示该代已提交，
孤立 checkpoint 不获得 live authority。

同一模型 request 可以产生多段用户可见 assistant 消息。每次工具边界前已经完整生成的 commentary 以
`assistant_part_id=commentary:N` 顺序追加，最终答复以 `assistant_part_id=final` 追加；repair/dedupe 用
`request_id + assistant_part_id` 幂等，旧记录缺 part id 时只兼容为 final。会话配对预览把 final 与 user
配成一轮，但 transcript/TUI 仍完整展示 commentary。普通 user/assistant 正文不按 12,000 字符等任意上限
截断；上下文投影超出软预算时只从最老的完整消息边界移除，至少保留最新完整项。原始大工具输出继续由
tool-result reducer、archive 和 refs 管理，不用裁剪对话正文代替。

上下文生命周期是：

1. 解析 owner 和稳定 thread。
2. 读取当前 summary 与 cursor 后的完整 raw tail。
3. 使用模型真实 context window；取不到时才使用配置的保守默认值。
4. 正常达到阈值时，先尝试把旧段压成候选并保留最多 4 个近期完整回合；近期尾部不超过精确触发点
   的 10%，且硬上限为 20,000 token。若完整下一轮投影仍超阈值，再尝试压缩全部旧段。若供应商
   已经返回上下文压力，则一次压缩当前请求之前的完整旧段，不把同一近期尾部反复压成多代 checkpoint。
5. 候选只有在完整下一轮投影低于 `触发线 - recent-tail 预算` 的恢复目标时才可提交，为下一段完整近期对话
   留出余量，避免刚压完就再次触发。提交先写完整 owner-scoped checkpoint，
   再用一次 generation CAS 原子推进 summary/cursor/checkpoint pointer。候选无法达到恢复目标但原上下文已
   完整恢复时，记录为展示态 `superseded/candidate_discarded`，不算失败、不推进 generation，也不占住后续
   transcript Compact；摘要调用、checkpoint 或 CAS 的真实异常才进入失败熔断并保留原 live 状态。
6. 同一 thread 连续三次 compact 失败后冷却 300 秒，避免每条消息重复消耗模型；冷却后半开尝试，
   成功提交即清零。该状态不删除 transcript，也不改变 task、memory 或 persona。
7. 当前 active turn 的原生 ToolCall/ToolResult 达到同一阈值，或 provider 实报 overflow 时，只能成对回收。
   对允许持久化的 main/child/grandchild，摘要器把上一代 thread summary 与当前工具历史合成一份完整替代
   summary；随后先写 `source_kind=live_tool_ir` checkpoint，再以同一 generation CAS 提交。该来源不推进
   transcript cursor，只累计 `compact_source_tool_pairs`，并在同一 turn 内用一个 `CompactionSummary` 继续。
   日常无摘要窗口化至少保留最新工具对；完整替代摘要已安装后，若最新单对本身超过恢复余量，则可把该对
   也成对回收。其事实已进入摘要，精确原文仍在 owner archive/ref，不能因机械保留一条巨型回执制造假失败。
   同一完整摘要也覆盖连续退休工具前缀中的旧 `RuntimeFactsTurn`；该旧快照随前缀回收，最新快照、所有真实
   `UserTurn` 和保留工具尾部不动。无摘要或非连续删除不能取得回收快照权限，floor 探针与真实提交共用规则。
   摘要模型请求按 会话运行时 compaction turn 排序：历史全部在前，合成的 Compact 指令作为最后一条 synthetic
   user 消息；不能把指令放在首条 prompt 后再追加历史，否则兼容模型可能忽略旧指令并普通续写末尾动作。
8. live-tool 摘要的 transport/调用异常、checkpoint 或 CAS 失败时，原生 IR 与 tool-context 恢复到压缩前，
   thread 只增加同一个失败熔断事实。若供应商请求已经正常完成、用量已入账但摘要正文为空，则不重试第二次，
   而是从 typed UserTurn/ToolCall/ToolResult/refs 构造有界机械替代摘要后继续提交；该投影不判断任务完成，精确
   副作用仍以 archive、operation ledger、artifact 和真实文件为准。transcript Compact 对正常完成的空正文
   使用相同原则，从旧摘要、raw row 投影和结构化 operation evidence 生成有界续接包。presentation/no-save
   辅助回合可做临时窗口整理，但不得推进 generation 或冒充 `compact N`。
   用户 stop/cancellation 不是摘要失败：三条 Compact 路径在慢摘要前后、候选改写前后、checkpoint 前后和
   generation CAS 前读取同一个中断回调；CAS 前恢复未提交的 IR/tool-context 并发布中性
   `superseded/candidate_discarded`，不增加失败或推进代次。checkpoint 已落盘但 CAS 未赢时，它只是不可达候选；
   CAS 已成功后不得回滚已提交代次，避免持久历史与客户端分叉。
9. `/status`、TUI 和 Web 只显示 thread 的一个 compact generation；消息段压缩与可持久 live-tool 压缩都
   推进它。每次真实尝试另有唯一 operation id，进度还必须携带成对的 `source_kind/commit_authority`：
   transcript 和可持久 live-tool 的提交权都属于 conversation thread，presentation/no-save 的临时整理只属于
   turn-local。Gateway 和界面调用同一个公开 normalizer，不能根据 operation id 前缀或中文标签猜来源；
   turn-local 动画不得增加 `compact N`。live 失败后的 transcript 后备不会复用失败 block；main/child 自动
   Compact 都发送 typed 进度。手动 `/compact` 在持久 outbox 入队后保留转圈块，真实回执才结束。

任务工作区不保存第二套 compact。`work/state.json`、任务进度和子代理 canonical state 只是结构化运行
事实；主代理始终只压缩自己的 thread history。每个子代理本身是独立 agent，因此拥有独立
`agent_thread_id` 和相同的 summary/generation/checkpoint 状态机；它不生成根任务级 compact 包，也不注入
另一份主 thread。孙代理递归遵守同一规则。

## Cache economics

系统通道的验证规则只限定证据表述，不要求每个动作前重新运行已有检查。相同版本、输入和观察点
复用有效结果，定位后推进已授权的修复，再针对实际改动验证；权限仍由原工具门判断，不由提示文字放宽。

动态状态由 PromptBuilder 的字段携带来源键，不能通过 Markdown 标题或公共前缀反推来源。记忆召回、工具
推荐、工作区、运行注入、执行事实各自形成一个分段；普通工具轮只追加相对同来源最新 IR 状态的变化，
不变项留在原位置，A→B→A 的最后 A 也必须追加。IR 是唯一比较基线，不另存永远 seen 的集合。
完整诊断/摘要 prompt 从同一分段无损渲染。预检查与真实发送共用 `project_native_prompt_history`，
前者只操作浅副本，后者提交原 IR；避免预检查多算整包或将动态字段混入稳定校准指纹。
执行事实来源只追加最近完成的工具批次，以归档 turn_id/tool_round 识别，保留有界调用状态、批准及引用；
不再每轮复制近期成功/失败明细和全轮统计。缺批次字段时仅陈述最后一条已返回记录，不猜任务意图。
完整工具结果仍成对留在历史，最终操作核验仍按全轮账本汇总；没有工具结果时不注入空账本说明。
这只减少后续新增字节，不追溯删除旧会话中的冗余快照；旧前缀仍只能在成功 Compact 后按原规则回收。
完整 Compact 摘要覆盖连续退休工具前缀后，才可回收其中旧分段，并保留每个来源的最新项；取消或 CAS
失败恢复原列表，也同时恢复比较基线。真正用户输入、Memory 存储、历史账本和权限不参与这一回收。

费用只使用 provider ledger 的 typed `input/cache_creation/cache_read`，不从屏幕 Context 或字符数倒推。
缓存创建没有独立价格时按普通输入单价 5 保守计。当前样本为：

- 普通输入与缓存创建合计 `B = 173,507 + 184,132 = 357,639`。
- 缓存命中 `H = 235,041`。
- 缓存价为 `p` 时，总成本 `C = 5B + pH`。
- `p = 0.1`：`C = 1,811,699.1`；相对全部按普通输入的 `2,963,400` 节省
  `1,151,700.9`，约 `38.86%`。
- `p = 1`：`C = 2,023,236`；节省 `940,164`，约 `31.73%`。

这些数字是按用户给定单价得到的比例计价单位，不擅自解释成人民币或美元。若某轮任意改写了原本可缓存的
100,000 token 稳定前缀，其边际额外成本是 `(5-p)×100,000`：两种价格分别为 `490,000` 和
`400,000`。因此普通轮必须保持已提交历史 append-only；仅真正 Compact 才允许一次性替换旧前缀。是否实际
命中仍只看 provider usage，不能把“看起来前缀相同”写成缓存成功。

原生工具链的 provider 顺序必须保持：

```text
稳定 system + 稳定 tools
→ 已提交 Compact summary + 已结束的完整 user/assistant 尾部
→ 当前 user
→ append-only 当前轮 assistant/tool/user steer IR
→ 当前 recall、推荐工具、工作区、wake/runtime injection 与执行事实
```

Anthropic 请求只推进一个 message-level `cache_control`，该断点始终位于最新可缓存历史块；
当前事实在它之后。普通回合结束后，上一轮 current user 与 assistant 会从同一 ConversationStore
逐字重建到下一轮 messages；不得把 transcript 再渲染进每轮变化的 runtime injection，也不得同时在 prompt
和 messages 发送当前 user。OpenAI-compatible 投影不伪造 `cache_control`，但保持相同时间顺序，让本地
模型服务器复用尽可能长的 KV 前缀。任务工作区会在首工具前后变化，因此必须放在动态尾部；这不是对上下文
的裁剪。只有真正 Compact 可以把旧 messages 前缀一次替换为新的 committed summary，之后重新建立稳定前缀。

## Turn scheduling boundary

同一 thread 仍只有一条历史，实际新建的 Gateway 请求按顺序执行；同一工具循环内到达的新用户输入按
FIFO 进入当前 turn：

- 没有 active turn：它开始下一轮，并读取同一 thread history。
- 已有 live Gateway request：同 owner/thread 的普通消息直接成为当前 turn 的真实 UserTurn，不创建第二个
  request，也不从文字猜测它是聊天还是任务；模型在下一个安全点读取它，生成中的旧动作会失效。
- 只有 durable task 记录、没有 live request：普通消息仍开始正常下一轮并拥有自己的回复信封，不会被
  静默吞进后台任务。
- 用户显式纠偏：`/btw 内容` 使用同一 FIFO input queue，并可在没有 live request 时继续绑定精确的
  durable task；它不是普通消息进入当前 turn 的前置触发词。
- 用户需要终止：显式 `/stop` 中断当前 turn 和其子代理，但不销毁 thread/history。

普通消息提交与 active turn 结束发生竞态时，expected-turn 检查失败就回落为正常新请求；它不能污染已经
结束的旧 turn，也不能自动进入随后才创建的新任务。真正进入队列的请求仍受同会话单飞约束，保证
transcript 落账顺序。

非阻塞 `wait` 可以结束当前 turn 并登记一次耐久 wake。scheduler 只有在没有 linked live turn 时才能启动后续
 turn；后续仍读取完整 thread summary + raw tail，再叠加精确 task 的运行状态。已终态任务的排队 wake 会被
 直接作废，不能复活旧任务。

child lifecycle wake 是原 root active turn 的跨进程工作片：exact task objective 继续占据原始 User Task，
wake 只进入 runtime state。root 自己的工具索引按 exact run/task 恢复；嵌套参数以有界递归脱敏 JSON 保留。
由于索引无法无损重造原 provider assistant/tool 配对，native 只安装一条 CompactionSummary handoff，不能
伪造 tool_use 或重放副作用。当前 task_progress 的 exact item ids 与 `items[].covers` 绑定路径作为结构化
plan continuation 投影；标题、goal 和模型总结不参与绑定或重复清单判定。

`wait` 让出会话槽前的用户回执仍由模型撰写，不使用固定“处理中”模板。这个无工具表达轮只接收有界的
结构化事实：当前用户请求、已执行动作数、工具轮次、当前 `/btw` 补充，以及精确 task lineage 下的子代理
总数/活跃数/终态数/异常数。`task_continues_without_more_user_input=true` 时，表达规则明确禁止向用户索要
进一步指示或确认；它只能说明会按当前要求自行继续。超长请求与补充各自保留首尾并标记截断，避免为了
一两句回执重复消耗整份长任务上下文。该表达轮不改变 task/thread 状态，模型失败或暴露内部协议时按统一
用户出口抑制，不用固定句子冒充成功。

子代理启动和重启恢复还必须核对结构化 conversation lifecycle：父 root task link 与当前 child run link
都为 `active` 时，才允许启动同一个 run。父或 child 已 completed/cancelled/interrupted 等关闭状态时，旧
child 通过统一取消链收敛；thread/link 缺失、损坏、身份不一致或未知状态时 fail-closed，不启动、不根据
用户文字猜测。没有 conversation attrs 的本地/admin run 继续使用原有恢复合同。盯守任务可以在父 root
仍 active 时从已结束 child 创建新 takeover，但不得在父 root 结束后补岗。

这仍与 会话运行时 有一个明确差距：my-agent 的非阻塞 `wait` 会结束当前 turn，再由持久 scheduler 启动后续
turn；会话运行时 的 `wait_agent` 留在同一个 active turn 内等待子代理。当前改动只收敛 history/compact，不把这项
既有调度语义伪装成已完全复刻，也不为 IM 单独改变它。

## `/btw` as real user input

`/btw` 只认结构化 owner/thread/current request-or-task，不从内容判断目标。普通消息在 live turn 期间也
复用同一 active-turn input 主链，但只有显式 `/btw` 能在没有 live request 时直接纠偏 durable task。
两者都在下一个模型安全点以
provider-neutral `UserTurn`（或 text history 的等价位置）进入当前 turn。模型成功接收后，该内容才以
guidance id 幂等追加到同一 raw transcript；provider 失败、进程崩溃或目标竞态切换时保持 pending 或退休，
不会污染下一任务。

相同文字的两次 `/btw` 是两个不同输入，不能按文本去重。已送达输入跨 active-turn compact continuation
使用 typed carrier 保留，不从摘要或自然语言反解析，也不再建立 task guidance history。

## Task records and workspaces

普通聊天不会预建任务目录。只有注册为 `promotes_task` 的工作工具、`task_progress`、`create_subagents`、
`wait` 等结构化动作真正开始工作时，当前 request 才晋升为 task，并在已解析 owner 下懒建工作区。

同一 thread 可以积累多个已完成或中断的 task records，但它们只是工作索引：

- 每条用户消息直接成为新的 active turn；无需选择、关闭或新建普通任务。
- 普通 `task_progress` 只允许 `read/update`，是模型可选的恢复笔记，不是会话或任务控制器。
- 补充同会话旧计划时，主代理在 update 显式携带 read 返回的 `run_id`，底座核对同 owner/thread
  的已有 task link 和账本。省略仍写当前计划；不因 read、条目重名或自然语言自动改绑。
  子代理只能写自身，独立后台与其他会话的计划不可据此改写；旧任务不被重新启动。
- exact request-level binding、未结束 Goal 或 active task 自动延续 canonical task root；普通
  completed/interrupted task 只保留历史/导航投影，不隐式吸附下一个新回合。
- 不解析“继续、重来、第二步”等自然语言来猜 task id 或控制生命周期。
- 若文件变更工具已携带显式绝对目标或 canonical owner-relative `tasks/...` 目标，
  且所有变更只落在同一 thread 的一个 canonical task root，统一工具 runtime 可精确回绑。
  同根的多个 terminal execution link 只是同项目的历史代次，选最新一代建 successor；读操作、
  普通相对路径、跨多根或同根多个 live executor 都不得猜测或自动绑定。
- workspace binding 只改变本轮结构化 cwd/lineage；模型历史仍是同一 thread。
- 完成、停止、取消和 supersede 只改变 task record，不切换聊天 lane。

任务晋升前的普通对话默认使用结构化 owner home；进程启动 cwd 或未获 Full Access 的外部 client cwd
不会成为权限。首个 `promotes_task` 动作创建或复用
`<owner_home>/tasks/<task_path>/` 后，该目录成为 main、child、grandchild 的唯一默认 execution cwd 与产品
写根；同一 active execution、exact request 恢复或 Goal 续转由结构化 task id 复用它，thread
`workspace_task_id` 只供状态/导航。`run_command` 与新建 PTY 没有显式
`working_dir` 时从该 task root 启动；调用者显式指定目录时仍必须落在结构化允许根并经过 sandbox 验证。
不得从 Gateway daemon 的 `/root`、模型文字、`child_outputs` 猜工作区，也不得让 child 另选家目录。这个
默认值不检查命令文字或用户措辞，适配 会话运行时 turn cwd 的单一事实源，同时落实本项目 owner 多租户边界。

子代理继承父任务的结构化引用，但只能关闭自己的 exact task link，不能关闭父根任务。

## `/stop`, `/goal`, and `/audit`

`/stop` 等价于停止当前 会话运行时 turn：终止当前模型/工具执行和子代理树，把 task 标记为 interrupted，并抑制
迟到回复。thread transcript、compact、workspace、artifacts、USER/SOUL 和 memory 都保留。后续模型仍能从
历史和 owner-local 任务索引找到原项目；当写工具精确指向该 canonical 目录时才结构化回绑，
不因“继续”两个字自动复活旧任务。

`/goal` 是同一 thread 上的持久目标 overlay，不创建第二个会话或模型历史。它绑定一个 durable root task，
用 typed command 和 `get_goal/update_goal` 管理生命周期；普通任务无需 `/goal`。

`/audit` 只由显式 prefix 激活保证档并写入结构化属性。普通句子、摘要或历史中出现 `/audit` 字样不会获得
运行 authority。

## Memory

thread transcript 负责“我们刚才说了什么”。长期 memory 负责跨很久的稳定偏好、事实或可检索经验。compact
不会自动修改 USER/SOUL，也不会把每个 task 复制进长期 memory。旧 transcript 可以进入 owner-local
LocalStore 派生索引，由 `session_search` 按需查回；索引不是第二事实源，也不能跨 owner。

## Multi-user boundary

所有 WorkspaceOnly owner（包括本地管理员）只能访问自己的完整 owner home。用户、群组的 transcript、
USER.md、SOUL.md、memory、tasks 和 artifacts 相互隔离；这道文件墙不关闭外网。只有结构化
`local/main + full-access` 能访问外部目录，普通/远程 owner 无法靠配置副本或自然语言提权；Full 父级的
child 仍恢复 owner/task 墙。唯一公共文件区是管理员发布的 `~/.my-agent/shared/`，用于 shared
skills/tools/workflows；随 wheel 发布的 builtin tools/skills 本身也是公共产品能力。Full 模式下，明确外部
路径或系统排障、其他 owner 必须点名、默认只读少改都属于软行为提示，不替代结构化权限。

## Subagents and user-visible delivery

子代理拥有自己的局部运行上下文和 workspace，但不直接写用户 transcript。子代理的私有 commentary、命令、
工具协议和中间输出只进入 task-local ledger；主代理读取结构化进度/结果后，在同一主 thread 中整合。

所有普通最终回复、后台主动结果和显式附件发送经过统一 `DeliveryService`。IM adapter 不重新解释任务状态，
也不把内部 XML、工具调用或子代理碎碎念投递给用户。

## 实现边界

实现使用 Python 接口、owner 范围的文件存储和统一运行时类型。会话语义不依赖具体 IM 通道，
也不通过自然语言分类器改变任务归属。设计与实际验收边界以 STATUS 和对应模块代码为准。


### Canonical消息扫描边界

原MessageStore的前向页支持显式through完整LF尾界与max_bytes正整数字节预算；不传参数保持原显示分页。complete_offset_report和倒读历史共用store_io.complete_jsonl_end，64KiB分块找最后完整LF，不读取后到追加或承认半行。单行超过页预算返回MessagePageBudgetExceeded，资源限额与坏数据区分；任何错误不推进原游标。有预算而未传through时也先取得完整尾界，尚未完成的巨大尾行等待下次读取，不误报单行超限。

append_once保持原幂等锁、首个同key内容校验和唯一append/update链，但逐行扫描固定物理EOF，不再载入全量消息；命中后仍校验后续记录，display不参与匹配。写前拒绝缺LF尾行，包括可解析JSON或纯空白，避免后续追加把两条JSON拼坏；不自动修复原文件。读取内存随最大行而非历史总量增长，扫描时间仍线性。

这些字节边界没有授予Compact摘要覆盖或跨任务读权限，不是抗文件替换凭据；scope筛选、完整来源验证及原checkpoint/CAS仍是唯一提交合同。详见[容量审计](../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
