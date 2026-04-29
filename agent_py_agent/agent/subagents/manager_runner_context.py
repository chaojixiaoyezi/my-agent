from __future__ import annotations

"""LLM contract: runner execution-context construction and persistence.

Human version:
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import *
from .reports import *
from .rendering import *
from .runner_rendering import *
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentRunnerContextMixin:
    def build_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """生成单个子代理执行器可读取的最小上下文。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        granted_skills: list[str] = []
        granted_tools: list[str] = []
        grants: list[dict[str, object]] = []
        for grant in task.capability_grants:
            granted_skills = _merge_list(granted_skills, grant.skills)
            granted_tools = _merge_list(granted_tools, grant.tools)
            grants.append(
                {
                    "id": grant.id,
                    "request_id": grant.request_id,
                    "skills": grant.skills,
                    "tools": grant.tools,
                    "reason": grant.reason,
                    "constraints": grant.constraints,
                    "expires_after_task": grant.expires_after_task,
                    "created_at": grant.created_at,
                }
            )

        allowed_skills = _merge_list(task.allowed_skills, granted_skills)
        allowed_tools = _merge_list(task.allowed_tools, granted_tools)
        return SubAgentExecutionContext(
            run_id=task.id,
            generated_at=time.time(),
            goal=task.goal,
            thought=task.thought,
            plan=task.plan,
            agent_name=task.agent_name,
            role=task.role,
            status=task.status,
            verification_status=task.verification_status,
            channel_status=task.channel_status,
            runner_attempts=task.runner_attempts,
            runner_last_error=task.runner_last_error,
            owner=task.owner,
            supervisor=task.supervisor,
            final_owner=task.final_owner,
            parent_id=task.parent_id,
            root_id=task.root_id,
            depth=task.depth,
            task_dir=task.task_dir,
            execution_context_file=task.execution_context_file,
            execution_context_json=task.execution_context_json,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            granted_cards=_dedupe_granted_cards(task.capability_grants, max_cards=max_cards),
            grants=grants,
            acceptance_checks=task.acceptance_checks,
            evidence=[asdict(item) for item in task.evidence],
            write_boundary={
                "task_dir": task.task_dir,
                "allowed_write_roots": task.allowed_write_roots,
                "forbidden_write_roots": task.forbidden_write_roots,
                "locked_files": task.locked_files,
                "status_file": task.status_file,
                "work_log_file": task.work_log_file,
                "action_receipts_file": task.action_receipts_file,
                "acceptance_file": task.acceptance_file,
                "test_checklist_file": task.test_checklist_file,
                "bugs_file": task.bugs_file,
                "skill_usage_file": task.skill_usage_file,
                "handoff_file": task.handoff_file,
                "debrief_file": task.debrief_file,
                "output_json": task.output_json,
                "dependencies_json": task.dependencies_json,
            },
            pending_requests=[
                asdict(item) for item in task.capability_requests if item.status == "OPEN"
            ],
            open_gaps=[asdict(item) for item in task.capability_gaps if item.status == "OPEN"],
            instructions=_execution_context_instructions(),
        )

    def write_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """写出子代理执行上下文 JSON 和 Markdown。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        context = self.build_execution_context(run_id, max_cards=max_cards)
        Path(context.execution_context_json).write_text(
            json.dumps(asdict(context), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(context.execution_context_file).write_text(
            render_execution_context_markdown(context),
            encoding="utf-8",
        )
        task = self.load(run_id)
        self._append_task_work_log(
            task,
            f"execution_context: 已生成执行上下文，cards={len(context.granted_cards)}。",
        )
        self._index_execution_context(context)
        return context

