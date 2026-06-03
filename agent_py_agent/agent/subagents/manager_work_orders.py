
from __future__ import annotations

"""Work-order path, file, and validation helpers for SubAgentBaseMixin."""

import time
from pathlib import Path
from typing import Any

from .models import SubAgentTask, TakeoverRecord, WorkOrderValidation
from .policies import _default_forbidden_write_roots
from .utils import _apply_missing_paths, _write_if_missing, _write_json_if_missing


def build_work_order_paths(
    manager: Any,
    run_id: str,
    task_dir: str | Path | None = None,
    extra_write_roots: list[str] | None = None,
) -> dict[str, object]:
    task_dir = Path(task_dir) if task_dir else manager.workspace / run_id
    paths = _work_order_base_paths(task_dir)
    paths.update(_work_order_report_paths(task_dir))
    paths.update(_work_order_runner_paths(task_dir))
    paths["allowed_write_roots"] = [str(task_dir)] + list(extra_write_roots or [])
    paths["forbidden_write_roots"] = _default_forbidden_write_roots()
    return paths


def _work_order_base_paths(task_dir: Path) -> dict[str, str]:
    return {
        "task_dir": str(task_dir),
        "data_dir": str(task_dir / "data"),
        "output_dir": str(task_dir / "output"),
        "tests_dir": str(task_dir / "tests"),
        "reports_dir": str(task_dir / "reports"),
        "logs_dir": str(task_dir / "logs"),
        "scratch_dir": str(task_dir / "scratch"),
        "status_file": str(task_dir / "STATUS.md"),
        "work_log_file": str(task_dir / "WORK_LOG.md"),
        "action_receipts_file": str(task_dir / "ACTION_RECEIPTS.md"),
        "acceptance_file": str(task_dir / "ACCEPTANCE.md"),
        "test_checklist_file": str(task_dir / "TEST_CHECKLIST.md"),
        "bugs_file": str(task_dir / "BUGS.md"),
        "skill_usage_file": str(task_dir / "SKILL_USAGE.md"),
        "skill_sparks_file": str(task_dir / "SKILL_SPARKS.md"),
        "handoff_file": str(task_dir / "HANDOFF.md"),
        "debrief_file": str(task_dir / "DEBRIEF.md"),
        "output_json": str(task_dir / "output.json"),
        "dependencies_json": str(task_dir / "dependencies.json"),
        "takeover_file": str(task_dir / "TAKEOVER.md"),
        "channel_probe_file": str(task_dir / "CHANNEL_PROBE.md"),
    }


def _work_order_report_paths(task_dir: Path) -> dict[str, str]:
    reports_dir = task_dir / "reports"
    return {
        "status_report_json": str(reports_dir / "status_report.json"),
        "inheritance_manifest_json": str(reports_dir / "inheritance_manifest.json"),
        "failure_handoff_json": str(reports_dir / "failure_handoff.json"),
        "takeover_readiness_json": str(reports_dir / "takeover_readiness.json"),
        "takeover_readiness_md": str(task_dir / "TAKEOVER_READINESS.md"),
        "checkpoint_json": str(reports_dir / "checkpoint.json"),
        "decision_ledger_json": str(reports_dir / "decision_ledger.json"),
        "progress_md": str(reports_dir / "progress.md"),
        "failing_tests_json": str(reports_dir / "failing_tests.json"),
        "next_actions_json": str(reports_dir / "next_actions.json"),
    }


def _work_order_runner_paths(task_dir: Path) -> dict[str, str]:
    return {
        "execution_context_file": str(task_dir / "EXECUTION_CONTEXT.md"),
        "execution_context_json": str(task_dir / "execution_context.json"),
        "runner_result_file": str(task_dir / "RUNNER_RESULT.md"),
        "runner_result_json": str(task_dir / "reports" / "runner_result.json"),
        "runner_prompt_file": str(task_dir / "logs" / "runner_prompt.md"),
        "runner_response_file": str(task_dir / "logs" / "runner_response.md"),
    }


def ensure_work_order_files(task: SubAgentTask) -> None:
    for directory in [
        task.data_dir,
        task.output_dir,
        task.tests_dir,
        task.reports_dir,
        task.logs_dir,
        task.scratch_dir,
    ]:
        Path(directory).mkdir(parents=True, exist_ok=True)

    _write_if_missing(Path(task.status_file), _status_content(task))
    _write_if_missing(Path(task.work_log_file), _WORK_LOG_TMPL.format(
        time_str=time.strftime("%Y-%m-%d %H:%M:%S"), task_id=task.id,
    ))
    _write_if_missing(Path(task.action_receipts_file), _ACTION_RECEIPTS_TMPL)
    _write_if_missing(Path(task.acceptance_file), _acceptance_content(task))
    _write_if_missing(Path(task.test_checklist_file), _TEST_CHECKLIST_TMPL)
    _write_if_missing(Path(task.bugs_file), _BUGS_TMPL)
    _write_if_missing(Path(task.skill_usage_file), _SKILL_USAGE_TMPL)
    _write_if_missing(Path(task.skill_sparks_file), _skill_sparks_content(task))
    _write_if_missing(Path(task.handoff_file), _handoff_content(task))
    _write_if_missing(Path(task.debrief_file), _DEBRIEF_TMPL)
    _write_json_if_missing(Path(task.output_json), _output_json_template(task.id, task.status))
    _write_json_if_missing(Path(task.status_report_json), _status_report_json_template(task.id, task.status))
    _write_json_if_missing(Path(task.checkpoint_json), _CHECKPOINT_JSON_TMPL(task.id, task.status))
    _write_json_if_missing(Path(task.decision_ledger_json), _DECISION_LEDGER_JSON_TMPL(task.id))
    _write_if_missing(Path(task.progress_md), _PROGRESS_MD_TMPL.format(task_id=task.id, status=task.status))
    _write_json_if_missing(Path(task.failing_tests_json), _FAILING_TESTS_JSON_TMPL(task.id))
    _write_json_if_missing(Path(task.next_actions_json), _NEXT_ACTIONS_JSON_TMPL(task.id))
    _write_json_if_missing(Path(task.dependencies_json), _deps_json_template(task.id))


def write_takeover_file(task: SubAgentTask, record: TakeoverRecord) -> None:
    content = (
        "# TAKEOVER\n\n"
        f"- takeover_id: {record.id}\n"
        f"- run_id: {record.run_id}\n"
        f"- take_over_by: {record.take_over_by}\n"
        f"- previous_owner: {record.previous_owner or 'none'}\n"
        f"- reason: {record.reason}\n"
        f"- created_at: {record.created_at}\n\n"
        "## Locked Files\n"
        + "\n".join(f"- {item}" for item in record.locked_files or ["none"])
        + "\n\n"
        "## Rule\n\n"
        "- 接管后，原子代理不得继续写 locked_files 中的文件。\n"
        "- 后续写入必须由 final_owner 或接管者统一收口。\n"
    )
    Path(task.takeover_file).write_text(content, encoding="utf-8")


def validate_work_order(manager: Any, run_id: str) -> WorkOrderValidation:
    task = manager.load(run_id)
    _apply_missing_paths(task, manager._build_work_order_paths(task.id, task.task_dir or None))
    required_paths = [
        task.task_dir,
        task.data_dir,
        task.output_dir,
        task.tests_dir,
        task.reports_dir,
        task.logs_dir,
        task.scratch_dir,
        task.status_file,
        task.work_log_file,
        task.action_receipts_file,
        task.acceptance_file,
        task.test_checklist_file,
        task.bugs_file,
        task.skill_usage_file,
        task.skill_sparks_file,
        task.handoff_file,
        task.debrief_file,
        task.output_json,
        task.status_report_json,
        task.checkpoint_json,
        task.decision_ledger_json,
        task.progress_md,
        task.failing_tests_json,
        task.next_actions_json,
        task.dependencies_json,
    ]
    if task.takeover_by:
        required_paths.append(task.takeover_file)
    missing = [item for item in required_paths if item and not Path(item).exists()]
    warnings: list[str] = []
    if not task.allowed_write_roots:
        warnings.append("未设置 allowed_write_roots")
    if not task.forbidden_write_roots:
        warnings.append("未设置 forbidden_write_roots")
    return WorkOrderValidation(run_id=run_id, ok=not missing, missing=missing, warnings=warnings)


def _status_content(task: SubAgentTask) -> str:
    return (
        "# STATUS\n\n"
        f"- id: {task.id}\n"
        f"- status: {task.status}\n"
        f"- owner: {task.owner or 'none'}\n"
        f"- supervisor: {task.supervisor or 'none'}\n"
        f"- final_owner: {task.final_owner or 'none'}\n"
        f"- updated_at: {task.updated_at or task.created_at}\n"
    )


def _acceptance_content(task: SubAgentTask) -> str:
    checks = "\n".join(f"- [ ] {item}" for item in task.acceptance_checks or ["未设置"])
    return _ACCEPTANCE_TMPL.format(checks=checks)


def _handoff_content(task: SubAgentTask) -> str:
    return (
        "# HANDOFF\n\n"
        "## Current State\n\n- 待填写\n\n"
        "## Done\n\n- 待填写\n\n"
        "## Not Done\n\n- 待填写\n\n"
        "## Next Step\n\n- 待填写\n\n"
        "## Recovery Entry\n\n"
        f"- status: {task.status_file}\n"
        f"- acceptance: {task.acceptance_file}\n"
        f"- tests: {task.test_checklist_file}\n"
    )


def _skill_sparks_content(task: SubAgentTask) -> str:
    return (
        "# SKILL_SPARKS\n\n"
        "这些条目只是本子代理工作目录里的 skill 学习候选，不写入主代理长期记忆，"
        "也不会自动升级为正式 skill。\n\n"
        "## Boundary\n\n"
        f"- run_id: {task.id}\n"
        f"- root_id: {task.root_id or task.id}\n"
        "- scope: task-local\n"
        "- promotion: requires review, evidence, and explicit later workflow\n\n"
        "## Sparks\n\n"
        "- 暂无\n\n"
        "## Template\n\n"
        "- title:\n"
        "  trigger:\n"
        "  reusable_steps:\n"
        "  evidence_refs:\n"
        "  limits_or_counterexamples:\n"
        "  suggested_skill_area:\n"
    )


_WORK_LOG_TMPL = "# WORK_LOG\n\n- {time_str} 创建工单 {task_id}\n"
_ACTION_RECEIPTS_TMPL = "# ACTION_RECEIPTS\n\n每轮推进后追加一条 receipt.\n\n## Template\n\n- time:\n- action:\n- evidence:\n- failure_or_fallback:\n- next:\n"
_ACCEPTANCE_TMPL = "# ACCEPTANCE\n\n## Checks\n{checks}\n\n## Evidence\n\n- 暂无\n"
_TEST_CHECKLIST_TMPL = "# TEST_CHECKLIST\n\n## From Requirement\n\n- [ ] 原始需求已转成可测试清单\n- [ ] P0/P1 验收标准已明确\n\n## Entrypoints\n\n- [ ] CLI/API/Web/文件入口已实际运行\n- [ ] 异常路径和边界输入已覆盖\n\n## Evidence\n\n- [ ] 测试命令、日志、截图或报告路径已记录\n"
_BUGS_TMPL = "# BUGS\n\n## Open P0/P1\n\n- 暂无\n\n## Non-blocking\n\n- 暂无\n"
_SKILL_USAGE_TMPL = "# SKILL_USAGE\n\n记录本任务匹配、读取和实际使用过的 skill / references / 外部知识库。\n\n## Used\n\n- 暂无\n\n## Considered But Not Used\n\n- 暂无\n"
_DEBRIEF_TMPL = "# DEBRIEF\n\n## 方法\n\n- 待填写\n\n## 结果\n\n- 待填写\n\n## 可沉淀经验\n\n- 待填写\n"
_PROGRESS_MD_TMPL = "# PROGRESS\n\n- run_id: {task_id}\n- status: {status}\n- progress: 0.0\n- current_step: {status}\n\n## Latest Summary\n\n- 暂无\n"


def _output_json_template(task_id: str, status: str) -> dict[str, object]:
    return {
        "run_id": task_id,
        "status": status,
        "artifacts": [],
        "tests": [],
        "acceptance": [],
        "blockers": [],
        "next_action": "",
    }


def _status_report_json_template(task_id: str, status: str) -> dict[str, object]:
    return {
        "run_id": task_id,
        "version": 0,
        "state": status,
        "progress": 0.0,
        "current_step": status,
        "summary_delta": {
            "facts_added": [],
            "facts_invalidated": [],
            "decisions_changed": [],
            "open_questions": [],
        },
        "budget_used": {},
        "artifact_refs": [],
        "evidence_refs": [],
        "blockers": [],
        "checkpoint_ref": "",
        "next_recommended_action": "",
    }


def _deps_json_template(task_id: str) -> dict[str, object]:
    return {"run_id": task_id, "dependencies": []}


def _CHECKPOINT_JSON_TMPL(task_id: str, status: str) -> dict[str, object]:
    return {
        "run_id": task_id,
        "status": status,
        "verification_status": "UNVERIFIED",
        "progress": 0.0,
        "current_step": status,
        "latest_summary": "",
        "blockers": [],
        "artifact_refs": [],
        "evidence_refs": [],
        "evidence_packet_ids": [],
        "finding_ids": [],
        "status_report_ref": "",
        "handoff_ref": "",
        "work_log_ref": "",
        "acceptance_ref": "",
        "output_ref": "",
        "decision_ledger_ref": "",
        "failing_tests_ref": "",
        "next_actions_ref": "",
        "updated_at": 0.0,
    }


def _DECISION_LEDGER_JSON_TMPL(task_id: str) -> dict[str, object]:
    return {"run_id": task_id, "decisions": [], "open_questions": []}


def _FAILING_TESTS_JSON_TMPL(task_id: str) -> dict[str, object]:
    return {"run_id": task_id, "failing_tests": []}


def _NEXT_ACTIONS_JSON_TMPL(task_id: str) -> dict[str, object]:
    return {"run_id": task_id, "next_actions": []}
