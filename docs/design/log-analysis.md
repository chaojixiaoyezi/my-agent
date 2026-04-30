# Log Analysis 模块设计与 worker 切片

状态：部分落地，CLI / tool registry / structured query plan 已复验

相关文档：
- `LOG_ANALYSIS_BACKLOG.md`
- `ACCEPTANCE.md`
- `EVIDENCE.md`
- `CODEBASE_TREE.md`

## 当前定位

日志分析模块不是让大模型直接吞大日志，而是把全量日志处理、软检测、case 生成、证据查询、analyst 子代理和 reviewer 验收拆开。

第一版目标：
- 本地接入 SecurityAlertV1 CSV / JSONL。
- 写入本地轻量 store。
- 支持受控 query / hunt / trace。
- 软检测器产出 Finding。
- Finding 合并成 Case。
- 生成 route draft、first response report、forensic package。
- analyst / reviewer 合同只传小对象和 evidence refs。

## 已落地

- `agent_py_agent/agent/log_analysis/models.py`：Source、Event、Finding、Case、EvidenceRef 等 DTO。
- `config.py` / `log_analysis_config.yaml`：独立配置，默认关闭高影响能力。
- `parsers/` / `ingest/`：SecurityAlertV1 CSV/JSONL 解析、checkpoint、dedup、dead letter。
- `storage/` / `tools.py`：本地 JSONL store、受控 query、hunt、trace。
- `cli/logs.py`：`my-agent logs status/ingest/query/hunt-ip/trace-case`。
- `tooling/registry.py`：安全查询工具默认隐藏，按授权或 `logs/security` capability 暴露。
- `analytics/` / `cases/` / `security/`：软检测器、case merge、route draft。
- `agents/` / `dispatch/`：analyst/reviewer 合同和本地 dispatch 队列。
- `reports.py`：第一响应报告和取证包内容渲染。
- `validation/security_fixtures/`：最小安全日志 fixture。
- `agent_py_agent/tests/test_log_analysis_*.py`：LOG 专项、CLI 和工具注册测试。

## 父验收结果

第一版已提交：
- `97b8bc4 Add log analysis foundation`

初验发现的问题已经修复：
- ingest 默认写入路径和 query 默认读取路径断裂。
- storage JSONL 坏行导致整体查询崩溃。
- query evidence 写入不受 limit 控制。
- EvidenceRef dataclass 被 analyst / dispatch 摘要转成不可用字符串。
- dispatch public protocol 与实际 engine 不一致。
- identity 类 case dedup 未纳入 user。
- detector 缺时间戳时错误放行时间窗口关联。
- route/report 批量 findings 未按 case refs 过滤。

复验结果：
- LOG 专项：`33 passed`。
- 全量测试：`164 passed`。
- 手工闭环：`ingest_file` 写入 3 条，`security_query` 查回 3 条。

第二轮复验：
- LOG CLI / tools / dispatch / detector / model 组合测试：`52 passed`。
- 全量测试：`172 passed`。
- 手工 CLI 闭环：`logs ingest` 写入 3 条，`logs query` 查回 3 条。

## 仍未完成

这些不是第一版阻断项，但会决定模块能不能进入可用体验：
- `logs/security` 场景还没有自动把 `granted_capabilities` 传进普通 run/chat runtime。
- `query_default_limit` / `query_max_limit` / `data_dir` 已贯穿 CLI 最小链路，但 storage 层仍有 `MAX_QUERY_LIMIT=500` 硬上限。
- 坏 JSONL 行只跳过，没有 corrupt-line audit / metric。
- detector gaps 已有 `gap_details`，但 reviewer 还没有自动判定 gap 是否关闭。
- 还没有 Live Lab / scenario replay，把 SecurityAlertV1 fixture 跑成可见第一响应。
- README 里还没有面向普通用户的 logs 快速开始。

## 下一批 worker 切片

### Worker 1：CLI 与配置贯通

状态（2026-04-30）：已完成。新增 `logs status/ingest/query/hunt-ip/trace-case`，CLI 最小接入 `data_dir`、`query_default_limit`、`query_max_limit`，CLI_REFERENCE 已更新。

写入范围：
- `agent_py_agent/cli/`
- `agent_py_agent/agent/log_analysis/config.py`
- `agent_py_agent/agent/log_analysis/doctor.py`
- `agent_py_agent/config/log_analysis_config.yaml`
- `agent_py_agent/tests/test_log_analysis_*.py`
- `CLI_REFERENCE.md`

任务：
- 增加 `my-agent logs status`。
- 增加 `my-agent logs ingest <file>`。
- 增加 `my-agent logs query` / `hunt-ip` / `trace-case` 的最小命令。
- 接入 `data_dir`、`query_default_limit`、`query_max_limit`。
- 默认 disabled 时只显示启用提示，不启动 worker、不注入安全 prompt。

验收：
- 普通 `my-agent status/run/chat` 行为不变。
- `my-agent logs status` 能显示 disabled / config warnings。
- ingest 后 query 能查回 fixture。

### Worker 2：Tool Registry 与安全 prompt profile

状态（2026-04-30）：已完成。LOG 安全工具已注册进 ToolRegistry，但默认隐藏；`logs/security` capability 或显式 allowed tool 才暴露；analyst prompt 已统一为 `security_query` / `security_hunt_ip` / `security_trace_case`。

写入范围：
- `agent_py_agent/agent/tooling/`
- `agent_py_agent/agent/log_analysis/tools.py`
- `agent_py_agent/agent/log_analysis/agents/prompts.py`
- `agent_py_agent/agent/log_analysis/capabilities.py`
- `agent_py_agent/tests/test_tools.py`
- `TEST_CHECKLIST.md`

任务：
- 把 `security_query`、`security_hunt_ip`、`security_trace_case` 包成主循环可授权工具。
- 安全工具默认不进入普通 prompt。
- 只有 logs/security 场景或显式 capability grant 才暴露。
- 统一 analyst prompt 里的工具名，不再混用 `traffic_query` 和 `security_query`。

验收：
- 默认工具目录不暴露安全工具。
- security/log task 命中时只暴露必要工具。
- 未授权调用失败且可解释。

### Worker 3：结构化 Query Plan

写入范围：
- `agent_py_agent/agent/log_analysis/models.py`
- `agent_py_agent/agent/log_analysis/analytics/detectors.py`
- `agent_py_agent/agent/log_analysis/security/correlation.py`
- `agent_py_agent/agent/log_analysis/reports.py`
- `agent_py_agent/tests/test_log_analysis_detectors.py`

状态（2026-04-30）：已完成。`Finding.next_queries` 支持最小结构化 query plan，同时兼容旧字符串；detector gaps 增加 `attributes["gap_details"]`，report 会把结构化 plan 渲染成人类可读文本。

残留风险：已由父会话集成修补，`CaseRecord.next_queries` 现在保留结构化 dict。

任务：
- 把 `Finding.next_queries` 从纯字符串升级为兼容结构化 query plan。
- 最小字段：`purpose`、`source_products`、`start_time`、`end_time`、`filters`、`limit`、`evidence_needed`。
- 保留自然语言展示字段，避免破坏报告可读性。
- gaps 增加 `missing_telemetry` / `missing_field` / `time_window` 等可验元数据。

验收：
- detector 输出仍可 JSON round-trip。
- reviewer 能判断 next query 是否可执行。
- report 仍能渲染人类可读文本。

### Worker 4：Storage Audit 与后端边界

写入范围：
- `agent_py_agent/agent/log_analysis/storage/`
- `agent_py_agent/agent/log_analysis/ingest/`
- `agent_py_agent/tests/test_log_analysis_query.py`
- `agent_py_agent/tests/test_log_analysis_ingest.py`

任务：
- 坏 JSONL 行写入 corrupt-line audit。
- query record 记录 skipped/corrupt 计数。
- 明确 JSONL store 是 dev/local 后端，不承诺大数据性能。
- 预留 SQLite/DuckDB/Parquet 后端接口，不急着引入重依赖。

验收：
- 坏行不崩，且可在 audit 中看到。
- query summary 能展示 corrupt/skipped 信息。
- 现有 JSONL 行为不破坏。

### Worker 5：Live Lab / Scenario Replay

写入范围：
- `scripts/live_lab/`
- `agent_py_agent/tests/`
- `validation/security_fixtures/`
- `TESTS.md`

任务：
- 增加 SecurityAlertV1 replay case。
- 跑 ingest -> detector -> case -> route -> report。
- 输出 transcript、evidence path、report path。
- 默认不调用真实 LLM。

验收：
- 一条命令能生成可读第一响应报告。
- 输出路径和证据可追溯。
- 失败时能指出卡在 ingest、detector、case 还是 report。

## 主会话保留裁决

这些不建议直接交给 worker 独立决定：
- 哪些能力默认开启。
- 安全工具何时进入主 prompt。
- 真实响应动作是否允许自动执行。
- 3 分钟目标下速度、证据完整性和误报之间的取舍。
- 是否引入 DuckDB / Parquet / Kafka / ML 依赖。
## 2026-04-30 Implementation Note: Storage Audit

- Status: landed in code and focused tests.
- Solves: bad JSONL lines are no longer silently skipped with no trail. Reads now produce a `JsonlReadAudit`, persist corrupt/non-object samples to `corrupt_lines.jsonl`, and include skipped/corrupt counts in query summaries and query records.
- Parent acceptance: this proves the local JSONL backend is still tolerant of bad rows, while giving reviewers evidence about what was skipped.
- Remaining after this slice: runtime capability auto-wiring, storage limit config alignment, Live Lab scenario replay, and user-facing quickstart docs.

## 2026-04-30 Implementation Note: Runtime Capability, Limits, and Replay

- Status: landed in code and full regression.
- Runtime capability solves: obvious security-log analysis prompts now auto-grant `logs/security` for ordinary `SimpleAgent.run()` calls, while normal tasks still keep security tools hidden.
- Runtime capability guardrail: explicit grants still work; implicit grants are limited to explicit/obvious security-log phrasing and tested for both English and Chinese prompts.
- Query-limit solves: CLI commands now pass configured `query_max_limit` into the storage query path, removing the mismatch between config defaults and storage's old hard-coded cap.
- Live Lab replay solves: `scripts/live_lab/log_analysis_replay.py` can run the SecurityAlertV1 fixture through ingest, detector, case, route, evidence, report, and forensic-package generation without a real model call.
- Dry-run note: the replay uses a local offline store and does not call a real LLM; on a fresh output root it reports `dry_run=true`, `total_events=3`, and `stored_events=3`.
- Remaining after this slice: add richer fixtures, wire real analyst subagent dispatch, and publish a user-facing quickstart once runtime routing is visible from chat/gateway.
