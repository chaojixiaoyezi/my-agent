# ARCHITECTURE BOUNDARY

LLM: Use this as the short root-level boundary contract.

给人看的解释：
更详细的边界文档在 `docs/architecture/BOUNDARY_RULES.md`。本文件用于代码规模治理的快速入口。

## CLI

- 只负责参数解析、命令注册、展示和退出码。
- 不直接承载状态机、文件迁移、Gateway 队列处理或 Memory 策略。

## Application / Services

- 负责用例编排、状态变更和调用 repository。
- 不直接依赖 argparse，也不拼 CLI 输出。

## Domain

- 负责模型、状态、策略和不变量。
- 不读写文件、不访问环境变量、不调用 LLM 或 HTTP。

## Repository / Infrastructure

- Repository 负责 JSONL、SQLite、Markdown 和索引读写。
- Infrastructure 负责 subprocess、HTTP、LLM backend、文件系统和协议实现。

## Extensions

- `log_analysis` 长期视作 extension。
- 扩展只能通过注册接口接入核心，不反向污染 core。
- 短期统一入口是 `agent_py_agent.agent.extensions.ExtensionPlugin` 和 `ExtensionRegistry`。
