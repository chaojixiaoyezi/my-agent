# 来源与迁移范围

参考来源：[GreyDGL/PentestGPT](https://github.com/GreyDGL/PentestGPT)，固定 commit `e8b1bb77d1ac00329675cec3b060aba971ec1ac8`。本轮只读本地公开副本。

已读：`docs/architecture.md`、`pentestgpt_agent/pyproject.toml`、`pentestgpt_agent/src/pentestgpt_agent/execution.py` 的证据验证入口及相关源码/测试路径。架构文档说明维护入口为 pentestgpt_agent，根 unified_agent 为旧兼容副本。
本包只改写范围/来源/证据不等于模型叙述的思想；整理脚本、数据格式和合成 fixture 均为新编写，没有复制原模型 SDK、数据库、控制循环、攻击工具或 payload。

| 上游范围 | 本包映射 | 当前覆盖与缺项 |
| --- | --- | --- |
| 作用范围与证据来源 | scope 字段、asset ID、来源与摘要检查 | 只验证输入结构，实际授权由宿主确认 |
| 证据引用和结果可靠性 | 同资产引用、去重、保留别名 | 没有真实执行轨迹编译或完整来源证明 |
| 报告 | 待复核发现和建议 | 不自动确认漏洞，不计算真实扫描覆盖率 |
| Supervisor/Executor、MemoryKernel | 无 | 不复制，复用宿主现有任务、子代理、记忆和恢复 |
| 扫描、实操验证、利用、外部运行时 | 无 | 未迁移，也不在本包作用范围 |

新编代码及文档按主仓库 Apache-2.0（`LICENSE`）；保留上游 MIT 许可于 `licenses/PentestGPT-LICENSE`。没有第三方运行依赖、联网、遥测和外部进程启动。
组件通过只说明整理行为正确，不能报告完整渗透能力、真实漏洞验证或 100% 内化。
