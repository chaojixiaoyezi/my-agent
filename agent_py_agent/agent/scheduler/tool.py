from __future__ import annotations

"""Single action-oriented owner scheduler tool."""

import json
import time
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerNotFoundError,
    SchedulerRepositoryError,
)
from .schedule import ScheduleValidationError, build_schedule, iso_from_timestamp

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MUTATING_EXISTING_ACTIONS = frozenset({"update", "pause", "resume", "delete"})


def build_schedule_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="schedule",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "管理当前用户自己的持久定时任务或提醒。任务到点后会回到创建它的同一会话继续执行，"
            "网关重启后仍会恢复；支持一次、固定间隔和五段 cron。"
        ),
        use_cases=[
            "用户要求稍后提醒、每天/每周定时执行一件事",
            "查看、修改、暂停、恢复或删除自己的定时任务",
            "立即试跑一个已有定时任务，或查看历史结果",
        ],
        avoid_when=[
            "只是当前长任务内部等待子代理或命令完成时，继续使用 wait",
            "用户没有要求跨当前执行轮持续存在的定时时，不要创建计划",
            "不能指定其他用户、群或任意会话；归属和会话由当前可信运行上下文固定",
        ],
        keywords=["提醒", "定时", "schedule", "cron", "每天", "每周", "稍后", "周期任务"],
        parameters={
            "action": "create/list/get/update/pause/resume/delete/run_now/history/status。",
            "job_id": "除 create/list/status 外，使用 list/get 返回的精确 job_id。",
            "expected_version": "update/pause/resume/delete 必填，使用 get/list 返回的 version。",
            "name": "create 必填；简短说明这个定时任务。update 可选。",
            "prompt": "create 必填；到点后要在同一会话继续执行的完整用户要求。update 可选。",
            "schedule_kind": "create 必填；at/every/cron。update 修改时间时填写。",
            "at": "at 的 ISO-8601 时间或 Unix 时间戳；无时区时按 timezone/config 解释。",
            "every_seconds": "every 的间隔秒数，最少 60。",
            "anchor_at": "every 可选锚点时间。",
            "cron": "cron 的五段表达式（分 时 日 月 周）。",
            "timezone": "可选 IANA 时区，例如 Asia/Shanghai；默认使用 agent 配置。",
            "misfire_grace_seconds": "可选。重启等原因错过后允许补执行的秒数。",
            "skill_ids": "可选。将当前可用 Skill 的稳定 ID/名称及内容版本固定到这个计划。",
            "limit": "list/history 的返回条数。",
        },
        parameter_schema={
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "list",
                    "get",
                    "update",
                    "pause",
                    "resume",
                    "delete",
                    "run_now",
                    "history",
                    "status",
                ],
            },
            "job_id": {"type": "string"},
            "expected_version": {"type": "integer", "minimum": 1},
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "schedule_kind": {"type": "string", "enum": ["at", "every", "cron"]},
            "at": {},
            "every_seconds": {"type": "integer", "minimum": 60},
            "anchor_at": {},
            "cron": {"type": "string"},
            "timezone": {"type": "string"},
            "misfire_grace_seconds": {"type": "integer", "minimum": 0, "maximum": 604800},
            "skill_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        required_parameters=["action"],
        internal_parameters=["__tool_call_id", "__run_scope"],
        examples=[
            '{"tool":"schedule","action":"create","name":"周报提醒","prompt":"整理本周项目进展并发给我","schedule_kind":"cron","cron":"0 18 * * 5","timezone":"Asia/Shanghai"}',
            '{"tool":"schedule","action":"list"}',
            '{"tool":"schedule","action":"pause","job_id":"job_...","expected_version":2}',
        ],
    )


class ScheduleTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_schedule_tool_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = str(params.get("action") or "").strip().lower()
        if action not in {
            "create",
            "list",
            "get",
            "update",
            "pause",
            "resume",
            "delete",
            "run_now",
            "history",
            "status",
        }:
            return _error("action 不受支持", "TOOL_INVALID_ARGUMENTS")
        repository = getattr(self.agent, "scheduler_repository", None)
        if repository is None:
            return _error("当前 owner 的 scheduler 未装配", "TOOL_UNAVAILABLE")
        try:
            return self._execute(repository, action, params)
        except ScheduleValidationError as exc:
            return _error(str(exc), "SCHEDULER_INVALID_SCHEDULE")
        except SchedulerNotFoundError as exc:
            return _error(str(exc), "SCHEDULER_NOT_FOUND")
        except SchedulerConflictError as exc:
            return _error(str(exc), "SCHEDULER_CONFLICT")
        except OwnerQuotaExceeded as exc:
            return _error(str(exc), "OWNER_DISK_QUOTA_EXCEEDED")
        except OwnerQuotaUnavailable:
            return _error("owner 配额策略当前不可用，已拒绝写入", "OWNER_QUOTA_UNAVAILABLE")
        except (OSError, SchedulerRepositoryError, ValueError) as exc:
            return _error(str(exc), "SCHEDULER_UNAVAILABLE")

    def _execute(self, repository, action: str, params: dict[str, object]) -> ToolExecutionResult:
        if action == "status":
            return _success(action, {"scheduler": repository.runtime_snapshot()})
        if action == "list":
            jobs, errors = repository.list_jobs()
            return _success(
                action,
                {
                    "jobs": [_job_projection(job, include_prompt=False) for job in jobs],
                    "load_error_codes": errors,
                },
            )
        job_id = str(params.get("job_id") or "").strip()
        if action not in {"create"} and not job_id:
            return _error("job_id 必填", "TOOL_INVALID_ARGUMENTS")
        if action == "get":
            return _success(
                action, {"job": _job_projection(repository.get_job(job_id), include_prompt=True)}
            )
        if action == "history":
            rows, errors = repository.history(job_id=job_id, limit=_limit(params))
            return _success(
                action,
                {
                    "job_id": job_id,
                    "runs": [_run_projection(row) for row in rows],
                    "load_error_codes": errors,
                },
            )
        if action == "run_now":
            run = repository.reserve_manual_run(job_id)
            return _success(action, {"run": _run_projection(run)})
        if action == "create":
            return self._create(repository, params)
        expected_version = _expected_version(params, required=action in _MUTATING_EXISTING_ACTIONS)
        if isinstance(expected_version, ToolExecutionResult):
            return expected_version
        if action == "pause":
            job = repository.pause_job(job_id, expected_version=expected_version)
        elif action == "resume":
            job = repository.resume_job(job_id, expected_version=expected_version)
        elif action == "delete":
            job = repository.delete_job(job_id, expected_version=expected_version)
        else:
            patch = self._update_patch(params)
            if isinstance(patch, ToolExecutionResult):
                return patch
            if not patch:
                return _error("update 至少需要一个可修改字段", "TOOL_INVALID_ARGUMENTS")
            job = repository.update_job(
                job_id,
                patch=patch,
                expected_version=expected_version,
            )
        return _success(action, {"job": _job_projection(job, include_prompt=True)})

    def _create(self, repository, params: dict[str, object]) -> ToolExecutionResult:
        name = str(params.get("name") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        if not name or not prompt or not str(params.get("schedule_kind") or "").strip():
            return _error("create 必须提供 name、prompt、schedule_kind", "TOOL_INVALID_ARGUMENTS")
        thread_id, source_task_id = _conversation_scope(self.agent)
        if not thread_id:
            return _error(
                "当前请求没有可信 conversation_thread_id，已拒绝猜测会话",
                "SCHEDULER_THREAD_REQUIRED",
            )
        skill_refs = _skill_refs(self.agent, params.get("skill_ids"))
        if isinstance(skill_refs, ToolExecutionResult):
            return skill_refs
        current = time.time()
        schedule = _schedule_from_params(self.agent, params, now=current)
        job, deduped = repository.create_job(
            SchedulerJobCreateRequest(
                name=name,
                prompt=prompt,
                thread_id=thread_id,
                source_task_id=source_task_id,
                schedule=schedule,
                misfire_grace_seconds=_optional_int(params.get("misfire_grace_seconds")),
                skill_refs=skill_refs,
                source_request_id=_source_request_id(self.agent, params),
                now=current,
            )
        )
        return _success(
            "create",
            {"job": _job_projection(job, include_prompt=True), "deduplicated": deduped},
        )

    def _update_patch(self, params: dict[str, object]) -> dict[str, object] | ToolExecutionResult:
        patch: dict[str, object] = {}
        for key in ("name", "prompt", "misfire_grace_seconds"):
            if key in params:
                patch[key] = params[key]
        if "skill_ids" in params:
            refs = _skill_refs(self.agent, params.get("skill_ids"))
            if isinstance(refs, ToolExecutionResult):
                return refs
            patch["skill_refs"] = refs
        schedule_keys = {"schedule_kind", "at", "every_seconds", "anchor_at", "cron", "timezone"}
        if any(key in params for key in schedule_keys):
            if not str(params.get("schedule_kind") or "").strip():
                return _error(
                    "修改执行时间时必须同时提供完整 schedule_kind 及对应参数",
                    "TOOL_INVALID_ARGUMENTS",
                )
            patch["schedule"] = _schedule_from_params(self.agent, params, now=time.time())
        return patch


def _schedule_from_params(
    agent: object, params: dict[str, object], *, now: float
) -> dict[str, object]:
    config = getattr(agent, "config", None)
    return build_schedule(
        kind=params.get("schedule_kind"),
        at=params.get("at"),
        every_seconds=params.get("every_seconds"),
        anchor_at=params.get("anchor_at"),
        cron=params.get("cron"),
        timezone_name=params.get("timezone"),
        default_timezone=str(getattr(config, "timezone", "") or ""),
        now=now,
    )


def _conversation_scope(agent: object) -> tuple[str, str]:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    )
    task_id = (
        str(attrs.get("conversation_task_id") or "").strip() if isinstance(attrs, dict) else ""
    ) or str(getattr(current, "task_id", "") or "").strip()
    if thread_id:
        return thread_id, task_id
    store = getattr(agent, "conversation_store", None)
    if not task_id or store is None:
        return "", task_id
    thread, error = store.thread_for_task_report(task_id)
    return (
        str(getattr(thread, "thread_id", "") or "") if error is None and thread else ""
    ), task_id


def _skill_refs(agent: object, raw: object) -> list[dict[str, str]] | ToolExecutionResult:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        return _error("skill_ids 必须是数组", "TOOL_INVALID_ARGUMENTS")
    try:
        snapshot = agent.current_skill_snapshot()
    except Exception as exc:
        return _error(f"无法读取 Skill 快照: {exc}", "SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE")
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    missing: list[str] = []
    for value in raw:
        reference = str(value or "").strip()
        entry = snapshot.resolve(reference) if reference else None
        if entry is None:
            missing.append(reference)
        elif entry.stable_id not in seen:
            refs.append(
                {
                    "stable_id": entry.stable_id,
                    "content_sha256": entry.content_sha256,
                }
            )
            seen.add(entry.stable_id)
    if missing:
        return _error(
            "当前 owner 不可用或已禁用的 Skill: " + ", ".join(missing),
            "SCHEDULER_SKILL_NOT_AVAILABLE",
        )
    return refs


def _expected_version(
    params: dict[str, object],
    *,
    required: bool,
) -> int | None | ToolExecutionResult:
    value = params.get("expected_version")
    if value in (None, ""):
        return (
            _error("expected_version 必填，请先 list/get", "TOOL_INVALID_ARGUMENTS")
            if required
            else None
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _error("expected_version 必须是正整数", "TOOL_INVALID_ARGUMENTS")
    return (
        parsed if parsed > 0 else _error("expected_version 必须是正整数", "TOOL_INVALID_ARGUMENTS")
    )


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ScheduleValidationError("misfire_grace_seconds must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError("misfire_grace_seconds must be an integer") from exc


def _limit(params: dict[str, object]) -> int:
    try:
        return max(1, min(int(params.get("limit") or 50), 200))
    except (TypeError, ValueError):
        return 50


def _source_request_id(agent: object, params: dict[str, object]) -> str:
    current = getattr(agent, "_current_run_params", None)
    request_id = str(getattr(current, "request_id", "") or "").strip()
    call_id = str(params.get("__tool_call_id") or "").strip()
    return ":".join(item for item in (request_id, call_id) if item)


def _job_projection(job: dict[str, object], *, include_prompt: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job["job_id"],
        "name": job["name"],
        "status": job["status"],
        "schedule": job["schedule"],
        "next_run_at": iso_from_timestamp(job.get("next_run_at")),
        "misfire_grace_seconds": job["misfire_grace_seconds"],
        "skill_refs": job.get("skill_refs") or [],
        "version": job["version"],
        "last_run_at": iso_from_timestamp(job.get("last_run_at")),
        "last_run_status": job.get("last_run_status") or "",
        "last_error_code": job.get("last_error_code") or "",
    }
    prompt = str(job.get("prompt") or "")
    payload["prompt" if include_prompt else "prompt_preview"] = (
        prompt if include_prompt else prompt[:240]
    )
    return payload


def _run_projection(run: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": run["run_id"],
        "job_id": run["job_id"],
        "trigger": run.get("trigger") or "",
        "status": run["status"],
        "scheduled_for": iso_from_timestamp(run.get("scheduled_for")),
        "started_at": iso_from_timestamp(run.get("started_at")),
        "ended_at": iso_from_timestamp(run.get("ended_at")),
        "delivery_status": run.get("delivery_status") or "",
        "delivery_reason": run.get("delivery_reason") or "",
        "error_code": run.get("error_code") or "",
        "error_message": run.get("error_message") or "",
        "response_preview": str(run.get("response") or "")[:500],
    }


def _success(action: str, payload: dict[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(
        "schedule",
        True,
        json.dumps({"ok": True, "action": action, **payload}, ensure_ascii=False),
    )


def _error(message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        "schedule",
        False,
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        error_code=code,
    )


__all__ = ["ScheduleTool", "build_schedule_tool_spec"]
