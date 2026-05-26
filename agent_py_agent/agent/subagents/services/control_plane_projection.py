# LLM: Subagent control-plane projection writes query rows while task/run files stay authoritative.
# 模块用途: 把子代理任务保存结果同步到 LocalStore 控制面投影。

from __future__ import annotations

"""sync subagent task state into LocalStore runtime control-plane tables.

给人看的解释：
这里不创建新的事实源，只把已经写入 task/run workspace 的状态投影到 SQLite。
上级代理和接管代理可以靠这些行快速看 agent tree；真正恢复仍回到文件。
"""

from typing import Any

from ...local_storage.control_plane_models import AgentEventInput, AgentRunRecord
from ..models import SubAgentTask

_TAKEOVER_READINESS_STATUSES = {"BLOCKED", "FAILED", "TIMEOUT", "ERROR"}


# LLM: sync_subagent_control_plane_projection writes run/event/rollup rows for one task save.
# 函数用途: 将单个子代理任务的最新状态同步到 LocalStore 控制面。
def sync_subagent_control_plane_projection(local_store: Any, task: SubAgentTask) -> None:
    if local_store is None:
        return
    run_record = _agent_run_record_from_task(task)
    local_store.upsert_agent_run(run_record)
    local_store.record_agent_event(_agent_event_from_task(task))
    local_store.rebuild_task_rollup(run_record.root_task_id)


# LLM: _agent_run_record_from_task keeps projection fields derived from the task dataclass only.
# 函数用途: 从 SubAgentTask 构造 agent_runs 表的一行记录。
def _agent_run_record_from_task(task: SubAgentTask) -> AgentRunRecord:
    root_task_id = task.root_id or task.id
    return AgentRunRecord(
        run_id=task.id,
        root_task_id=root_task_id,
        parent_run_id=task.parent_id,
        depth=int(task.depth),
        role=task.role,
        agent_name=task.agent_name,
        status=task.status,
        progress=float(task.progress or 0.0),
        current_step=task.current_step,
        latest_summary=task.latest_summary,
        workspace_path=task.agent_run_workspace_dir or task.task_dir,
        checkpoint_ref=task.agent_run_checkpoint_json or task.checkpoint_ref or task.checkpoint_json,
        latest_compact_ref=task.agent_run_latest_compaction_summary_md,
        compact_count=0,
        heartbeat_at=float(task.heartbeat_at or 0.0),
        created_at=float(task.created_at or task.updated_at or 0.0),
        updated_at=float(task.updated_at or task.heartbeat_at or task.created_at or 0.0),
        metadata=_agent_run_metadata(task),
        reserved={
            "schema_name": "agent_run_projection",
            "schema_version": 1,
            "extensions": {},
            "compat": {},
            "future": {},
        },
    )


# LLM: _agent_run_metadata keeps optional panel/security refs out of rows when facts are empty.
# 函数用途: 构造控制面 metadata，只暴露有意义的继承清单、失败交接和安全预留引用。
def _agent_run_metadata(task: SubAgentTask) -> dict[str, object]:
    metadata: dict[str, object] = {
        "verification_status": task.verification_status,
        "failure_type": task.failure_type,
        "blockers": list(task.blockers),
        "status_report_ref": task.status_report_json,
        "system_tree": dict((task.attributes or {}).get("system_tree") or {}),
    }
    security_signal_types = [item.signal_type for item in task.security_signals if item.signal_type]
    if task.security_review_required or security_signal_types:
        metadata["security_review_required"] = bool(task.security_review_required)
        metadata["security_signal_count"] = len(task.security_signals)
        metadata["security_signal_types"] = list(dict.fromkeys(security_signal_types))
    scope_metadata = _runtime_scope_metadata(task)
    if scope_metadata:
        metadata.update(scope_metadata)
    if task.inheritance_manifest.source_run_id:
        metadata["inheritance_manifest_ref"] = task.inheritance_manifest_json
    if task.failure_handoff.run_id:
        metadata["failure_handoff_ref"] = task.failure_handoff_json
    if task.takeover_readiness_json and task.status in _TAKEOVER_READINESS_STATUSES:
        metadata["takeover_readiness_ref"] = task.takeover_readiness_json
    return metadata


# LLM: _runtime_scope_metadata projects scope fields without turning them into permissions.
# 函数用途: 把员工/会话/记忆/配置隔离元数据暴露给控制面，默认不写全局配置。
def _runtime_scope_metadata(task: SubAgentTask) -> dict[str, object]:
    identity = getattr(task, "runtime_identity", None)
    if not identity:
        return {}
    runtime_identity = {
        "service_owner_id": identity.service_owner_id,
        "requester_id": identity.requester_id,
        "effective_principal_id": identity.effective_principal_id,
        "conversation_id": identity.conversation_id,
        "root_run_id": identity.root_run_id or task.root_id or task.id,
    }
    memory_scope = {
        "namespace": identity.memory_namespace,
        "conversation_memory_policy": identity.conversation_memory_policy or "not_enabled",
        "promotion_policy": identity.promotion_policy or "explicit_review",
        "task_memory_is_temporary": True,
    }
    config_scope = {
        "scope": identity.config_scope or "run_override",
        "overlay_ref": identity.config_overlay_ref,
        "promotion_policy": identity.config_promotion_policy or "admin_approval_required",
        "writes_global_config": False,
    }
    if not any(runtime_identity.values()) and not memory_scope["namespace"] and not config_scope["overlay_ref"]:
        return {}
    return {"runtime_identity": runtime_identity, "memory_scope": memory_scope, "config_scope": config_scope}


# LLM: _agent_event_from_task appends an audit event for every save projection.
# 函数用途: 写入运行保存事件；scope 信息保留在 run metadata，事件不复制权限边界。
def _agent_event_from_task(task: SubAgentTask) -> AgentEventInput:
    root_task_id = task.root_id or task.id
    return AgentEventInput(
        root_task_id=root_task_id,
        run_id=task.id,
        parent_run_id=task.parent_id,
        event_type="agent_run_saved",
        payload={
            "status": task.status,
            "progress": float(task.progress or 0.0),
            "current_step": task.current_step,
            "latest_summary": task.latest_summary,
            "checkpoint_ref": task.agent_run_checkpoint_json or task.checkpoint_ref or task.checkpoint_json,
        },
        created_at=float(task.updated_at or task.heartbeat_at or task.created_at or 0.0),
        reserved={
            "schema_name": "agent_event_projection",
            "schema_version": 1,
            "extensions": {},
            "compat": {},
            "future": {},
        },
    )
