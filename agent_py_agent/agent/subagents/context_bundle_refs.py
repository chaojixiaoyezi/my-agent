
# LLM: 子代理路径事实源区分可信 cwd 和内部运行记录；投影不得隐式授予权限。
# 模块用途: 为工具、提示和结果交接提供一致的目录与身份引用。
from __future__ import annotations

from pathlib import Path

from ..conversation.authority import conversation_execution_cwd
from ..model_visible_refs import current_model_ref
from .models import SubAgentTask


# LLM: Child-visible workspace refs include execution/recovery locations but
# exclude the host-owned final report, which is written only after the child
# returns and is delivered to the parent through the completion envelope.
# 函数用途: 投影子代理执行与恢复所需的工作区路径，不要求它自行写宿主收口报告。
def workspace_refs(task: SubAgentTask) -> dict[str, str]:
    task_workspace = safe_string_ref(task, "task_workspace_dir")
    work_dir = str(Path(task_workspace) / "work") if task_workspace else ""
    output_dir = str(Path(task_workspace) / "output") if task_workspace else ""
    return {
        "execution_cwd": execution_cwd(task),
        "owner_workspace_dir": execution_cwd(task),
        "task_root": task_workspace,
        "task_work_dir": work_dir,
        "task_output_dir": output_dir,
        "agent_work_dir": safe_string_ref(task, "agent_run_workspace_dir"),
        "agent_run_task": safe_string_ref(task, "agent_run_task_md"),
        "agent_run_checkpoint": safe_string_ref(task, "agent_run_checkpoint_json"),
        "agent_run_summary": safe_string_ref(task, "agent_run_summary_md"),
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


# LLM: 当前会话 cwd 优先于创建时 workspace 投影；缺字段才读 owner home，不从 task 归档根推导。
# 函数用途: 返回子代理实际项目工作目录，供提示和工具共同使用。
def execution_cwd(task: SubAgentTask) -> str:
    attrs = getattr(task, "attributes", {}) or {}
    if cwd := conversation_execution_cwd(attrs):
        return current_model_ref(cwd)
    if isinstance(attrs, dict):
        workspace_root = current_model_ref(attrs.get("workspace_root"))
        if workspace_root:
            return workspace_root
    permissions = getattr(task, "effective_permissions", {}) or {}
    if not isinstance(permissions, dict):
        return ""
    owner_home = current_model_ref(permissions.get("owner_home"))
    if not owner_home:
        return ""
    try:
        return current_model_ref(Path(owner_home).expanduser().resolve(strict=False))
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
