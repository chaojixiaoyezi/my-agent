# 失败自省与自适应派工 实现任务书

## 背景

my-agent 当前有 `SubAgentFailureAnalyzer`（规则分类器），但它只做机械分类：失败类型 → 固定规则（重试/拆分/人工）。不分析"为什么失败"，导致：
- 超时后机械重试，不调参 → 死循环
- 失败后停住等人催
- 不分析原因，不会调参

目标：在 failure_analyzer 分类之后，增加 LLM 自省层，分析失败原因并自动调参。

---

## 目标

在 dispatch 失败后，LLM 自动分析失败原因，给出调参建议，并注入下一轮 task。

---

## 功能清单

### 1. 失败自省接口

新建 `agent_py_agent/agent/agent_core/failure_introspector.py`：

```python
@dataclass
class FailureIntrospection:
    """LLM 自省结果。"""
    analysis_reason: str          # 人可读的失败原因分析
    root_cause: str              # 根因分类
    suggested_params: dict       # 建议调整的参数
    should_retry: bool           # 是否应该重试
    should_split: bool           # 是否应该拆分
    confidence: float            # 分析置信度 0-1

class FailureIntrospector:
    """LLM 失败自省器。"""

    def introspect(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
        failure_analysis: FailureAnalysis,  # 来自现有的 failure_analyzer
    ) -> FailureIntrospection:
        """LLM 分析失败原因并返回调参建议。"""
```

### 2. LLM 自省逻辑

在 `introspect()` 中调用 LLM：

```python
def _call_llm_introspect(self, task, runner_result, failure_analysis):
    prompt = f"""
分析以下任务失败原因，给出调参建议：

任务: {task.goal}
失败类型: {failure_analysis.failure_type}
当前参数:
- timeout: {task.timeout_seconds}
- runner: {task.runner_command}

错误信息: {runner_result.message}

请分析：
1. 为什么失败？（文件太大？模型太慢？超时太短？工具缺失？）
2. 建议怎么调参？（提高超时？拆分任务？换策略？）
3. 是否应该重试？

输出格式：
{{
  "analysis_reason": "任务太大，需要拆分",
  "root_cause": "task_too_large",
  "suggested_params": {{"new_timeout_seconds": 180}},
  "should_retry": false,
  "should_split": true,
  "confidence": 0.9
}}
"""
    # 调用现有 LLM
    response = agent.run(prompt, save=False)
    # 解析 JSON
    return FailureIntrospection(**json.loads(response))
```

### 3. 集成到 dispatch_mixin.py

在 `agent_py_agent/agent/subagents/dispatch_mixin.py` 中：

**失败后调用自省**：

```python
# 在 dispatch_mixin.py 的 _handle_runner_failure 或类似位置
failure_analysis = self.failure_analyzer.analyze(task, runner_result)

# 新增：调用 LLM 自省
introspection = self.failure_introspector.introspect(task, runner_result, failure_analysis)

# 把自省结果存入 task
task.failure_introspection = introspection

# 如果 LLM 建议调参，应用到下一轮
if introspection.should_retry and introspection.suggested_params:
    task = self._apply_introspection_params(task, introspection.suggested_params)
```

### 4. 参数调整函数

在 `dispatch_mixin.py` 中添加：

```python
def _apply_introspection_params(
    self,
    task: SubAgentTask,
    params: dict,
) -> SubAgentTask:
    """根据自省结果调整任务参数。"""

    if "new_timeout_seconds" in params:
        task.timeout_seconds = params["new_timeout_seconds"]

    if "split_goal" in params:
        # 拆分任务
        return self._split_task(task, params["split_goal"])

    return task
```

### 5. 失败后注入记忆（联动 Memory Push Mode）

如果 Memory Push Mode 已实现，在 LLM prompt 中注入相关记忆：

```python
# 在 introspect() 中
relevant_memories = push_relevant_memories(agent, "failure", context)
prompt += f"\n相关记忆：{relevant_memories}"
```

### 6. 记录自省日志

在 task.json 或 LocalStore 中记录：

```json
{
  "failure_introspection": {
    "analysis_reason": "任务太大，需要拆分",
    "root_cause": "task_too_large",
    "suggested_params": {"new_timeout_seconds": 180},
    "confidence": 0.9,
    "applied": true,
    "result": "success"
  }
}
```

---

## 文件修改指引

1. **新建 `agent_py_agent/agent/agent_core/failure_introspector.py`**：
   - `FailureIntrospection` dataclass
   - `FailureIntrospector` 类

2. **修改 `agent_py_agent/agent/subagents/dispatch_mixin.py`**：
   - 在失败处理逻辑后调用 `FailureIntrospector`
   - 添加 `_apply_introspection_params()` 方法

3. **修改 `agent_py_agent/agent/subagents/models.py`**（可选）：
   - 在 `SubAgentTask` 加 `failure_introspection` 字段

---

## 约束

- 不要改动现有 `failure_analyzer.py`，保持规则分类不变
- LLM 调用要设 `save=False`，避免污染主对话记忆
- LLM 输出要求严格 JSON，解析失败要降级到规则分类
- 已有 749 tests 要继续通过

## 验收

1. 失败后自动调用 LLM 自省，返回 `FailureIntrospection`
2. 自省结果记录到 task.json 的 `failure_introspection` 字段
3. 自省建议的参数能自动应用到下一轮（如提高超时）
4. 如果 LLM 调用失败，降级到现有规则分类（不影响原功能）
5. 已有测试继续通过

## 完成后必须更新

1. **`docs/modules/subagent/02-progress.md`**：
   - 记录 Failure Introspection 已实现
   - 简述架构改动（LLM 自省层）

2. **`STATUS.md`**：
   - 在"最新推进"部分添加 Failure Introspection 条目
   - 在"最新验收"部分添加新测试结果（如果有）

3. **如果改动较大**，在 `HANDOFF_current-state.md` 添加说明