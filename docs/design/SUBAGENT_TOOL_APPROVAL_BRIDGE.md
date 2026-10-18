## 2026-10-17 审批只读SQLite暂忙窄修已合候选

根96按87准确前像合一生产与四直接测试，Astra92独立157项与2项取消/extended BUSY边界通过；旧源同实际WAL锁反例仍在原五回执断言失败并保留。仅W提交前的完整只读签卡/身份校验可对真实结构化SQLITE_BUSY最多尝试三次，原interruptible等待5/10ms；权限每次重新校验，input/决定/工具动作不重试。默认C/Count0、Root消费、ro0数据库与unknown/P3不变；15ms只是退避等待和，不承诺硬实时。历史现场具体锁持有者尚未知，最终组合、全仓及发行另验。

# 后台主/子代理具体工具审批桥

状态：子代理桥已有真实 TUI 验收；2026-09-19 后台主代理桥已实现，428 项定向和实际 TUI 的批准、拒绝、暂停及换轮续用通过。
具体证据及已知边界见 STATUS；历史目录和 schema 保持。
安装版长任务新增发现：父会话关联场景下的子代理审批在发布前失效；归属修正已通过定向回归，安装版真实复验待做。

<a id="im-approval-followup-20261012"></a>

## IM 自动委派追加（2026-10-12，R06/R07，待实施）

用户要求重点验证 `/delegate enable` 后后台主代理带子/孙代理执行，覆盖普通用户、管理员非 full-access 和管理员 full-access；R07 同时包含主代理自身的具体工具审批，以及孩子申请经父级或用户处理的链路。TUI 未启动、前台接单回合已经结束时，也应在 IM 完成本人任务的有效审批并继续原调用。

下文 TUI/Web consumer、当前选中主任务与租约规则描述既有实现，不能据此宣称无 TUI 的 IM 入口已可用。实施应扩展原桥与 IM 控制/卡片：从 canonical task/run/claim 识别有效后台主任务，向可信请求者投影同一 pending，决定仍写回原记录；不以 TUI 选中项充当后台任务权限来源，也不另建 IM 审批账。没有合法决定仍不执行，当前 consumer 缺失/失联的反馈和重入要沿原生命周期明确处理，不能把入口缺失藏成普通工具失败。

父级对 capability request 的裁决复用原父级唤醒与工具；需要用户给出的具体 tool approval 则由用户在原身份、个人锁和运行轮校验后决定。两种状态与缓存保持各自语义，主代理模型不能代用户批准。IM/TUI 同时决定、多个后台主/子并发、取消/换代与迟到回调均复用原原子决定和去重；等待某任务不占住聊天或其它独立任务。

详细范围与待测矩阵见 [主子家目录权限及后台审批](OWNER_HOME_PERMISSION_REVIEW.md)。本追加仅为后续修订合同，既有实现与历史验收记录保留，开发等待用户开始指令。

## 解决问题

子代理与主代理共用 ToolExecutor 的 `allow / ask / deny` 安全门，但过去子代理的后台
`BackgroundTranscriptSink` 没有交互审批入口。结果是具体调用返回 `APPROVAL_REQUIRED` 后，模型只能把它
当普通工具失败继续或结束；用户既看不到确认框，也不能批准后让同一调用原地续跑。更危险的旧绕法是把
capability grant 当成批准，直接执行 `controlled_exec(apply=true)`。

后台主代理的活动 sink 曾未透传同一审批入口，Goal 工作片也会遇到相同缺口。本设计让主/子后台调用
复用所属用户的原审批桥，决定后继续同一调用；不改变 capability、任务完成或自然语言指导。
主代理只接纳当前会话选中的 active 任务及有效 running claim，非当前任务的独立车道仍关闭式失败。

## 对照证据

- Codex `codex-rs/core/src/session/mod.rs::request_command_approval` 先注册当前回合的精确 callback，再发布 typed
  `ExecApprovalRequest` 并等待决定；`codex-rs/tui/src/chatwidget/interrupts.rs` 将并发确认排队，
  `bottom_pane` 只展示当前项。工具执行和 UI 展示不靠正文互相猜状态。
- 终端交互 `src/hooks/toolPermission/handlers/swarmWorkerHandler.ts` 先注册精确 callback，再经 mailbox 将
  worker 请求送给 leader，并在 abort 时收口；`src/utils/swarm/leaderPermissionBridge.ts` 复用 leader
  标准 `ToolUseConfirm` 队列，不给 worker 另造一套确认界面。
- 本项目继续复用 `agent/contracts/tool_approval.py` 与
  `agent_core/tool_loop/round_execution.py::_resolve_tool_approval`：批准后重新执行的仍是原
  `ToolCall`，而不是 TUI 拼出的新命令。

## 合同边界

1. capability grant 只回答“这个 child 能否看见/请求某工具、命令、路径或网络范围”。
2. tool approval 只回答“用户是否批准当前 exact tool/run/operation/idempotency/args binding”。
3. 标题、代理类型展示标签、名称、行号、选中位置、feedback 和用户自然语言都没有授权效力。
4. TUI/Web 只是 owner-scoped consumer；唯一决定仍写回服务端 canonical pending record。
5. 没有交互 consumer、consumer 离线、请求损坏、child 结束或取消、main claim 失效/换轮时一律 fail closed。
6. 本切片是安全 bug fix，不增加配置开关；部署方现有 approval policy 仍是准入上界。
7. Goal pause 只关自动续跑，当前回合的挂起审批仍有效；interrupt 取消当前审批，但不关闭独立 PTY 或 child。
   明确任务资源停止沿原取消入口处理归属资源，不由审批队列实现。

## 唯一耐久记录

记录根固定在当前 owner 的 `ConversationStore`：

```text
<conversation-store>/subagent_tool_approvals/<root_task_id>/
|-- .consumer.json
`-- <run_id>.<sha256(permission_id)[:24]>.json
```

待审批记录 schema 为 `subagent_tool_approval.v1`，只保存：

- canonical `root_task_id / run_id / thread_id`；
- main 的精确 `claim_id`；child 沿原 canonical run/attempt 授权；
- 已由共享合同校验和脱敏的完整 `ToolApprovalRequest`；
- `pending / decided` 状态与 typed `ToolApprovalDecision`；
- 创建时间。

外部 id 不直接成为路径；permission id 只以哈希进入文件名。发布、决定和清理共用同记录的 transition lock。
同一路径重放只有完整 root/run/thread/claim/request 相同才幂等；同 id 异请求、异决定和畸形记录全部关闭式失败。
历史目录及 schema 名中的 `subagent` 保持，避免把一个耐久概念拆成两处；内部 API 统一为 agent，不留旧名转发。
`conversation/tool_approval_scope.py` 只读原任务、线程、child 和 claim，不建立第二份运行账本。
子代理创建生命周期会把孩子登记到父会话的任务列表；这个 link 只用于查找、展示，不代表该孩子是主代理。
归属读取先按精确 run ID 查询 canonical 子代理记录，得到它自己的 thread/root/status；孙代理同样处理。
只有该记录不存在时，才读取主任务 link 与有效 claim。记录损坏不能退回主任务，孤立子线程也不能被认作 main。
实测修复前，合法后台命令会直接返回 `APPROVAL_REQUIRED`，父级即使授予 capability 也不会产生具体工具批准。
修复不替模型扩大权限、自动重派孩子或恢复旧失败任务；已发生的运行和失败记录原样保留。

`.consumer.json` 是 `subagent_tool_approval_consumer.v1` 交互能力租约，只表示 owner TUI/Web 正在领取请求，
不是用户批准。当前发现等待为 1.5 秒，活跃租约为 15 秒；这些常量只决定“多久后返回 unavailable”，不能
延长或放宽执行权限。

## 运行流

```text
main/child ToolExecutor ask
  -> main 活动 sink 透传原请求与取消令牌（child 直接使用 transcript sink）
  -> BackgroundTranscriptSink.request_permission(exact request)
  -> 发布 root/run-scoped pending record，main 同时冻结 claim
  -> 原 ToolCall 阻塞等待
  -> owner TUI /client/notices 显式声明 tool_approval capability 并续租
  -> Gateway 返回有界 pending rows
  -> TUI 全局 FIFO 显示当前一项
  -> 用户选择 typed decision
  -> POST /client/agent-permission
  -> owner/thread/root/current-attempt 授权门 + 完整 request 比对
  -> 原子写 decided
  -> 原 waiter 消费并删除自己的精确记录
  -> _resolve_tool_approval 对原 ToolCall 写 approved binding 并原地重执行
```

拒绝/取消沿现有 `runtime_rejected_actions` 进入模型上下文；`unavailable` 保持原
`APPROVAL_REQUIRED` 工具结果。任何分支都不把模型正文改成机器状态。

## TUI 队列与页面

一个 root `TuiRuntime` 只有一个 `TuiPermissionCoordinator`。当前主代理工具回合和多个 child 的控制器
按到达顺序进入同一 FIFO；只有队首可以发布 `permission_requested` overlay 和消费方向键/Enter。队尾不会
覆盖 reducer 里当前面板。

main 标题显示 `主代理`，child 显示 `子代理 <name>`；回写闭包始终捕获服务端原始 request。用户切入 child 详情时，
正文 store 可以切换，但审批 overlay 继续绑定 root runtime；否则正查看另一个页面时会错过安全确认。
写回失败时面板保持打开并显示短提示，绝不能把网络失败当批准。

## 并发和故障语义

- 主/子同时请求：服务端记录彼此独立，TUI 逐个展示；每项按 exact run/request 决定。
- 多 TUI 同时查看：都可读 owner-scoped pending row，第一份合法决定获胜；其它客户端下次轮询收起 stale 行。
- TUI 退出：租约过期后 waiter 得到 `unavailable`，不会永久卡住，也不会执行 handler。
- 当前回合取消：等待者删除自己的 exact record 并返回 `cancelled`；Goal pause 本身不触发取消。
- child 已终态：投影不再显示，控制服务拒绝迟到决定。
- main claim 已结束、过期或被新一轮替代：隐藏旧审批，拒绝迟到决定，旧等待者不能消费新一轮的记录。
- 文件损坏或 identity 冲突：不猜、不修复为批准，返回 unavailable/mismatch。
- `approved_session`：只在该 sink/attempt 内缓存 exact `run_id + claim_id + tool_name + args_hash`，
  查缓存前仍校验当前归属及取消令牌，不跨执行、进程或参数扩散。

## 不做的事

- 不把 capability 请求合并成 tool approval，也不删除 capability 主链。
- 不让主代理模型代替用户自动批准具体副作用。
- 不把审批记录写进 task workspace、transcript 或 child 输出目录。
- 不从“模型已生成回复”“Working”“等待用户”等文案判断请求是否仍有效。
- 不为测试端口、HTTP 服务、超级玛丽或任何具体任务增加专项规则。

## 验证要求

合同回归至少覆盖：实际主/子 sink 发布/等待/恢复、无 consumer fail closed、owner/root/current attempt/claim
授权、完整 request mismatch、三种来源的统一 FIFO、原始 request 回写和 child 页面仍显示 root overlay。
还须覆盖 Goal paused 后当前审批可继续、换 claim 后旧批准与缓存失效，以及主审批不放开 child 查看/插话/停止端点。
创建生命周期登记父会话 link 后，必须重测子/孙审批的批准与拒绝，不能只使用没有 link 的裸 manager 夹具。
本轮对照本地 Codex `tools/approvals.rs::resolve_tool_apporval` 的精确请求/回合路由；参考的是归属及决定边界，未移植 reviewer 或权限策略。

真实验收必须通过每台机器唯一 Gateway、官方 MiniMax-M2.7 的实际 TUI：未批准前 handler/端口/operation 均不存在；
面板明确标出来源；批准后原调用成功继续，拒绝时不执行。暂停 Goal、中断回合、明确停止资源分别核对，
不能用一个控制的结果代替另一个。测试者只发普通中文任务和正常控制/审批按键，不替 Agent 旁路执行任务。
