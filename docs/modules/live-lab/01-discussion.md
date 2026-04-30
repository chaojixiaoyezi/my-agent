# Live Lab：灵感碰撞 / 功能讨论记录

## 为什么想做

Live Lab 是为了把“代码测试通过”再往前推一步：用可见、可重复、尽量接近真实使用的场景跑完整链路，观察 agent、gateway、subagent、LOG replay 等模块在一起时有没有断。

## 讨论过什么

- `scripts/live_agent_lab.py` 做薄入口，具体 case 放到 `scripts/live_lab/`。
- smoke 套件先验证可见真实环境测试台能跑起来。
- log-analysis 套件离线 replay SecurityAlertV1 fixture，不依赖真实 LLM。
- macOS 可用 `open_live_lab.sh` 新开可见 Terminal，方便观察真实长链路。
- Live Lab 失败应该产出明确 stage、error_type、summary 和 artifacts，而不是只说命令失败。

## 痛点

- 单元测试能证明局部函数，但不能证明用户真实入口和跨模块链路舒服。
- 长任务、gateway、subagent runner、父级验收这类流程很容易在集成处坏。
- 手工跑一遍如果没有固定脚本和产物，失败很难复现。

## 方向

第一版先把可重复脚本和离线 replay 做稳：每个 suite 都有固定入口、输出目录、summary 和 stage 结果。后续再加入更多真实 API 长链路和坏天气场景。

## 相关旧文档

- [TESTS.md](../../../TESTS.md)
- [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)
- [ACCEPTANCE.md](../../../ACCEPTANCE.md)
- [EVIDENCE.md](../../../EVIDENCE.md)
- [LOG_ANALYSIS_BACKLOG.md](../../../LOG_ANALYSIS_BACKLOG.md)

## 当前第一版索引 / 待补齐

本页先收拢 Live Lab 的目标和讨论。后续需要补 suite 列表、每个 case 的输入输出样例、失败样本和推荐运行频率。
