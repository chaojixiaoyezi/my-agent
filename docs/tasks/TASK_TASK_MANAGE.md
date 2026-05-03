# Task Abandon/Pause/Resume 实现任务书

## 背景

my-agent 是一个多主代理系统。当前任务执行有 Dispatch 闭环问题：任务一旦开始，除非人工干预，否则会一直重试直到完成。用户有很多做一半不想做的任务，需要能主动放弃/暂停任务。

## 目标

实现任务状态管理（ABANDONED/PAUSED/RESUMED），让用户能控制任务生命周期，同时 Dispatch 闭环能正确处理这些状态。

---

## 功能清单

### 1. 任务状态机

在 `agent_py_agent/agent/subagents/task.py`（或新建）中定义：

```python
class TaskStatus(str, Enum):
    PLANNING = "PLANNING"       # 刚创建，还没开始执行
    RUNNING = "RUNNING"         # 正在执行
    BLOCKED = "BLOCKED"         # 被阻塞（等待某个条件）
    PAUSED = "PAUSED"           # 用户暂停 later 可 resume
    ABANDONED = "ABANDONED"     # 用户主动放弃，不再重试
    COMPLETED = "COMPLETED"     # 完成了
    FAILED = "FAILED"           # 穷尽策略后失败
```

### 2. 数据模型扩展

在现有 task.json 或 LocalStore subagent 表中增加：
- `status`: TaskStatus 枚举
- `description`: str，用户可见的任务一句话描述
- `paused_at`: datetime，暂停时间（可选）
- `abandoned_at`: datetime，放弃时间（可选）

**注意**：description 由用户在创建任务时提供，或由 LLM 根据任务目标自动生成。

### 3. 命令实现

在 `agent_py_agent/cli/task_commands.py` 实现：

| 命令 | 功能 |
|------|------|
| `task list` | 列出所有任务：task_id, name, description, status, updated_at |
| `task show <task_id>` | 显示任务详情（含 description、状态历史） |
| `task abandon <task_id>` | 标记任务为 ABANDONED |
| `task pause <task_id>` | 标记任务为 PAUSED |
| `task resume <task_id>` | 将 PAUSED 任务恢复为 RUNNING |
| `task search "<模糊描述>"` | LLM 模糊搜索匹配的任务 |

#### `task list` 输出格式
```
任务列表：
  [RUNNING]  subagent-xxx  gateway-stability  "实现 Hermes 风格 PID tracking..."
  [PAUSED]   subagent-yyy  qq-adapter-fix     "修复 QQ WebSocket 断线问题..."
  [ABANDONED] subagent-zzz old-feature         "旧功能，已不需要"
  [COMPLETED] subagent-aaa memory-archive      "完成记忆归档功能"
```

### 4. Dispatch 闭环联动

修改 `agent_py_agent/agent/subagents/dispatch_mixin.py` 或相关文件：

- 在 dispatch 调度循环开始前，过滤掉 `PAUSED`、`ABANDONED`、`COMPLETED`、`FAILED` 状态任务
- 只对 `PLANNING`、`RUNNING`、`BLOCKED` 状态任务进行调度
- 任务失败穷尽策略后标记 `FAILED` 并通知用户

### 5. 任务创建时自动生成 description

在创建 subagent task 时，LLM 需要根据任务目标自动生成 `description`。
可以在 `create_subagents` 工具返回时附带 `description` 字段。

### 6. HTML 看板预留

- 所有任务 ID 使用 URL-safe 格式（已满足）
- task list 输出时每行格式为 `[STATUS] task_id name description`
- 给以后留 `task --format html` 或独立 HTTP 端点 `/tasks` 的空间

---

## 文件修改指引

1. **新建 `agent_py_agent/cli/task_commands.py`**：
   - 实现 `cmd_task_list`, `cmd_task_show`, `cmd_task_abandon`, `cmd_task_pause`, `cmd_task_resume`, `cmd_task_search`
   - 注册到 `parser.py`

2. **修改 `agent_py_agent/agent/subagents/task.py`**（或相关 model）：
   - 加 `status`, `description`, `paused_at`, `abandoned_at` 字段
   - 或直接在 LocalStore 的 subagent 表加字段

3. **修改 Dispatch 闭环**：
   - 在 `dispatch_mixin.py` 或 `run_dispatch_cycle()` 中添加状态过滤

4. **修改 `agent_py_agent/agent/subagents/manager.py`**：
   - `create_subagents` 返回时生成 `description`

---

## 约束

- 不要破坏现有功能，749 tests 要继续通过
- 保持向后兼容：已有 task.json 不强制迁移，新任务自动带新字段
- description 最多 100 字
- 使用现有 `LocalStore` 和 `gateway_paths` 模式，不要自己造轮子

## 验收

1. `my-agent task list` 能列出所有任务（含 description 和 status）
2. `my-agent task abandon <id>` 能标记任务为 ABANDONED，Dispatch 不再调度
3. `my-agent task pause <id>` 能暂停任务
4. `my-agent task resume <id>` 能恢复暂停的任务
5. `my-agent task search "模糊描述"` 能找到匹配的任务
6. `my-agent task show <id>` 能显示任务详情
7. 已有测试继续通过