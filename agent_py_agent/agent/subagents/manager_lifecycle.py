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

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from ..memory_routing import load_routes, match_routes, resolve_required_paths
from .models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    SubAgentTask,
    VerificationEvidence,
)
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
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
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
from .runner_rendering import _render_runner_item_line
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentLifecycleMixin:
    def _lifecycle_service(self):
        """LLM: lazily create lifecycle service for legacy mixin-only tests.

        人话说明：
        正常 SubAgentManager 初始化时会设置 self.lifecycle；一些旧测试会直接
        `__new__` mixin 并手动挂 load/save，所以这里保留兼容懒加载。
        """

        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is None:
            from .services.lifecycle import SubAgentLifecycleService

            lifecycle = SubAgentLifecycleService(self)
            self.lifecycle = lifecycle
        return lifecycle

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

        return self._lifecycle_service().record_capability_request(
            run_id,
            problem=problem,
            needed_capability=needed_capability,
            expected_output=expected_output,
            tried=tried,
            evidence=evidence,
            constraints=constraints,
        )

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

        return self._lifecycle_service().record_capability_grant(
            run_id,
            request_id=request_id,
            skills=skills,
            tools=tools,
            capability_cards=capability_cards,
            reason=reason,
            constraints=constraints,
            expires_after_task=expires_after_task,
        )

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

        return self._lifecycle_service().record_capability_gap(
            run_id,
            missing_capability=missing_capability,
            why_failed=why_failed,
            attempted_skills=attempted_skills,
            attempted_tools=attempted_tools,
            needed_outputs=needed_outputs,
            suggested_skill=suggested_skill,
            suggested_tool=suggested_tool,
        )

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

        return self._lifecycle_service().record_evidence(
            run_id,
            kind=kind,
            summary=summary,
            command=command,
            path=path,
            url=url,
            ok=ok,
        )

    def touch_heartbeat(self, run_id: str) -> None:
        """刷新子代理心跳时间。"""

        self._lifecycle_service().touch_heartbeat(run_id)

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

        return self._lifecycle_service().set_status(
            run_id,
            status,
            result=result,
            failure_type=failure_type,
            require_evidence=require_evidence,
        )

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
