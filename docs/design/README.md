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
| [subagent-quality-contract.md](subagent-quality-contract.md) | subagent 派工与验收 | 设计中 | 质量契约、受控施工队、context pack、producer/critic/reviewer、用户少说模式 |
| [main-agent-contract-testing.md](main-agent-contract-testing.md) | 主代理合同驱动测试 | 进行中 | 真实环境降级为最终收口，主开发切到合同单测、fake tool、fake LLM 和 replay |

## 后续待拆模块

| 计划文档 | 模块 | 拆分触发点 |
| --- | --- | --- |
| `memory.md` | 记忆系统 | 继续扩展 compression、routing、raw archive、HOT/INDEX 规则时 |
| `gateway.md` | gateway 常驻与外部协议 | `GATEWAY_DESIGN.md` 和主台账之间出现重复设计时 |

## 维护要求

- 新增模块设计文档后，同步更新本索引、`DESIGN_LEDGER.md` 和 `CODEBASE_TREE.md`。
- 如果模块设计已经进入操作层面，同步检查对应 runbook、backlog 或 test checklist。
- 主设计台账里不要复制模块文档全文，只放能帮后来者找到入口的摘要。
