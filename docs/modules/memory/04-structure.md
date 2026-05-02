# Memory：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- memory.py                         # 旧兼容入口，真实存储已拆到 memory_store/
|-- memory_settings.py                # 旧兼容入口，真实配置解析在 settings/memory.py
|-- settings/memory.py                # memory 配置、默认值、warning、安全归一化和参数边界
|-- memory_store/                     # 长期记忆 JSONL 事实流水，可选同步索引到 LocalStore
|-- memory_routing/                   # route index、匹配、required/candidate path、read receipt
`-- memory_archive/                   # hook snapshot、raw archive、留存、token 估算

agent_py_agent/cli/
|-- memory_commands.py                # memory-route / memory-doctor 等可见诊断命令
`-- memory_archive_commands.py        # memory-archive-list/search/resume 命令
```

## 核心文件

- `memory_store/jsonl.py`：读写长期记忆 JSONL，是最朴素的事实落盘层；LocalStore 只是索引，不替代 JSONL。
- `settings/memory.py`：解析配置，处理非法值回退和 warning；会原地更新 AgentConfig-like 对象。
- `memory_routing/loader.py`：读取 route index；JSON 面向程序稳定性，Markdown 面向人工维护，并兼容常见中英文列表分隔符。
- `memory_routing/models.py`：定义 route、match、path resolution、read receipt 等票据结构。
- `memory_routing/matcher.py`：根据用户输入匹配可能需要读取的长期规则，并区分 required/candidate path。
- `memory_routing/context.py`：把命中的规则变成运行时可注入的上下文片段。
- `memory_archive/models.py`：定义 `CompressionSnapshot` 和 `RawMemoryEvent` 两类归档数据形状。
- `memory_archive/storage.py`：保存 raw archive、hook snapshot 和 `memory_archive/snapshots/*.json` 权威快照；写入都做 readback 校验。
- `memory_archive/runtime.py`：把 run turn 的用户、助手、工具元数据写成 raw archive 事件。
- `memory_archive/snapshots.py`：在 run/gateway/subagent 完成点写轻量恢复 snapshot，并提供压缩前必须成功的 `write_compression_snapshot()` hook。
- `memory_archive/query.py`：把 raw/hook JSONL 读成统一可搜索记录，并整理 resume 线索。
- `memory_archive/resume_brief.py`：把归档、LocalStore、任务事实源压成恢复简报。
- `memory_archive/resume_context.py`：在“继续/恢复”类提示里按配置构造自动注入的恢复上下文。
- `memory_archive/tokens.py`：为归档预算提供保守 token 估算，并维护 session 级 token 账本。
- `cli/memory_commands.py`：给用户和开发者看 route/doctor 结果。
- `cli/memory_archive_commands.py`：给用户查看归档列表、搜索归档和生成恢复简报。

## 数据流

1. 用户对话或命令触发记忆写入，基础事实先落到 JSONL。
2. LocalStore 可以为旧 memory 补建索引，让搜索和 timeline 能看到它。
3. 当新任务需要规则时，memory routing 根据 query 匹配 route index。
4. 匹配到的 authority path 会被安全读取成上下文片段。
5. token 预算逼近阈值时，run 主链路先写 `memory_archive/snapshots/*.json` 权威快照，再做组合压缩。
6. 长任务和普通保存路径都会继续写 raw event / hook snapshot，方便恢复和审计。
7. raw event、hook snapshot 和权威快照写完后都会读回校验，确保恢复线索真实落盘。
8. 用户说“继续/恢复”时，resume context 可以按配置从 archive、LocalStore 和任务事实源生成恢复块；跨天时会同时扫描最近 raw/hook 文件。
9. doctor 命令检查配置、route index、hook/raw/snapshot 目录和层级一致性 warning。

## 跨天恢复链路

```text
2026-04-29 raw event
  -> 记录用户当时的任务意图、request_id、run_id、task_id

2026-04-30 hook snapshot
  -> 记录压缩/交接后的恢复锚点、next_actions、task_refs、content_paths

LocalStore subagent_run
  -> 保存可搜索的任务索引，不作为最终事实，只帮助找到 run_id

LocalStore gateway_request
  -> 保存可搜索的 gateway 请求索引，不作为最终事实，只帮助找到 request_id

subagents/<run_id>/
  -> STATUS.md / WORK_LOG.md / HANDOFF.md / ACCEPTANCE.md / TEST_CHECKLIST.md 是最终恢复事实源

gateway/requests/done/<request_id>.json 或 gateway/requests/failed/<request_id>.json
  -> gateway 请求的终态请求事实源；如果 LocalStore 里还是 processing 路径，resume 会尽量纠偏到这里

gateway/responses/<request_id>.json
  -> gateway 请求的响应事实源；通常要和 request JSON 一起读

memory-resume 或 run(auto resume)
  -> 输出 Recovery Brief，把推荐阅读路径和下一步动作带回父会话
```

日期窗口说明：`--since YYYY-MM-DD` 从当天 00:00 开始；`--until YYYY-MM-DD` 包含当天全天。这样用户按自然日期查跨天交接时，不会漏掉当天白天的 hook snapshot。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/memory_commands.py`，理解用户如何运行 `memory-route` 和 `memory-doctor`。
2. 再看 `agent_py_agent/agent/settings/memory.py`，学习配置如何设置默认值、warning 和安全回退。
3. 再看 `agent_py_agent/agent/memory_store/jsonl.py`，理解 JSONL 事实流水和 LocalStore 索引的区别。
4. 再看 `memory_routing/models.py`，认识 route、match、required/candidate path 和 read receipt 的数据形状。
5. 再看 `memory_routing/matcher.py`，理解关键词和别名如何命中规则。
6. 再看 `memory_routing/loader.py`、`validator.py` 和 `context.py`，理解人工索引怎么读入、怎么校验冲突和死链、authority 文件怎么安全注入。
7. 再看 `memory_archive/models.py`、`storage.py`、`runtime.py` 和 `snapshots.py`，理解归档保存什么、怎么写入、怎么验收、压缩前 hook 为什么必须先成功。
8. 再看 `memory_archive/query.py`、`resume_brief.py` 和 `resume_context.py`，理解“继续任务”时怎么找回线索。
9. 再看 `memory_archive/tokens.py` 和 `agent_core/runtime_mixin.py`，理解 session token 账本和压缩触发点。
10. 再看 `agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources` 和 `test_memory_runtime.py::test_auto_resume_context_recovers_cross_day_handoff_task`，理解 subagent 跨天恢复如何从线索回到事实源。
11. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py`，理解真实 gateway 请求和 parent/subagent runner 结果如何通过跨天恢复回到文件事实源。
12. 最后看 `agent_py_agent/tests/test_memory_*.py` 和 `test_memory_first_loop.py`，用测试反推每一层必须保证的行为。

## 当前第一版索引 / 待补齐

本页先讲主结构和阅读路径。后续需要补真实 route index 样例、raw archive 样例、doctor 输出样例、LocalStore 命中样例，以及更长时间的真实跨午夜恢复链路图。
