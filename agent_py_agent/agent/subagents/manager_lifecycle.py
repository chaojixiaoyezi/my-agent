from __future__ import annotations

"""LLM contract: SubAgentLifecycleMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
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
from ..memory_routing import load_routes, match_routes, resolve_required_paths

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentLifecycleMixin:
    def record_capability_request(
        self,
        run_id: str,
        *,
        problem: str,
        needed_capability: str,
        expected_output: str = "",
        tried: list[str] | None = None,
        evidence: list[str] | None = None,
        constraints: dict[str, str] | None = None,
    ) -> CapabilityRequest:
        """给某个子代理记录一条能力请求。"""

        task = self.load(run_id)
        request = CapabilityRequest(
            id=_new_id("capreq"),
            from_run_id=run_id,
            problem=problem,
            needed_capability=needed_capability,
            expected_output=expected_output,
            tried=tried or [],
            evidence=evidence or [],
            constraints=constraints or {},
            created_at=time.time(),
        )
        task.capability_requests.append(request)
        task.updated_at = time.time()
        self.save(task)
        return request

    def record_capability_grant(
        self,
        run_id: str,
        *,
        request_id: str,
        skills: list[str] | None = None,
        tools: list[str] | None = None,
        capability_cards: list[dict[str, str]] | None = None,
        reason: str = "",
        constraints: dict[str, str] | None = None,
        expires_after_task: bool = True,
    ) -> CapabilityGrant:
        """给某个子代理记录一条能力授权。"""

        task = self.load(run_id)
        grant = CapabilityGrant(
            id=_new_id("capgrant"),
            request_id=request_id,
            grant_to_run_id=run_id,
            skills=skills or [],
            tools=tools or [],
            capability_cards=capability_cards or [],
            reason=reason,
            constraints=constraints or {},
            expires_after_task=expires_after_task,
            created_at=time.time(),
        )
        task.capability_grants.append(grant)
        task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
        task.allowed_tools = _merge_list(task.allowed_tools, grant.tools)
        task.updated_at = time.time()
        self.save(task)
        return grant

    def record_capability_gap(
        self,
        run_id: str,
        *,
        missing_capability: str,
        why_failed: str,
        attempted_skills: list[str] | None = None,
        attempted_tools: list[str] | None = None,
        needed_outputs: list[str] | None = None,
        suggested_skill: str = "",
        suggested_tool: str = "",
    ) -> CapabilityGap:
        """给某个子代理记录一条能力缺口。"""

        task = self.load(run_id)
        injected_rule_paths: list[str] = []
        memory_routes: list[dict[str, str]] = []
        route_query = " ".join([missing_capability, why_failed, task.goal]).strip()
        route_mode = "soft"
        if route_query and route_mode != "off":
            try:
                index_path = (self.workspace_root / "memory" / "routing" / "INDEX.md").resolve()
                if index_path.exists():
                    routes = load_routes(index_path)
                    matches = match_routes(route_query, routes, limit=5)
                    resolution = resolve_required_paths(
                        matches,
                        mode="strict",
                        auto_read_limit=3,
                    )
                    injected_rule_paths = list(dict.fromkeys(
                        [*resolution.required_read_paths, *resolution.candidate_paths]
                    ))
                    memory_routes = [
                        {
                            "route_id": match.route.route_id,
                            "source_file": match.route.authority_file(),
                            "inject_mode": match.route.inject_mode,
                        }
                        for match in matches
                    ]
            except Exception:
                injected_rule_paths = []
                memory_routes = []
        gap = CapabilityGap(
            id=_new_id("capgap"),
            run_id=run_id,
            missing_capability=missing_capability,
            source_task=task.goal,
            why_failed=why_failed,
            attempted_skills=attempted_skills or [],
            attempted_tools=attempted_tools or [],
            needed_outputs=needed_outputs or [],
            suggested_skill=suggested_skill,
            suggested_tool=suggested_tool,
            memory_routes=memory_routes,
            injected_rule_paths=injected_rule_paths,
            created_at=time.time(),
        )
        task.capability_gaps.append(gap)
        if injected_rule_paths:
            task.context_manifest.required_read_paths = _merge_list(
                task.context_manifest.required_read_paths,
                injected_rule_paths,
            )
        task.updated_at = time.time()
        self.save(task)
        return gap

    def record_evidence(
        self,
        run_id: str,
        *,
        kind: str,
        summary: str,
        command: str = "",
        path: str = "",
        url: str = "",
        ok: bool = True,
    ) -> VerificationEvidence:
        """记录一条验收证据。"""

        task = self.load(run_id)
        evidence = VerificationEvidence(
            kind=kind,
            summary=summary,
            command=command,
            path=path,
            url=url,
            ok=ok,
            created_at=time.time(),
        )
        task.evidence.append(evidence)
        task.verification_status = "VERIFIED" if ok else "FAILED"
        task.updated_at = time.time()
        self.save(task)
        return evidence

    def touch_heartbeat(self, run_id: str) -> None:
        """刷新子代理心跳时间。"""

        task = self.load(run_id)
        task.heartbeat_at = time.time()
        task.updated_at = task.heartbeat_at
        self.save(task)

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ) -> SubAgentTask:
        """更新任务状态。

        `require_evidence=True` 时，没有验收证据不能标记为 DONE。
        这是防 Fake Done 的第一道硬约束。
        """

        task = self.load(run_id)
        normalized = status.upper()
        if require_evidence and normalized == "DONE" and not task.evidence:
            raise ValueError("缺少验收证据，不能标记为 DONE。")
        task.status = normalized
        if result:
            task.result = result
        if failure_type:
            task.failure_type = failure_type
        if normalized in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.save(task)
        return task

    def prepare_runner_attempt(self, run_id: str, *, retry_reason: str = "") -> SubAgentTask:
        """把任务切到 RUNNING，准备启动一次 runner。

        初次执行和重试都走这里。大白话说：
        - 旧的失败原因保留在 runner_last_error / 日志里；
        - 当前状态先恢复成 RUNNING，避免 runner 看到 BLOCKED 后误以为任务已经不能做；
        - 真正成功或失败由 record_runner_result 再写回。
        """

        task = self.load(run_id)
        previous = f"{task.status}/{task.failure_type or 'none'}"
        attempt_id = _new_id("attempt")
        task.status = "RUNNING"
        task.verification_status = "UNVERIFIED"
        task.failure_type = ""
        task.ended_at = 0.0
        task.runner_active_attempt_id = attempt_id
        task.updated_at = time.time()
        task.heartbeat_at = task.updated_at
        self.save(task)
        suffix = f" retry_reason={retry_reason}" if retry_reason else ""
        self._append_task_work_log(
            task,
            (
                f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
                f"attempt_id={attempt_id}{suffix}"
            ),
        )
        return task

    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = "") -> SubAgentTask:
        """标记一个 runner attempt 已被放弃，后续迟到结果不再覆盖账本。"""

        task = self.load(run_id)
        normalized = str(attempt_id or "").strip()
        if not normalized:
            return task
        if normalized not in task.runner_abandoned_attempt_ids:
            task.runner_abandoned_attempt_ids.append(normalized)
        if task.runner_active_attempt_id == normalized:
            task.runner_active_attempt_id = ""
        task.updated_at = time.time()
        self.save(task)
        if reason:
            self._append_task_work_log(
                task,
                f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
            )
        return task
