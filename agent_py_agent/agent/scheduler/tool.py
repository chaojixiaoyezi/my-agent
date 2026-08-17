from __future__ import annotations

"""Single action-oriented owner scheduler tool."""

import json
import time
from typing import TYPE_CHECKING

from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
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


def build_schedule_tool_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="schedule",
        description=(
            "管理当前用户自己的持久定时任务或提醒。任务到点后会回到创建它的同一会话继续执行，"
            "网关重启后仍会恢复；支持一次、固定间隔和五段 cron。"
        ),
        input_schema={
            "type": "object",
            "properties": {
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
                    "description": "create/list/get/update/pause/resume/delete/run_now/history/status。",
                },
                "job_id": {"type": "string", "description": "除 create/list/status 外，使用 list/get 返回的精确 job_id。"},
                "expected_version": {"type": "integer", "minimum": 1, "description": "更新现有任务时使用 get/list 返回的 version。"},
                "name": {"type": "string", "description": "create 必填的简短名称；update 可选。"},
                "prompt": {"type": "string", "description": "到点后在同一会话继续执行的完整用户要求。"},
                "schedule_kind": {"type": "string", "enum": ["at", "every", "cron"], "description": "计划类型。"},
                "at": {
                "type": "string",
                "description": (
                    "Future absolute ISO-8601 time for schedule_kind=at; "
                    "never pass a duration or relative seconds."
                ),
                },
                "after_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 31622400,
                "description": (
                    "Relative seconds from now for schedule_kind=at; "
                    "the runtime persists the resolved absolute instant."
                ),
                },
                "every_seconds": {"type": "integer", "minimum": 60, "description": "every 的间隔秒数。"},
                "anchor_at": {"description": "every 的可选锚点时间。"},
                "cron": {"type": "string", "description": "五段 cron 表达式（分 时 日 月 周）。"},
                "timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai。"},
                "misfire_grace_seconds": {"type": "integer", "minimum": 0, "maximum": 604800, "description": "错过后允许补执行的秒数。"},
                "skill_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32, "description": "固定到计划的当前可用 Skill 稳定 ID。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "list/history 的返回条数。"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=(
                "用户要求稍后提醒或每天每周定时执行",
                "查看、修改、暂停、恢复或删除自己的定时任务",
                "立即试跑已有定时任务或查看历史结果",
            ),
            avoid_when=(
                "当前长任务内部等待使用 wait",
                "用户没有要求跨当前执行轮持续存在时不要创建计划",
                "不能指定其他用户、群或任意会话",
            ),
            keywords=("提醒", "定时", "schedule", "cron", "每天", "每周", "稍后", "周期任务"),
            examples=(
                '{"tool":"schedule","action":"create","name":"稍后提醒","prompt":"提醒用户检查备份","schedule_kind":"at","after_seconds":90}',
                '{"tool":"schedule","action":"create","name":"周报提醒","prompt":"整理本周项目进展并发给我","schedule_kind":"cron","cron":"0 18 * * 5","timezone":"Asia/Shanghai"}',
                '{"tool":"schedule","action":"list"}',
                '{"tool":"schedule","action":"pause","job_id":"job_...","expected_version":2}',
            ),
        ),
    )


class ScheduleTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.model_spec = build_schedule_tool_model_spec()
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                "mutating",
                by_parameter=((
                    "action",
                    (
                        ("list", "read_only"),
                        ("get", "read_only"),
                        ("history", "read_only"),
                        ("status", "read_only"),
                        ("create", "mutating"),
                        ("update", "mutating"),
                        ("pause", "mutating"),
                        ("resume", "mutating"),
                        ("delete", "dangerous"),
                        ("run_now", "dangerous"),
                    ),
                ),),
            ),
            idempotency_policy=IdempotencyPolicy("operation"),
            resource_scopes=ResourceScopePolicy(parameter_names=("job_id",),
            parameter_kinds={"job_id": "logical"}),
            input_policy=ToolInputPolicy(internal_parameters=("__tool_call_id", "__run_scope")),
        )

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
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

    def _execute(self, repository, action: str, params: dict[str, object]) -> ToolHandlerOutcome:
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
        if isinstance(expected_version, ToolHandlerOutcome):
            return expected_version
        if action == "pause":
            job = repository.pause_job(job_id, expected_version=expected_version)
        elif action == "resume":
            job = repository.resume_job(job_id, expected_version=expected_version)
        elif action == "delete":
            job = repository.delete_job(job_id, expected_version=expected_version)
        else:
            patch = self._update_patch(params)
            if isinstance(patch, ToolHandlerOutcome):
                return patch
            if not patch:
                return _error("update 至少需要一个可修改字段", "TOOL_INVALID_ARGUMENTS")
            job = repository.update_job(
                job_id,
                patch=patch,
                expected_version=expected_version,
            )
        return _success(action, {"job": _job_projection(job, include_prompt=True)})

    def _create(self, repository, params: dict[str, object]) -> ToolHandlerOutcome:
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
        if isinstance(skill_refs, ToolHandlerOutcome):
            return skill_refs
        # A2：cron 策略注册（fail-closed）——owner 没有 async cron 策略时，到点的
        # cron producer 会拒绝写 intent（不绕过授权拉起）。注册失败 → 创建失败
        # （绝不静默建一个永远触发的定时任务）。放在参数验证之后、建 job 之前。
        policy_error = _ensure_cron_wake_policy(self.agent, task_id=source_task_id)
        if policy_error is not None:
            return _error(policy_error, "WAKE_POLICY_REQUIRED")
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

    def _update_patch(self, params: dict[str, object]) -> dict[str, object] | ToolHandlerOutcome:
        patch: dict[str, object] = {}
        for key in ("name", "prompt", "misfire_grace_seconds"):
            if key in params:
                patch[key] = params[key]
        if "skill_ids" in params:
            refs = _skill_refs(self.agent, params.get("skill_ids"))
            if isinstance(refs, ToolHandlerOutcome):
                return refs
            patch["skill_refs"] = refs
        schedule_keys = {
            "schedule_kind",
            "at",
            "after_seconds",
            "every_seconds",
            "anchor_at",
            "cron",
            "timezone",
        }
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
    at = params.get("at")
    after_seconds = params.get("after_seconds")
    if after_seconds not in (None, ""):
        if str(params.get("schedule_kind") or "").strip().lower() != "at":
            raise ScheduleValidationError("after_seconds requires schedule_kind=at")
        if at not in (None, ""):
            raise ScheduleValidationError("at and after_seconds cannot be used together")
        if isinstance(after_seconds, bool) or not isinstance(after_seconds, int):
            raise ScheduleValidationError("after_seconds must be an integer")
        relative_seconds = after_seconds
        if not 1 <= relative_seconds <= 31_622_400:
            raise ScheduleValidationError("after_seconds must be between 1 and 31622400")
        at = now + relative_seconds
    elif str(params.get("schedule_kind") or "").strip().lower() == "at" and not isinstance(
        at, str
    ):
        raise ScheduleValidationError("at must be a future absolute ISO-8601 string")
    return build_schedule(
        kind=params.get("schedule_kind"),
        at=at,
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


def _skill_refs(agent: object, raw: object) -> list[dict[str, str]] | ToolHandlerOutcome:
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
) -> int | None | ToolHandlerOutcome:
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


def _provider_scope_ref(agent: object) -> str:
    """模型通道 scope（provider circuit 路由 / 授权匹配用）。"""
    scope = str(getattr(agent, "model_provider", "") or "").strip()
    if not scope:
        scope = str(
            getattr(getattr(agent, "model_config", None), "provider", "") or ""
        ).strip()
    return scope or "opencode"


def _ensure_cron_wake_policy(agent: object, *, task_id: str) -> str | None:
    """注册 owner 级 async cron 策略（canonical wake_policies，幂等）。

    fail-closed：owner 无 runtime.db / 策略注册异常 → 返回错误串（调用方
    拒绝创建定时任务）；成功返回 None。scope_ref = owner:task（task 级，
    seq2463 作用域匹配：更窄的 policy 不能授权更宽的 intent）。
    """
    try:
        from ..runtime_db.repository import RuntimeRepository
        from ..runtime_db.schema import runtime_db_path
        from ..wake_producer import ensure_wake_policy

        home_root = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if not home_root:
            return "当前 owner 没有可用的运行时目录（wake policy 无法注册）"
        db_path = runtime_db_path(home_root)
        if not db_path.is_file():
            return "当前 owner 运行时数据库缺失（wake policy 无法注册）"
        owner_id = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "").strip()
        if not owner_id:
            owner_id = "local/main"
        task_id = str(task_id or "").strip()
        if not task_id:
            return "定时任务缺少绑定任务（wake policy 作用域无法确定）"
        repo = RuntimeRepository(db_path)
        ensure_wake_policy(
            repo,
            owner_id=owner_id,
            continuation_policy="async",
            policy_generation=1,
            allowed_sources="cron",
            provider_scope_ref=_provider_scope_ref(agent),
            scope_ref=f"{owner_id}:{task_id}",
        )
        return None
    except Exception as exc:  # noqa: BLE001 注册失败 fail-closed
        return f"定时任务策略注册失败（{type(exc).__name__}）"


def _success(action: str, payload: dict[str, object]) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "schedule",
        True,
        json.dumps({"ok": True, "action": action, **payload}, ensure_ascii=False),
    )


def _error(message: str, code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "schedule",
        False,
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        error_code=code,
    )


__all__ = ["ScheduleTool", "build_schedule_tool_model_spec"]
