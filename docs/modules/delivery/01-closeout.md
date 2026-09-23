# Task Completion and Goal Closeout

本文描述当前主代理任务完成边界。旧的 `delivery_closeout`、目录扫描验收器和
`submit_for_acceptance` 已退出生产主链。

## 普通任务

- 普通任务与 会话运行时 一样：模型读取当前对话、工具结果、测试结果和任务进度，自行判断是否已经完成，
  然后用自然语言给出最终回复并结束当前回合。
- runtime 不扫描 `output/` 推断完成，不要求产出文件，不生成机器完成 marker，也不在模型最终回复后
  再运行一套独立验收器。
- 没有文件产物的分析、问答和建议任务可以正常结束；需要修改文件的任务由模型根据用户要求和真实工具
  结果完成工作并报告验证事实。
- `task_progress` 是模型管理多步骤工作的辅助账本，不是完成硬门。未完成事项会继续提供给模型判断；
  runtime 不因清单状态重写或拒绝模型最终回复。
- 子代理结果、产物引用和工具错误是结构化运行事实，供主代理判断与汇总；它们不自动把普通任务判成
  完成或失败。
- 若当前请求较早的 effect-bearing 工具操作仍为 `failed/unknown/cancelled/incomplete`，而后续另一操作
  已成功，首份自然 final 会像 会话运行时 blocking stop hook 一样把有界未决事实和草稿交回同一模型核对。
  这是最多两次的软 continuation：模型可继续修复、补证据或如实披露，宿主不扫描正文/文件/Todo 判定
  任务质量，也不拒绝模型核对后的第二份自然回复。
- 所有外部通道只投递净化后的模型正文。内部工具协议、运行信号和宿主绝对路径不能进入用户回复；
  外部路径脱敏发生在通道出口，内部 transcript 仍保留后续工作所需的真实路径。

## 后台能力事件中的最终回复

能力申请或授权唤醒的是一个正常后台工作片。工作片开始于 `subagent_capability_request_open` 或
`subagent_capability_granted`，不代表结束时仍在处理权限：父级可能已吸收孩子结果并自行完成整合。
`conversation/runtime.py::_background_delivery_decision` 在模型返回后读取同一 `ConversationTaskLink`；
其当前状态为 `completed` 时，模型原回复沿既有交付链发送并进入公开历史。仍未完成的能力事件继续静默，
取消、停止和活跃 Goal 的原规则不变。空正文且无附件仍被交付层抑制；不生成替代文案、不补发历史片，
也不因原生历史已有 final 自动重跑任务。

孩子本轮结束后仍可保留 `BLOCKED` 等待权限或目标恢复；是否仍有执行器须读 exact attempt 与执行事实，
不能只凭该状态宣称孩子已完成或仍在运行。
父级自然完成、公开正文交付、AgentRun 终态和整棵 TaskRun 关闭分别读取各自原账本；本修复不修改
`runtime_db` 的整树关闭条件、不清除 UNKNOWN，也不扩大路径或工具权限。

参考边界：只窄读 Codex `578c1b22` 的 `codex-rs/core/src/tasks/mod.rs` 中结束事件携带
`last_agent_message` 的路径，以及 Hermes `0a62610f1` 的 `gateway/run.py` 最终响应取回与
`gateway/delivery_ledger.py` 的生成、尝试和送达分账。借鉴结束事实与交付事实分离；不移植它们的队列、
重投策略或状态存储，不声称完整审阅。验收覆盖当前状态矩阵、同片子状态变化、空载荷与重复提交；
真实同版 TUI 复验由发布主线执行，旧 TUI212 的准备失误与交付失败证据保留。

## `/goal` 持续目标

- `/goal` 是同一 conversation thread 上的持久目标 overlay，不是普通任务的前置条件。
- 旧未命名 `/goal <目标>` 同时只能有一个未结束目标；显式
  `/goal <时长> <名称> <任务>` 可在同一 thread 并存。每个命名目标有独立 `goal_id/task_id`
  和计时，状态为 `active`、`paused`、`blocked`、`usage_limited`、`budget_limited` 或 `complete`。
- 模型目标工具为 `get_goal`、`create_goal`、`update_goal`；当前普通会话还可用
  `stop_named_work` 精确停止命名 Audit/Goal。创建目标只能来自用户或系统的显式请求；
  token budget、name 和 duration 不能从普通任务文字中自动推断。
- `update_goal` 只允许模型写 `complete` 或 `blocked`。`blocked` 是模型在连续多轮确认同一阻塞且无法
  继续后作出的判断，不由关键词或目录扫描器推断。
- `active` 目标在当前回合至少执行过一次真实工具调用后，且没有排队用户消息时，才安排下一次去重续跑；
  零工具回合停止自动续跑，避免无效消耗。
- 每个续跑回合读取同一 thread history，并按精确 `task_id` 叠加紧凑 `task_progress`，沿结构化下一步继续；
  child 终态只更新与其精确 `run_id` 对应的进度项，不替模型判断根任务完成。
- 非缓存输入 token 与输出 token 计入目标预算；达到预算进入 `budget_limited`。提供方的结构化用量限制
  进入 `usage_limited`；回合错误进入 `blocked`。
- 目标完成后保留最终用量事实。旧单目标使用 `/goal clear`；命名目标使用
  `/goal <名称> clear`，只删除精确目标并停止其 task/子代理，不影响同 thread 的其他目标。
  `/goal edit` 仍只用于唯一的旧单目标。多个 Goal 并存时普通前台聊天不选择或注入任意一个，
  后台续跑通过精确 `thread_goal_id + task_id` 读取自己的目标。

## 仍然保留的安全边界

- 文件写入、压缩包、PDF、Office 文档等格式检查属于工具安全边界：坏候选不能覆盖已有好文件。
- artifact registry 记录路径、hash、owner、run/task 和可发送状态，供附件复用及权限检查；它不是普通
  任务完成判定器。
- 显式结构化 `delivery_contract` 只约束调用方明确提供的路径、格式和权限事实；默认 chat、CLI、Gateway
  不从用户自然语言自动生成合同，也不借合同恢复旧验收主链。
- 所有权限、取消、目标状态、owner、task 和 artifact 决策只读结构化字段；普通自然语言只交给模型理解。
