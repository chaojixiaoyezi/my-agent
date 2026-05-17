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

## 2026-05-17 Natural suite 家具 HTML canary
- 中文说明：新增 `natural` suite，用真实用户风格提示词验证主代理能不能安排小傻妞完成一个网页产物任务。提示词不写 `dispatch`、`runner`、`contract`，避免测试变成背内部术语。
- 已实现：`natural_html_subagent` case 会启动真实 gateway、发送家具品牌单文件 HTML 任务、保存 response，并检查 `lab_outputs/furniture-home/index.html` 真实存在、HTML 基础标签完整、没有 `href="#"`、没有 disabled 按钮、没有外部图片/字体/脚本/CSS 背景资源依赖。
- 已实现：`natural` suite 是 opt-in；只有显式 `--suite natural --real-llm` 才会跑，普通 smoke 不会消耗真实模型 API。
- 已测试：`python3 -m pytest -q agent_py_agent/tests/test_live_lab_natural_case.py agent_py_agent/tests/test_live_lab_runner_interface.py` -> `5 passed`。
- 真实复测：`python3 scripts/live_agent_lab.py --suite natural --real-llm --runs-dir /Users/xiaoyezi/my-claude-code/real_e2e_next --run-id 20260517-natural-html-01 --timeout 360 --count 2 --max-runners 2 --max-cycles 3 --keep-going` -> `LIVE_LAB_PASS`。MiniMax-M2.7 通过主代理创建 1 个小傻妞，写出约 50KB HTML，并完成父级验收。
- 后续风险：第一次真实跑出来的 HTML 使用了 Google Fonts 和 Unsplash 图片，说明模型会自然引入外部资源；现在 canary 已收紧为离线单文件资源策略，下一轮真实复测要验证模型是否能按这个策略生成页面。

## 2026-05-17 Natural suite 工具轮数上限修正
- 中文说明：离线资源策略复测 `20260517-natural-html-02-offline-assets` 暴露 Live Lab 自己的隔离配置太紧：`max_tool_rounds: 8` 会把稍长的单文件 HTML 截断，导致文件缺 `</body></html>`。父级没有误报成功，Live Lab 也正确失败。
- 已实现：Live Lab 隔离配置默认写 `max_tool_rounds: 0`，表示不限制工具轮数；真实长任务不应该被测试台人为截断，除非某个 stress case 显式覆盖。
- 已测试：新增 `test_live_lab_config_keeps_tool_rounds_unlimited`，确认隔离配置会写入不限制工具轮数。
- 真实复测：`20260517-natural-html-03-unlimited-rounds` 暴露 artifact integrity 路径解析仍会把相对产物根拼到子代理私有目录；`20260517-natural-html-04-path-root-fix` 暴露最终 closeout 会把仍在 `PLANNING` 的同目标 sibling 算作完成。

## 2026-05-17 Natural suite 状态门和路径合同修正
- 中文说明：自然语言 E2E 现在不只看 HTML 文件本身，还会看主代理最终回复和磁盘 `task.json` 是否一致。这样能防止“文件能打开，但子代理控制面其实还有阻塞”的假绿。
- 已实现：`case_natural_html_subagent()` 增加 `_assert_no_subagent_state_blockers()` 和 `_assert_persisted_subagent_state_clean()`；如果回复里有 `blocking_run_ids`，或持久化任务仍有未解决 run，Live Lab 会失败。
- 已实现：artifact integrity 相对 `product_write_roots` 按项目工作区根解析；`输出路径/保存路径/产物文件` 这类自然语言写目标不会再被误判成输入依赖。
- 已测试：新增/更新 `test_live_lab_natural_case.py`、`test_subagent_finalize_helpers.py`、`test_runner_input_dependencies.py` 和 `test_orchestration_dispatch_completion_gate.py` 覆盖这些边界。
- 真实复测：`python3 scripts/live_agent_lab.py --suite natural --real-llm --runs-dir /Users/xiaoyezi/my-claude-code/real_e2e_next --run-id 20260517-natural-html-05-input-output-contract --timeout 420 --count 2 --max-runners 2 --max-cycles 3 --keep-going` -> `LIVE_LAB_PASS`。这轮 root 只调用 `create_subagents` 和 `dispatch_subagents`，没有亲自 `write_file`；只创建 1 个小傻妞，最终 `DONE/VERIFIED`，HTML 产物约 16KB，无外部资源、无空链接、无 disabled。
