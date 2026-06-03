# SubAgent Service Refactor Plan
# 子代理服务重构计划：从分布式上帝类到服务化架构

这是一份低风险迁移计划。不要一次性重写状态机，先把边界切出来。每一步都要保证旧测试通过。

---

## 1. Current State Analysis / 现状分析

### 1.1 The Distributed God Class Problem / 分布式上帝类问题

历史上 `SubAgentManager` 由多组业务 mixin 拼合而成，总行数超过 8,000 行。当前主链路已迁为服务组合，manager 只保留基础持久化骨架和 kernel 骨架：

```python
class SubAgentManager(
    SubAgentBaseMixin,           # 基础 CRUD + 卡片 + 工作流 + 校验骨架
    SubagentKernelMixin,         # kernel 快照和查询骨架
    SubAgentLifecycleService,    # service - 能力记录、证据、心跳和 runner attempt
    SubAgentCapabilityService,   # service - 能力请求路由和 route report
    SubAgentActionService,       # service - action apply 执行和审计记录
    SubAgentPatchService,        # service - patch审核、应用、回滚和测试
    SubAgentRunnerContextService,# service - runner上下文和写入边界
    SubAgentRunnerResultService, # service - runner结果记录和唤醒通知
    SubAgentChannelProbeService, # service - 通道健康探测和证据报告
    SubAgentBudgetService,       # service - runner refs-only 预算报告
    SubAgentLearningService,     # service - 学习反馈候选草稿
    SubAgentMemoryGateService,   # service - run-local memory gate 审核/导出
    SubAgentIndexingService,     # service - 索引管理和 LocalStore 事件
    SubAgentWorkflowService,     # service - workflow plan/realize
    SubAgentHierarchyService,    # service - hierarchy schedule/recovery
    SubAgentDispatchService,     # service - dispatch/watch report
    SubAgentParentPlannerService,# service - parent planner report
):
    pass  # 兼容 facade；新能力应继续迁到 service composition
```

`SubAgentBoardMixin` 已从继承链移除，公开 board API 由 `SubAgentManager` 薄转发到 `SubAgentBoardFacade`，再委托 `SubAgentBoardService`。

`SubAgentLearningMixin` 也已从继承链移除。学习候选草稿的保存、去重、确认和统计进入
`services/learning.py`；`manager_learning.py` 暂时保留兼容 wrapper 和 helper re-export，
避免旧测试/旧 import 立即断裂。

`SubAgentBudgetMixin` 已从继承链移除。runner refs-only 预算报告进入 `services/budget.py`；
旧 `manager_budget.py` 已删除，外部直接使用 `SubAgentManager` 或 `SubAgentBudgetService`。

`SubAgentMemoryGateMixin` 已从继承链移除。run-local memory gate 候选列出、审核、导出、
retention 和 verifier 进入 `services/memory_gate.py`；旧 `manager_memory_gate.py` 已删除。

`SubAgentLifecycleMixin` 已从继承链移除。能力请求、能力授权、能力缺口、证据、状态、
心跳和 runner attempt 更新进入 `services/lifecycle.py` 与
`services/lifecycle_runner_attempts.py`；`manager_lifecycle.py` 暂时保留兼容 wrapper。

`SubAgentCapabilityMixin` 已从继承链移除。OPEN capability request 到 skill/tool card 的路由、
route report 写入和 grant/gap 记录衔接进入 `services/capabilities/`；
旧 `manager_capabilities.py` 已删除。

`SubAgentActionMixin` 已从继承链移除。action apply、action apply report、任务 work log 和
action audit log 进入 `services/actions/`；`manager_actions.py` 暂时保留兼容 wrapper。

`SubAgentPatchMixin` 已从继承链移除。patch review/apply/report/rollback 通过
`services/patch_apply/facade.py` 组合 `patch/patch_service.py` 和 `patch/patch_apply.py`；
`manager_patch.py` 暂时保留兼容 wrapper 和旧 helper re-export。

`SubAgentRunnerContextMixin` 已从继承链移除。runner execution context、context bundle、
write boundary 和 grant 注入进入 `services/runner_context/`；
`manager_runner_context.py` 暂时保留兼容 wrapper。

`SubAgentRunnerResultMixin` 已从继承链移除。runner result 记录、structured output 处理、
debrief、learning side effects、session compact 和 completion wake 进入
`services/runner_result/`；`manager_runner_results.py` 暂时保留兼容 wrapper 和
`RecordRunnerResultParams` re-export。

`SubAgentChannelProbeMixin` 已从继承链移除。通道健康探测、probe 证据写入和批量报告进入
`services/channel_probe.py`；`manager_channel_probe.py` 暂时保留兼容 wrapper。

`SubAgentWorkflowMixin` 已从继承链移除。workflow 规划和 materialize worker 的实现继续由
`services/workflow.py` 承担，`SubAgentManager` 只通过 `SubAgentWorkflowService` 组合转发。

`SubAgentIndexingMixin` 已从继承链移除。任务索引、LocalStore 事件、报告索引和 dataclass
record 索引进入 `services/indexing/`；`manager_indexing.py` 暂时保留兼容 wrapper，
`SubAgentManager` 只通过 `SubAgentIndexingService` 组合转发。

`SubAgentHierarchyMixin` 已从继承链移除。child scheduling、hierarchy recovery packet、
leadership recovery 计划/应用进入 `services/hierarchy/facade.py`；`services/hierarchy/facade.py`
是唯一实现入口，旧 `manager_hierarchy.py` 已删除，`SubAgentManager` 只通过
`SubAgentHierarchyService` 组合转发。

`SubAgentDispatchMixin` 已从继承链移除。dispatch record 和 dispatch watch 进入
`services/dispatch/service.py`；parent planner report 进入
`services/dispatch/parent_planner_service.py`。`manager_dispatch.py` 暂时保留兼容 wrapper，
`SubAgentManager` 只通过 `SubAgentDispatchService` 与 `SubAgentParentPlannerService` 组合转发。

### 1.2 Why This is Dangerous / 为什么这是危险的

**问题 1: 无边界的 `self` 访问**
每个 mixin 都可以通过 `self.xxx` 访问其他 mixin 的所有方法和属性。例如 `manager_patch.py` 中的 `_review_patch_task()` 可以调用 `self.load()`（来自 base）、`self.save()`（来自 base）、`self._render_runner_item_line()`（来自 runner_rendering）。这使得每个 mixin 都隐式依赖整个类。

**问题 2: 测试隔离困难**
要测试 patch 审核逻辑，必须实例化完整的 `SubAgentManager`，因为方法内部通过 `self` 访问持久化、渲染、策略等所有功能。

**问题 3: 变更影响不可预测**
修改 `manager_base.py` 中的 `save()` 方法可能影响所有其他 13 个 mixin 的行为，因为它们都通过 `self.save()` 调用。

**问题 4: 职责边界模糊**
`manager_base.py` (744行) 承担了：数据归一化（`_normalize_quality_contract`）、CRUD（`load`, `save`, `list_runs`）、工作流管理（`ensure_workflow_plan`, `realize_workflow_plan`）、工作单校验（`validate_work_order`）、接管记录（`record_takeover`）等多种职责。

### 1.3 Current Extraction Progress / 当前提取进度

已完成的提取：

| 服务 | 源文件 | 方法 | 状态 |
|---|---|---|---|
| `SubAgentPersistenceService` | `services/persistence/` | `load`, `list_runs`, `save`, 归一化 | 已完成 |
| `SubAgentLifecycleService` | `services/lifecycle.py`, `services/lifecycle_runner_attempts.py` | `record_capability_request`, `record_capability_grant`, `record_capability_gap`, `record_verification_evidence`, `update_status`, `heartbeat`, `prepare_runner_attempt`, `abandon_runner_attempt` | 已完成 |
| `SubAgentCapabilityService` | `services/capabilities/` | `route_capability_requests`, `write_capability_route_report`, grant/gap 路由衔接 | 已完成 |
| `SubAgentActionService` | `services/actions/` | action apply、action apply report、task work log、action audit log | 已完成 |
| `SubAgentPatchService` | `services/patch_apply/facade.py`, `patch/` | patch review、apply、report、rollback、diff/test helper | 已完成 |
| `SubAgentRunnerContextService` | `services/runner_context/` | execution context、context bundle、write boundary、grant 注入 | 已完成 |
| `SubAgentRunnerResultService` | `services/runner_result/` | runner result 记录、debrief、session compact、completion wake | 已完成 |
| `SubAgentBoardService` + `SubAgentBoardFacade` | `services/board/service.py`, `services/board/facade.py` | board item、risk flags、board/due-check/action-plan 写入 | 已完成 |
| `SubAgentBudgetService` | `services/budget.py` | runner refs-only 预算报告生成和落盘 | 已完成 |
| `SubAgentLearningService` | `services/learning.py` | 学习候选草稿保存、去重、确认、统计 | 已完成 |
| `SubAgentMemoryGateService` | `services/memory_gate.py` | memory gate 候选列出、审核、导出、retention、verifier | 已完成 |
| `SubAgentChannelProbeService` | `services/channel_probe.py` | 通道健康探测、probe 证据写入和报告落盘 | 已完成 |
| `SubAgentWorkflowService` | `services/workflow.py` | workflow 规划和 worker materialize | 已完成 |
| `SubAgentIndexingService` | `services/indexing/` | 任务索引、LocalStore 事件、报告索引 | 已完成 |
| `SubAgentHierarchyService` | `services/hierarchy/facade.py` | child scheduling、hierarchy recovery、leadership recovery report | 已完成 |
| `SubAgentDispatchService` | `services/dispatch/service.py` | dispatch record、watch report | 已完成 |
| `SubAgentParentPlannerService` | `services/dispatch/parent_planner_service.py` | parent planner prompt/response 和 report | 已完成 |

---

## 2. Target Service Decomposition / 目标服务分解

### 2.1 Service Architecture Overview / 服务架构概览

```
SubAgentManager (facade, 纯组合)
    |
    ├── LifecycleService       # 创建/暂停/恢复/放弃/状态变更
    ├── DispatchService        # 调度派工/并发控制/重试
    ├── AcceptanceService      # 验收流程/发现处理
    ├── PatchService           # patch审核/应用/回滚/测试
    ├── CapabilityService      # 能力路由/请求/授权/缺口
    ├── RunnerContextService   # runner上下文注入/指令构建
    ├── RunnerResultService    # runner结果处理/归档
    ├── BoardService           # 看板渲染/报告生成
    ├── IndexingService        # 索引管理/搜索
    └── LearningService        # 学习反馈/自省
```

### 2.2 Service Definitions / 服务定义

#### LifecycleService（生命周期服务）

**职责**: 子代理任务的创建、状态变更、暂停、恢复、放弃

**当前来源**: `manager_base.py` 中的 `create_run`, `split`, `add_child` + `manager_lifecycle.py`

**目标方法**:
```python
class SubAgentLifecycleService:
    def __init__(self, persistence: SubAgentPersistenceService):
        self.persistence = persistence

    def create_task(self, ...) -> SubAgentTask:
        """创建子代理任务"""

    def split_goal(self, goal: str, count: int, ...) -> list[SubAgentTask]:
        """拆分目标为多个子任务"""

    def pause(self, run_id: str, reason: str) -> SubAgentTask:
        """暂停任务"""

    def resume(self, run_id: str) -> SubAgentTask:
        """恢复任务"""

    def abandon(self, run_id: str, reason: str) -> SubAgentTask:
        """放弃任务"""

    def update_status(self, run_id: str, status: str, ...) -> SubAgentTask:
        """更新状态"""

    def heartbeat(self, run_id: str) -> SubAgentTask:
        """心跳更新"""

    def add_child(self, parent_id: str, child_id: str) -> None:
        """添加子任务关系"""
```

**依赖**: `PersistenceService`

#### DispatchService（调度派工服务）

**职责**: 调度决策、并发控制、重试策略、runner 分发

**当前实现**: `services/dispatch/` 是唯一业务实现入口，`manager_dispatch.py` 暂时保留兼容 facade。

**目标方法**:
```python
class SubAgentDispatchService:
    def __init__(self, lifecycle: SubAgentLifecycleService, persistence: SubAgentPersistenceService):
        self.lifecycle = lifecycle
        self.persistence = persistence

    def dispatch_runners(self, tasks: list[SubAgentTask], ...) -> DispatchReport:
        """分发 runner 执行任务"""

    def check_due(self, task: SubAgentTask) -> list[DueCheckIssue]:
        """检查任务到期状态"""

    def resolve_concurrency(self, tasks: list[SubAgentTask]) -> int:
        """解析并发数"""

    def resolve_timeout(self, task: SubAgentTask) -> int:
        """解析超时时间"""

    def should_retry(self, task: SubAgentTask, error: Exception) -> bool:
        """判断是否应该重试"""
```

**依赖**: `LifecycleService`, `PersistenceService`

#### AcceptanceService（验收服务）

**职责**: 验收流程、验收发现处理、质量门禁

**当前来源**: `manager_acceptance.py` (293行) + `manager_acceptance_findings.py` (390行) + `acceptance_helpers.py` (364行)

**目标方法**:
```python
class SubAgentAcceptanceService:
    def __init__(self, persistence: SubAgentPersistenceService):
        self.persistence = persistence

    def evaluate_completion(self, task: SubAgentTask) -> AcceptanceResult:
        """评估任务完成度"""

    def process_findings(self, task: SubAgentTask, findings: list[Finding]) -> AcceptanceResult:
        """处理验收发现"""

    def check_quality_gate(self, task: SubAgentTask) -> QualityGateResult:
        """检查质量门禁"""

    def generate_acceptance_report(self, task: SubAgentTask) -> AcceptanceReport:
        """生成验收报告"""
```

**依赖**: `PersistenceService`

#### PatchService（Patch 服务）

**职责**: patch 审核、应用、回滚、测试执行

**当前来源**: `manager_patch.py` (794行) — 这是最大的待拆分文件

**目标方法**:
```python
class SubAgentPatchService:
    def __init__(self, persistence: SubAgentPersistenceService):
        self.persistence = persistence

    def review_patches(self, task: SubAgentTask, ...) -> PatchReviewReport:
        """审核 patch"""

    def apply_patches(self, task: SubAgentTask, ...) -> PatchApplyReport:
        """应用 patch"""

    def rollback_patches(self, task: SubAgentTask, record: PatchApplyRecord) -> None:
        """回滚 patch"""

    def run_patch_tests(self, commands: list[str]) -> list[dict]:
        """执行 patch 测试"""

    def validate_patch_target(self, raw_path: str) -> Path:
        """校验 patch 目标路径"""

    def build_unified_diff(self, path: str, before: str, after: str) -> str:
        """构建统一 diff"""
```

**依赖**: `PersistenceService`, `write_boundary`（安全校验）

#### CapabilityService（能力服务）

**职责**: 能力路由、请求、授权、缺口管理

**当前来源**: `services/capabilities/` + `services/lifecycle.py` 中的能力记录方法

**目标方法**:
```python
class SubAgentCapabilityService:
    def route_capability_requests(self, router, config=None, ...) -> CapabilityRouteReport:
        """把 OPEN capability request 路由到 skill/tool card"""

    def write_capability_route_report(self, router, config=None, ...) -> CapabilityRouteReport:
        """写入 route report，并在 apply 时写入 route 审计记录"""
```

**依赖**: `PersistenceService`, `capability/router.py`

#### RunnerContextService（Runner 上下文服务）

**职责**: runner 上下文注入、指令构建、工具白名单

**当前来源**: `services/runner_context/` + `agent_core/planner.py`

**目标方法**:
```python
class SubAgentRunnerContextService:
    def build_runner_instruction(self, task: SubAgentTask, ...) -> str:
        """构建 runner 指令"""

    def inject_context(self, task: SubAgentTask, ...) -> dict:
        """注入上下文"""

    def resolve_allowed_tools(self, task: SubAgentTask) -> list[str]:
        """解析允许的工具"""

    def build_write_boundary(self, task: SubAgentTask) -> dict:
        """构建写入边界"""
```

**依赖**: 无（纯函数服务）

#### RunnerResultService（Runner 结果服务）

**职责**: runner 结果处理、归档、token 统计

**当前来源**: `services/runner_result/` + `result_processors.py` (358行)

**目标方法**:
```python
class SubAgentRunnerResultService:
    def __init__(self, persistence: SubAgentPersistenceService):
        self.persistence = persistence

    def process_result(self, task: SubAgentTask, result: RunnerResult) -> SubAgentTask:
        """处理 runner 结果"""

    def archive_result(self, task: SubAgentTask, result: RunnerResult) -> None:
        """归档结果"""

    def update_token_usage(self, task: SubAgentTask, tokens: int) -> None:
        """更新 token 使用量"""

    def extract_findings(self, result: RunnerResult) -> list[Finding]:
        """提取发现"""
```

**依赖**: `PersistenceService`

#### BoardService（看板服务）

**职责**: 看板渲染、报告生成、摘要

**当前来源**: `services/board/service.py` + `services/board/facade.py` + `rendering.py` + `reports.py`

**目标方法**:
```python
class SubAgentBoardService:
    def render_board(self, tasks: list[SubAgentTask], ...) -> str:
        """渲染看板（终端格式）"""

    def render_html_board(self, tasks: list[SubAgentTask], ...) -> str:
        """渲染看板（HTML 格式）"""

    def generate_summary(self, task: SubAgentTask) -> str:
        """生成任务摘要"""

    def generate_work_log(self, task: SubAgentTask) -> str:
        """生成工作日志"""
```

**依赖**: 无（纯渲染服务）

#### IndexingService（索引服务）

**职责**: 索引管理、搜索、模糊匹配

**当前来源**: `manager_indexing.py` (382行)

**目标方法**:
```python
class SubAgentIndexingService:
    def build_index(self, tasks: list[SubAgentTask]) -> None:
        """构建索引"""

    def search(self, query: str) -> list[SubAgentTask]:
        """搜索任务"""

    def fuzzy_match(self, query: str) -> list[SubAgentTask]:
        """模糊匹配"""
```

**依赖**: `PersistenceService`

#### LearningService（学习服务）

**职责**: 学习反馈、自省结果记录

**当前来源**: `manager_learning.py` (255行)

**目标方法**:
```python
class SubAgentLearningService:
    def record_feedback(self, task: SubAgentTask, feedback: dict) -> None:
        """记录反馈"""

    def record_introspection(self, task: SubAgentTask, introspection: dict) -> None:
        """记录自省结果"""

    def get_learning_history(self, run_id: str) -> list[dict]:
        """获取学习历史"""
```

**依赖**: `PersistenceService`

---

## 3. Target Repositories / 目标仓库

### 3.1 Repository Interfaces (Domain Ports) / 仓库接口（领域端口）

```python
# domain/subagent/ports.py

from typing import Protocol

class TaskRepository(Protocol):
    """子代理任务仓库接口"""

    def load(self, run_id: str) -> SubAgentTask: ...
    def save(self, task: SubAgentTask) -> None: ...
    def list_all(self) -> list[SubAgentTask]: ...
    def list_by_status(self, status: str) -> list[SubAgentTask]: ...
    def delete(self, run_id: str) -> None: ...

class ArtifactRepository(Protocol):
    """子代理产物仓库接口（patch、报告、日志）"""

    def save_patch_review(self, record: PatchReviewRecord) -> None: ...
    def save_patch_apply(self, record: PatchApplyRecord) -> None: ...
    def load_patch_review(self, record_id: str) -> PatchReviewRecord: ...
    def load_patch_apply(self, record_id: str) -> PatchApplyRecord: ...
    def save_report(self, report: Report) -> None: ...
    def list_artifacts(self, run_id: str) -> list[Artifact]: ...

class IndexRepository(Protocol):
    """子代理索引仓库接口"""

    def build_index(self, tasks: list[SubAgentTask]) -> None: ...
    def search(self, query: str) -> list[str]: ...  # 返回 run_id 列表
    def update_index(self, task: SubAgentTask) -> None: ...
```

### 3.2 Repository Implementations / 仓库实现

```python
# infrastructure/persistence/repositories/task_repo.py

class JsonlTaskRepository:
    """基于 JSONL 文件的任务仓库实现"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def load(self, run_id: str) -> SubAgentTask:
        path = self.data_dir / f"{run_id}.json"
        # 读取并归一化
        ...

    def save(self, task: SubAgentTask) -> None:
        path = self.data_dir / f"{task.run_id}.json"
        # 写入 JSON
        ...
```

---

## 4. Migration Order and Compatibility Strategy / 迁移顺序与兼容策略

### 4.1 Migration Principles / 迁移原则

1. **永不停机**: 每一步迁移都保持 `SubAgentManager` 的公开 API 不变
2. **小步快走**: 每次只提取一个服务，确保测试通过后再继续
3. **Facade 兼容**: `SubAgentManager` 保持为纯组合类，内部逐步委托给服务
4. **测试先行**: 提取服务前先为现有行为编写测试

### 4.2 Migration Steps / 迁移步骤

#### Step 1: 完善 PersistenceService（已完成）
- [x] `services/persistence/` 已提取 `load`, `list_runs`, `save`, 归一化逻辑
- [x] `SubAgentBaseMixin` 已委托 persistence 方法给服务
- [x] 测试: `test_subagent_persistence_service.py`

#### Step 2: 完善 LifecycleService（已完成）
- [x] `services/lifecycle.py` 已提取能力请求/授予/缺口/证据/状态/心跳
- [x] `SubAgentLifecycleMixin` 已委托 lifecycle 方法给服务
- [x] 测试: `test_subagent_lifecycle_service.py`

#### Step 2.5: 移除 BoardMixin（已完成）
- [x] 删除 `manager_board.py`
- [x] 新增 `services/board/facade.py`，让旧 board API 保持可用但不再挂到继承链
- [x] `SubAgentManager` 只保留薄转发：`build_board`、`write_board`、`due_check`、`plan_actions`
- [x] 测试: `test_manager_board_class.py`, `test_manager_board_actions_class.py`, `test_subagent_coordinator_due_check.py`

#### Step 3: 提取 PatchService（已完成）
- [x] 使用现有 `patch/` 子包承载 review/apply/renderer 实现，新增 `services/patch_apply/facade.py`
- [x] 从 `manager_patch.py` 提取 `review_patches`, `apply_patches`, `rollback_patches`
- [x] 从 `manager_patch.py` 提取辅助方法（路径校验、diff 构建、测试执行）
- [x] `SubAgentManager` 不再继承 `SubAgentPatchMixin`，主链路通过 `self.patch` 服务组合转发
- [x] 测试: `test_manager_patch.py`, `test_subagent_bundle_interfaces.py`, `test_public_aliases.py`

#### Step 4: 提取 AcceptanceService
- [ ] 创建 `services/acceptance.py`
- [ ] 从 `manager_acceptance.py` 提取验收流程
- [ ] 从 `manager_acceptance_findings.py` 提取发现处理
- [ ] 从 `acceptance_helpers.py` 提取辅助函数
- [ ] 测试: `test_subagent_acceptance_service.py`

#### Step 5: 提取 DispatchService
- [x] 创建 `services/dispatch/`
- [x] 从 `manager_dispatch.py` 提取 dispatch/watch 报告逻辑到 `SubAgentDispatchService`
- [x] 从 `manager_dispatch.py` 提取 parent planner 报告逻辑到 `SubAgentParentPlannerService`
- [x] `SubAgentManager` 不再继承 `SubAgentDispatchMixin`，主链路通过 `self.dispatch` 和 `self.parent_planner` 服务组合转发
- [x] 测试: `test_manager_dispatch.py`, `test_manager_indexing.py`, `test_subagent_debug_trace.py`, `test_dispatch_mixin.py`, `test_dispatch_loop.py`, `test_dispatch_loop_class.py`

#### Step 6: 提取 BoardService（已完成）
- [x] `services/board/service.py` 已承载看板核心服务
- [x] `services/board/facade.py` 已承载旧公开 API 的兼容转发
- [x] `SubAgentManager` 已不再继承 board mixin
- [x] 测试: `test_manager_board_class.py`, `test_manager_board_actions_class.py`, `test_subagent_coordinator_due_check.py`

#### Step 7: 提取剩余服务
- [x] `services/capabilities/` — 从 `services/capabilities/` 提取 capability route
- [x] `services/runner_context/` — 从 `manager_runner_context.py` 提取
- [x] `services/runner_result/` — 从 `manager_runner_results.py` 提取
- [x] `services/indexing/` — 从 `manager_indexing.py` 提取
- [x] `services/learning.py` — 从 `manager_learning.py` 提取

#### Step 8: 引入 Repository Pattern
- [ ] 定义 `domain/subagent/ports.py`（TaskRepository, ArtifactRepository, IndexRepository）
- [ ] 实现 `infrastructure/persistence/repositories/`
- [ ] 服务层使用 Repository 接口而非直接文件操作

### 4.3 Facade Compatibility / Facade 兼容性

迁移过程中 `SubAgentManager` 始终保持公开 API 不变：

```python
class SubAgentManager:
    """公开的子代理管理门面，内部委托给服务。"""

    def __init__(self, ...):
        # 初始化所有服务
        self.persistence = SubAgentPersistenceService(...)
        self.lifecycle = SubAgentLifecycleService(self.persistence)
        self.patch = SubAgentPatchService(self.persistence)
        self.acceptance = SubAgentAcceptanceService(self.persistence)
        self.dispatch = SubAgentDispatchService(self.lifecycle, self.persistence)
        # ...

    # ---- 兼容方法（委托给服务）----

    def load(self, run_id: str) -> SubAgentTask:
        return self.persistence.load(run_id)

    def save(self, task: SubAgentTask) -> None:
        self.persistence.save(task)

    def create_run(self, ...) -> SubAgentTask:
        return self.lifecycle.create_task(...)

    def review_patches(self, ...) -> PatchReviewReport:
        return self.patch.review_patches(...)

    # ... 所有旧方法名保留为 facade
```

---

## 5. Testing Strategy / 测试策略

### 5.1 Unit Tests / 单元测试

每个服务独立测试，不依赖完整的 `SubAgentManager`：

```python
# test_subagent_patch_service.py

def test_review_patches_creates_report(tmp_path):
    """patch 审核应生成审核报告"""
    persistence = SubAgentPersistenceService(tmp_path)
    patch_service = SubAgentPatchService(persistence)

    # 创建测试任务
    task = create_test_task(tmp_path)
    persistence.save(task)

    # 审核 patch
    report = patch_service.review_patches(task, ...)

    assert report.status == "completed"
    assert len(report.records) > 0

def test_apply_patches_rollback_on_failure(tmp_path):
    """patch 应用失败应回滚"""
    persistence = SubAgentPersistenceService(tmp_path)
    patch_service = SubAgentPatchService(persistence)

    task = create_test_task(tmp_path)
    persistence.save(task)

    # 模拟失败
    with pytest.raises(PatchApplyError):
        patch_service.apply_patches(task, invalid_patch)

    # 验证已回滚
    assert not (tmp_path / "patched_file.txt").exists()
```

### 5.2 Integration Tests / 集成测试

通过 `SubAgentManager` facade 测试完整流程：

```python
# test_subagent_manager_integration.py

def test_full_lifecycle(tmp_path):
    """完整生命周期: 创建 -> 派工 -> 执行 -> 验收"""
    manager = SubAgentManager(tmp_path, ...)

    # 创建任务
    task = manager.create_run(goal="test task", ...)

    # 派工
    report = manager.dispatch_runners([task], ...)

    # 验收
    result = manager.evaluate_completion(task)

    assert task.status == "completed"
```

### 5.3 Regression Tests / 回归测试

迁移前后行为必须完全一致：

```python
def test_load_save_roundtrip_compatibility(tmp_path):
    """迁移前后的 load/save 行为必须兼容"""
    # 用旧方式保存
    old_manager = SubAgentManager(tmp_path, ...)
    old_task = old_manager.create_run(goal="test", ...)
    old_manager.save(old_task)

    # 用新方式加载
    new_manager = SubAgentManager(tmp_path, ...)
    new_task = new_manager.load(old_task.run_id)

    assert new_task.goal == old_task.goal
    assert new_task.status == old_task.status
```

---

## 6. Rollback Strategy / 回滚策略

### 6.1 Feature Flags / 功能开关

每个服务提取可以通过配置开关控制：

```python
# settings/config.py
@dataclass
class AgentConfig:
    # ...
    use_patch_service: bool = True  # False 时回退到 mixin 方式
    use_acceptance_service: bool = True
    use_dispatch_service: bool = True
```

### 6.2 Rollback Steps / 回滚步骤

如果某个服务提取导致问题：

1. 将对应的 `use_xxx_service` 设为 `False`
2. `SubAgentManager` 回退到 mixin 直接调用
3. 修复服务中的问题
4. 重新开启开关
5. 运行全量测试

### 6.3 Data Compatibility / 数据兼容性

- 不修改子代理状态文件的 schema
- 不修改 JSONL 记忆文件的格式
- 不修改工作单文件的格式
- 所有新服务使用与旧 mixin 完全相同的归一化逻辑

---

## 7. Stop Conditions / 停止条件

在以下情况下暂停迁移并添加 ADR：

- 服务提取导致现有测试失败且无法快速修复
- 需要修改子代理状态文件 schema
- 需要修改 CLI 命令行为
- 服务边界导致循环依赖
- 性能回退超过 10%
