# 轮结束与自然收口设计（状态：2026-08-23 同轮停止核对本地已实现，待真机）

> 参考：会话运行时 的模型/工具 active turn 和 DSH `turn/end.reason`。
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
- 普通 `task_progress` 仍是模型的软计划，不是机器质量验收或跨轮自动续跑授权；但模型准备自然 final 时，
  宿主会仿照 会话运行时 stop hook，把仍 open 的 exact 结构化清单在同一 active turn 有界返给模型核对一次。
- 同轮核对不读取回复正文、代码量、测试、目录或产物。模型可以继续调用原工具，或把真实无法推进项更新为
  `blocked` 并如实说明；核对耗尽仍 open 时，本轮投影为 typed `blocked`，不得把 durable task 写成完成。
- 显式 `/goal` 是 thread 上的持久目标 overlay；只有它可以按 typed goal 状态与预算续跑。
- 已明确的工具 `failed/not_started` 与最终回复冲突时，可在同一 active turn
  把结构化冲突交回模型有界返工；这是工具事实冲突修复，不是质量验收。
- `unknown/cancelled/incomplete` 继续按安全边界 fail-closed，不盲目重放可能已发生的副作用。

## 6. 验证要求

- 六种 reason 的归一和主/子/Gateway 投影有定向测试；
- 模型最终正文不被交付层改写；
- 无 acceptance/evidence 的普通 child 可启动并自然 `DONE`；
- `max-tokens` / `interrupted` 不得误标 `DONE`；
- 真机只用一个 Gateway，通过 TUI 发送普通用户 prompt，测试者不旁路补产物。
