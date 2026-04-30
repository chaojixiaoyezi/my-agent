# ACCEPTANCE

## 第一轮验收
- [x] Python3 可运行
- [x] CLI 支持
- [x] JSONL 记忆功能
- [x] 参数外置且中文说明
- [x] subagent 思维/计划/结果记录
- [x] 动态 prompt 注入
- [x] 工程分层、可扩展 backend
- [x] 暂无 Web，但预留 extensions 边界
- [x] 真实入口和错误入口测试通过

## 仍可迭代
- [ ] 接真实模型后端（OpenAI / 本地 HTTP / llama.cpp）
- [ ] 工具系统
- [ ] 更强记忆检索（向量/RAG）
- [ ] subagent 执行器与验收器
- [ ] 插件机制

## 2026-04-30 / 日志分析第一版父会话验收

阶段提交：
- `97b8bc4 Add log analysis foundation`

已通过：
- [x] `python -m pytest`：初验 155 passed，修复后复验 164 passed。
- [x] LOG CLI / tool registry / structured query plan 接入后，`python -m pytest` 复验 172 passed。
- [x] LOG 模块 `py_compile` 通过。
- [x] `SecurityAlertV1` ingest / query / dispatch / detector 专项测试通过，修复后 33 passed。
- [x] LOG CLI / tools / dispatch / detector / model 组合测试通过，52 passed。
- [x] 手工执行 `my-agent logs status --json`，确认默认 disabled 且不创建重型后台任务。
- [x] 手工执行 `my-agent logs ingest` 后 `my-agent logs query`，3 条 fixture 可查回。
- [x] LOG 模块默认配置为关闭，不影响普通命令。

已修复并复验：
- [x] 默认 `ingest_file(root=...)` 写入路径与 `security_query(root=...)` 读取路径不一致，导致导入成功后默认查询为空。
- [x] storage JSONL 读到坏行时会让整个查询失败，缺少坏行隔离。
- [x] query 的 `limit` 只限制返回，不限制 evidence 写入，受控查询边界不够硬。
- [x] `EvidenceRef` dataclass 在 analyst/dispatch 摘要里可能被转成不可用字符串。
- [x] dispatch public protocol 与实际 engine 方法和 result 结构不一致。
- [x] identity 类 case dedup 没纳入 user，可能把同源 IP、同时间桶的不同账号合并。
- [x] detector 缺时间戳时会把时间窗口判断放行，可能跨任意时间错误关联。
- [x] route/report 接收批量 findings 时缺少按 `case.finding_refs` 过滤。
- [x] LOG 模块没有 CLI 子命令，用户只能 import Python API。
- [x] LOG 安全工具没有接入主工具 registry，且 analyst prompt 仍混用旧 `traffic_*` 名称。
- [x] detector 的 `next_queries` 是自然语言字符串，不是结构化 query plan。

验收未通过项：
- [ ] `query_default_limit/query_max_limit/data_dir` 已贯穿 CLI 最小链路，但 storage 层仍有 `MAX_QUERY_LIMIT=500` 硬上限，与配置默认 `query_max_limit=1000` 存在可见 warning。
- [ ] 坏 JSONL 行目前只跳过，没有独立 corrupt-line audit/metric。
- [ ] `logs/security` 场景如何自动把 `granted_capabilities` 传入普通 run/chat runtime 还未接线；当前 CLI 和显式工具授权可用。
- [ ] 还没有 Live Lab / scenario replay，把 SecurityAlertV1 fixture 跑成可见第一响应报告。

已拆给 worker：
- storage/ingest/query 闭环修复。
- evidence/ref 与 dispatch 合同修复。
- detector 时间关联、case dedup、route/report 过滤修复。
- CLI 与配置贯通。
- Tool registry 与安全 prompt profile。
- 结构化 query plan。

父会话保留：
- 决定 runtime capability 自动接线、storage audit 和 Live Lab replay 的下一批派工。

## 2026-04-30 / Subagent Workflow Phase 1 父会话验收

状态：
- [x] Phase 1 基础骨架已落地：配置开关、模板 store、QualityContract / Context Pack。

已通过：
- [x] `python -m pytest agent_py_agent\tests\test_subagent_workflow_config.py agent_py_agent\tests\test_subagent_workflow_templates.py agent_py_agent\tests\test_subagent_quality_contract.py`：14 passed。
- [x] `python -m pytest agent_py_agent\tests\test_packaging.py agent_py_agent\tests\test_agent.py agent_py_agent\tests\test_tools.py`：63 passed。
- [x] `python -m pytest`：186 passed。
- [x] `git diff --check` 通过。

已落地：
- [x] `subagent_workflow_mode: auto | manual | off` 配置和非法值回退。
- [x] 内置 workflow 模板加载器，支持 JSON 内置模板和用户 JSON 覆盖模板。
- [x] 首批内置模板：`single_worker_verified`、`code_feature_split`、`producer_critic_repair`。
- [x] 模板 schema 要求 `solves`，每个开发项必须说明解决问题。
- [x] `SubAgentTask` / `SubAgentExecutionContext` 接入 `QualityContract`、`ContextManifest` 和 `context_packs`。
- [x] `EXECUTION_CONTEXT.md` 明确子代理不能自判最终完成，父会话 final gate 才能最终验收。

验收未通过项：
- [ ] workflow router 尚未接入 chat / gateway / spawn 入口。
- [ ] workflow compiler 尚未把模板 phase 编译成真实多 worker 派工单。
- [ ] parent acceptance planner 尚未按 workflow 自动生成结构化验收报告。
- [ ] 用户模板第一版只支持 JSON，YAML 仍未支持。
## 2026-04-30 / Subagent Workflow Phase 3 Parent Acceptance

Status:
- [x] Workflow router first slice landed.
- [x] Workflow compiler first slice landed.
- [x] Parent acceptance planner first slice landed.

Problems solved:
- [x] User no longer has to explicitly spell out the first workflow shape for common code/quality tasks.
- [x] Worker prompts can now be compiled from templates with dependencies, write boundaries, evidence requirements, and non-self-acceptance rules.
- [x] Parent acceptance can be planned before worker output arrives, reducing blind trust in worker PASS summaries.
- [x] Chinese task wording is covered for common quality-delivery and log-analysis development routing.

Accepted evidence:
- [x] Subagent workflow focused tests passed: `29 passed`.
- [x] Router/compiler/acceptance APIs exported from `agent_py_agent.agent.subagent_workflows`.
- [x] Full regression passed: `python -m pytest` -> `207 passed`.

Remaining:
- [ ] Wire router/compiler/parent gate into real chat/gateway/spawn dispatch.
- [ ] Persist structured acceptance reports for real runs.
- [ ] Add more built-in templates beyond the first three.

## 2026-04-30 / Log Analysis Storage Audit Parent Acceptance

Status:
- [x] Storage audit first slice landed.

Problems solved:
- [x] Bad JSONL lines are no longer only skipped silently.
- [x] Query summary records corrupt/skipped counts for parent/reviewer inspection.
- [x] A corrupt-line audit JSONL trail records sampled bad rows without breaking existing local-store behavior.
- [x] The previous `log_analysis.tools` import cycle is fixed for focused log-analysis tests.

Accepted evidence:
- [x] Log focused tests passed: `9 passed`.
- [x] Bad JSONL fixture confirms one invalid JSON row and one non-object JSON row are counted and persisted.
- [x] Full regression passed: `python -m pytest` -> `207 passed`.

Remaining:
- [ ] Align storage query max limit with config instead of hard-coded `MAX_QUERY_LIMIT=500`.
- [ ] Wire `logs/security` granted capability into normal run/chat runtime.
- [ ] Build Live Lab scenario replay for first-response reports.

## 2026-04-30 / Runtime Capability and Live Lab Integration Acceptance

Status:
- [x] Runtime `logs/security` capability auto-wiring landed.
- [x] Log query max-limit config alignment landed.
- [x] Offline SecurityAlertV1 Live Lab replay landed.
- [x] Workflow router/compiler/parent-gate dry-run planner landed.

Problems solved:
- [x] Ordinary `run` prompts no longer need the user to explicitly pass `granted_capabilities=["logs/security"]` for obvious security-log analysis tasks.
- [x] Security tools are still hidden for ordinary tasks, so the default tool surface stays quiet and safe.
- [x] Chinese security-log wording is covered by regression tests, not only English trigger phrases.
- [x] CLI query limits now honor `query_max_limit` instead of being silently capped by storage's previous hard-coded limit.
- [x] The log-analysis chain now has a one-command offline replay that produces case, route, evidence, report, and forensic-package artifacts without a real LLM call.
- [x] Workflow planning now has a single dry-run API that composes route -> compile -> parent acceptance before real worker creation.

Accepted evidence:
- [x] Focused integration tests passed: `python -m pytest agent_py_agent\tests\test_runtime_capabilities.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_live_lab_log_analysis_replay.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `23 passed`.
- [x] Offline replay passed: `python scripts\live_lab\log_analysis_replay.py --output-root $env:TEMP\openclaw-log-replay-codex` -> `ok=true`, `total_events=3`, `case_count=1`, `finding_count=1`, all replay stages pass.
- [x] Full regression passed: `python -m pytest` -> `223 passed`.
- [x] Whitespace check passed: `git diff --check`.

Remaining:
- [ ] Wire `plan_workflow_for_goal()` into real chat/gateway/spawn dispatch instead of keeping it as a dry-run planning helper.
- [ ] Add richer log-analysis Live Lab fixtures beyond the minimal SecurityAlertV1 replay.
- [ ] Add user-facing logs quickstart docs once the runtime path is wired into the visible agent workflow.

## 2026-04-30 / Workflow Plan CLI and LOG Replay Gate Acceptance

Status:
- [x] `subagents-workflow-plan` dry-run CLI landed.
- [x] LOG Live Lab replay now has a readable no-finding negative fixture.
- [x] LOG quickstart is documented in README and CLI reference.

Problems solved:
- [x] Users and the parent session can preview automatic workflow routing, worker split, and parent acceptance checks without creating or running subagents.
- [x] The workflow planner is now visible from the CLI, reducing the gap between Python-only planning and real user-facing dispatch.
- [x] LOG replay can now fail at the `detector` stage with a clear negative fixture instead of only proving the happy path.
- [x] Replay summaries now include parsed, stored, duplicate, dead-letter, skipped, error type, and error message fields for parent/reviewer inspection.
- [x] User-facing docs now explain explicit LOG commands, ordinary-language runtime capability behavior, and offline replay expectations.

Accepted evidence:
- [x] Focused tests passed: `python -m pytest agent_py_agent\tests\test_live_lab_log_analysis_replay.py agent_py_agent\tests\test_subagent_workflow_planner.py agent_py_agent\tests\test_cli_reference.py` -> `9 passed`.
- [x] Workflow CLI preview passed: `python -m agent_py_agent subagents-workflow-plan "Fix API bug and add tests" --json` -> selected `code_feature_split`, 3 worker specs, 8 parent acceptance checks.
- [x] Fresh positive replay passed: `python scripts\live_lab\log_analysis_replay.py --output-root <fresh temp>` -> `ok=true`, `parsed_events=3`, `stored_events=3`, all stages pass.
- [x] Live Lab suite passed: `python scripts\live_agent_lab.py --suite log-analysis --runs-dir <temp> --run-id <id>` -> `LIVE_LAB_PASS`.
- [x] Full regression passed: `python -m pytest` -> `226 passed`.
- [x] Whitespace check passed: `git diff --check`.

Remaining:
- [ ] Wire workflow preview into actual task creation with a manual/auto confirmation policy.
- [ ] Persist workflow-plan previews and parent acceptance decisions for real dispatches.
- [ ] Add case/evidence-stage negative replay fixtures after the next LOG dispatch bridge exists.
