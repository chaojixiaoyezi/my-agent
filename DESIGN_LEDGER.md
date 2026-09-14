# 设计台账

本台账保留当前决策、设计入口和未落地边界。逐次排障流水不作为产品规范；实际实现以代码、配置和结构化协议为准。

## 已采用的原则

| 决策 | 当前约束 | 设计入口 |
|---|---|---|
| 用户家目录是默认工作区 | 不以启动终端目录改变默认归属；家目录内整理依靠提示约定，权限是硬边界 | [目录规范](docs/architecture/MY_AGENT_HOME_LAYOUT.md) |
| 单一身份与事实源 | owner/thread/task/run/attempt 显式传递；索引仅用于查找展示 | [架构](docs/design/ARCHITECTURE_GUIDE.md) |
| 单 Gateway、多客户端 | 客户端关闭与后台任务生命周期分开；恢复不能重复执行 | [Gateway](docs/design/GATEWAY_DESIGN.md) |
| 会话独立模型配置 | 会话选择不覆盖其他会话；用户默认只影响按规则继承的新会话 | [模型选择](docs/design/SESSION_MODEL_SELECTION.md) |
| 主子代理共享会话能力 | 历史、插话、停止、压缩、终态以相同底层协议处理 | [子代理](docs/modules/subagent/04-structure.md) |
| 终态与通知可恢复 | 先持久化结论，再提交账本，最后按精确 attempt 去重通知 | [收口](docs/design/closeout_state_machine.md) |
| 完整历史与展示窗口分离 | 完整未压缩历史来自 canonical 消息，分页不能裁掉模型记忆 | [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md) |
| 权限硬、任务组织软 | 自然语言不决定运行状态、越权、任务归属或验收结果 | [开发规则](docs/development/DEVELOPMENT_RULES.md) |
| 分层验证 | 确定性合同/替身/回放用于开发反馈，真实 TUI 用于最终验收 | [测试分层](docs/design/main-agent-contract-testing.md) |

## 当前待落地或待复验

- 执行器退出而无结果时，补充可追踪的运行终止事实；不能用慢模型的静默时间冒充死亡事实。
- 收口恢复积压需要分页、去重与公平消费，不能只扩大固定扫描窗口。
- 长会话的模型输入、展示估算、累计用量和缓存费用统一核算，并保留压缩前后的可恢复历史。
- 旧会话目标冲突的显式迁移、小时级慢模型并发操作、IM 环境能力仍需专项验证。
- 详细现象和优先级以 [STATUS](STATUS.md) 与 [ROADMAP](docs/ROADMAP.md) 为准。

## 发布资料约定

产品统一命名为 my-agent。文档只保留使用、部署、功能和开发资料；示例使用保留域名或虚构用户。真实凭据、个人数据、临时评估产物、机器现场配置不进仓库。

运行必需的服务商名称、模型 ID、HTTP header、兼容文件名、依赖名、正式仓库地址保留。第三方许可和版权说明见 LICENSE、NOTICE 及对应 vendor 目录，发布包必须携带。
