from __future__ import annotations

"""LLM contract: SubAgentBaseMixin - thin facade delegating core task lifecycle to services.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
业务逻辑已移至 services/base.py, services/persistence.py, services/workflow.py。
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    SubAgentCard,
    SubAgentTask,
    TakeoverRecord,
    WorkOrderValidation,
)
from .policies import _default_forbidden_write_roots
from .utils import (
    _apply_missing_paths,
    _new_id,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore


class SubAgentBaseMixin:
    """Thin facade delegating core task lifecycle to SubAgentBaseService and SubAgentPersistenceService."""

    def __init__(
        self,
        workspace: str | Path,
        local_store: LocalStore | None = None,
        workspace_root: str | Path | None = None,
        enable_self_learning: bool = False,
    ):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}
        self.local_store = local_store
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else self.workspace.resolve().parent
        self.enable_self_learning = bool(enable_self_learning)
        from .services.base import SubAgentBaseService
        from .services.lifecycle import SubAgentLifecycleService
        from .services.persistence import SubAgentPersistenceService

        self.lifecycle = SubAgentLifecycleService(self)
        self.persistence = SubAgentPersistenceService(self)
        self.base_service = SubAgentBaseService(self)

    # --- Core task operations delegated to base_service ---

    def split(self, goal: str, count: int, *, workflow_mode: str = "off") -> list[SubAgentTask]:
        return self.base_service.split(goal, count, workflow_mode=workflow_mode)

    def register_card(self, card: SubAgentCard) -> None:
        self.cards[card.name] = card

    def create_run(self, **kwargs) -> SubAgentTask:
        return self.base_service.create_run(**kwargs)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ):
        return self.base_service.record_takeover(run_id, take_over_by=take_over_by, reason=reason, locked_files=locked_files)

    # --- Persistence delegated to persistence service ---

    def load(self, run_id: str) -> SubAgentTask:
        return self.persistence.load(run_id)

    def list_runs(self) -> list[SubAgentTask]:
        return self.persistence.list_runs()

    def save(self, task: SubAgentTask) -> None:
        self.persistence.save(task)

    def add_child(self, parent_id: str, child_id: str) -> None:
        try:
            parent = self.load(parent_id)
        except FileNotFoundError:
            return
        if child_id not in parent.child_ids:
            parent.child_ids.append(child_id)
            parent.updated_at = time.time()
            self.save(parent)

    # --- Work order utilities (used by persistence service) ---

    def _build_work_order_paths(
        self,
        run_id: str,
        task_dir: str | Path | None = None,
        extra_write_roots: list[str] | None = None,
    ) -> dict[str, object]:
        """Generate standard work order directory paths."""
        task_dir = Path(task_dir) if task_dir else self.workspace / run_id
        paths = {
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
            "handoff_file": str(task_dir / "HANDOFF.md"),
            "debrief_file": str(task_dir / "DEBRIEF.md"),
            "output_json": str(task_dir / "output.json"),
            "dependencies_json": str(task_dir / "dependencies.json"),
            "takeover_file": str(task_dir / "TAKEOVER.md"),
            "channel_probe_file": str(task_dir / "CHANNEL_PROBE.md"),
            "execution_context_file": str(task_dir / "EXECUTION_CONTEXT.md"),
            "execution_context_json": str(task_dir / "execution_context.json"),
            "runner_result_file": str(task_dir / "RUNNER_RESULT.md"),
            "runner_result_json": str(task_dir / "reports" / "runner_result.json"),
            "runner_prompt_file": str(task_dir / "logs" / "runner_prompt.md"),
            "runner_response_file": str(task_dir / "logs" / "runner_response.md"),
        }
        paths["allowed_write_roots"] = [str(task_dir)] + list(extra_write_roots or [])
        paths["forbidden_write_roots"] = _default_forbidden_write_roots()
        return paths

    def _ensure_work_order_files(self, task: SubAgentTask) -> None:
        """Initialize standard work order directories and minimal files."""
        for directory in [
            task.data_dir, task.output_dir, task.tests_dir,
            task.reports_dir, task.logs_dir, task.scratch_dir,
        ]:
            Path(directory).mkdir(parents=True, exist_ok=True)

        _write_if_missing(Path(task.status_file), _STATUS_TMPL.format(
            task_id=task.id, status=task.status,
            owner=task.owner or 'none', supervisor=task.supervisor or 'none',
            final_owner=task.final_owner or 'none',
            updated_at=task.updated_at or task.created_at,
        ))
        _write_if_missing(Path(task.work_log_file), _WORK_LOG_TMPL.format(
            time_str=time.strftime('%Y-%m-%d %H:%M:%S'), task_id=task.id,
        ))
        _write_if_missing(Path(task.action_receipts_file), _ACTION_RECEIPTS_TMPL)
        _write_if_missing(Path(task.acceptance_file), _ACCEPTANCE_TMPL.format(
            checks="\n".join(f"- [ ] {item}" for item in task.acceptance_checks or ["未设置"]),
        ))
        _write_if_missing(Path(task.test_checklist_file), _TEST_CHECKLIST_TMPL)
        _write_if_missing(Path(task.bugs_file), _BUGS_TMPL)
        _write_if_missing(Path(task.skill_usage_file), _SKILL_USAGE_TMPL)
        _write_if_missing(Path(task.handoff_file), _HANDOFF_TMPL.format(
            status_file=task.status_file,
            acceptance_file=task.acceptance_file,
            test_checklist_file=task.test_checklist_file,
        ))
        _write_if_missing(Path(task.debrief_file), _DEBRIEF_TMPL)
        _write_json_if_missing(Path(task.output_json), _OUTPUT_JSON_TMPL.format(task_id=task.id, status=task.status))
        _write_json_if_missing(Path(task.dependencies_json), _DEPS_JSON_TMPL.format(task_id=task.id))

    def _write_takeover_file(self, task: SubAgentTask, record: TakeoverRecord) -> None:
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

    def validate_work_order(self, run_id: str) -> WorkOrderValidation:
        """Check if subagent work order directory has minimum takeable structure."""
        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        required_paths = [
            task.task_dir, task.data_dir, task.output_dir, task.tests_dir,
            task.reports_dir, task.logs_dir, task.scratch_dir,
            task.status_file, task.work_log_file, task.action_receipts_file,
            task.acceptance_file, task.test_checklist_file, task.bugs_file,
            task.skill_usage_file, task.handoff_file, task.debrief_file,
            task.output_json, task.dependencies_json,
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

    # --- ID generation helper for other mixins ---
    def _new_id(self, prefix: str) -> str:
        return _new_id(prefix)


# Re-export helpers from services for backward compatibility and tests
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

# Work order file templates (concise, readable)
_STATUS_TMPL = "# STATUS\n\n- id: {task_id}\n- status: {status}\n- owner: {owner}\n- supervisor: {supervisor}\n- final_owner: {final_owner}\n- updated_at: {updated_at}\n"
_WORK_LOG_TMPL = "# WORK_LOG\n\n- {time_str} 创建工单 {task_id}\n"
_ACTION_RECEIPTS_TMPL = "# ACTION_RECEIPTS\n\n每轮推进后追加一条 receipt.\n\n## Template\n\n- time:\n- action:\n- evidence:\n- failure_or_fallback:\n- next:\n"
_ACCEPTANCE_TMPL = "# ACCEPTANCE\n\n## Checks\n{checks}\n\n## Evidence\n\n- 暂无\n"
_TEST_CHECKLIST_TMPL = "# TEST_CHECKLIST\n\n## From Requirement\n\n- [ ] 原始需求已转成可测试清单\n- [ ] P0/P1 验收标准已明确\n\n## Entrypoints\n\n- [ ] CLI/API/Web/文件入口已实际运行\n- [ ] 异常路径和边界输入已覆盖\n\n## Evidence\n\n- [ ] 测试命令、日志、截图或报告路径已记录\n"
_BUGS_TMPL = "# BUGS\n\n## Open P0/P1\n\n- 暂无\n\n## Non-blocking\n\n- 暂无\n"
_SKILL_USAGE_TMPL = "# SKILL_USAGE\n\n记录本任务匹配、读取和实际使用过的 skill / references / 外部知识库。\n\n## Used\n\n- 暂无\n\n## Considered But Not Used\n\n- 暂无\n"
_HANDOFF_TMPL = "# HANDOFF\n\n## Current State\n\n- 待填写\n\n## Done\n\n- 待填写\n\n## Not Done\n\n- 待填写\n\n## Next Step\n\n- 待填写\n\n## Recovery Entry\n\n- status: {status_file}\n- acceptance: {acceptance_file}\n- tests: {test_checklist_file}\n"
_DEBRIEF_TMPL = "# DEBRIEF\n\n## 方法\n\n- 待填写\n\n## 结果\n\n- 待填写\n\n## 可沉淀经验\n\n- 待填写\n"
_OUTPUT_JSON_TMPL = '{{"run_id": "{task_id}", "status": "{status}", "artifacts": [], "tests": [], "acceptance": [], "blockers": [], "next_action": ""}}'
_DEPS_JSON_TMPL = '{{"run_id": "{task_id}", "dependencies": []}}'