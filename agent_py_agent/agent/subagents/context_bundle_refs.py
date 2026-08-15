
from __future__ import annotations

from pathlib import Path

from ..model_visible_refs import current_model_ref
from .models import SubAgentTask


def workspace_refs(task: SubAgentTask) -> dict[str, str]:
    task_workspace = safe_string_ref(task, "task_workspace_dir")
    work_dir = str(Path(task_workspace) / "work") if task_workspace else ""
    output_dir = str(Path(task_workspace) / "output") if task_workspace else ""
    return {
        # LLM: 远程 owner 的长期资料/项目根与单次任务交付根不是同一个目录。
        #   把两者作为结构化环境事实同时交给 runner，避免模型把
        #   owner/workspace/input 错拼成 task_root/workspace/input。
        # 人类: 这相当于 会话运行时/模型助手 Code 明确告诉代理“主工作目录”；
        #   task_root 仍只负责本任务的 work/output，不改变任何读写权限。
        "owner_workspace_dir": _owner_workspace_dir(task),
        "task_root": task_workspace,
        "task_work_dir": work_dir,
        "task_output_dir": output_dir,
        "agent_work_dir": safe_string_ref(task, "agent_run_workspace_dir"),
        "agent_run_task": safe_string_ref(task, "agent_run_task_md"),
        "agent_run_checkpoint": safe_string_ref(task, "agent_run_checkpoint_json"),
        "agent_run_summary": safe_string_ref(task, "agent_run_summary_md"),
        "agent_run_final_report": safe_string_ref(task, "agent_run_final_report_md"),
        "agent_run_findings": safe_string_ref(task, "agent_run_findings_jsonl"),
        "agent_run_timeline": safe_string_ref(task, "agent_run_timeline_jsonl"),
        "shared_blackboard": safe_string_ref(task, "task_workspace_shared_blackboard"),
        "shared_messages": safe_string_ref(task, "task_workspace_shared_messages_jsonl"),
        "shared_findings": safe_string_ref(task, "task_workspace_shared_findings_jsonl"),
        "shared_evidence_index": safe_string_ref(task, "task_workspace_shared_evidence_index_jsonl"),
        "agent_run_inbox": safe_string_ref(task, "agent_run_inbox_dir"),
        "agent_run_outbox": safe_string_ref(task, "agent_run_outbox_dir"),
        "artifacts_dir": safe_string_ref(task, "agent_run_artifacts_dir") or output_dir,
        "execution_context_json": safe_string_ref(task, "execution_context_json"),
        "execution_context_file": safe_string_ref(task, "execution_context_file"),
    }


def _owner_workspace_dir(task: SubAgentTask) -> str:
    permissions = getattr(task, "effective_permissions", {}) or {}
    if not isinstance(permissions, dict):
        return ""
    owner_home = current_model_ref(permissions.get("owner_home"))
    if not owner_home:
        return ""
    try:
        return current_model_ref(
            Path(owner_home).expanduser().resolve(strict=False) / "workspace"
        )
    except OSError:
        return ""


def runtime_task_attributes(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    result = dict(attrs) if isinstance(attrs, dict) else {}
    task_root = safe_string_ref(task, "task_workspace_dir") or safe_string_ref(task, "task_dir")
    if task_root:
        root = Path(task_root)
        result["run_workspace"] = {
            "task_root": str(root),
            "output_dir": str(root / "output"),
            "work_dir": str(root / "work"),
        }
    agent_work = safe_string_ref(task, "agent_run_workspace_dir")
    if agent_work:
        result["agent_run_workspace_dir"] = agent_work
    return result


def lineage(task: SubAgentTask) -> dict[str, object]:
    parent_id = str(task.parent_id or "")
    task_workspace_ref = safe_string_ref(task, "task_workspace_dir")
    agent_run_ref = safe_string_ref(task, "agent_run_workspace_dir")
    task_workspace = Path(task_workspace_ref) if task_workspace_ref else Path("")
    parent_agent_ref = (
        str(task_workspace / "work" / "agents" / parent_id / "context_bundle.json")
        if parent_id and task_workspace_ref
        else ""
    )
    return {
        "root_id": task.root_id or task.id,
        "parent_id": parent_id,
        "depth": int(task.depth or 0),
        "own_context_bundle_ref": str(Path(agent_run_ref) / "context_bundle.json") if agent_run_ref else "",
        "parent_context_bundle_ref": parent_agent_ref,
        "inheritance_manifest_ref": safe_string_ref(task, "inheritance_manifest_json"),
    }


def safe_string_ref(task: SubAgentTask, field_name: str) -> str:
    value = getattr(task, field_name, "")
    if isinstance(value, Path):
        return current_model_ref(value)
    return current_model_ref(value) if isinstance(value, str) else ""
