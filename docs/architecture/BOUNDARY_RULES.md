# Boundary Rules
# 模块边界规则：导入、写入、大小、调用、安全

LLM: Apply these rules before importing across modules or writing files.
给人看的解释：边界规则的目的不是限制开发速度，而是防止一个文件慢慢吞掉整个系统。违反这些规则的 PR 不应被合并。

---

## 1. Import Rules / 导入规则

### 1.1 Layer Import Rules / 分层导入规则

这些规则定义了 `agent_py_agent/` 内部各层之间的合法导入方向。

| 源层 | 可导入 | 不可导入 |
|---|---|---|
| `cli/` | `agent/agent_core/`, `agent/session/`, `agent/settings/`, `agent/gateway_parts/` | `agent/memory_store/`, `agent/tooling/`, `agent/subagents/`（直接） |
| `agent/agent_core/` | `agent/subagents/`, `agent/capability/`, `agent/memory_routing/`, `agent/memory_archive/`, `agent/tooling/` | `cli/`, `agent/adapter/` |
| `agent/subagents/` | `agent/memory_store/`（通过仓库）, `agent/capability/`, `agent/local_storage/` | `cli/`, `agent/agent_core/`, `agent/gateway_parts/` |
| `agent/gateway_parts/` | `agent/session/`, `agent/settings/`, `agent/memory_store/` | `cli/`, `agent/agent_core/`, `agent/subagents/` |
| `agent/memory_store/` | 无（纯基础设施） | `agent/memory_routing/`, `agent/memory_archive/` |
| `agent/memory_routing/` | `agent/memory_store/`（只读） | `agent/subagents/`, `agent/agent_core/` |
| `agent/memory_archive/` | `agent/memory_store/`, `agent/memory_routing/` | `agent/subagents/`, `agent/agent_core/` |
| `agent/tooling/` | `agent/settings/` | `agent/agent_core/`, `agent/subagents/` |
| `agent/adapter/` | `agent/session/`, `agent/settings/` | `agent/agent_core/`, `agent/subagents/` |
| `agent/extensions/` | `agent/settings/`, `agent/memory_store/` | `agent/agent_core/`, `agent/subagents/` |

### 1.2 Forbidden Import Patterns / 禁止的导入模式

```python
# ---- 禁止 1: CLI 直接操作底层存储 ----
# 错误: CLI 直接读写 JSONL 记忆文件
from agent.memory_store.jsonl import JsonlMemoryStore, append_jsonl

# ---- 禁止 2: 基础设施反向依赖上层 ----
# 错误: memory_store 导入 subagents
from agent.subagents.models import SubAgentTask

# ---- 禁止 3: 子代理直接调用 CLI ----
# 错误: 子代理回调 CLI 渲染
from cli.chat import render_response

# ---- 禁止 4: 新代码使用 import * ----
# 所有新代码禁止 import *，已有的正在逐步消除
from agent.subagents.policies import *  # 禁止

# ---- 禁止 5: 循环导入 ----
# A 导入 B，B 又导入 A，导致初始化失败
# 解决方案: 提取共享依赖到更底层

# ---- 禁止 6: 条件导入中的隐式耦合 ----
# 错误: 在运行时动态 import 来绕过静态检查
import importlib
mod = importlib.import_module("agent.subagents.manager")
```

### 1.3 Required Import Patterns / 要求的导入模式

```python
# ---- 要求 1: 使用绝对导入 ----
# 错误:
from .models import SubAgentTask
# 正确（跨包时）:
from agent.subagents.models import SubAgentTask

# ---- 要求 2: TYPE_CHECKING 保护 ----
# 只在类型标注中使用的导入，放在 TYPE_CHECKING 块中
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.session.manager import SessionManager

# ---- 要求 3: 通过 __init__.py 控制公开接口 ----
# 不直接导入内部模块，通过包的 __init__.py 导入公开 API
from agent.subagents import SubAgentManager  # 正确
from agent.subagents.manager import SubAgentManager  # 也可接受
from agent.subagents.manager_base import SubAgentBaseMixin  # 内部使用可接受
```

### 1.4 Import Validation Tooling / 导入校验工具

项目应配置 `import-linter` 或自定义 lint 规则，CI 中自动校验：

```toml
# pyproject.toml
[tool.importlinter]
root_package = "agent_py_agent"

[[tool.importlinter.contracts]]
name = "cli-no-direct-storage"
type = "forbidden"
source_modules = ["agent_py_agent.cli"]
forbidden_modules = [
    "agent_py_agent.agent.memory_store",
    "agent_py_agent.agent.local_storage",
]

[[tool.importlinter.contracts]]
name = "no-star-imports"
type = "independent"
modules = ["agent_py_agent"]
```

---

## 2. File Writing Rules / 文件写入规则

### 2.1 Repository Pattern / 仓库模式

所有持久化写入必须经过仓库层。当前实现状态：

| 存储目标 | 写入方式 | 仓库状态 |
|---|---|---|
| JSONL 记忆文件 | `memory_store/jsonl.py` -> `append_jsonl()` | 直接文件操作，需封装为仓库 |
| 子代理状态文件 | `subagents/services/persistence.py` | 已提取为服务，待进一步仓库化 |
| SQLite 本地存储 | `local_storage/` | 直接 SQL 操作，需封装为仓库 |
| 审计日志 | `audit/logger.py` | 直接文件操作，需封装为仓库 |
| 归档快照 | `memory_archive/snapshots.py` | 直接文件操作，需封装为仓库 |
| 通知数据 | `notification/` | 直接文件操作，需封装为仓库 |

### 2.2 Write Boundary Enforcement / 写入边界执行

`tooling/write_boundary.py` 是子代理写文件的门禁系统：

```python
# 写入边界校验流程:
# 1. 工具名检查: write_file、apply_patch 和授权命令写入都会走写入边界
# 2. 路径规范化: 去除控制字符、限制长度(4096字符)
# 3. 统一路径策略: normal 模式只拒绝 path_dangerous_roots，full 模式路径全开
# 4. 显式禁止目录检查: 仍尊重 forbidden_write_roots
# 5. 锁定文件检查: 不能修改被 locked_files 标记的文件
```

### 2.3 Write Rules by Module / 各模块写入规则

| 模块 | 可写入位置 | 不可写入位置 |
|---|---|---|
| `memory_store/` | `data/memory.jsonl`, `data/memory/` | 任何其他路径 |
| `subagents/services/persistence.py` | `data/subagents/` | 任何其他路径 |
| `memory_archive/` | `data/memory_archive/` | 任何其他路径 |
| `audit/` | `data/audit.jsonl` | 任何其他路径 |
| `local_storage/` | `data/local_store/` | 任何其他路径 |
| `gateway_parts/` | `data/gateway/` | 任何其他路径 |
| `notification/` | `data/notifications/` | 任何其他路径 |
| `tooling/` | 经 `write_boundary` 校验后的路径 | 未校验的路径 |
| `tests/` | `tmp_path`, 临时目录, 显式 fixture 沙箱 | 任何持久化路径 |

### 2.4 Gitignore Enforcement / Gitignore 强制规则

以下路径不得提交到版本控制：

```gitignore
# 运行时数据
data/subagents/
data/local_store/
data/gateway/
data/notifications/
data/log_fixtures/

# 记忆文件
memory/raw/
memory_archive/tokens/

# 配置覆盖
config/local*.yaml
config/local*.json

# 临时文件
*.tmp
*.bak
```

---

## 3. Size Rules / 代码大小规则

### 3.1 File Size Limits / 文件大小限制

| 级别 | 行数限制 | 动作 |
|---|---|---|
| SOFT | 400 行 | 警告：考虑拆分 |
| HARD | 600 行 | 阻断：必须拆分后才能合入新功能 |
| FROZEN | 当前大小 | 冻结：不允许新增代码，只允许重构拆分 |

### 3.2 Current Violations / 当前违规文件

| 文件 | 行数 | 级别 | 拆分计划 |
|---|---|---|---|
| `cli/chat.py` | 989 | FROZEN | -> `chat_parts/` 继续拆分（见 CHAT_REFACTOR_PLAN.md） |
| `agent_core/dispatch_mixin.py` | 889 | FROZEN | -> `dispatch/planner.py`, `dispatch/runner.py`, `dispatch/loop.py` |
| `memory_archive/query.py` | 839 | FROZEN | -> `archive/query_builder.py`, `archive/query_executor.py` |
| `subagents/manager_patch.py` | 794 | FROZEN | -> `subagent_services/patch.py` |
| `settings/config.py` | 751 | FROZEN | -> `shared/config/agent_config.py` + 加载逻辑 |
| `log_analysis/analytics/detectors/rules.py` | 747 | FROZEN | -> `detectors/rule_engine.py`, `detectors/rule_loader.py` |
| `subagents/manager_base.py` | 744 | FROZEN | -> `subagent_services/persistence.py` + `subagent_services/board.py` |

### 3.3 Function Size Limits / 函数大小限制

| 级别 | 行数限制 | 动作 |
|---|---|---|
| SOFT | 50 行 | 警告：考虑提取子函数 |
| HARD | 80 行 | 阻断：必须拆分 |

### 3.4 Class Size Limits / 类大小限制

| 级别 | 方法数 | 动作 |
|---|---|---|
| SOFT | 15 个公开方法 | 警告：考虑职责拆分 |
| HARD | 25 个公开方法 | 阻断：必须拆分为多个类 |

### 3.5 Mixin Limits / Mixin 限制

当前 `SubAgentManager` 由 14 个 mixin 拼合，公开方法数量已超过 100 个。这是典型的分布式上帝类（distributed god class）。

| 规则 | 限制 | 说明 |
|---|---|---|
| 单个 mixin 最大行数 | 400 行 | 超过说明承担了过多职责 |
| manager 类 mixin 数量 | 5 个 | 当前 14 个是历史债务 |
| mixin 间方法调用 | 禁止直接 `self.other_mixin_method()` | 应通过注入的服务调用 |

---

## 4. Cross-Module Call Rules / 跨模块调用规则

### 4.1 No Implicit Coupling / 禁止隐式耦合

```python
# ---- 禁止: 通过文件路径耦合 ----
# 错误: 硬编码其他模块的文件路径
path = "data/subagents/" + task_id + ".json"

# 正确: 通过仓库接口
repo.save_task(task_id, task_data)

# ---- 禁止: 通过约定的键名耦合 ----
# 错误: 在一个模块中写入特定键，另一个模块依赖这个键
data["_internal_status"] = "running"  # 其他模块不能依赖这个键

# 正确: 通过明确的数据模型
task.status = TaskStatus.RUNNING

# ---- 禁止: 通过环境变量耦合 ----
# 错误: 模块 A 设置环境变量，模块 B 读取
os.environ["AGENT_DISPATCH_STATE"] = "active"

# 正确: 通过函数参数或依赖注入
dispatch_loop.run(state=DispatchState.ACTIVE)
```

### 4.2 Event-Based Communication / 事件通信

跨模块的松耦合通信应通过事件机制，而非直接调用：

```python
# 当前: 直接调用（紧耦合）
class DispatchMixin:
    def dispatch_subagents(self):
        # 直接调用子代理管理器
        self.subagent_manager.create_task(...)
        # 直接调用审计
        self.audit_logger.log_dispatch(...)

# 目标: 事件驱动（松耦合）
class DispatchMixin:
    def dispatch_subagents(self):
        # 发布事件
        self.event_bus.emit("dispatch.started", {...})
        # 由订阅者处理
```

### 4.3 Shared State Rules / 共享状态规则

| 规则 | 说明 |
|---|---|
| 不通过全局变量共享状态 | 使用依赖注入或上下文对象 |
| 不通过文件系统隐式通信 | 使用明确的仓库接口 |
| 不通过 monkey-patch 修改行为 | 使用策略模式或插件机制 |
| 不通过 `__subclasses__()` 发现实现 | 使用显式注册 |

---

## 5. Security Boundary Rules / 安全边界规则

### 5.1 Write Boundary / 写入边界

`tooling/write_boundary.py` 是核心安全机制：

```python
# 写入边界校验链:
# 1. 工具边界: write_file、apply_patch 和授权命令写入
# 2. 路径校验:
#    - normal 模式只拒绝 path_dangerous_roots
#    - full 模式不做危险目录路径拒绝
#    - workspace_root 是相对路径基准，不是普通产物唯一白名单
#    - 不能包含控制字符
#    - 长度不超过 4096 字符
# 3. 目录黑名单:
#    - .git/ (版本控制)
#    - config/ (配置文件)
#    - docs/ (文档)
#    - agent_py_agent/agent/settings/ (代码配置)
# 4. 文件锁定:
#    - 被锁定的文件不可修改
```

### 5.2 Subagent Isolation / 子代理隔离

| 边界 | 说明 |
|---|---|
| 文件系统 | 子代理只能写入 `data/subagents/{task_id}/` 目录 |
| 工具访问 | 子代理只能使用白名单中的工具 |
| 模型访问 | 子代理使用受限的 runner prompt，不能访问系统提示词 |
| 网络访问 | 子代理的 Web 工具受 `allowed_domains` 限制 |
| Shell 执行 | 子代理的 Shell 工具受 `allowed_commands` 限制 |

### 5.3 Gateway Security / Gateway 安全

| 边界 | 说明 |
|---|---|
| 租约机制 | `lease.py` 防止多个 gateway 实例同时运行 |
| PID 追踪 | `daemon_control.py` 通过 PID 文件追踪进程 |
| 进程监管 | `supervisor.py` 自动重启崩溃的 gateway |
| 请求校验 | `http_service.py` 校验请求来源和格式 |

### 5.4 Configuration Security / 配置安全

| 规则 | 说明 |
|---|---|
| 不提交敏感配置 | `.env`, API key 文件必须在 `.gitignore` |
| 配置归一化 | `config_normalize.py` 过滤未知字段，防止注入 |
| 配置校验 | `AgentConfig` 使用 dataclass 类型检查 |
| 配置隔离 | 运行时配置覆盖不修改磁盘上的配置文件 |

### 5.5 Audit Trail / 审计追踪

| 操作 | 审计要求 |
|---|---|
| 子代理创建/销毁 | 必须写入审计日志 |
| 文件写入 | 必须记录写入路径和来源 |
| 配置变更 | 必须记录变更内容和来源 |
| 权限变更 | 必须记录授权/撤销操作 |
| 失败事件 | 必须记录失败原因和自省结果 |
