# Ordinary Conversation Context Design

Status: implemented in the current worktree. Release and 1.10 evidence remain governed by
`docs/PRODUCT_FACTS.md`.

## Core invariant

每个普通用户或群组只有一条持续会话主链：

`owner + channel + channel_conversation_id + channel_user_id -> thread`

一个 thread 只有一份模型可见历史。聊天、问答、修改文件、派子代理、短任务、长任务、
定时唤醒和任务恢复都继续使用这份历史。任务账本、工作区、进度、wake、子代理树和产物是结构化运行事实，
可以辅助当前 turn，但不能过滤、替换、复制 transcript，也不能建立“聊天上下文”和“任务上下文”两条主链。

IM 只完成身份映射、消息/附件接收和回复投递。Feishu、CLI、HTTP 以及未来 IM 都不得定义 compact、memory、
task 或 turn 语义；它们进入同一个 Gateway/runtime。

## Transcript and compact

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
   再用一次 generation CAS 原子推进 summary/cursor/checkpoint pointer；任何失败都保留原 live 状态。
6. 同一 thread 连续三次 compact 失败后冷却 300 秒，避免每条消息重复消耗模型；冷却后半开尝试，
   成功提交即清零。该状态不删除 transcript，也不改变 task、memory 或 persona。
7. 当前 active turn 的原生 ToolCall/ToolResult 达到同一阈值，或 provider 实报 overflow 时，只能成对回收。
   对允许持久化的 main/child/grandchild，摘要器把上一代 thread summary 与当前工具历史合成一份完整替代
   summary；随后先写 `source_kind=live_tool_ir` checkpoint，再以同一 generation CAS 提交。该来源不推进
   transcript cursor，只累计 `compact_source_tool_pairs`，并在同一 turn 内用一个 `CompactionSummary` 继续。
   摘要模型请求按 会话运行时 compaction turn 排序：历史全部在前，合成的 Compact 指令作为最后一条 synthetic
   user 消息；不能把指令放在首条 prompt 后再追加历史，否则兼容模型可能忽略旧指令并普通续写末尾动作。
8. live-tool 摘要、checkpoint 或 CAS 失败时，原生 IR 与 tool-context 恢复到压缩前，thread 只增加同一个失败
   熔断事实。presentation/no-save 辅助回合可做临时窗口整理，但不得推进 generation 或冒充 `compact N`。
9. `/status`、TUI 和 Web 只显示 thread 的一个 compact generation；消息段压缩与 live-tool 压缩都推进它。
   每次真实尝试另有唯一 operation id，live 失败后的 transcript 后备不会复用失败 block；main/child 自动
   Compact 都发送 typed 进度。手动 `/compact` 在持久 outbox 入队后保留转圈块，真实回执才结束。

任务工作区不保存第二套 compact。`work/state.json`、任务进度和子代理 canonical state 只是结构化运行
事实；主代理始终只压缩自己的 thread history。每个子代理本身是独立 agent，因此拥有独立
`agent_thread_id` 和相同的 summary/generation/checkpoint 状态机；它不生成根任务级 compact 包，也不注入
另一份主 thread。孙代理递归遵守同一规则。

## Cache economics

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
- sticky workspace 自动延续 canonical task root；上一执行已终态时，首个工作工具在同一 task-path lineage
  创建本轮执行身份。
- 不解析“继续、重来、第二步”等自然语言来猜 task id 或控制生命周期。
- 若文件变更工具已经携带显式绝对目标，且所有变更目标只落在同一 thread 的一个旧 task 内，统一工具
  runtime 可以把这一结构化路径事实用于精确 workspace binding；读操作、相对路径、跨多个 task 或有
  live executor 的目录都不得猜测或自动绑定。
- workspace binding 只改变本轮结构化 cwd/lineage；模型历史仍是同一 thread。
- 完成、停止、取消和 supersede 只改变 task record，不切换聊天 lane。

任务晋升前的普通对话默认使用结构化 owner home；进程启动 cwd 或未获 Full Access 的外部 client cwd
不会成为权限。首个 `promotes_task` 动作创建或复用
`<owner_home>/tasks/<task_path>/` 后，该目录成为 main、child、grandchild 的唯一默认 execution cwd 与产品
写根；后续轮由 `ConversationThread.workspace_task_id` 复用它。`run_command` 与新建 PTY 没有显式
`working_dir` 时从该 task root 启动；调用者显式指定目录时仍必须落在结构化允许根并经过 sandbox 验证。
不得从 Gateway daemon 的 `/root`、模型文字、`child_outputs` 猜工作区，也不得让 child 另选家目录。这个
默认值不检查命令文字或用户措辞，适配 会话运行时 turn cwd 的单一事实源，同时落实本项目 owner 多租户边界。

子代理继承父任务的结构化引用，但只能关闭自己的 exact task link，不能关闭父根任务。

## `/stop`, `/goal`, and `/audit`

`/stop` 等价于停止当前 会话运行时 turn：终止当前模型/工具执行和子代理树，把 task 标记为 interrupted，并抑制
迟到回复。thread transcript、compact、workspace、artifacts、USER/SOUL 和 memory 都保留。用户之后说
“继续”时，模型可精确 select 原 task 后接着工作，无需重发整段 prompt。

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

## References checked

- 会话运行时 current checkout `578c1b22`:
  `会话运行时-rs/core/src/context_manager/history.rs`, `session/session.rs`, `session/turn.rs`, `tasks/compact.rs`,
  `compact.rs`, `thread_manager.rs`. Adopted one thread history, steer as current-turn input, interrupt without
  thread loss, tool-output-only truncation, whole-item paired removal, compact replacing the same history,
  bounded recent user context and explicit before/after compact accounting.
  The remaining nonblocking-wait lifecycle difference is recorded above.
- 通道运行时: stable channel/session identity, active-run control, parent-only child aggregation and typed delivery
  boundaries were checked. Its product-specific session defaults were not copied.
- 长期助手 current checkout `4be38125af06`: gateway conversation keys, protected recent tail,
  persisted ineffective/failure guards, cancellation/commit fence, memory provider separation and
  shutdown/recovery boundaries were checked. Its compression algorithm and profile-wide memory layout were not copied.
- 终端交互 current checkout `6b25ab6`: `src/services/compact/microCompact.ts` keeps local messages unchanged on the
  cached path and removes tool results through API-layer cache edits; bounded messages-to-keep, before/after token
  accounting, compact boundary events and repeated-failure circuit were also checked. Its TypeScript layout was not copied.
- DeepSeek Harness current checkout: `docs/subsystems/compaction.md` and
  `packages/compaction/compaction-basic/src/{region,summarizer,index}.ts` were checked for append-only event logging,
  one surface replacement, priced recent-tail retention, tool-pair-balanced boundaries and prefix-cache-aligned summary
  requests. Its Cordis service/event layout was not copied.

The adaptation is limited to Python interfaces, owner-scoped file storage and my-agent runtime types. No
Feishu-specific context branch or natural-language task classifier is part of this design.
