# Live Lab：开发推进记录

## 已完成

- `scripts/live_agent_lab.py` 已作为 Live Lab 薄入口。
- `scripts/live_lab/` 已拆出 CLI、runner、cases、constants 和 log-analysis replay。
- log-analysis replay 可离线跑 SecurityAlertV1 fixture，产出 case、route、evidence、first response report、forensic package 和 replay summary。
- replay 已有 no-finding negative fixture，以及 detector/evidence/report stage 失败模拟测试。
- `TESTS.md` 已记录 Live Lab 和 offline replay 的运行入口。

## 解决的问题

- 把“真实链路能不能跑”从临时手工命令变成可重复 suite。
- LOG replay 不依赖真实 LLM，也能验证 ingest、detector、case、route、evidence、report 的闭环。
- 失败 stage、error_type、error_message 等字段让父会话和 reviewer 能知道哪里坏了。
- Live Lab 产物目录让验收不只看终端输出，还能检查真实文件。

## 下一步

- 增加更多 suite：gateway ask 长链路、subagent runner 长链路、memory 恢复链路。
- 给每个 suite 建固定输出 schema 和人类可读 summary。
- 把 Live Lab 结果接入 acceptance/evidence 记录模板。
- 如果新增 case 或改变产物路径，同步更新本文件和 `04-structure.md`。

## 已跑测试

- 历史记录显示 `agent_py_agent/tests/test_live_lab_log_analysis_replay.py` 已覆盖正向 replay、no-finding、evidence/report failure gates。
- 历史验收记录显示 `python scripts\live_agent_lab.py --suite log-analysis ...` 曾返回 `LIVE_LAB_PASS`。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。

## 未跑测试

- 本文档第一版没有重新启动可见 Terminal 或真实 API Live Lab。
- 暂未新增 gateway/memory/subagent 的 Live Lab suite。

## 风险

- Live Lab 很容易被误解成普通单测；它更像“可见演练”，成本和依赖可能更高。
- 当前 log-analysis replay 较成熟，其它模块 suite 还需要补。
- 如果 suite 输出路径变化但文档没同步，新手会按旧路径找不到产物。
## 2026-05-06 code-size cleanup
- 中文说明：这一轮只拆 Live Lab 代码结构，不改变 replay 行为。重点是把 log-analysis replay 的 setup 和 stage 执行拆小，降低代码体积风险，后续加更多真实演练 case 会更好维护。
- Split log-analysis replay setup and stage execution into smaller parameterized helpers.
- Kept live-lab replay behavior stable while reducing script-level near-soft code-size risk.

## 2026-05-07 LLM annotation coverage update
- 中文说明：这一轮是注释/可维护性补强，不改 Live Lab 的 suite 行为和产物路径。以后改 `scripts/live_lab/` 的流程、artifact 路径或执行副作用时，要同步更新 `LLM:` 和 `函数用途:` / `类用途:` 注释。
- `scripts/live_lab/` and related governance scripts now follow the same definition-level double-layer comments rule as product code: every module/class/function/method has `LLM:` plus `函数用途:` / `类用途:`.
- This is a documentation/maintainability pass only; Live Lab suite behavior and artifact paths are intended to stay unchanged.
- Added a code-size script regression test that scans non-test, non-runtime Python files so future changes cannot silently miss annotation coverage.

## 2026-05-17 Live Lab case surface compatibility
- 中文说明：第 8 步真实模型 Live Lab 里，`gateway_ask` 的真实 LLM 调用已经成功，但 case 写 response 文件时发现 `_LabInterface` 没有转发 `responses_dir`。这是测试台兼容层问题，不是模型或 gateway 本身失败。
- 已实现：`LiveLab` / `_LabInterface` 暴露 `run_root`、`prompts_dir`、`responses_dir`、`summary_path`，让旧 case surface 和拆分后的 runner/session 目录合同一致。
- 已测试：新增 `test_live_lab_runner_interface.py` 覆盖 case workspace 目录转发；后续真实 Live Lab 可继续把 prompt、response、summary 写到隔离 run 目录。
- 真实复测：`python3 scripts/live_agent_lab.py --suite real --real-llm --run-id step8-real-qa-refs-r4 --timeout 300 --count 2 --max-runners 2 --max-cycles 4` -> `LIVE_LAB_PASS`。`health`、`gateway_ask` 和 `long_subagent` 都通过，真实 MiniMax-M2.7 调用能创建两个 worker、分两轮 dispatch、写出 2 个 scenario output 文件并完成验收。

## 2026-05-17 Real suite path/backend hardening
- 中文说明：第 8 步真实模型复测先后暴露两个底层问题：`.my_agent/subagents/<run>` 私有目录里的 runner 读取项目根 `README.md` 时会被误判缺输入；Anthropic-compatible 流式接口偶发只返回空文本时会让整轮 gateway ask 失败。
- 已实现：新增共享 `workspace_roots.py`，让 runner input gate 和 artifact integrity 都能从 `.my_agent/subagents/<run>`、`.my-agent/subagents/<run>`、`data/subagents/<run>` 受控推导项目工作区根；`AGENTS.md（如有）` 这类可选读取不再阻塞 runner。
- 已实现：`AnthropicCompatibleBackend` 在两次流式空文本后，会在后端边界做一次非流式 `/v1/messages` 兜底；如果兜底成功，仍按同一 `ModelResponse` 返回，不把 provider 抖动泄漏给上层调度。
- 已测试：新增 runner input dependency、workspace artifact roots、backend stream fallback 和 Live Lab interface 回归；真实 `step8-real-qa-refs-r4` 已跑通。
- 下一步：把同样的 real suite 扩到更长的自然语言家具/购物网站 E2E，并继续和 OpenClaw/Hermes/Codex 的结构化 refs-first 思路对照。
