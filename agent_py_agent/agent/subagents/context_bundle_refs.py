# LLM: Context bundle refs collect task/run workspace paths without reading artifact bodies.
# 模块用途: 为子代理 context bundle 生成 workspace_refs、lineage 和安全路径字符串。

from __future__ import annotations

from pathlib import Path

from .models import SubAgentTask


# LLM: workspace_refs is the only model-facing path vocabulary for subagent workspaces.
# 函数用途: 生成当前任务根、交付目录、工作目录和当前 agent 工作目录；旧工单目录只留给恢复文件，不再进入模型提示。
def workspace_refs(task: SubAgentTask) -> dict[str, str]:
    task_workspace = safe_string_ref(task, "task_workspace_dir")
    work_dir = str(Path(task_workspace) / "work") if task_workspace else ""
    output_dir = str(Path(task_workspace) / "output") if task_workspace else ""
    return {
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
        "agent_run_compactions": safe_string_ref(task, "agent_run_compactions_dir"),
        "agent_run_latest_continue_packet": latest_continue_packet_ref(task),
        "agent_run_latest_compaction_summary": safe_string_ref(task, "agent_run_latest_compaction_summary_md"),
        "agent_run_latest_compaction_metadata": safe_string_ref(task, "agent_run_latest_compaction_metadata_json"),
        "agent_run_session_compaction_ledger": safe_string_ref(task, "agent_run_session_compaction_ledger_jsonl"),
        "agent_run_latest_session_compaction_summary": safe_string_ref(
            task, "agent_run_latest_session_compaction_summary_md"
        ),
        "agent_run_latest_session_compaction_metadata": safe_string_ref(
            task, "agent_run_latest_session_compaction_metadata_json"
        ),
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


# LLM: latest_continue_packet_ref reserves a stable task-local subagent resume packet path.
# 函数用途: 从 agent_run_compactions_dir 派生 latest_continue_packet.json，供 runner prompt 按存在性读取。
def latest_continue_packet_ref(task: SubAgentTask) -> str:
    explicit = safe_string_ref(task, "agent_run_latest_session_continue_packet_json")
    if explicit:
        return explicit
    compactions = safe_string_ref(task, "agent_run_compactions_dir")
    return str(Path(compactions) / "session" / "latest_continue_packet.json") if compactions else ""


# LLM: lineage gives nested runners parent/root refs without expanding ancestor files into prompt text.
# 函数用途: 记录当前子代理在任务树中的位置，以及直接父级 context bundle 的可读路径。
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


# LLM: safe_string_ref protects JSON context bundles from mocks or missing optional path refs.
# 函数用途: 读取可选路径字段；只有字符串和 Path 会进入 bundle，MagicMock/None 归一成空串。
def safe_string_ref(task: SubAgentTask, field_name: str) -> str:
    value = getattr(task, field_name, "")
    if isinstance(value, Path):
        return str(value)
    return value if isinstance(value, str) else ""
