from __future__ import annotations

"""LLM: base task creation and lifecycle service.

给人看的解释：
这里承接子代理任务创建、分割、注册卡等基础能力。
SubAgentManager 通过 facade 方法委托到这里。
"""

import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import ContextManifest, QualityContract, SubAgentCard, SubAgentTask


# LLM: patterns for extracting directory paths from user goal text.
_DIR_PATTERN = re.compile(r"(?:/[\w.\-]+){2,}")
_HOME_DIR_PATTERN = re.compile(r"(?:~/[\w.\-]+(?:/[\w.\-]+)*)")


def _extract_write_dirs(goal: str) -> list[str]:
    """Extract directory paths from user goal text for automatic subagent write permission."""
    dirs: list[str] = []
    for pattern in [_DIR_PATTERN, _HOME_DIR_PATTERN]:
        for match in pattern.finditer(goal):
            path = match.group().strip()
            if path and path not in dirs:
                dirs.append(path)
    return dirs


class SubAgentBaseService:
    """Base task creation and lifecycle service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def split(self, goal: str, count: int, *, workflow_mode: str = "off") -> list[SubAgentTask]:
        """Split a goal into multiple subagent task records.

        Currently uses template-based splitting for simplicity.
        """
        from ..services.workflow import _normalize_workflow_mode_value

        extra_roots = _extract_write_dirs(goal)
        count = max(1, count)
        tasks: list[SubAgentTask] = []
        for i in range(1, count + 1):
            task = self.create_run(
                goal=f"{goal} / 子任务{i}",
                thought="先缩小任务边界，明确输入、输出和验证证据，再执行。",
                plan=["理解目标", "列出交付物", "执行最小验证", "汇报结果和证据"],
                extra_write_roots=extra_roots,
                workflow_mode=workflow_mode,
            )
            tasks.append(task)
        return tasks

    def register_card(self, card: SubAgentCard) -> None:
        """Register a subagent role card."""
        self.manager.cards[card.name] = card

    def create_run(
        self,
        *,
        goal: str,
        thought: str,
        plan: list[str],
        agent_name: str = "general",
        role: str = "general",
        parent_id: str = "",
        root_id: str = "",
        depth: int = 0,
        allowed_skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        owner: str = "",
        supervisor: str = "",
        final_owner: str = "",
        acceptance_checks: list[str] | None = None,
        quality_contract: QualityContract | dict[str, object] | None = None,
        context_manifest: ContextManifest | dict[str, object] | None = None,
        context_packs: list[dict[str, object]] | dict[str, object] | None = None,
        extra_write_roots: list[str] | None = None,
        workflow_mode: str = "off",
    ) -> SubAgentTask:
        """Create a subagent task record.

        workflow_mode controls workflow planning:
          - "off" : default, no workflow (backward compatible)
          - "plan" : run workflow planning, write result to task.workflow_plan
          - "auto" : run workflow planning, auto-merge worker spec and parent gate into acceptance checklist
        """
        from ..services.workflow import (
            _merge_workflow_acceptance_checks,
            _normalize_workflow_mode_value,
            _try_workflow_plan,
        )

        now = time.time()
        run_id = self.manager._new_id("subagent")
        paths = self.manager._build_work_order_paths(run_id, extra_write_roots=extra_write_roots)
        normalized_workflow_mode = _normalize_workflow_mode_value(workflow_mode)

        workflow_plan_dict: dict[str, object] | None = None
        merged_acceptance = list(acceptance_checks or [])
        if normalized_workflow_mode != "off":
            workflow_plan_dict = _try_workflow_plan(
                goal,
                quality_contract=quality_contract,
                context_manifest=context_manifest,
                allowed_write_roots=extra_write_roots,
            )
            merged_acceptance = _merge_workflow_acceptance_checks(merged_acceptance, workflow_plan_dict)

        from ..models import SubAgentTask
        from ..services.persistence import (
            _normalize_context_manifest,
            _normalize_context_packs,
            _normalize_quality_contract,
        )

        task = SubAgentTask(
            id=run_id,
            goal=goal,
            thought=thought,
            plan=plan,
            agent_name=agent_name,
            role=role,
            owner=owner,
            supervisor=supervisor,
            final_owner=final_owner,
            parent_id=parent_id,
            root_id=root_id or run_id,
            depth=depth,
            allowed_skills=allowed_skills or [],
            allowed_tools=allowed_tools or [],
            acceptance_checks=merged_acceptance,
            quality_contract=_normalize_quality_contract(quality_contract),
            context_manifest=_normalize_context_manifest(context_manifest),
            context_packs=_normalize_context_packs(context_packs),
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            workflow_mode=normalized_workflow_mode,
            workflow_template_id=str((workflow_plan_dict or {}).get("selected_template_id") or ""),
            workflow_plan=workflow_plan_dict or {},
            **paths,
        )
        self.manager.save(task)
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
        return task

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

        task = self.manager.load(run_id)
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
        self.manager.save(task)
        self.manager._write_takeover_file(task, record)
        return record