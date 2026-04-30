# Log Analysis：灵感碰撞 / 功能讨论记录

## 为什么想做

日志分析模块的目标不是让大模型直接吞大日志，而是把接入、查询、检测、case、证据、报告和 analyst 派工拆开。这样既能保留可追溯证据，也能让模型只处理小而受控的对象。

## 讨论过什么

- 第一版先支持本地 SecurityAlertV1 CSV / JSONL 接入。
- 使用轻量 JSONL store 做本地开发后端，不承诺生产级大数据性能。
- 查询、hunt、trace 要受控，不能把安全工具默认暴露到普通 prompt。
- finding、case、route、report 要保留 evidence refs，方便复核。
- analyst/reviewer work order 先 dry-run，避免过早创建不可控 subagent。

## 痛点

- 大日志直接交给模型，容易慢、贵、不可复核。
- 没有 evidence refs 时，安全结论很难追溯。
- 安全工具默认暴露到普通任务，会扩大权限和提示面。
- 本地 JSONL 坏行、limit、配置路径等小问题会破坏闭环体验。

## 方向

第一版先做可离线回放、可查询、可检测、可生成报告的本地闭环；后续再考虑更大后端、真实 analyst dispatch 和更丰富 fixture。

## 相关旧文档

- [docs/design/log-analysis.md](../../design/log-analysis.md)
- [LOG_ANALYSIS_BACKLOG.md](../../../LOG_ANALYSIS_BACKLOG.md)
- [ACCEPTANCE.md](../../../ACCEPTANCE.md)
- [EVIDENCE.md](../../../EVIDENCE.md)
- [CODEBASE_TREE.md](../../../CODEBASE_TREE.md)

## 当前第一版索引 / 待补齐

本页先保留讨论入口和旧文档链接。后续需要把每轮 worker 切片、关键修复和失败样本按时间补齐。
