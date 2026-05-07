# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""bridge legacy subagent work orders into runtime memory task workspaces."""

from pathlib import Path

from ...memory_archive.task_workspace import ensure_subagent_task_workspace
from ..models import SubAgentTask


# LLM: sync_task_workspace_fields 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理同步任务workspace字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def sync_task_workspace_fields(workspace: str | Path, task: SubAgentTask) -> None:
    """Create/update the task workspace adapter and copy its paths onto the task."""

    task_workspace_paths = ensure_subagent_task_workspace(workspace, task)
    _sync_task_workspace_paths(task, task_workspace_paths)
    _sync_agent_run_paths(task, task_workspace_paths)
    _sync_runtime_refs(task, task_workspace_paths)


# LLM: _sync_task_workspace_paths 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理同步任务workspace路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _sync_task_workspace_paths(task: SubAgentTask, task_workspace_paths) -> None:
    # LLM: task workspace paths are root-task scoped and safe for sibling discovery.
    task.task_workspace_dir = str(task_workspace_paths.root)
    task.task_workspace_task_yaml = str(task_workspace_paths.task_yaml)
    task.task_workspace_state_json = str(task_workspace_paths.state_json)
    task.task_workspace_timeline_jsonl = str(task_workspace_paths.timeline_jsonl)
    task.task_workspace_summary_file = str(task_workspace_paths.current_summary)
    task.task_workspace_shared_dir = str(task_workspace_paths.shared_dir)
    # LLM: shared workspace paths let sibling runs inspect task-local facts without main memory writes.
    task.task_workspace_shared_blackboard = str(task_workspace_paths.shared.blackboard_md)
    task.task_workspace_shared_messages_jsonl = str(task_workspace_paths.shared.messages_jsonl)
    task.task_workspace_shared_findings_jsonl = str(task_workspace_paths.shared.findings_jsonl)
    task.task_workspace_shared_evidence_packets_dir = str(task_workspace_paths.shared.evidence_packets_dir)
    task.task_workspace_shared_evidence_index_jsonl = str(task_workspace_paths.shared.evidence_index_jsonl)
    task.task_workspace_artifacts_dir = str(task_workspace_paths.artifacts_dir)
    task.task_workspace_agents_dir = str(task_workspace_paths.agents_dir)
    task.agent_run_workspace_dir = str(task_workspace_paths.agent_adapter_dir)


# LLM: _sync_agent_run_paths 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理同步agentrun路径相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def _sync_agent_run_paths(task: SubAgentTask, task_workspace_paths) -> None:
    # LLM: 新运行工作区路径继续写进旧 SubAgentTask JSON，保证旧读取方可发现。
    task.agent_run_agent_yaml = str(task_workspace_paths.agent_run.agent_yaml)
    task.agent_run_state_json = str(task_workspace_paths.agent_run.state_json)
    task.agent_run_task_md = str(task_workspace_paths.agent_run.task_md)
    task.agent_run_timeline_jsonl = str(task_workspace_paths.agent_run.timeline_jsonl)
    task.agent_run_checkpoint_json = str(task_workspace_paths.agent_run.checkpoint_json)
    task.agent_run_summary_md = str(task_workspace_paths.agent_run.summary_md)
    task.agent_run_final_report_md = str(task_workspace_paths.agent_run.final_report_md)
    task.agent_run_findings_jsonl = str(task_workspace_paths.agent_run.findings_jsonl)
    task.agent_run_inbox_dir = str(task_workspace_paths.agent_run.inbox_dir)
    task.agent_run_outbox_dir = str(task_workspace_paths.agent_run.outbox_dir)
    task.agent_run_artifacts_dir = str(task_workspace_paths.agent_run.artifacts_dir)
    task.agent_run_compactions_dir = str(task_workspace_paths.agent_run.compactions_dir)
    task.legacy_run_ref_json = str(task_workspace_paths.legacy_run_ref_json)


# LLM: _sync_runtime_refs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理同步运行时refs相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def _sync_runtime_refs(task: SubAgentTask, task_workspace_paths) -> None:
    # LLM: expose the latest daily ledger append for recovery/debug without storing full context.
    task.daily_ledger_file = str(task_workspace_paths.daily_ledger.events_jsonl)
    task.daily_ledger_last_event_id = task_workspace_paths.daily_ledger.event_id
    # LLM: manifest paths let old task.json consumers find artifact metadata without reading bodies.
    task.task_artifact_manifest_jsonl = str(task_workspace_paths.artifact_manifest.task_manifest_jsonl)
    task.agent_run_artifact_manifest_jsonl = str(task_workspace_paths.artifact_manifest.agent_manifest_jsonl)
    # LLM: compact-chain paths are recovery pointers; original timeline/artifacts remain authoritative.
    task.agent_run_compaction_ledger_jsonl = str(task_workspace_paths.compact_chain.ledger_jsonl)
    task.agent_run_latest_compaction_summary_md = str(task_workspace_paths.compact_chain.latest_summary_md)
    task.agent_run_latest_compaction_metadata_json = str(task_workspace_paths.compact_chain.latest_metadata_json)
    # LLM: gate paths keep memory/skill learning candidates reviewable without writing main memory.
    task.agent_run_memory_gate_dir = str(task_workspace_paths.memory_gate.gate_dir)
    task.agent_run_memory_candidates_jsonl = str(task_workspace_paths.memory_gate.candidates_jsonl)
    task.agent_run_memory_review_queue_jsonl = str(task_workspace_paths.memory_gate.review_queue_jsonl)
    # LLM: decisions are review audit logs; later promotion commands must read them explicitly.
    task.agent_run_memory_decisions_jsonl = str(task_workspace_paths.memory_gate.decisions_jsonl)
    # LLM: exports are opt-in promotion records and remain separate from review decisions.
    task.agent_run_memory_exports_jsonl = str(task_workspace_paths.memory_gate.exports_jsonl)
    task.agent_run_skill_spark_gate_json = str(task_workspace_paths.memory_gate.skill_spark_gate_json)
