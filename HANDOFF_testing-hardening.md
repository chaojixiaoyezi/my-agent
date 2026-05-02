# Workstream Handoff

## 基本信息

- workstream: testing-hardening
- branch: (main, no new branch created per instructions)
- worktree: (using main working directory per instructions)
- owner: Claude Code (minimax-m2.7)
- date: 2026-05-02

## 本线目标

为 `my-agent` 项目新增两个坏天气场景测试，补强 testing-hardening：

1. **gateway-processing-stop**：验证 gateway 正在处理请求时（worker 已领任务、正在调模型）主动 stop/restart 不卡死、不丢请求、不留半截 JSON、重启后能正确恢复。
2. **real-model-recovery-multi-round**：用真实外部模型跑 parent/subagent runner，至少 2 轮工具调用（read_file + search_text），验证 memory-resume 恢复上下文包含真实模型响应摘要、每轮工具调用 evidence、任务目录 output.json。

## 实际完成

### gateway-processing-stop（新增场景）
- 在 `gateway_cases.py` 新增 `run_scenario_gateway_processing_stop_case()` 函数
- 模拟：投递请求 → worker 领任务并进入 processing 状态（带 lease）→ 模拟 gateway stop/restart（调用 requeue_gateway_processing_requests）→ 新 worker 重拾请求完成
- 验证：requeued==1、processing 清空、pending 有请求、最终 done、response JSON 有效

### real-model-recovery-multi-round（新增场景）
- 在 `real_model_recovery_case.py` 新增 `ScenarioRealModelMultiRoundBackend` wrapper 类和 `run_scenario_real_model_recovery_multi_round_case()` 函数
- 3 轮 generate：第 1 轮真实模型 + 返回 read_file，第 2 轮真实模型 + 返回 search_text，第 3 轮真实模型响应摘要包装成 AWAITING_ACCEPTANCE
- 验证：tool_rounds>=2、used_tools 含 read_file+search_text、tool_sequence 含两者、output.json 可解析

### 注册与文档
- `scenario_cases/__init__.py`：新增两个 case 的 import 和 `__all__` 更新
- `scenario.py`：新增 case 路由（`gateway-processing-stop`、`real-model-recovery-multi-round`）
- `CLI_REFERENCE.md`：在 scenario-test 小节新增两个 case 条目说明和 `all` suite 更新

## 改动文件

| 文件 | 改动类型 |
| --- | --- |
| `agent_py_agent/cli/scenario_cases/gateway_cases.py` | 新增 `run_scenario_gateway_processing_stop_case()` |
| `agent_py_agent/cli/scenario_cases/real_model_recovery_case.py` | 新增 `ScenarioRealModelMultiRoundBackend` 和 `run_scenario_real_model_recovery_multi_round_case()`；新增 `from pathlib import Path` import |
| `agent_py_agent/cli/scenario_cases/__init__.py` | 新增两个 case 的 re-export 和 `__all__` 更新 |
| `agent_py_agent/cli/scenario.py` | 新增两个 case 的 import 和路由分发 |
| `CLI_REFERENCE.md` | 新增 case 说明条目、更新 `all` suite 列表 |

## 测试命令和结果

```bash
# 语法检查（通过）
python3 -m py_compile agent_py_agent/cli/scenario_cases/gateway_cases.py \
  agent_py_agent/cli/scenario_cases/real_model_recovery_case.py \
  agent_py_agent/cli/scenario.py \
  agent_py_agent/cli/scenario_cases/__init__.py
# 输出：SYNTAX_OK

# 场景测试（由于环境问题 ModuleNotFoundError: agent_py_agent.agent.log_analysis.bounded_query
# 无法直接运行，需要先修复环境依赖问题）
python3 -m agent_py_agent scenario-test --case gateway-processing-stop --workspace /tmp/test-scenarios
python3 -m agent_py_agent scenario-test --case real-model-recovery-multi-round --workspace /tmp/test-scenarios
```

结果：语法检查通过。运行时因 `log_analysis` 模块缺失 `bounded_query` 而报 ModuleNotFoundError，此为环境问题，非代码问题。

## 影响范围

- scenario case 新增：不影响已有功能，只新增测试入口
- CLI_REFERENCE.md 文档更新：纯文档变动
- 已有测试套件：原有 `run_tests.py` 中的 scenario-test 命令列表需要新增两个 case 才能覆盖新场景

## 需要主线重点复查

1. **环境依赖问题**：`agent_py_agent/agent/log_analysis/__init__.py` 第 32 行引用了不存在的 `bounded_query` 模块，导致整个 agent 包无法导入。需要确认 `bounded_query` 是否应该存在或应该从 import 中移除。
2. **真实 API 调用**：两个新 case 都涉及真实 API 调用（`real-model-recovery-multi-round` 必须真实调用），测试时需要确保 API 配置正确。
3. **多轮工具调用时序**：`ScenarioRealModelMultiRoundBackend` 假设 exactly 3 轮 generate（read_file → search_text → AWAITING_ACCEPTANCE），如果模型行为不符合预期，可能需要调整。

## 需要其他线协调

- `log_analysis.bounded_query` 模块缺失问题（可能属于 memory 或 log-analysis 工作流范畴）

## 剩余风险

1. **环境依赖**：缺少 `bounded_query` 模块导致无法运行完整测试，需要环境修复后验证
2. **真实 API 依赖**：`real-model-recovery-multi-round` 需要真实 API key 配置才能运行成功
3. **时序假设**：多轮工具调用的 round 数量假设可能需要根据实际模型行为调整

## 后续建议

1. 优先修复 `bounded_query` 模块问题（或从 `log_analysis/__init__.py` 移除该 import）
2. 环境修复后运行完整测试验证
3. 考虑将新 case 加入 `run_tests.py` 的 scenario-test 测试列表
