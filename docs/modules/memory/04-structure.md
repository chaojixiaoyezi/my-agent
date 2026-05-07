# Memory：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- memory.py                         # 旧兼容入口，真实存储已拆到 memory_store/
|-- memory_settings.py                # 旧兼容入口，真实配置解析在 settings/memory.py
|-- settings/memory.py                # memory 配置、默认值、warning、安全归一化和参数边界
|-- memory_store/                     # 长期记忆 JSONL 事实流水，可选同步索引到 LocalStore
|-- memory_routing/                   # route index、匹配、required/candidate path、read receipt
`-- memory_archive/                   # hook snapshot、raw archive、留存、token 估算、compact 预演
	    |-- agent_run_workspace.py         # Phase 1 task-local agent run workspace 骨架
	    |-- artifact_registry.py           # Phase 3 artifact manifest summary/hash/path 规范
		    |-- compact_chain.py               # Phase 4 checkpoint-first compact chain ledger
		    |-- daily_ledger.py                # Phase 2 daily/YYYY-MM-DD/events.jsonl 事件索引
		    |-- memory_gate_export.py          # Phase 6 显式长期 memory / skill draft 导出
		    |-- memory_gate_retention.py       # Phase 6 保守 retention 计划和 active queue 压缩
		    |-- memory_gate_verifier.py        # Phase 6 no-auto-promotion 边界检查
		    |-- shared_workspace.py            # Phase 5 task-local blackboard/messages/findings/evidence
	    |-- task_workspace.py             # Phase 0 文件系统 task workspace 骨架和旧 subagent run adapter
	    |-- task_workspace_rendering.py   # task workspace YAML/Markdown 小文件渲染 helper
	    `-- query/task_sources.py          # task/run 恢复入口推荐，不把子代理内容写入主 memory

agent_py_agent/cli/
|-- memory_commands.py                # memory-route / memory-doctor 等可见诊断命令
|-- memory_archive_commands.py        # memory-archive-list/search/resume 命令
`-- memory_compact_commands.py        # memory-compact 只读预演命令
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
- `memory_archive/query/task_sources.py`：集中维护 subagent 恢复事实源优先级；checkpoint artifacts 优先，传统 `STATUS.md` / `HANDOFF.md` 继续保留。
- `memory_archive/task_workspace.py`：创建文件系统版 task workspace 的最小骨架，并写 `agents/<run_id>/legacy_run_ref.json` 指向旧 subagent work-order 目录；这是 adapter，不迁移历史目录。
- `memory_archive/task_workspace.py`、`agent_run_workspace.py`、`daily_ledger.py`、`artifact_registry.py`、`compact_chain.py`、`shared_workspace.py`：这些 runtime memory 写入入口统一提供 `*Request` bundle；旧参数形态只作为兼容 adapter，新增字段应进入 bundle，避免跨阶段继续拉长函数签名。
- `memory_archive/task_workspace_rendering.py`：承接 task workspace 的 `task.yaml`、summary 和 blackboard 初始内容渲染，避免同步编排文件继续膨胀。
- `memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 run workspace 骨架，包含 agent 身份、run state、任务说明、checkpoint、summary、final report、findings 和 inbox/outbox/artifacts/compactions 目录。
- `memory_archive/daily_ledger.py`：维护 `daily/YYYY-MM-DD/events.jsonl`，只追加 task/run 状态、摘要、duration、refs、artifact/evidence refs 和检索字段，不存完整上下文或工具输出。
- `memory_archive/artifact_registry.py`：把 `SubAgentTask.artifact_refs` 规范化为 task/run 两份 `artifacts/manifest.jsonl`，记录 ref、resolved path、exists、size、sha256、summary 和 kind，不复制 artifact 正文。
- `memory_archive/compact_chain.py`：在 run `compactions/` 下追加 checkpoint snapshot ledger，写每次 summary/metadata，并把最新 compact refs 回写到 run checkpoint；当前只做恢复链，不删除原始 timeline/artifact。
- `memory_archive/shared_workspace.py`：同步 task-local shared blackboard、status messages、findings 和 evidence packet 文件；这是 sibling 子代理协作面，不写入主代理长期 memory。
- `memory_archive/memory_gate_export.py`：只在显式命令触发时，把 `approve_memory` 候选写入主 JSONL memory，或把 `approve_skill` 候选写成 run-local skill draft；不会自动安装正式 skill。
- `memory_archive/memory_gate_retention.py`：生成 retention 计划，apply 时只从 active review queue 移除 closed 候选，候选、decision 和 export 审计日志继续保留。
- `memory_archive/memory_gate_verifier.py`：写 `verifier_report.json`，检查 decision/export 是否仍保持 `auto_promote=false` 和显式提升边界。
- `docs/modules/memory/06-runtime-memory-requirements.md`：定义 memory 作为运行时档案系统的开发要求，明确主代理长期记忆、每日账本、task workspace、agent run workspace、artifact、checkpoint、compact 和 retention 的边界。
- `memory_archive/resume_brief.py`：把归档、LocalStore、任务事实源压成恢复简报。
- `memory_archive/resume_context.py`：在“继续/恢复”类提示里按配置构造自动注入的恢复上下文。
- `memory_archive/tokens.py`：为归档预算提供保守 token 估算，并维护 session 级 token 账本。
- `memory_archive/compact.py`：构建只读 compact plan，汇总 raw/hook、权威 snapshot、token ledger、风险和建议动作。
- `cli/memory_commands.py`：给用户和开发者看 route/doctor 结果。
- `cli/memory_archive_commands.py`：给用户查看归档列表、搜索归档和生成恢复简报。
- `cli/memory_compact_commands.py`：把 compact plan 暴露为 `memory-compact --dry-run`，当前不会应用真实压缩。

## 数据流

1. 用户对话或命令触发记忆写入，基础事实先落到 JSONL。
2. LocalStore 可以为旧 memory 补建索引，让搜索和 timeline 能看到它。
3. 当新任务需要规则时，memory routing 根据 query 匹配 route index。
4. 匹配到的 authority path 会被安全读取成上下文片段。
5. token 预算逼近阈值时，run 主链路先写 `memory_archive/snapshots/*.json` 权威快照，再做组合压缩。
6. 长任务和普通保存路径都会继续写 raw event / hook snapshot，方便恢复和审计。
7. raw event、hook snapshot 和权威快照写完后都会读回校验，确保恢复线索真实落盘。
8. subagent 保存时会同步 `tasks/<root_id>/` 的 `state.json`、`timeline.jsonl`、`summaries/current_summary.md` 和 legacy run adapter；旧 `subagents/<run_id>/` 仍是当前兼容事实源。
9. 同一保存流程会同步 `tasks/<root_id>/agents/<run_id>/` 的 agent run workspace skeleton，先写恢复和接管需要的最小 run 文件，不搬迁旧工单目录。
10. 同一保存流程会追加 `daily/YYYY-MM-DD/events.jsonl`，作为主代理按天查 task/run/event/artifact refs 的轻量索引。
11. 同一保存流程会写 task/run artifact manifest，并让 daily ledger refs 指向 manifest；需要正文时再读 artifact 文件本身。
12. 同一保存流程会追加 run `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并让 run `checkpoint.json` 指向最新 compact refs；这不是删除上下文的 compact apply。
13. 同一保存流程会同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享任务局部 facts；这仍然不是主 memory 写入。
14. `subagents-memory-gate` 默认只列出候选或写 `decisions.jsonl`；只有显式 `--export-memory` 才写主 JSONL memory，只有显式 `--export-skill` 才写 skill draft，`--retention-apply` 也只压缩 active queue，不删除审计日志。
15. 用户说“继续/恢复”时，resume context 可以按配置从 archive、LocalStore、daily ledger 和任务事实源生成恢复块；跨天时会同时扫描最近 raw/hook 文件。subagent 任务会先推荐 `reports/checkpoint.json`、`status_report.json`、`progress.md` 等 compact recovery artifacts，再推荐 `STATUS.md`、`HANDOFF.md` 和 `output.json`。这只是恢复入口推荐，不代表把子代理内容写入主代理长期 memory。
16. doctor 命令检查配置、route index、hook/raw/snapshot 目录和层级一致性 warning。
17. `memory-compact --dry-run` 在真实压缩前只读扫描上述事实源，输出计划和风险，不修改文件。
18. runtime memory 的跨模块写入入口先把 CLI/manager 参数收敛成 `*Request` / `*Options` bundle，再进入具体 service；这保证后续 memory gate、compact chain、artifact refs、shared workspace 继续扩展时，不影响既有调用方。

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
  -> reports/checkpoint.json / status_report.json / progress.md 是 compact-first 恢复入口
  -> STATUS.md / WORK_LOG.md / HANDOFF.md / ACCEPTANCE.md / TEST_CHECKLIST.md 是最终核对事实源

gateway/requests/done/<request_id>.json 或 gateway/requests/failed/<request_id>.json
  -> gateway 请求的终态请求事实源；如果 LocalStore 里还是 processing 路径，resume 会尽量纠偏到这里

gateway/responses/<request_id>.json
  -> gateway 请求的响应事实源；通常要和 request JSON 一起读

memory-resume 或 run(auto resume)
  -> 输出 Recovery Brief，把推荐阅读路径和下一步动作带回父会话
```

## Runtime Memory 目标结构

长期目标以 `06-runtime-memory-requirements.md` 为准：

```text
主代理 = 长期记忆 + 每日事件账本 + 全局索引
任务 = task workspace + state + timeline + summaries + shared + artifacts
子代理 = task 内 agent run workspace + checkpoint + compact chain + findings
共享 = blackboard + messages + evidence packets + artifact refs
```

当前 Phase 0/1/2/3/4/5/6 已创建 task workspace 外壳、agent run workspace 外壳、daily event ledger、artifact manifest、checkpoint-first compact chain、shared 协作面和 run-local memory gate。`tasks/<root_id>/agents/<run_id>/legacy_run_ref.json` 会继续指向旧 run 目录；`memory_gate/` 保存 review 候选、decision log、export log、retention report 和 verifier report。只有显式 export 命令才会写主代理长期记忆或生成 skill draft。

LocalStore / sqlite / 搜索索引只帮助定位事实源，不替代 task/run 目录里的权威文件。

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
10. 再看 `agent_py_agent/agent/memory_archive/task_workspace.py` 和相邻的 runtime sync 模块，理解 `*Request` bundle 如何把 task/run/artifact/checkpoint/shared refs 打包传递，同时保留旧 subagent work-order 兼容路径。
11. 再看 `agent_py_agent/agent/memory_archive/memory_gate.py`、`memory_gate_retention.py`、`memory_gate_export.py`、`memory_gate_verifier.py` 和 `agent_py_agent/agent/subagents/manager_memory_gate.py`，理解 lesson/finding 为什么先进入 review queue，以及 reviewer decision 为什么仍然不等于正式提升。
12. 再看 `agent_py_agent/cli/_memory_gate.py`，理解 `subagents-memory-gate` 如何显式写回 decision、导出 approved 候选、生成 skill draft 和跑边界检查。
13. 再看 `agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources` 和 `test_memory_runtime.py::test_auto_resume_context_recovers_cross_day_handoff_task`，理解 subagent 跨天恢复如何从线索回到事实源。
14. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py`，理解真实 gateway 请求和 parent/subagent runner 结果如何通过跨天恢复回到文件事实源。
15. 最后看 `agent_py_agent/tests/test_memory_*.py` 和 `test_memory_first_loop.py`，用测试反推每一层必须保证的行为。

## 当前第一版索引 / 待补齐

本页先讲主结构和阅读路径。后续需要补真实 route index 样例、raw archive 样例、doctor 输出样例、LocalStore 命中样例，以及更长时间的真实跨午夜恢复链路图。
## 2026-05-06 structure update
- Memory archive query logic now delegates filtering, task payload construction, gateway payload collection, and resume guidance rendering to focused helpers.
- Memory routing matcher internals separate scoring specs, score accumulation, and receipt creation while keeping public model outputs stable.

## 2026-05-07 bundle structure update
- `memory_archive/query/query_logic.py` now exposes explicit archive collection/filter fields, and higher-level query service code converts compatibility inputs before filtering.
- `resume_brief.py`, snapshot helpers, and memory route read receipts now use explicit Params/Options-style fields rather than var-keyword option bags.
- Memory remains a fact-source locator: bundle cleanup does not change the authority model where task/run/gateway files stay authoritative and LocalStore/archive records are clues.
