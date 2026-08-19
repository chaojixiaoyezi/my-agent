
from __future__ import annotations

"""sync subagent task records into runtime memory task workspaces."""

from pathlib import Path

from ...common.opaque_id import OpaqueIdError, validate_opaque_id
from ...memory_archive.task_workspace import ensure_subagent_task_workspace
from ...runtime_db.operations import directory_id_for_opaque
from ..models import SubAgentTask


def sync_task_workspace_fields(workspace: str | Path, task: SubAgentTask) -> None:
    """Create/update the task workspace adapter and copy its paths onto the task."""

    task_workspace_paths = ensure_subagent_task_workspace(workspace, task)
    _sync_task_workspace_paths(task, task_workspace_paths)
    _sync_agent_run_paths(task, task_workspace_paths)
    _sync_work_order_paths(task, task_workspace_paths, workspace)
    _sync_runner_io_paths(task, task_workspace_paths)
    _sync_runtime_refs(task, task_workspace_paths)


def _sync_task_workspace_paths(task: SubAgentTask, task_workspace_paths) -> None:
    task.task_workspace_dir = str(task_workspace_paths.root)
    task.task_workspace_task_yaml = str(task_workspace_paths.task_yaml)
    task.task_workspace_state_json = str(task_workspace_paths.state_json)
    task.task_workspace_timeline_jsonl = str(task_workspace_paths.timeline_jsonl)
    task.task_workspace_summary_file = str(task_workspace_paths.current_summary)
    task.task_workspace_shared_dir = str(task_workspace_paths.shared_dir)
    task.task_workspace_shared_blackboard = str(task_workspace_paths.shared.blackboard_md)
    task.task_workspace_shared_messages_jsonl = str(task_workspace_paths.shared.messages_jsonl)
    task.task_workspace_shared_findings_jsonl = str(task_workspace_paths.shared.findings_jsonl)
    task.task_workspace_shared_evidence_packets_dir = str(task_workspace_paths.shared.evidence_packets_dir)
    task.task_workspace_shared_evidence_index_jsonl = str(task_workspace_paths.shared.evidence_index_jsonl)
    task.task_workspace_artifacts_dir = str(task_workspace_paths.artifacts_dir)
    task.task_workspace_agents_dir = str(task_workspace_paths.agents_dir)
    task.agent_run_workspace_dir = str(task_workspace_paths.agent_adapter_dir)


def _sync_agent_run_paths(task: SubAgentTask, task_workspace_paths) -> None:
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


def _sync_work_order_paths(task: SubAgentTask, task_workspace_paths, workspace: str | Path) -> None:
    root = task_workspace_paths.agent_adapter_dir
    reports = root / "reports"
    previous_task_dir = str(getattr(task, "task_dir", "") or "").strip()
    task.task_dir = str(root)
    task.allowed_write_roots = _canonical_allowed_write_roots(task, root, previous_task_dir, workspace)
    task.data_dir = str(root / "data")
    task.output_dir = str(root / "output")
    task.tests_dir = str(root / "tests")
    task.reports_dir = str(reports)
    task.logs_dir = str(root / "logs")
    task.scratch_dir = str(root / "scratch")
    task.status_file = str(root / "STATUS.md")
    task.work_log_file = str(root / "WORK_LOG.md")
    task.action_receipts_file = str(root / "ACTION_RECEIPTS.md")
    task.acceptance_file = str(root / "ACCEPTANCE.md")
    task.test_checklist_file = str(root / "TEST_CHECKLIST.md")
    task.bugs_file = str(root / "BUGS.md")
    task.skill_usage_file = str(root / "SKILL_USAGE.md")
    task.skill_sparks_file = str(root / "SKILL_SPARKS.md")
    task.handoff_file = str(root / "HANDOFF.md")
    task.debrief_file = str(root / "DEBRIEF.md")
    task.dependencies_json = str(root / "dependencies.json")
    task.takeover_file = str(root / "TAKEOVER.md")
    task.channel_probe_file = str(root / "CHANNEL_PROBE.md")
    task.status_report_json = str(reports / "status_report.json")
    task.inheritance_manifest_json = str(reports / "inheritance_manifest.json")
    task.failure_handoff_json = str(reports / "failure_handoff.json")
    task.takeover_readiness_json = str(reports / "takeover_readiness.json")
    task.takeover_readiness_md = str(root / "TAKEOVER_READINESS.md")
    task.checkpoint_json = str(reports / "checkpoint.json")
    task.decision_ledger_json = str(reports / "decision_ledger.json")
    task.progress_md = str(reports / "progress.md")
    task.failing_tests_json = str(reports / "failing_tests.json")
    task.next_actions_json = str(reports / "next_actions.json")


def _canonical_allowed_write_roots(
    task: SubAgentTask,
    root: Path,
    previous_task_dir: str,
    workspace: str | Path,
) -> list[str]:
    result = [str(root)]
    locator_root = _manager_locator_root(workspace, task)
    for item in list(getattr(task, "allowed_write_roots", []) or []):
        text = str(item or "").strip()
        if not text or _same_or_inside_any(text, [previous_task_dir, locator_root]) or text in result:
            continue
        result.append(text)
    return result


def _manager_locator_root(workspace: str | Path, task: SubAgentTask) -> str:
    # task.id 拼进路径前先过拒绝式校验 + 走框架目录 ID（G1/B.3）：这里的解析
    # 结果会进 allowed_write_roots，非法 ID 逃逸到 workspace 外会让模型获得任意
    # 写权限。fail-closed：非法即返回空（调用方会忽略该 root）。directory_id
    # 与权威库 id_path_mapping 登记值一致（directory_id 即 validate 结果）。
    try:
        directory_id = directory_id_for_opaque(task.id, kind="run_id")
        return str((Path(workspace).expanduser() / directory_id).resolve(strict=False))
    except (OSError, RuntimeError, OpaqueIdError):
        return ""


def _same_or_inside_any(value: str, roots: list[str]) -> bool:
    return any(_same_or_inside_root(value, root) for root in roots if root)


def _same_or_inside_root(value: str, root_value: str) -> bool:
    try:
        path = Path(value).expanduser().resolve(strict=False)
        root = Path(root_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return value == root_value
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sync_runner_io_paths(task: SubAgentTask, task_workspace_paths) -> None:
    root = task_workspace_paths.agent_adapter_dir
    task.execution_context_file = str(root / "EXECUTION_CONTEXT.md")
    task.execution_context_json = str(root / "execution_context.json")
    task.runner_result_file = str(root / "RUNNER_RESULT.md")
    task.runner_result_json = str(root / "runner_result.json")
    task.runner_prompt_file = str(root / "runner_prompt.md")
    task.runner_response_file = str(root / "runner_response.md")
    task.output_json = str(root / "output.json")


def _sync_runtime_refs(task: SubAgentTask, task_workspace_paths) -> None:
    task.daily_ledger_file = str(task_workspace_paths.daily_ledger.events_jsonl)
    task.daily_ledger_last_event_id = task_workspace_paths.daily_ledger.event_id
    task.task_artifact_manifest_jsonl = str(task_workspace_paths.artifact_manifest.task_manifest_jsonl)
    task.agent_run_artifact_manifest_jsonl = str(task_workspace_paths.artifact_manifest.agent_manifest_jsonl)
