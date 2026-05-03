# Workstream Handoff

## 基本信息

- workstream: autonomous-dispatch-advanced
- branch: workstream/autonomous-dispatch-advanced
- worktree: ../my-agent-worktrees/autonomous-dispatch-advanced
- owner: claude-code
- date: 2026-05-02

## 本线目标

实现模型速度感知、动态超时、失败分析和自适应重派，提升子代理系统在高自动化场景下的稳定运行能力。

## 实际完成

- ✅ 模型速度基准测试模块
- ✅ 动态超时计算模块
- ✅ 失败分析器模块
- ✅ 自适应重派模块
- ✅ CLI 命令 bench-model
- ✅ 测试用例（46 passed）

## 改动文件

### 新增文件

- `agent_py_agent/agent/model_speed/`（新目录）
  - `models.py`：SpeedProfile、SpeedSample 数据类
  - `benchmark.py`：run_speed_benchmark() 函数
  - `storage.py`：保存和加载 speed_profile.json
  - `__init__.py`：模块导出

- `agent_py_agent/cli/bench_model.py`：CLI 命令实现

- `agent_py_agent/agent/agent_core/dynamic_timeout.py`：动态超时计算
  - `calculate_dynamic_timeout()`：根据输入大小和速度模型计算超时
  - `estimate_tokens_from_text()`：粗略估算文本 token 数
  - `estimate_task_tokens()`：根据任务描述和计划估算 token 数

- `agent_py_agent/agent/agent_core/failure_analyzer.py`：失败分析器
  - `FailureAnalysis` 数据类
  - `SubAgentFailureAnalyzer` 类
  - `analyze()` 方法及各种分析方法

- `agent_py_agent/agent/agent_core/adaptive_retry.py`：自适应重派
  - `adaptive_retry()`：根据分析结果决定下一步
  - `split_task()`：把大任务拆分成小任务
  - `should_auto_split()`、`estimate_split_count()` 辅助函数

- `agent_py_agent/tests/test_dynamic_timeout.py`：动态超时测试（13 tests）
- `agent_py_agent/tests/test_failure_analyzer.py`：失败分析器测试（15 tests）
- `agent_py_agent/tests/test_adaptive_retry.py`：自适应重派测试（18 tests）

### 修改文件

- `agent_py_agent/config/agent_config.yaml`：
  - 新增 `subagent_automation_level` 配置
  - 新增 `dynamic_timeout_safety_margin` 配置
  - 新增 `dynamic_timeout_min` 配置
  - 新增 `dynamic_timeout_max` 配置
  - 新增 `max_auto_split_depth` 配置
  - 新增 `model_speed_profile_path` 配置
  - 新增 `auto_bench_model_on_first_use` 配置

- `agent_py_agent/agent/subagents/models.py`：
  - 在 `SubAgentTask` 数据类中新增 `attributes: dict[str, object]` 字段

- `agent_py_agent/agent/agent_core/runner_dispatch.py`：
  - 新增导入 `calculate_dynamic_timeout`
  - 支持 `dynamic_timeout_seconds` 属性用于 runner 超时

- `agent_py_agent/agent/agent_core/dispatch_mixin.py`：
  - 新增导入 `adaptive_retry`、`calculate_dynamic_timeout`、`SubAgentFailureAnalyzer`
  - 修改 `dispatch_subagents()` 在 runner 失败后调用失败分析和自适应重派
  - 新增动态超时计算逻辑，在调用 `_run_subagent_worker` 时使用

- `agent_py_agent/cli/parser.py`：
  - 新增 `bench_model` 子命令导入
  - 新增 `bench` 子命令定义（支持 `--show` 参数）

- `agent_py_agent/__main__.py`：
  - 新增 `cmd_bench_model` 导出

## 测试命令和结果

```bash
python3 -m pytest my-agent/agent_py_agent/tests/test_dynamic_timeout.py -v
```

结果：
- 13 passed
- 覆盖：token 估算、任务 token 估算、无速度模型情况、有速度模型插值、边界测试、自定义参数

```bash
python3 -m pytest my-agent/agent_py_agent/tests/test_failure_analyzer.py -v
```

结果：
- 15 passed
- 覆盖：超时分析（首次/多次）、能力缺口（缺失/不足）、解析错误（可重试/持久）、工具失败、模型错误、通道损坏、验收失败、通用失败、拆分建议生成

```bash
python3 -m pytest my-agent/agent_py_agent/tests/test_adaptive_retry.py -v
```

结果：
- 18 passed
- 覆盖：自适应重派（无操作/调整超时/拆分/简单重试）、任务拆分（两部分/继承 plan/单建议/继承属性）、自动拆分判断、拆分数量估算

```bash
python3 -m pytest my-agent/agent_py_agent/tests/test_dynamic_timeout.py my-agent/agent_py_agent/tests/test_failure_analyzer.py my-agent/agent_py_agent/tests/test_adaptive_retry.py -v
```

结果：
- 46 passed

## 影响范围

- 模型速度感知：不影响现有功能，新增独立模块
- 动态超时：默认向后兼容，当没有动态超时时使用配置的静态超时
- 失败分析：仅在 execute_runners=True 时触发，dry-run 模式不受影响
- 自适应重派：仅在 execute_runners=True 时触发，dry-run 模式不受影响
- SubAgentTask.attributes：新增字典字段，不破坏现有数据结构

## 需要主线重点复查

- **动态超时计算**：`calculate_dynamic_timeout()` 函数的插值逻辑是否合理，是否需要更精细的 token 估算
- **失败分析策略**：各种失败类型的处理策略是否符合预期，特别是超时后的拆分决策
- **自适应重派逻辑**：拆分后的子任务属性继承是否完整（allowed_skills、allowed_tools、context_manifest 等）
- **dispatch_mixin.py 集成**：失败分析和自适应重派是否正确集成到并发和非并发 runner 路径

## 需要其他线协调

- 无

## 剩余风险

- **Token 估算精度**：当前使用简化的字符估算（英文 4 字符/token，中文 1.5 字符/token），可能不准确
- **速度模型持久化**：首次使用时如果 `auto_bench_model_on_first_use=true` 会自动运行测速，可能影响首次启动性能
- **拆分策略**：当前 `_suggest_splits()` 方法使用简单的字符串匹配和 plan 拆分，可能无法处理所有复杂场景

## 后续建议

1. **真实模型测速验证**：使用真实模型 API 运行 `my-agent bench-model`，验证速度模型的准确性
2. **优化 Token 估算**：考虑集成实际的 tokenizer（如 tiktoken）提高估算精度
3. **拆分策略增强**：根据任务类型（翻译、重构、分析等）提供更精细的拆分建议
4. **观察和日志**：在实际运行中观察失败分析的效果，根据实际情况调整分析策略
5. **配置文档更新**：更新 CLI_REFERENCE.md 添加 `bench-model` 命令说明
6. **文件树更新**：更新 CODEBASE_TREE.md 添加新模块的说明
