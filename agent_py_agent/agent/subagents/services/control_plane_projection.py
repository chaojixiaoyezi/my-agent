
from __future__ import annotations

"""sync subagent task state into LocalStore runtime control-plane tables.

这里不创建新的事实源，只把已经写入 task/run workspace 的状态投影到 SQLite。
上级代理和接管代理可以靠这些行快速看 agent tree；真正恢复仍回到文件。
"""

import json
from pathlib import Path
from typing import Any

from ...local_storage.control_plane_models import AgentRunRecord
from ..models import SubAgentTask, task_has_failure_status


def sync_subagent_control_plane_projection(local_store: Any, task: SubAgentTask) -> None:
    if local_store is None:
        return
    run_record = _agent_run_record_from_task(task)
    local_store.upsert_agent_run(run_record)
    local_store.rebuild_task_rollup(run_record.root_task_id)


# LLM: The LocalStore row is a read-model projection of one canonical task and
# must reuse shared runtime usage readers rather than recounting ledgers differently.
# 函数用途: 把一个子代理任务转换成控制面查询所需的 AgentRun 记录。
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
        latest_compact_ref=runtime_latest_compact_ref(task),
        compact_count=runtime_compact_count(task),
        heartbeat_at=float(task.heartbeat_at or 0.0),
        created_at=float(task.created_at or task.updated_at or 0.0),
        updated_at=float(task.updated_at or task.heartbeat_at or task.created_at or 0.0),
        metadata=_agent_run_metadata(task),
    )


# LLM: This reader is the shared, read-only projection of the canonical compact
# apply ledger; callers may display/count rows but must not derive lifecycle state.
# 函数用途: 读取一个子代理运行目录里的真实 Compact 应用记录，文件缺失或损坏行按空记录处理。
def runtime_compact_rows(task: SubAgentTask) -> list[dict[str, object]]:
    root = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not root:
        return []
    path = Path(root) / "memory_archive" / "compact_applies" / "ledger.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


# LLM: The latest ref is selected only from persisted compact ledger refs and
# remains a display/index projection, never a completion or recovery decision.
# 函数用途: 返回一个子代理最近一次 Compact 的元数据引用。
def runtime_latest_compact_ref(task: SubAgentTask) -> str:
    for row in reversed(runtime_compact_rows(task)):
        refs = row.get("refs") if isinstance(row.get("refs"), dict) else {}
        metadata = str(refs.get("metadata") or "").strip()
        if metadata:
            return metadata
    return ""


# LLM: Count only successfully parsed canonical ledger rows; UI and LocalStore
# share this helper so Compact totals cannot drift between surfaces.
# 函数用途: 统计一个子代理真实完成过多少次 Compact。
def runtime_compact_count(task: SubAgentTask) -> int:
    return len(runtime_compact_rows(task))


# LLM: The child-row token value is the latest canonical provider-visible
# context snapshot, not cumulative billing usage. It is written before each real
# model call and read only from the exact run's structured attributes.
# 函数用途: 读取子代理当前上下文总 token，每次新的模型调用前都会刷新。
def runtime_context_token_count(task: SubAgentTask) -> int:
    attrs = getattr(task, "attributes", None)
    usage = attrs.get("model_visible_context_usage") if isinstance(attrs, dict) else None
    if not isinstance(usage, dict):
        return 0
    if str(usage.get("schema") or "") != "model_visible_context_usage.v1":
        return 0
    value = usage.get("current_tokens")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
    if task.takeover_readiness_json and task_has_failure_status(task):
        metadata["takeover_readiness_ref"] = task.takeover_readiness_json
    return metadata


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
