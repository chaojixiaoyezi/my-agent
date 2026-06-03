# Subagent Autonomous Dispatch 设计文档

## 一句话结论

新增 `subagent_automation_level` 配置（1/2/3），控制主代理多大程度优先使用子代理完成任务。配合模型速度感知、动态超时、失败分析和自适应重派，实现高自动化场景下的稳定运行。

## 需求背景

### 用户场景

用户希望主代理能高度自动化地使用子代理：
- 级别1：几乎所有多轮任务都派子代理，极大优先子代理完成
- 级别2：中型任务派子代理
- 级别3：大型/超大型或用户指定才派子代理

### 痛点

当前系统的问题：
1. **派发阈值不明确**：没有量化标准决定"什么规模的任务该派子代理"
2. **超时是静态的**：`runner_timeout_seconds` 不考虑输入大小，10K token 和 100K token 用同一个超时
3. **失败处理机械**：失败后要么停下，要么按原样重派，不会分析原因、拆分任务
4. **主代理可能代劳**：没有机制阻止主代理绕过子代理自己执行

## 核心设计

### 1. 自动化级别配置

```yaml
# agent_config.yaml

# 子代理自动化级别
# - 1: 极大优先子代理。预估 >= 2 轮的任务都派子代理。
# - 2: 中等优先。预估 >= 4 轮的任务派子代理。
# - 3: 保守优先。预估 >= 8 轮或用户明确指定才派子代理。
subagent_automation_level: 2
```

**级别与任务规模映射**：

| 级别 | 派子代理阈值（预估轮数） | 典型场景 |
|------|------------------------|----------|
| 1 | >= 2 轮 | 几乎所有多轮任务 |
| 2 | >= 4 轮 | 中型任务 |
| 3 | >= 8 轮 | 大型/超大型任务 |

### 2. 任务规模预判

新增函数 `estimate_task_complexity()`，基于以下因素估算：

```python
@dataclass
class TaskComplexityEstimate:
    estimated_rounds: int          # 预估工具调用轮数
    estimated_input_tokens: int    # 预估输入 token 数
    estimated_output_tokens: int   # 预估输出 token 数
    confidence: float              # 预估置信度 0-1
    factors: dict[str, Any]        # 影响因素明细
```

**预判因素**：
- goal 长度和关键词（"翻译全文" vs "查看文件"）
- plan 步骤数
- allowed_tools 数量
- 历史相似任务的轮数（如果有）

**预判逻辑**（第一版简化）：

```python
def estimate_task_complexity(goal: str, plan: list[str], allowed_tools: list[str]) -> TaskComplexityEstimate:
    # 基础分：plan 步骤数
    base_rounds = max(1, len(plan))
    
    # 关键词加权
    high_complexity_keywords = ["翻译", "重构", "分析", "迁移", "部署", "测试", "优化"]
    medium_complexity_keywords = ["修改", "更新", "添加", "检查", "查找"]
    
    keyword_bonus = 0
    for kw in high_complexity_keywords:
        if kw in goal:
            keyword_bonus += 3
    for kw in medium_complexity_keywords:
        if kw in goal:
            keyword_bonus += 1
    
    # 工具数量加权（多工具通常意味着多步骤）
    tool_bonus = max(0, len(allowed_tools) - 1)
    
    estimated_rounds = base_rounds + keyword_bonus + tool_bonus
    
    return TaskComplexityEstimate(
        estimated_rounds=estimated_rounds,
        estimated_input_tokens=estimated_rounds * 2000,  # 粗略估算
        estimated_output_tokens=estimated_rounds * 500,
        confidence=0.6,  # 第一版置信度较低
        factors={
            "plan_steps": len(plan),
            "keyword_bonus": keyword_bonus,
            "tool_bonus": tool_bonus,
        }
    )
```

### 3. 模型速度感知

#### 3.1 速度基准测试

新增命令 `my-agent bench-model`，测试不同上下文大小下的模型速度：

```bash
my-agent bench-model
# 输出：
# Backend: anthropic_compatible
# Model: claude-sonnet-4-20250514
#
# Input 1K,  Output 500:  1.2s (417 tok/s)
# Input 5K,  Output 500:  2.1s (238 tok/s)
# Input 10K, Output 500:  3.5s (143 tok/s)
# Input 50K, Output 500:  8.2s (61 tok/s)
# Input 100K, Output 500: 15.3s (33 tok/s)
#
# Speed model saved to data/model_speed_profile.json
```

#### 3.2 速度模型存储

```json
{
  "backend": "anthropic_compatible",
  "model": "claude-sonnet-4-20250514",
  "tested_at": "2026-05-02T10:00:00Z",
  "samples": [
    {"input_tokens": 1000, "output_tokens": 500, "latency_seconds": 1.2},
    {"input_tokens": 5000, "output_tokens": 500, "latency_seconds": 2.1},
    {"input_tokens": 10000, "output_tokens": 500, "latency_seconds": 3.5},
    {"input_tokens": 50000, "output_tokens": 500, "latency_seconds": 8.2},
    {"input_tokens": 100000, "output_tokens": 500, "latency_seconds": 15.3}
  ],
  "interpolation_method": "log_linear"
}
```

#### 3.3 动态超时计算

```python
def calculate_dynamic_timeout(
    speed_profile: SpeedProfile,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    safety_margin: float = 2.0,
) -> float:
    """根据输入大小和速度模型计算动态超时。"""
    
    # 从速度模型插值预估耗时
    base_latency = speed_profile.interpolate(estimated_input_tokens, estimated_output_tokens)
    
    # 加上安全边际（模型可能比测试时慢）
    timeout = base_latency * safety_margin
    
    # 设置下限和上限
    min_timeout = 30.0   # 最少 30 秒
    max_timeout = 600.0  # 最多 10 分钟
    
    return max(min_timeout, min(max_timeout, timeout))
```

### 4. 失败分析器

新增 `SubAgentFailureAnalyzer`，分析子代理失败原因并给出建议。

**关键设计**：每次失败后不是机械重试，而是引入 LLM 分析现状，根据分析结果决定下一步。

```python
@dataclass
class FailureAnalysis:
    failure_type: str                    # 原始失败类型
    root_cause: str                      # 根因分类
    suggested_action: str                # 建议动作
    llm_reasoning: str                   # LLM 分析推理过程
    details: dict[str, Any]              # 详细信息
    should_retry: bool                   # 是否应该重试
    should_split: bool                   # 是否应该拆分任务
    should_adjust_timeout: bool          # 是否应该调整超时
    should_give_up: bool                 # 是否应该放弃并通知用户
    new_timeout_seconds: float | None    # 建议的新超时
    split_suggestions: list[str]         # 拆分建议
    adjusted_goal: str                   # 调整后的目标（如果有）
    adjusted_plan: list[str]             # 调整后的计划（如果有）
```

**根因分类**：

| 根因 | 触发条件 | 建议动作 |
|------|----------|----------|
| `timeout` | failure_type=runner_timeout | 提高超时后重试 |
| `capability_missing` | failure_type=capability_request | 补充能力后重试 |
| `output_parse_error` | failure_type=structured_output_parse_error | 调整 prompt 后重试 |
| `task_too_large` | 轮数超预期 + 多次 timeout | 拆分成子任务 |
| `tool_failure` | failure_type=tool_result_missing | 换工具或重试 |
| `model_error` | failure_type=model_error/api_error | 等待后重试 |
| `channel_broken` | channel_status=BROKEN | 需要人工介入 |
| `verification_failed` | 验收失败 | 分析产出质量，可能需要重做 |

**分析逻辑**（LLM 驱动）：

```python
class SubAgentFailureAnalyzer:
    def __init__(self, config: AgentConfig, backend):
        self.config = config
        self.backend = backend
        self.max_auto_retry_attempts = config.max_auto_retry_attempts  # 默认 3
    
    def analyze(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        """分析失败原因，引入 LLM 决定下一步。"""
        
        # 检查是否超过最大重试次数
        if task.runner_attempts >= self.max_auto_retry_attempts:
            return self._build_give_up_analysis(task, "超过最大自动重试次数")
        
        # 构建分析 prompt
        prompt = self._build_analysis_prompt(task, runner_result)
        
        # 调用 LLM 分析
        llm_response = self.backend.generate(prompt)
        
        # 解析 LLM 输出
        analysis = self._parse_llm_analysis(llm_response.text, task)
        
        return analysis
    
    def _build_analysis_prompt(self, task: SubAgentTask, result: SubAgentRunnerResult) -> str:
        """构建失败分析 prompt。"""
        
        return f"""你是一个任务调度专家。以下是一个子代理任务的失败信息，请分析原因并决定下一步。

## 任务信息
- 任务ID: {task.id}
- 目标: {task.goal}
- 当前状态: {task.status}
- 失败类型: {task.failure_type}
- 已尝试次数: {task.runner_attempts}/{self.max_auto_retry_attempts}

## 执行计划
{chr(10).join(f'- {step}' for step in task.plan)}

## Runner 输出摘要
{result.message[:2000] if result.message else '无'}

## 失败证据
{self._format_evidence(task)}

## 请分析并输出 JSON:
{{
    "root_cause": "失败根因分类",
    "reasoning": "你的分析推理过程",
    "suggested_action": "建议的动作",
    "should_retry": true/false,
    "should_split": true/false,
    "should_give_up": true/false,
    "adjusted_goal": "如果需要调整目标，写在这里",
    "adjusted_plan": ["调整后的计划步骤"],
    "split_suggestions": ["如果需要拆分，列出子任务建议"],
    "new_timeout_seconds": null 或新的超时值
}}

## 可选的 suggested_action:
- "retry_with_adjustment": 调整后重试
- "split_task": 拆分任务
- "increase_timeout": 提高超时后重试
- "补充能力": 需要补充能力后重试
- "give_up": 放弃并通知用户

请根据实际情况选择最合适的动作。不要机械重试，如果问题明显无法解决，请建议放弃。"""
    
    def _parse_llm_analysis(self, llm_output: str, task: SubAgentTask) -> FailureAnalysis:
        """解析 LLM 输出为 FailureAnalysis。"""
        
        try:
            # 提取 JSON
            json_match = re.search(r'\{.*\}', llm_output, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = {}
        except json.JSONDecodeError:
            data = {}
        
        return FailureAnalysis(
            failure_type=task.failure_type or "",
            root_cause=data.get("root_cause", "unknown"),
            suggested_action=data.get("suggested_action", "retry_with_adjustment"),
            llm_reasoning=data.get("reasoning", ""),
            details={"llm_output": llm_output[:1000]},
            should_retry=data.get("should_retry", False),
            should_split=data.get("should_split", False),
            should_adjust_timeout=data.get("new_timeout_seconds") is not None,
            should_give_up=data.get("should_give_up", False),
            new_timeout_seconds=data.get("new_timeout_seconds"),
            split_suggestions=data.get("split_suggestions", []),
            adjusted_goal=data.get("adjusted_goal", ""),
            adjusted_plan=data.get("adjusted_plan", []),
        )
    
    def _build_give_up_analysis(self, task: SubAgentTask, reason: str) -> FailureAnalysis:
        """构建放弃分析结果。"""
        
        return FailureAnalysis(
            failure_type=task.failure_type or "",
            root_cause="max_attempts_exceeded",
            suggested_action="give_up",
            llm_reasoning=f"已尝试 {task.runner_attempts} 次，{reason}。建议人工介入。",
            details={"reason": reason},
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            should_give_up=True,
            new_timeout_seconds=None,
            split_suggestions=[],
            adjusted_goal="",
            adjusted_plan=[],
        )
```

### 5. 自适应重派

失败后不是机械重派，而是根据分析结果调整策略：

```python
def adaptive_retry(task: SubAgentTask, analysis: FailureAnalysis) -> SubAgentTask | list[SubAgentTask]:
    """根据失败分析结果决定下一步。"""
    
    # 不应该重试的情况
    if not analysis.should_retry and not analysis.should_split:
        return []  # 停止，需要人工介入
    
    # 需要拆分的情况
    if analysis.should_split:
        return split_task(task, analysis.split_suggestions)
    
    # 需要调整超时的情况
    if analysis.should_adjust_timeout:
        # 更新任务的超时配置
        task.attributes["dynamic_timeout_seconds"] = analysis.new_timeout_seconds
        task.runner_attempts = 0  # 重置尝试次数
        task.status = "PLANNING"
        task.failure_type = ""
        return [task]
    
    # 普通重试
    task.runner_attempts = 0
    task.status = "PLANNING"
    task.failure_type = ""
    return [task]


def split_task(task: SubAgentTask, suggestions: list[str]) -> list[SubAgentTask]:
    """把大任务拆分成多个小任务。"""
    
    # 根据建议拆分
    subtasks = []
    for i, suggestion in enumerate(suggestions):
        subtask = SubAgentTask(
            id=f"{task.id}-part-{i+1}",
            goal=f"{task.goal} - 第{i+1}部分：{suggestion}",
            # ... 继承父任务的部分属性
            parent_run_id=task.id,
        )
        subtasks.append(subtask)
    
    # 原任务标记为已拆分
    task.status = "SPLIT"
    task.attributes["split_into"] = [st.id for st in subtasks]
    
    return subtasks
```

### 6. 主代理代劳防护

在自动化级别1时，主代理不能绕过子代理直接执行多轮任务：

```python
class SubagentAutomationGuard:
    """防止主代理在高自动化级别下绕过子代理。"""
    
    def __init__(self, config: AgentConfig):
        self.level = config.subagent_automation_level
        self.threshold = {1: 2, 2: 4, 3: 8}.get(self.level, 8)
    
    def should_delegate(self, complexity: TaskComplexityEstimate) -> bool:
        """判断任务是否应该派给子代理。"""
        return complexity.estimated_rounds >= self.threshold
    
    def warn_if_not_delegating(self, complexity: TaskComplexityEstimate, delegated: bool):
        """如果应该派但没派，发出警告。"""
        if self.should_delegate(complexity) and not delegated:
            logger.warning(
                f"自动化级别 {self.level}：任务预估 {complexity.estimated_rounds} 轮，"
                f"超过阈值 {self.threshold}，建议派子代理完成。"
            )
```

## 配置项汇总

```yaml
# agent_config.yaml

# 子代理自动化级别
# - 1: 极大优先子代理（>= 2 轮就派）
# - 2: 中等优先（>= 4 轮就派）
# - 3: 保守优先（>= 8 轮或用户指定才派）
subagent_automation_level: 2

# 动态超时安全边际
# - 实际超时 = 预估耗时 * safety_margin
# - 默认 2.0，即预估 10 秒的任务给 20 秒超时
dynamic_timeout_safety_margin: 2.0

# 动态超时下限（秒）
dynamic_timeout_min: 30

# 动态超时上限（秒）
dynamic_timeout_max: 600

# 失败后最大自动拆分次数
# - 避免无限拆分
# - 0 表示不自动拆分
max_auto_split_depth: 2

# 失败后最大自动重试次数
# - 每次重试会引入 LLM 分析失败原因，根据分析结果调整策略
# - 超过此次数后放弃自动重试，通知用户介入
# - 默认 3 次
max_auto_retry_attempts: 3

# 模型速度配置文件路径
model_speed_profile_path: "data/model_speed_profile.json"

# 是否在首次使用时自动运行速度测试
auto_bench_model_on_first_use: true
```

## CLI 命令

```bash
# 运行模型速度基准测试
my-agent bench-model

# 查看当前速度模型
my-agent bench-model --show

# 查看任务复杂度预估（调试用）
my-agent estimate-task "翻译这篇文档"
```

## 实现计划

### 第一阶段：基础框架

1. 新增 `subagent_automation_level` 配置项
2. 实现 `estimate_task_complexity()` 任务规模预判
3. 修改 `spawn_subagents()` 集成自动化级别判断
4. 新增主代理代劳防护警告

### 第二阶段：模型速度感知

1. 实现 `bench-model` CLI 命令
2. 实现速度模型存储和加载
3. 实现 `calculate_dynamic_timeout()` 动态超时计算
4. 集成到 `_run_subagent_worker()` 使用动态超时

### 第三阶段：失败分析和自适应重派（LLM 驱动）

1. 实现 `SubAgentFailureAnalyzer` 失败分析器
   - `_build_analysis_prompt()`: 构建失败分析 prompt，包含任务信息、runner 输出、失败证据
   - `_parse_llm_analysis()`: 解析 LLM 返回的 JSON，映射到 FailureAnalysis
   - `_build_give_up_analysis()`: 超过 max_auto_retry_attempts 时构建放弃结果
2. 实现 `adaptive_retry()` 自适应重派逻辑
   - 根据 FailureAnalysis 的 should_retry/should_split/should_give_up 决定下一步
   - 调整后重试时更新 goal/plan/timeout
3. 实现 `split_task()` 任务拆分逻辑
4. 集成到 dispatch 循环
5. 新增 `max_auto_retry_attempts` 配置项（默认 3）

### 第四阶段：测试和文档

1. 单元测试覆盖各组件
2. 集成测试：模拟各种失败场景
3. 更新 CLI_REFERENCE.md
4. 更新 CODEBASE_TREE.md

## 与现有系统的关系

| 现有组件 | 改动 |
|----------|------|
| `agent_config.yaml` | 新增配置项 |
| `spawn_subagents()` | 集成自动化级别判断 |
| `_run_subagent_worker()` | 使用动态超时 |
| `dispatch_subagents()` | 集成失败分析和自适应重派 |
| `agent_core/runner/dispatch.py` | 扩展超时逻辑 |
| `subagent_mixin.py` | 扩展失败处理 |

## 安全边界

- 级别1虽然"极大优先子代理"，但不会强制所有任务都派（用户可以在聊天里覆盖）
- 动态超时有上下限，不会无限放大
- 自动拆分有深度限制，不会无限拆分
- 主代理代劳防护是警告，不是硬阻断（紧急情况主代理仍可自己执行）
