# 模块设计文档

这个目录放”可以长期长大”的模块设计文档。

导航：
- `LLM_GUIDE.md`：LLM 总入口，含开工前/收工后清单
- `docs/ROADMAP.md`：待做/进行中功能清单
- `docs/COMPLETED.md`：已落地功能清单
- `DESIGN_LEDGER.md`：主设计台账，只负责导航、决策摘要和状态索引

详细方案、字段草案、分阶段开发计划和验收规则应该放到这里的模块文档里。

## 拆分规则

遇到这些情况时，不要继续把内容塞进 `DESIGN_LEDGER.md`：
- 单个设计条目已经超过约 100 行。
- 内容只属于一个模块，例如 subagent、memory、gateway、log analysis。
- 设计里包含 schema、配置开关、阶段计划、验收策略或成功/失败样本。
- 后续开发会反复引用这份设计，而不是只查一次历史背景。

推荐结构：
- `DESIGN_LEDGER.md`：一句话背景、状态、关键决策、模块文档链接。
- `docs/design/<module>.md`：完整背景、痛点、方案、开关、落地步骤、验证策略。
- 运行手册或 backlog：更贴近操作的流程、命令、检查清单。

## 当前模块文档

| 文档 | 模块 | 状态 | 说明 |
| --- | --- | --- | --- |
| [MAINTAINABILITY_AND_JEV_REVIEW.md](MAINTAINABILITY_AND_JEV_REVIEW.md) | 模块可维护性 / Computer Use / Jev | 评估完成，方案待实施 | 热点源码及参考证据、渐进重构顺序、桌面能力现状；Jev 新接入方向见决策模型计划 |
| [DECISION_MODEL_INTEGRATION.md](DECISION_MODEL_INTEGRATION.md) | 可选决策模型 / Jev | P1—P5 实施中，P1-A 已本地验收 | 原模型配置复用、短期限与失败隔离、逐点开关、记忆/派工/能力推荐、缓存和并行开发验收 |
| [PLUGIN_DISPLAY.md](PLUGIN_DISPLAY.md) | 插件面板 / 只读订阅 | 第 9 步已发布，真实 TUI 已验 | 声明式面板、公开主题投影、单在途与撤销、TUI 只渲染核心校验结果 |
| [PLUGIN_WORKSPACE_WRITE.md](PLUGIN_WORKSPACE_WRITE.md) | 插件写入 / 工作区写入上下文 | 第 10 步本地已实施 | 协商扩展且声明写效果才下发、与内置写工具同一裁决且只可能更严 |
| [PLUGIN_LIFECYCLE.md](PLUGIN_LIFECYCLE.md) | 可选插件 / 动态命令 / 热装卸 | 设计草案，待实施 | 核心与插件边界、停用无运行影响、Python 进程隔离、版本快照与卡死卸载；参考范围和 TUI 验收矩阵 |
| [PLUGIN_SAMPLE_ACCEPTANCE.md](PLUGIN_SAMPLE_ACCEPTANCE.md) | 插件样本 / 功能组合 / TUI 验收 | 计划已记录，待实施 | 社区热度与随机抽样、10 个自有简易插件、分批功能验证和故障卸载；Audit 不在本轮重构范围 |
| [computer-use.md](computer-use.md) | Computer Use / MCP | 真 TUI 通过 | 开源执行器选型、官方可选依赖、管理员 Full Access 硬门、effect 与 OCR 验收边界 |
| [FEATURE-20260804-tool-runtime-unification.md](FEATURE-20260804-tool-runtime-unification.md) | 工具运行时统一功能规格 | 完成 | 用户可见行为、需求、状态机、测试与验收权威 |
| [tool-runtime-unification.md](tool-runtime-unification.md) | ToolRuntime / ToolCall / ToolResult / ActionPolicy / ToolExecutor | 完成 | 参考证据、完整架构、迁移删除表、并行边界和完成证据 |
| [SUBAGENT_TOOL_APPROVAL_BRIDGE.md](SUBAGENT_TOOL_APPROVAL_BRIDGE.md) | child exact tool approval / owner TUI FIFO | 真机通过 | 子代理具体审批的唯一记录、租约、授权、队列与失败语义 |
| [MANAGED_BACKGROUND_PROCESS_SESSIONS.md](MANAGED_BACKGROUND_PROCESS_SESSIONS.md) | 受管后台进程会话 | 真机通过 | one-shot runner 之外的 host 所有权、持久记录、PID 身份与进程树回收 |
| [main-agent-contract-testing.md](main-agent-contract-testing.md) | 主代理合同驱动测试 | 进行中 | 真实环境降级为最终收口，主开发切到合同单测、fake tool、fake LLM 和 replay |
| [P1_MAINLINE_CONVERGENCE.md](P1_MAINLINE_CONVERGENCE.md) | P1 主链收敛 | 本地验收通过 | import/wheel 边界、唯一入口与插件链、语义检索、PTY、LSP、OpenAI native tools |
| [P2_SCALE_MAINLINE.md](P2_SCALE_MAINLINE.md) | P2 规模主链 | 部分可用 | PG/Redis/OTel/migration、正式 scale 入口与诚实缺口 |
| [P2_SCALE_ROLLOUT_DR_OWNER_STORE.md](P2_SCALE_ROLLOUT_DR_OWNER_STORE.md) | P2 灰度/灾备/Owner 存储 | 进行中 | release channel、S3 owner 事实源、灾备清单与运行中的 24 小时 proof |

## 后续待拆模块

| 计划文档 | 模块 | 拆分触发点 |
| --- | --- | --- |
| `memory.md` | 记忆系统 | 继续扩展 compression、routing、raw archive、HOT/INDEX 规则时 |
| `gateway.md` | gateway 常驻与外部协议 | `GATEWAY_DESIGN.md` 和主台账之间出现重复设计时 |

## 维护要求

- 新增模块设计文档后，同步更新本索引、`DESIGN_LEDGER.md` 和 `CODEBASE_TREE.md`。
- 如果模块设计已经进入操作层面，同步检查对应 runbook、backlog 或 test checklist。
- 主设计台账里不要复制模块文档全文，只放能帮后来者找到入口的摘要。
