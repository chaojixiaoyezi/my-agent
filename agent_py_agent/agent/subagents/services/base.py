
"""base task creation and lifecycle service.

这里承接子代理任务创建、分割、注册卡等基础能力。
SubAgentManager 通过当前服务组合调用这里。
运行身份会写 memory scope 和 runtime config scope，供 worker 装载 task overlay。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...common.id_generator import new_id as _framework_new_id
from ..authorization_gate import (
    AuthorizationError,
    OperationRequest,
    authorize_operation,
)
from ..effective_permissions import effective_permission_snapshot
from ..models import SubAgentTask
from .inheritance_manifest import build_inheritance_manifest
from .persistence.model_normalizers import (
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
)

if TYPE_CHECKING:
    from ..models import ContextManifest, QualityContract, SubAgentCard


@dataclass(frozen=True)
class CreateRunParams:
    """Bundle of create_run parameters."""

    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    allowed_skills: list[str] | None = None
    allowed_tools: list[str] | None = None
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    acceptance_checks: list[str] | None = None
    quality_contract: Any = None
    context_manifest: Any = None
    context_packs: Any = None
    extra_write_roots: list[str] | None = None
    normalize_role: bool = True
    attributes: dict[str, object] | None = None
    parent_access_mode: str = ""
    memory_retention_policy: str = "parent_review_or_cleanup"
    memory_delete_after_days: int = 0
    destroy_summary_required: bool = True


@dataclass(frozen=True)
class OutputRefRebinding:
    field: str
    from_ref: str
    to_ref: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "from": self.from_ref, "to": self.to_ref}


def _load_parent_task(manager: Any, parent_id: str):
    if not parent_id:
        return None
    try:
        return manager.load(parent_id)
    except FileNotFoundError:
        return None


# LLM: 路径比对只用于结构化 workspace/allow/deny 的精确同路径调和，不解析 goal。
# 函数用途: 把工作区路径归一化成可安全比较的绝对路径文本。
def _scope_path_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return ""


# LLM: 会话运行时 child 继承当前 turn 的 cwd 和完整 permission profile。本项目仅在
# local/unmanaged owner 下移除与已允许且已结构化继承的 workspace root 完全同路径的默认 deny；
# 远程 owner 的 home 围栏不受影响，`.ssh` 等更窄 deny 也始终保留。
# 函数用途: 消除“父级明确允许工作区，子代理又被同路径默认禁止”的矛盾。
def _reconciled_forbidden_write_roots(task: SubAgentTask) -> list[str]:
    owner_id = str(task.owner or "").strip()
    if owner_id and not owner_id.startswith("local/"):
        return list(task.forbidden_write_roots)
    attrs = task.attributes if isinstance(task.attributes, dict) else {}
    workspace_values: list[object] = [attrs.get("workspace_root")]
    raw_workspace_roots = attrs.get("workspace_roots")
    if isinstance(raw_workspace_roots, (list, tuple)):
        workspace_values.extend(raw_workspace_roots)
    workspace_roots = {_scope_path_key(item) for item in workspace_values}
    allowed_roots = {_scope_path_key(item) for item in task.allowed_write_roots}
    inherited_allowed = (workspace_roots & allowed_roots) - {""}
    return [
        item
        for item in task.forbidden_write_roots
        if _scope_path_key(item) not in inherited_allowed
    ]


def _session_identity_fields(run_id: str, parent_task: Any | None) -> dict[str, str]:
    session_id = f"session-{run_id}"
    parent_session = str(getattr(parent_task, "subagent_session_id", "") or "")
    root_session = str(getattr(parent_task, "root_subagent_session_id", "") or parent_session or session_id)
    return {
        "subagent_session_id": session_id,
        "agent_thread_id": f"thread-{run_id}",
        "parent_subagent_session_id": parent_session,
        "root_subagent_session_id": root_session,
    }


def register_collaboration_agent_capability(manager: Any, task: Any) -> None:
    store = getattr(manager, "collaboration_store", None)
    if store is None or not hasattr(store, "register_agent"):
        return
    try:
        from ...collaboration import AgentCapability

        store.register_agent(
            AgentCapability(
                agent_id=str(getattr(task, "id", "") or ""),
                role=str(getattr(task, "role", "") or ""),
                capabilities=tuple(collaboration_capabilities_for_task(task)),
                sources=tuple(collaboration_sources_for_task(task)),
                status="available",
                load=0.0,
                updated_at=time.time(),
                metadata={
                    "agent_name": str(getattr(task, "agent_name", "") or ""),
                    "run_id": str(getattr(task, "id", "") or ""),
                    "depth": int(getattr(task, "depth", 0) or 0),
                },
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return


def collaboration_capabilities_for_task(task: Any) -> list[str]:
    capabilities: list[str] = []
    attrs = getattr(task, "attributes", {}) if isinstance(getattr(task, "attributes", {}), dict) else {}
    for value in _capability_sources(task, attrs):
        _extend_capabilities(capabilities, value)
    for tool in _string_items(getattr(task, "allowed_tools", [])):
        _add_capability(capabilities, tool)
    return capabilities


def _capability_sources(task: Any, attrs: dict[str, object]) -> tuple[object, ...]:
    return (
        getattr(task, "role", ""),
        getattr(task, "agent_name", ""),
        attrs.get("capabilities"),
        attrs.get("collaboration_capabilities"),
        attrs.get("provided_capabilities"),
        attrs.get("required_capabilities"),
    )


def collaboration_sources_for_task(task: Any) -> list[str]:
    attrs = getattr(task, "attributes", {}) if isinstance(getattr(task, "attributes", {}), dict) else {}
    sources: list[str] = []
    for key in ("sources", "source_ids", "source_refs"):
        _extend_capabilities(sources, attrs.get(key))
    return sources


def _extend_capabilities(target: list[str], value: object) -> None:
    for item in _string_items(value):
        _add_capability(target, item)


def _add_capability(target: list[str], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    for candidate in (text, text.lower()):
        if candidate and candidate not in target:
            target.append(candidate)


def _string_items(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _memory_retention_policy(value: object) -> str:
    text = str(value or "").strip()
    return text or "parent_review_or_cleanup"


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _apply_runtime_identity_and_memory_scope(task: Any, params: CreateRunParams, *, parent_task: Any | None) -> None:
    owner_id = str(task.owner or params.owner or "").strip()
    task.runtime_identity.root_run_id = task.root_id or task.id
    task.runtime_identity.service_owner_id = owner_id
    task.runtime_identity.effective_principal_id = owner_id
    task.runtime_identity.conversation_id = task.root_subagent_session_id or task.subagent_session_id
    task.runtime_identity.memory_namespace = f"subagent:{task.root_id or task.id}:{task.id}"
    task.runtime_identity.conversation_memory_policy = "task_scoped"
    task.runtime_identity.promotion_policy = "explicit_parent_review"
    apply_config_overlay_ref(task, params, parent_task=parent_task)
    task.attributes = {
        **dict(task.attributes or {}),
        "memory_scope": _memory_scope(task, params),
        "runtime_config_scope": runtime_config_scope(task),
    }


def _memory_scope(task: Any, params: CreateRunParams) -> dict[str, object]:
    return {
        "schema_version": "subagent_memory_scope.v1",
        "namespace": task.runtime_identity.memory_namespace,
        "retention_policy": _memory_retention_policy(params.memory_retention_policy),
        "delete_after_days": _nonnegative_int(params.memory_delete_after_days),
        "destroy_summary_required": bool(params.destroy_summary_required),
        "auto_promote_to_parent_memory": False,
    }


def apply_config_overlay_ref(task: Any, params: CreateRunParams, *, parent_task: Any | None) -> None:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    explicit = str(attrs.get("config_overlay_ref") or attrs.get("runtime_config_overlay_ref") or "").strip()
    inherited = inherited_config_overlay_ref(params, parent_task=parent_task)
    overlay_ref = explicit or inherited
    if overlay_ref:
        task.runtime_identity.config_overlay_ref = overlay_ref
    scope = str(attrs.get("config_scope") or attrs.get("runtime_config_scope") or "").strip()
    if scope:
        task.runtime_identity.config_scope = scope
    elif overlay_ref:
        task.runtime_identity.config_scope = "run"


def inherited_config_overlay_ref(params: CreateRunParams, *, parent_task: Any | None) -> str:
    parent_id = str(params.parent_id or "").strip()
    if not parent_id or parent_task is None:
        return ""
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    if attrs.get("inherit_config_overlay_ref") is False:
        return ""
    identity = getattr(parent_task, "runtime_identity", None)
    return str(attrs.get("parent_config_overlay_ref") or getattr(identity, "config_overlay_ref", "") or "").strip()


def runtime_config_scope(task: Any) -> dict[str, object]:
    identity = task.runtime_identity
    return {
        "schema_version": "runtime_config_scope.v1",
        "scope": identity.config_scope or "run_override",
        "overlay_ref": identity.config_overlay_ref,
        "promotion_policy": identity.config_promotion_policy,
        "loaded_as": "run_layer" if identity.config_overlay_ref else "base_config",
    }


def rebind_task_output_refs_to_run(task: SubAgentTask) -> list[OutputRefRebinding]:
    rewrites: list[OutputRefRebinding] = []
    task.attributes = _rewrite_attribute_output_refs(task.attributes, task.id, rewrites)
    if rewrites:
        _store_rebindings(task, rewrites)
    return rewrites


def _rewrite_attribute_output_refs(
    attributes: dict[str, object],
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> dict[str, object]:
    attrs = dict(attributes or {})
    system_default = attrs.get("system_default_output_ref") is True
    for field in ("output_refs", "output_files", "artifact_refs"):
        if field in attrs:
            attrs[field] = _rewrite_attribute_value(
                field,
                attrs.get(field),
                run_id,
                rewrites,
                system_default=system_default,
            )
    return attrs


def _rewrite_attribute_value(
    field: str,
    value: object,
    run_id: str,
    rewrites: list[OutputRefRebinding],
    *,
    system_default: bool = False,
) -> object:
    if isinstance(value, list):
        return [
            _rewrite_attribute_value(
                field,
                item,
                run_id,
                rewrites,
                system_default=system_default,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    rebound = (
        _rebound_system_default_ref(value, run_id)
        if system_default
        else _rebound_subagent_ref(value, run_id)
    )
    if rebound and rebound != value:
        rewrites.append(OutputRefRebinding(field, value, rebound))
        return rebound
    return value


def _rebound_system_default_ref(ref: str, run_id: str) -> str:
    """Place runtime-created collaboration slots under the concrete child run."""
    path = Path(str(ref or ""))
    parts = list(path.parts)
    try:
        index = parts.index("child_outputs")
    except ValueError:
        return ""
    if index + 1 < len(parts) and parts[index + 1] == run_id:
        return ""
    parts.insert(index + 1, run_id)
    return str(Path(*parts))


def _rebound_subagent_ref(ref: str, run_id: str) -> str:
    parts = str(ref or "").split("/")
    for index, part in enumerate(parts[:-1]):
        if part != "subagents":
            continue
        next_index = index + 1
        if next_index >= len(parts) or not parts[next_index].startswith("subagent-"):
            continue
        if parts[next_index] == run_id:
            return ""
        parts[next_index] = run_id
        return "/".join(parts)
    return ""


def _store_rebindings(task: SubAgentTask, rewrites: list[OutputRefRebinding]) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    existing = attrs.get("output_ref_rebindings")
    records = list(existing) if isinstance(existing, list) else []
    records.extend(item.to_dict() for item in rewrites)
    attrs["output_ref_rebindings"] = records
    task.attributes = attrs


class SubAgentBaseService:
    """Base task creation and lifecycle service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def split(
        self,
        goal: str,
        count: int,
        *,
        allowed_tools: list[str] | None = None,
    ) -> list[SubAgentTask]:
        """Split a goal into multiple subagent task records.

        Currently uses template-based splitting for simplicity.
        """
        count = max(1, count)
        tasks: list[SubAgentTask] = []
        for i in range(1, count + 1):
            task = self.create_run(
                params=CreateRunParams(
                    goal=f"{goal} / 子任务{i}",
                    thought="先缩小任务边界，明确输入、输出和验证证据，再执行。",
                    plan=["理解目标", "列出交付物", "执行最小验证", "汇报结果和证据"],
                    role="worker",
                    allowed_tools=list(allowed_tools) if allowed_tools else None,
                    extra_write_roots=[],
                ),
            )
            tasks.append(task)
        return tasks

    def register_card(self, card: SubAgentCard) -> None:
        """Register a subagent role card."""
        self.manager.cards[card.name] = card

    def create_run(
        self,
        *,
        params: CreateRunParams,
    ) -> SubAgentTask:
        """Create one explicit subagent task record."""
        from ..role_contracts import apply_role_contract_to_create_params

        params = apply_role_contract_to_create_params(
            params,
            role_template_dirs=getattr(self.manager, "role_template_dirs", None),
        )
        prepared = self._prepare_run(params)
        task = self._build_task(params, prepared)
        self._write_authority_records(task, params)
        self._finalize_task(task, params.parent_id)
        return task

    def _write_authority_records(self, task: SubAgentTask, params: CreateRunParams) -> None:
        """R1：create_run 权威主链写入（A.4/A.5/A.6/A.7/A.9）。

        单事务落 Task→TaskRun→root AgentRun→第一个 AgentAttempt→(child)
        Delegation→runtime_events；conversation_task_id/thread_id 同事务进
        tasks 行，ConversationTaskLink 不再独立权威。owner_home_dir 为空
        （无 home 上下文）时无权威库，只走投影（向后兼容）。

        F8（fail-closed）：有 home 上下文时权威写入不允许静默失败——缺记录
        的 run 会被授权门按"权威记录缺失"拒绝（B.6），成为不可管理的幻影；
        故 attach 降级（repo None）或写入失败一律抛错，任务创建不落地。
        """
        repo = getattr(self.manager, "runtime_db", None)
        if repo is None:
            from ...runtime_db.execution_mode import expects_managed_authority

            if expects_managed_authority(self.manager):
                raise AuthorizationError(
                    f"create_run: 权威库不可用（ExecutionMode.MANAGED 但 "
                    f"runtime.db 未挂载，run={task.id} 拒绝创建）"
                )
            return  # LOCAL_UNMANAGED（纯文件层/投影）→ 只走投影（显式选择）
        attrs = params.attributes if isinstance(params.attributes, dict) else {}
        owner_id = str(task.owner or params.owner or "").strip()
        if not owner_id:
            owner_id = str(getattr(self.manager, "owner_id", "") or "").strip()
        try:
            record = repo.record_run_creation(
                owner_id=owner_id,
                goal=str(task.goal or "").strip(),
                conversation_task_id=str(attrs.get("conversation_task_id") or "").strip(),
                thread_id=str(attrs.get("conversation_thread_id") or "").strip(),
                run_id=str(task.id or "").strip(),
                role=str(task.role or "").strip(),
                parent_run_id=str(params.parent_id or "").strip(),
            )
        except (OSError, KeyError) as exc:
            # 权威写入失败不再吞掉：有 home 时记录缺失 = 门后拒绝（fail-closed）。
            logging.getLogger(__name__).warning(
                "runtime.db 权威写入失败(run=%s): %s", task.id, exc, exc_info=True
            )
            raise
        # seq 253 闭合：链身份回存任务属性——runner 启动时轮换 DB attempt、
        # run scope 携带 DB task_id（授权门比对键）都要从这里拿，不另起查询。
        _persist_runtime_authority_attrs(task, record)

    def _prepare_run(self, params: CreateRunParams) -> dict[str, object]:
        """Prepare run paths and the caller-provided acceptance contract."""
        run_id = _framework_new_id("run_id")
        paths = self.manager._build_work_order_paths(run_id, extra_write_roots=params.extra_write_roots)
        return {
            "run_id": run_id,
            "paths": paths,
            "acceptance_checks": list(params.acceptance_checks or []),
            "now": time.time(),
        }

    def _build_task(self, params: CreateRunParams, prepared: dict[str, object]) -> SubAgentTask:
        """Build SubAgentTask from params and prepared context."""
        run_id = prepared["run_id"]
        now = prepared["now"]

        parent_task = _load_parent_task(self.manager, params.parent_id)
        task = SubAgentTask(
            id=run_id,
            goal=params.goal,
            thought=params.thought,
            plan=params.plan,
            agent_name=params.agent_name,
            role=params.role,
            owner=str(params.owner or getattr(self.manager, "owner_id", "") or "").strip(),
            supervisor=params.supervisor,
            final_owner=params.final_owner,
            parent_id=params.parent_id,
            root_id=params.root_id or run_id,
            depth=params.depth,
            **_session_identity_fields(run_id, parent_task),
            allowed_skills=params.allowed_skills or [],
            allowed_tools=params.allowed_tools or [],
            acceptance_checks=prepared["acceptance_checks"],
            quality_contract=_normalize_quality_contract(params.quality_contract),
            context_manifest=_normalize_context_manifest(params.context_manifest),
            context_packs=_normalize_context_packs(params.context_packs),
            effective_permissions=_task_permission_snapshot(self.manager, params, parent_task),
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            attributes=_task_attrs_for_create(self.manager, params),
            **prepared["paths"],
        )
        task.forbidden_write_roots = _reconciled_forbidden_write_roots(task)
        _apply_runtime_identity_and_memory_scope(task, params, parent_task=parent_task)
        task.inheritance_manifest = build_inheritance_manifest(parent_task, task)
        rebind_task_output_refs_to_run(task)
        return task

    def _finalize_task(self, task: SubAgentTask, parent_id: str) -> None:
        """Save task, register in local store, and link to parent if needed."""
        from ..debug_trace import trace_task_created

        self.manager.save(task)
        register_collaboration_agent_capability(self.manager, task)
        trace_task_created(self.manager, task)
        if self.manager.local_store:
            self.manager.local_store.task_registry.register_task(
                task_id=task.id,
                session_id=task.root_id,
                user_id=task.owner or "",
                status=task.status,
                goal=task.goal,
            )
        if parent_id:
            self.manager.add_child(parent_id, task.id)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ) -> TakeoverRecord:
        """Record a takeover and write TAKEOVER.md.

        This does not actually kill the subagent process, but records ownership and lock files.
        """
        from ..models import TakeoverRecord
        from ..utils import _merge_list

        # 3.txt B.4：takeover 落账同样过统一授权查询门（owner 一致性）。
        task = authorize_operation(
            self.manager,
            OperationRequest(
                operation="takeover",
                run_id=run_id,
                requester_owner=str(getattr(self.manager, "owner_id", "") or ""),
            ),
        )
        record = TakeoverRecord(
            id=self.manager._new_id("takeover"),
            run_id=run_id,
            take_over_by=take_over_by,
            reason=reason,
            locked_files=locked_files or [],
            previous_owner=task.owner,
            created_at=time.time(),
        )
        task.takeover_records.append(record)
        task.takeover_by = take_over_by
        task.takeover_reason = reason
        task.locked_files = _merge_list(task.locked_files, record.locked_files)
        task.final_owner = take_over_by
        task.status = "TAKEN_OVER"
        task.updated_at = time.time()
        attrs = dict(getattr(task, "attributes", {}) or {})
        runtime_scope = dict(attrs.get("runtime_config_scope") or runtime_config_scope(task))
        runtime_scope["takeover_by"] = take_over_by
        runtime_scope["takeover_record_id"] = record.id
        runtime_scope["loaded_as"] = "task_layer_after_takeover" if runtime_scope.get("overlay_ref") else "base_config"
        attrs["runtime_config_scope"] = runtime_scope
        task.attributes = attrs
        self.manager.save(task)
        self.manager._write_takeover_file(task, record)
        return record


def _manager_workspace_attrs(manager: Any) -> dict[str, object]:
    root = getattr(manager, "workspace_root", None)
    roots = getattr(manager, "workspace_roots", None)
    attrs: dict[str, object] = {}
    if root:
        attrs["workspace_root"] = str(root)
    if isinstance(roots, list):
        attrs["workspace_roots"] = [str(item) for item in roots if str(item or "").strip()]
    return attrs


def _task_attrs_for_create(manager: Any, params: CreateRunParams) -> dict[str, object]:
    return {
        **dict(params.attributes or {}),
        **_manager_workspace_attrs(manager),
    }


def _task_permission_snapshot(manager: Any, params: CreateRunParams, parent_task: Any | None) -> dict[str, object]:
    return effective_permission_snapshot(
        parent_task=parent_task,
        parent_access_mode=params.parent_access_mode,
        owner_policy=getattr(manager, "owner_policy_snapshot", {}),
    )


def _persist_runtime_authority_attrs(task: SubAgentTask, record: dict[str, object]) -> None:
    """回存权威链身份到任务属性（save 前调用）。

    只有 record_run_creation 成功返回才调用（LOCAL_UNMANAGED 早退不经过）。
    子代理 runner 启动（attempt 轮换）、授权门比对（scope task_id）共用这份
    单一权威来源，不另起查询。
    """
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["runtime_authority"] = {
        "task_id": str(record.get("task_id") or "").strip(),
        "task_run_id": str(record.get("task_run_id") or "").strip(),
        "agent_run_id": str(record.get("agent_run_id") or "").strip(),
        "attempt_id": str(record.get("attempt_id") or "").strip(),
    }
    task.attributes = attrs
