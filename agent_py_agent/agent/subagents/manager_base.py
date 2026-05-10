# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""SubAgentBaseMixin facade for core lifecycle and work-order operations.

Work-order path/file helpers live in manager_work_orders.py to keep this mixin small.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .manager_work_orders import (
    build_work_order_paths,
    ensure_work_order_files,
    validate_work_order,
    write_takeover_file,
)
from .models import SubAgentCard, SubAgentTask, TakeoverRecord, WorkOrderValidation
from .services.base import CreateRunParams
from .utils import _new_id

if TYPE_CHECKING:
    from ..local_store import LocalStore


# LLM: SubAgentManagerInitParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent管理器init参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubAgentManagerInitParams:

    local_store: LocalStore | None = None
    workspace_root: str | Path | None = None
    workspace_roots: list[str | Path] | None = None
    role_template_dirs: list[str | Path] | None = None
    enable_self_learning: bool = False
    debug_trace_level: int = 0


# LLM: SubAgentBaseMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent基础混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentBaseMixin:
    """Facade delegating core task lifecycle to services."""

    # LLM: __init__ 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def __init__(
        self,
        workspace: str | Path,
        *,
        params: SubAgentManagerInitParams | None = None,
        local_store: LocalStore | None = None,
        workspace_root: str | Path | None = None,
        workspace_roots: list[str | Path] | None = None,
        role_template_dirs: list[str | Path] | None = None,
        enable_self_learning: bool = False,
    ):
        params = params or SubAgentManagerInitParams(
            local_store=local_store,
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            role_template_dirs=role_template_dirs,
            enable_self_learning=enable_self_learning,
        )
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}
        self.local_store = params.local_store
        self.workspace_root = Path(params.workspace_root).resolve() if params.workspace_root else self.workspace.resolve().parent
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, params.workspace_roots)
        self.role_template_dirs = _normalized_template_dirs(self.workspace_root, params.role_template_dirs)
        self.enable_self_learning = bool(params.enable_self_learning)
        self.debug_trace_level = _normalize_debug_trace_level(params.debug_trace_level)

        from .services.base import SubAgentBaseService
        from .services.lifecycle import SubAgentLifecycleService
        from .services.persistence import SubAgentPersistenceService

        self.lifecycle = SubAgentLifecycleService(self)
        self.persistence = SubAgentPersistenceService(self)
        self.base_service = SubAgentBaseService(self)

    # LLM: split must preserve configured default allowed_tools when spawn_subagents delegates through the manager.
    # 函数用途: 拆分目标并创建子任务；可传入默认工具白名单，保证真实 runner 拿到父级配置的工具边界。
    def split(
        self,
        goal: str,
        count: int,
        *,
        workflow_mode: str = "off",
        allowed_tools: list[str] | None = None,
    ) -> list[SubAgentTask]:
        return self.base_service.split(
            goal,
            count,
            workflow_mode=workflow_mode,
            allowed_tools=allowed_tools,
        )

    # LLM: register_card 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理registercard相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def register_card(self, card: SubAgentCard) -> None:
        self.cards[card.name] = card

    # LLM: create_run 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建createrun所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def create_run(
        self,
        *,
        params: CreateRunParams | None = None,
        goal: str = "",
        thought: str = "",
        plan: list[str] | None = None,
        agent_name: str = "general",
        role: str = "general",
        parent_id: str = "",
        root_id: str = "",
        depth: int = 0,
        allowed_skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        owner: str = "",
        supervisor: str = "",
        final_owner: str = "",
        acceptance_checks: list[str] | None = None,
        quality_contract: Any = None,
        context_manifest: Any = None,
        context_packs: Any = None,
        extra_write_roots: list[str] | None = None,
        workflow_mode: str = "off",
    ) -> SubAgentTask:
        params = params or CreateRunParams(
            goal=goal,
            thought=thought,
            plan=plan or [],
            agent_name=agent_name,
            role=role,
            parent_id=parent_id,
            root_id=root_id,
            depth=depth,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            owner=owner,
            supervisor=supervisor,
            final_owner=final_owner,
            acceptance_checks=acceptance_checks,
            quality_contract=quality_contract,
            context_manifest=context_manifest,
            context_packs=context_packs,
            extra_write_roots=extra_write_roots,
            workflow_mode=workflow_mode,
        )
        return self.base_service.create_run(params=params)

    # LLM: record_takeover 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入takeover的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ):
        return self.base_service.record_takeover(run_id, take_over_by=take_over_by, reason=reason, locked_files=locked_files)

    # LLM: load 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询load需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def load(self, run_id: str) -> SubAgentTask:
        return self.persistence.load(run_id)

    # LLM: list_runs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询runs需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def list_runs(self) -> list[SubAgentTask]:
        return self.persistence.list_runs()

    # LLM: save 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入save的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def save(self, task: SubAgentTask) -> None:
        self.persistence.save(task)

    # LLM: save_hierarchy_links is reserved for controlled reparent operations that intentionally remove child edges.
    # 函数用途: 精确保存任务的 child_ids，用于显式领导权恢复/子树重挂；普通保存仍走 save() 的防覆盖合并。
    def save_hierarchy_links(self, task: SubAgentTask) -> None:
        self.persistence.save(task, preserve_child_links=False)

    # LLM: add_child 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理add子级相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def add_child(self, parent_id: str, child_id: str) -> None:
        try:
            parent = self.load(parent_id)
        except FileNotFoundError:
            return
        if child_id not in parent.child_ids:
            parent.child_ids.append(child_id)
            parent.updated_at = time.time()
            self.save(parent)

    # LLM: _build_work_order_paths 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建workorder路径所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _build_work_order_paths(
        self,
        run_id: str,
        task_dir: str | Path | None = None,
        extra_write_roots: list[str] | None = None,
    ) -> dict[str, object]:
        return build_work_order_paths(self, run_id, task_dir, extra_write_roots)

    # LLM: _ensure_work_order_files 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验workorder文件需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _ensure_work_order_files(self, task: SubAgentTask) -> None:
        ensure_work_order_files(task)

    # LLM: _write_takeover_file 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入takeover文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _write_takeover_file(self, task: SubAgentTask, record: TakeoverRecord) -> None:
        write_takeover_file(task, record)

    # LLM: validate_work_order 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验workorder需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def validate_work_order(self, run_id: str) -> WorkOrderValidation:
        return validate_work_order(self, run_id)

    # LLM: _new_id 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建id所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _new_id(self, prefix: str) -> str:
        return _new_id(prefix)


# LLM: _normalized_workspace_roots 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化normalizedworkspaceroots的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalized_workspace_roots(primary: Path, roots: list[str | Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: _normalized_template_dirs adds the standard user role-template directory when config is empty.
# 函数用途: 子代理角色模板目录为空时默认使用工作区 `.agent/subagents/roles`，用户无需配置即可扩展。
def _normalized_template_dirs(primary: Path, dirs: list[str | Path] | None) -> list[Path]:
    raw_dirs = dirs if dirs else [primary / ".agent" / "subagents" / "roles"]
    resolved: list[Path] = []
    for raw in raw_dirs:
        path = Path(raw)
        if not path.is_absolute():
            path = primary / path
        path = path.resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: _normalize_debug_trace_level keeps direct manager construction aligned with AgentConfig normalization.
# 函数用途: 把子代理调试追踪等级裁剪到 0-5，坏值按 0 关闭，避免测试配置把 manager 初始化打崩。
def _normalize_debug_trace_level(value: object) -> int:
    try:
        level = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, level))


from .services.base import _extract_write_dirs
from .services.persistence import (
    _field_names,
    _list_value,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
    _string_list_value,
)
from .services.workflow import (
    _CODING_SUBAGENT_TOOLS,
    _READ_ONLY_SUBAGENT_TOOLS,
    _WORKFLOW_MODES,
    _normalize_workflow_mode_value,
    _workflow_worker_tools,
)
