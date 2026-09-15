# 轮结束与自然收口设计（状态：2026-08-24 r9 no-save 绕过已定位并本地修复）

> 参考：会话运行时 的模型/工具 active turn 和 任务运行时 `turn/end.reason`。
> 本文只定义“一轮为什么停”，不定义通用业务质量验收。

## 1. 解决的问题

历史实现在模型自然最终回复之后，还会经过交付扫描、产物清单、
`acceptance_checks`、`verification_status` 和子代理结果格式二次裁决。同一任务
因此同时存在“模型已结束”和“机器还没验收”两套结论，导致：

- 模型已完成但系统又挂起或打断；
- 子代理比主代理更容易因结果格式和验收字段失败；
- 模型正文被二次生成的短回复覆盖；
- Gateway、TUI、父级 wake 与子代理账本对“完成”的理解不一致。

## 2. 唯一轮结束协议

`agent/turn_end.py` 是唯一归一入口。宿主从 provider stop reason、用户控制事件和
runtime status 获取客观事实，归一为六种公开 reason：

| `turn_end.reason` | 大白话 | 子代理生命周期投影 |
|---|---|---|
| `completed` | 模型不再请求工具，本轮自然结束 | `DONE` |
| `blocked` | 缺少用户输入或结构化授权 | `BLOCKED` |
| `max-tokens` | provider 明确因长度/上下文截断 | `PENDING` |
| `aborted` | 用户停止或取消 | `CANCELLED` |
| `error` | provider/runner 明确失败 | `FAILED` |
| `interrupted` | 轮被中断，尚无完成事实 | `PENDING` |

优先级是：宿主明示 reason → provider stop reason → runtime status/reason。
未知字符串不得猜成 `completed`。

## 3. 明确禁止的完成信号

以下内容可以给模型和人类理解，但不得改写 `turn_end.reason`：

- 模型正文中的“已完成”、“正在推进”或任何多语言变体；
- `acceptance_checks`、`verification_status`、报告分数、测试数量或产物数量；
- 某个目录、`final_report.md` 或其他历史收口文件是否存在；
- 旧 `SUBAGENT_RESULT`、完成 marker 或状态别名；
- 语义摘要对工具成功/失败的描述。

工具的真实 succeeded/failed/unknown、路径权限、附件 owner/hash 和危险效果仍是不可
绕过的客观事实。它们告诉模型“实际发生了什么”，但不在最终回复后再建一个
通用质量裁判庭。

## 4. 主代理与后代的一致语义

主代理、子代理和孙代理共用同一模型/工具循环。子代理最终回复作为普通
协作者消息原样归档；宿主另行记录 `turn_end.reason`，并以结构化 wake 通知父级。
父级可以查看结果 refs、发补充消息或取消，不需要再调一个“推进”工具。

后代调用 `create_subagents` 后，当前工作片按宿主
`interrupted/SUBAGENTS_ACTIVE` 投影为 `PENDING`，并以精确直属 child ids 记录等待。
这是主动让出，不是挂死。成功 child 收齐后宿主唤醒直属父级一次；失败或
capability 阻塞立即唤醒。新工作片从 canonical child state 获得结果引用，不依赖轮询工具。

历史 task 里的 `acceptance_checks` / `verification_status` 暂不物理删除，以便读取旧账本；
它们不再进入 TaskEnvelope、runner 模型摘要、父级 wake、树摘要或完成算法。

## 5. 与 `/goal` 和返工的边界

- 普通任务自然结束后不自动新开一轮。
- 普通 `task_progress` 仍是模型的软计划，不是机器质量验收或跨轮自动续跑授权；模型给出自然 final 时，
  open item 不触发隐藏核对轮、不覆盖正文，也不把普通任务改成 blocked。
- `save=False` 只控制可选运行档案/记忆写入；生命周期继续由 plain final、显式 `/goal`、直属 child、取消、
  权限和 UNKNOWN 等各自 typed 事实决定，不能拿 save 或 Todo 状态替代。
- Todo、失败命令和产物事实继续进入历史、Compact 和下一次用户回合，供模型如实判断；宿主不读取正文、
  代码量、测试、目录或产物来生成第二份完成裁决。
- 显式 `/goal` 是 thread 上的持久目标 overlay；只有它可以按 typed goal 状态与预算续跑。
- 已明确的工具 `failed/not_started` 与最终回复冲突时，可在同一 active turn
  把结构化冲突交回模型有界返工；这是工具事实冲突修复，不是质量验收。
- `unknown/cancelled/incomplete` 继续按安全边界 fail-closed，不盲目重放可能已发生的副作用。

## 6. 验证要求

执行器异常退出由 `runtime_db/executor_liveness.py` 记录 exact attempt 的进入/退出与进程身份。
同宿主内存登记是当前执行区间的存活观测，持久元数据负责跨实例/跨进程恢复；没有 session 也可以
根据原进程死亡事实恢复。宿主活着、未知线程、静默时间和心跳过期不能单独触发收口。
没有结果的真实退出按 error/FAILED 落账并通知父级；若工具处于 EXECUTING/UNKNOWN 或已进入 handler
但没有结论，则保留 unknown 执行权封存，子代理显示 BLOCKED 并通知父级核对，不自动重跑。
来源岗位继续使用已有独立恢复策略，不在本轮改变其持续工作语义。

未落盘收口事实通过 `pending_events_page` 在既有运行事件账本中分页轮转：消费标记按
agent_run/attempt 精确去重，不限于最近若干条；扫描游标单独存于 metadata，进程重启仍可继续。
读取一页不等于消费。单条失败保持待恢复，扫描到尾部后绕回重试；已有同身份 WAL 只补消费标记，
已被新 attempt 取代的事件标为 superseded，不把旧结果写进新轮。

- 六种 reason 的归一和主/子/Gateway 投影有定向测试；
- 模型最终正文不被交付层改写；
- 无 acceptance/evidence 的普通 child 可启动并自然 `DONE`；
- `max-tokens` / `interrupted` 不得误标 `DONE`；
- 真机只用一个 Gateway，通过 TUI 发送普通用户 prompt，测试者不旁路补产物。
- `e94f8ec` 与 r9 是已退役 stop-nudge 方案的历史证据；当前回归反向要求普通 open Todo 只发生一次
  provider final，不得再出现 `task-progress-closeout-reconciliation`。
  修复回归必须覆盖 Gateway `save=True/False` 和后台 `save=False`，且 task-path ledger 跨 attempt 不漂移；
  发布后仍需原样重型 TUI 证明 open 项会被同轮继续或 typed blocked。
- 产品代码量、生成测试数量和真实界面质量继续留给模型/用户验收。r19 的“112 passed 但真实 TUI 白屏”
  是明确反例，禁止因此把宿主目录扫描或业务质量判定重新塞回本状态机。
