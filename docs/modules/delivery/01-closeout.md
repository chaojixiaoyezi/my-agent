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
- 所有外部通道只投递净化后的模型正文。内部工具协议、运行信号和宿主绝对路径不能进入用户回复；
  外部路径脱敏发生在通道出口，内部 transcript 仍保留后续工作所需的真实路径。

## `/goal` 持续目标

- `/goal` 是同一 conversation thread 上的持久目标 overlay，不是普通任务的前置条件。
- 每个 thread 同时只能有一个未结束目标。目标状态为 `active`、`paused`、`blocked`、
  `usage_limited`、`budget_limited` 或 `complete`。
- 模型工具只有 `get_goal`、`create_goal`、`update_goal`。创建目标只能来自用户或系统的显式请求；
  token budget 也只能在用户显式给出时设置。
- `update_goal` 只允许模型写 `complete` 或 `blocked`。`blocked` 是模型在连续多轮确认同一阻塞且无法
  继续后作出的判断，不由关键词或目录扫描器推断。
- `active` 目标在当前回合至少执行过一次真实工具调用后，且没有排队用户消息时，才安排下一次去重续跑；
  零工具回合停止自动续跑，避免无效消耗。
- 每个续跑回合读取同一 `task_id` 的紧凑 `task_progress` 和 task compact refs，沿结构化下一步继续；
  child 终态只更新与其精确 `run_id` 对应的进度项，不替模型判断根任务完成。
- 非缓存输入 token 与输出 token 计入目标预算；达到预算进入 `budget_limited`。提供方的结构化用量限制
  进入 `usage_limited`；回合错误进入 `blocked`。
- 目标完成后保留最终用量事实。`/goal clear` 删除目标记录；`/goal edit` 保留目标身份和已用资源。

## 仍然保留的安全边界

- 文件写入、压缩包、PDF、Office 文档等格式检查属于工具安全边界：坏候选不能覆盖已有好文件。
- artifact registry 记录路径、hash、owner、run/task 和可发送状态，供附件复用及权限检查；它不是普通
  任务完成判定器。
- 显式结构化 `delivery_contract` 只约束调用方明确提供的路径、格式和权限事实；默认 chat、CLI、Gateway
  不从用户自然语言自动生成合同，也不借合同恢复旧验收主链。
- 所有权限、取消、目标状态、owner、task 和 artifact 决策只读结构化字段；普通自然语言只交给模型理解。
