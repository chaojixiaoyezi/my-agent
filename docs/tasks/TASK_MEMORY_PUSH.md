# Memory Push Mode 实现任务书

## 背景

my-agent 的记忆系统当前是"拉模式"：记忆存在那里，需要 agent 主动去查才生效。但 agent 在派工循环里想不到主动查记忆，所以记忆白记了。

目标是改成"推模式"：在关键决策点（dispatch 超时、任务失败、planner 决策）自动查询相关记忆并注入上下文，不需要 agent 主动想。

---

## 目标

在关键决策点自动注入相关记忆，让 agent "不用主动想，系统推着给"。

---

## 功能清单

### 1. 记忆类型标签系统

在 `agent_py_agent/agent/memory_archive/` 或相关位置定义记忆类型：

```python
class MemoryType(str, Enum):
    LESSON_GENERAL = "lesson_general"     # 通用教训，永不过期
    LESSON_TASK = "lesson_task"           # 任务教训，有场景限制
    LESSON_TEMP = "lesson_temp"           # 临时经验，单次有效
    CONTEXT = "context"                   # 上下文记忆
    FACT = "fact"                         # 事实
```

### 2. 记忆条目结构扩展

扩展记忆条目（memory.jsonl 或 LocalStore）：

```json
{
  "type": "lesson_task",
  "trigger_type": "timeout",
  "tags": ["timeout", "gateway", "hermes"],
  "content": "gateway PID tracking 用 Hermes 风格，超时后要检查 start_time",
  "trigger_conditions": {
    "file_lines": ">1000",
    "timeout_count": ">=3"
  },
  "lesson": "用 Hermes 风格的 scoped lock",
  "action": "use_scoped_lock",
  "result": "success",
  "created_at": "..."
}
```

### 3. 决策点自动注入

在以下关键决策点自动注入相关记忆：

#### 3.1 Dispatch 超时
- 位置：`agent_py_agent/agent/subagents/dispatch_mixin.py`
- 注入：timeout 相关教训
- 格式：简短提示，agent 直接可见

#### 3.2 任务失败
- 位置：`agent_py_agent/agent/subagents/task.py` 或 failure analyzer
- 注入：同类失败解决策略
- 格式：action + result

#### 3.3 Planner 决策前
- 位置：`SimpleAgent.run()` 或 planner 相关
- 注入：与当前任务相关的 context 记忆
- 格式：上下文摘要

### 4. 注入接口

新建 `agent_py_agent/agent/memory_push.py`：

```python
def push_relevant_memories(
    agent: SimpleAgent,
    trigger_type: str,  # "timeout", "failure", "planning"
    context: dict,       # 当前任务上下文
    limit: int = 3,     # 最多注入几条
) -> list[str]:
    """查询并返回与当前上下文相关的记忆文本。"""
```

**逻辑**：
1. 按 `trigger_type` 和 `tags` 过滤
2. 按语义相似度排序（用现有 embedding 或简单关键词）
3. 返回最多 `limit` 条记忆文本

### 5. 记忆写入增强

修改记忆写入时自动打标签：
- 失败教训 → `type: lesson_task`, `trigger_type: failure`
- 成功经验 → `type: lesson_temp`, `result: success`
- 上下文 → `type: context`

---

## 文件修改指引

1. **新建 `agent_py_agent/agent/memory_push.py`**：
   - `MemoryType` 枚举
   - `push_relevant_memories()` 函数
   - 记忆注入逻辑

2. **修改 `agent_py_agent/agent/subagents/dispatch_mixin.py`**：
   - dispatch 超时后调用 `push_relevant_memories(agent, "timeout", context)`
   - 把返回的记忆注入到下一轮 prompt

3. **修改 `agent_py_agent/agent/subagents/task.py`**：
   - 任务失败后调用 `push_relevant_memories(agent, "failure", context)`
   - 把返回的记忆注入到失败报告

4. **修改记忆写入逻辑**（可选，当前记忆系统已有结构可复用）

---

## 约束

- 不要破坏现有功能，749 tests 要继续通过
- 注入的记忆要简短（最多 200 字），不要把太多记忆塞进 prompt
- 使用现有 `LocalStore` 和 embedding 查询（不要自己训练模型）
- 保持向后兼容：旧记忆条目没有 type 标签的当作 `LESSON_GENERAL`

## 验收

1. `push_relevant_memories(agent, "timeout", context)` 能返回相关记忆
2. dispatch 超时后自动注入 timeout 相关教训
3. 任务失败后自动注入同类失败解决策略
4. 新记忆自动带 type 和 tags 标签
5. 已有测试继续通过

## 完成后必须更新

1. **`docs/modules/memory/02-progress.md`**：
   - 记录 Memory Push Mode 已实现
   - 简述架构改动

2. **`STATUS.md`**：
   - 在"最新推进"部分添加 Memory Push Mode 条目
   - 在"最新验收"部分添加新测试结果（如果有）

3. **如果改动较大**，在 `HANDOFF_current-state.md` 添加说明