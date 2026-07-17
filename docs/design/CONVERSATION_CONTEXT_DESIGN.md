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
compact summary、精确消息/字节 cursor、generation 和 `/verbose` 设置。compact 不删除或改写 raw 消息：
旧段被摘要后，新的 user/assistant 消息继续追加在同一文件尾部。

上下文生命周期是：

1. 解析 owner 和稳定 thread。
2. 读取当前 summary 与 cursor 后的完整 raw tail。
3. 使用模型真实 context window；取不到时才使用配置的保守默认值。
4. 达到阈值时压缩同一历史的旧段，原子推进 summary/cursor/generation，并保留近期 raw tail。
5. 当前 active turn 内因 context pressure 需要续跑时，通过 typed compact carrier 保留 UserTurn、工具事实和
   当前状态；它仍是同一 turn，不创建 task history。
6. `/status` 只显示 thread 的一个 compact generation。

任务工作区不保存第二套 compact。`work/state.json`、任务进度和子代理 canonical state 只是结构化运行
事实；主代理始终只压缩同一条 thread history。每个子代理本身是独立 agent，因此只压缩自己的 session
history，不生成根任务级 compact 包，也不注入另一份主 thread。

## Turn scheduling boundary

同一 thread 的普通 Gateway 请求按顺序执行；同一工具循环内的输入也按 FIFO 进入当前 turn：

- 没有 active turn：它开始下一轮，并读取同一 thread history。
- 已有普通 Gateway request：后一条普通消息留在该会话的顺序队列。
- 用户需要立即纠偏：显式 `/btw 内容` 进入当前 turn 的 FIFO input queue。
- 用户需要终止：显式 `/stop` 中断当前 turn 和其子代理，但不销毁 thread/history。

非阻塞 `wait` 可以结束当前 turn 并登记一次耐久 wake。scheduler 只有在没有 linked live turn 时才能启动后续
 turn；后续仍读取完整 thread summary + raw tail，再叠加精确 task 的运行状态。已终态任务的排队 wake 会被
 直接作废，不能复活旧任务。

这仍与 会话运行时 有一个明确差距：my-agent 的非阻塞 `wait` 会结束当前 turn，再由持久 scheduler 启动后续
turn；会话运行时 的 `wait_agent` 留在同一个 active turn 内等待子代理。当前改动只收敛 history/compact，不把这项
既有调度语义伪装成已完全复刻，也不为 IM 单独改变它。

## `/btw` as real user input

`/btw` 只认结构化 owner/thread/current request-or-task，不从内容判断目标。它在下一个模型安全点以
provider-neutral `UserTurn`（或 text history 的等价位置）进入当前 turn。模型成功接收后，该内容才以
guidance id 幂等追加到同一 raw transcript；provider 失败、进程崩溃或目标竞态切换时保持 pending 或退休，
不会污染下一任务。

相同文字的两次 `/btw` 是两个不同输入，不能按文本去重。已送达输入跨 active-turn compact continuation
使用 typed carrier 保留，不从摘要或自然语言反解析，也不再建立 task guidance history。

## Task records and workspaces

普通聊天不会预建任务目录。只有注册为 `promotes_task` 的工作工具、`task_progress`、`create_subagents`、
`wait` 等结构化动作真正开始工作时，当前 request 才晋升为 task，并在已解析 owner 下懒建工作区。

同一 thread 可以积累多个已完成或中断的 task records，但它们只是工作索引：

- 继续旧任务必须由模型使用精确 `task_progress(action=select, task_id=...)`。
- 新任务在存在候选时必须显式 `action=start, new_task=true`。
- 不解析“继续、重来、第二步”等自然语言来猜 task id。
- selection 只切换结构化 workspace/progress lineage；模型历史仍是同一 thread。
- 完成、停止、取消和 supersede 只改变 task record，不切换聊天 lane。

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

远程 owner 只能访问自己的 owner home。用户、群组的 transcript、USER.md、SOUL.md、memory、tasks 和
artifacts 相互隔离。唯一公共文件区是管理员发布的 `~/.my-agent/shared/`，用于 shared skills/tools/workflows；
随 wheel 发布的 builtin tools/skills 本身也是公共产品能力。自然语言路径不能越过 owner 边界。

## Subagents and user-visible delivery

子代理拥有自己的局部运行上下文和 workspace，但不直接写用户 transcript。子代理的私有 commentary、命令、
工具协议和中间输出只进入 task-local ledger；主代理读取结构化进度/结果后，在同一主 thread 中整合。

所有普通最终回复、后台主动结果和显式附件发送经过统一 `DeliveryService`。IM adapter 不重新解释任务状态，
也不把内部 XML、工具调用或子代理碎碎念投递给用户。

## References checked

- 会话运行时 current checkout `03bb3b12367397e14a8facc2e018d645ff4d8e83`:
  `会话运行时-rs/core/src/session/session.rs`, `session/turn.rs`, `tasks/compact.rs`, `compact.rs`,
  `thread_manager.rs`. Adopted one thread history, steer as current-turn input, interrupt without thread loss,
  and compact replacing the same history. The remaining nonblocking-wait lifecycle difference is recorded above.
- 通道运行时: stable channel/session identity, active-run control, parent-only child aggregation and typed delivery
  boundaries were checked. Its product-specific session defaults were not copied.
- 长期助手: gateway conversation keys, memory provider separation and shutdown/recovery boundaries were checked.
  Its compression algorithm and profile-wide memory layout were not copied.

The adaptation is limited to Python interfaces, owner-scoped file storage and my-agent runtime types. No
Feishu-specific context branch or natural-language task classifier is part of this design.
