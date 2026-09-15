# 设计台账

本台账保留当前决策、设计入口和未落地边界。逐次排障流水不作为产品规范；实际实现以代码、配置和结构化协议为准。

## 已采用的原则

产物交接补修已实现并通过真实新包主链验收：自然回复从本 run 工具账本交回真实文件，声明与工具使用同一 cwd；
删除内部 output 重定位与隐式搬运。补丁新增精确文件引用和删除墓碑，沿原工具 registry 交接。
实际分层基线已完成，分工选择不冒充运行时故障。详见
[父子并行与交接](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

| 决策 | 当前约束 | 设计入口 |
|---|---|---|
| 用户家目录是默认工作区 | 不以启动终端目录改变默认归属；家目录内整理依靠提示约定，权限是硬边界 | [目录规范](docs/architecture/MY_AGENT_HOME_LAYOUT.md) |
| 单一身份与事实源 | owner/thread/task/run/attempt 显式传递；索引仅用于查找展示 | [架构](docs/design/ARCHITECTURE_GUIDE.md) |
| 单 Gateway、多客户端 | 客户端关闭与后台任务生命周期分开；恢复不能重复执行 | [Gateway](docs/design/GATEWAY_DESIGN.md) |
| 会话独立模型配置 | 会话选择不覆盖其他会话；用户默认只影响按规则继承的新会话 | [模型选择](docs/design/SESSION_MODEL_SELECTION.md) |
| 软件不预选模型 | 模型/协议/地址默认留空；未配置只开放设置和历史，用户已保存选择不变 | [模型配置](docs/design/TUI_MODEL_PROFILES.md) |
| 模型统计与上下文分离 | 轮次取调用账，缓存取服务商用量，累计按当前代理会话；展示字段不回灌模型输入 | [统计口径](docs/design/TUI_DESIGN.md#模型统计口径) |
| 单代理单持续目标 | 每个代理至多一个未结束 Goal，主子分别归属；active Goal 在安全边界续跑，不依赖工具数量或 Todo；旧冲突仅显式迁移，不自动激活 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标计时只有一份基线 | 同一 Gateway 的同源会话存储共享单调时钟；模型轮次、后台工作片及控制查询不重复累计同一秒；旧多记耗时不从日志推测回写 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| Goal 终态不冒充执行终态 | 完成目标只停止该目标的续跑；当前回合、消息和子树完成后由统一 finalization 关闭任务，不在目标工具中提前关父任务 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 名称不决定执行通道 | Goal 名称不创建额外执行器；已有未结束目标时新名字返回冲突。后台数量不授予前台执行权，父级取精确任务身份 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标编号不证明仍可续跑 | 轮限交接重读当前 Goal 状态与任务绑定，旧编号、已完成或暂停记录不产生自动续跑承诺；不新增完成质量判断 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 主子代理共享会话能力 | 历史、插话、停止、压缩、终态以相同底层协议处理 | [子代理](docs/modules/subagent/04-structure.md) |
| 逐项交付与阶段诊断 | 创建不强制等待、成功不等全树；原执行车道接收结果，长静默只诊断、不强杀或自动替代 | [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md) |
| 终态与通知可恢复 | 先持久化结论，再提交账本，最后按精确 attempt 去重通知 | [收口](docs/design/closeout_state_machine.md) |
| 执行器退出是独立事实 | 进入/退出登记属于 exact attempt；没有 session 可核对 OS 身份，未知副作用保留封存 | [收口](docs/design/closeout_state_machine.md) |
| 恢复游标不等于消费 | 未消费事件分页轮转；成功写入 canonical WAL 后才记回执，失败项可重试 | [收口](docs/design/closeout_state_machine.md) |
| 完整历史与展示窗口分离 | 完整未压缩历史来自 canonical 消息，分页不能裁掉模型记忆 | [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md) |
| 权限硬、任务组织软 | 自然语言不决定运行状态、越权、任务归属或验收结果 | [开发规则](docs/development/DEVELOPMENT_RULES.md) |
| 分层验证 | 确定性合同/替身/回放用于开发反馈，真实 TUI 用于最终验收 | [测试分层](docs/design/main-agent-contract-testing.md) |

## 当前待落地或待复验

- 已实现，主链已验收、复杂组合待补：父子孙继续独立工作，成功通知只保留有界合批窗口，不等待整树终态。
  模型自然让出才进入直属等待，任一新结果可解除等待；复用 canonical 事件与原执行权。
  停滞诊断区分慢首 token、流活动、长工具与已退出执行器，不引入静默超时强杀或自动替代。
  四路真实 TUI 已验证快结果接入、父级独立工作和低阈值提醒后继续完成；后续已完成真实孙级交接，重复实现仍需改进。
  设计和验收边界见 [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

- 已落地并完成真实 TUI 主流程验证：每个代理会话至多一个未结束 Goal；主代理与每个子代理独立保存。新增第二个名字不再启动另一个执行器。
  派工可只给 prompt，也可显式附持续目标；Todo 按代理复用已有账本、完全可选，不参与完成门。
  TUI 通过方向键和 Enter 打开目标草稿，明确保存才生效，退出丢弃；保存使用内容版本比较，计费刷新不制造编辑冲突。
  修改内容不隐式恢复暂停目标，不改变 run/task、历史和权限。主子目标保存、放弃、停止以及父子消息隔离已实测；孙级、IM 和旧数据迁移仍待专项验证。详见 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

- 执行器退出与积压分页修复已落地并通过定向回归，真实 TUI 故障组合待验；不按静默时长判死。
- 长会话的模型输入、展示估算、累计用量和缓存费用统一核算，并保留压缩前后的可恢复历史。
- 旧会话目标冲突的显式迁移、小时级慢模型并发操作、IM 环境能力仍需专项验证。
- Goal 提前关闭任务及命名后插话身份断裂已通过真实 TUI；多目标去重分工和重复正文仍未解决，模型自行填写 Goal 时限仍是开放边界，
  尚未把模型的时间参数改为必须由用户控制面授权的方案，不通过解析用户正文判断。
- 详细现象和优先级以 [STATUS](STATUS.md) 与 [ROADMAP](docs/ROADMAP.md) 为准。

## 发布资料约定

产品统一命名为 my-agent。文档只保留使用、部署、功能和开发资料；示例使用保留域名或虚构用户。真实凭据、个人数据、临时评估产物、机器现场配置不进仓库。

运行必需的服务商名称、模型 ID、HTTP header、兼容文件名、依赖名、正式仓库地址保留。第三方许可和版权说明见 LICENSE、NOTICE 及对应 vendor 目录，发布包必须携带。
