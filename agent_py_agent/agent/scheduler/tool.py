# LLM: 持久计划经当前 owner 的原 repository 与可信 thread 归属提交；新 IM 隔离线程，TUI 沿原线程，状态只读原回执。
# 模块用途: 向模型提供统一的计划登记、管理和触发工具，不用草稿或回复文案充当计划事实。
from __future__ import annotations

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
from .execution_policy import (
    EXECUTION_SCHEMA,
    PRECHECK_SCHEMA,
    normalize_execution,
    normalize_precheck,
    validate_execution_for_agent,
)
from .policy import normalize_policy
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


# LLM: 参数声明同时解释 create/立即运行的不同副作用及来源隔离；不根据模型文案认定计划已创建，必须读原 repository 回执。
# 函数用途: 给模型完整的持久计划操作合同，避免只写计划草稿或把登记误认为马上执行。
def build_schedule_tool_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="schedule",
        description=(
            "管理当前用户自己的持久定时任务或提醒。IM 创建的任务在独立后台会话执行并通知原布置者，TUI 创建沿原会话执行，"
            "网关重启后仍会恢复；支持一次、固定间隔和五段 cron。"
            "create 只登记未来计划，不会立即运行正文；run_now 才是立即触发。用户要求建好定时计划但不现在执行时，"
            "应创建计划，不需要再索要一次确认，也不能只用 task_progress 保存计划草稿冒充定时计划。"
            "可显式设置独立上下文、固定模型、重叠与失败策略；需要变化检查时在 precheck 对象填写 tool_name 和 arguments。"
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
                "prompt": {"type": "string", "description": "到点后执行的完整用户要求，执行与通知身份由宿主固定。"},
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
                "policy": {"type": "object", "description": "可选运行策略；不提供保持原行为。超宽限可跳过或合并补一次，失败只影响以后周期，不重放已执行动作。",
                    "properties": {
                        "misfire": {"type": "string", "enum": ["skip", "coalesce"]},
                        "queue_timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 604800, "description": "预留后最长排队时间；0 不限制。"},
                        "failure_pause_after": {"type": "integer", "minimum": 0, "maximum": 604800, "description": "连续失败多少次后暂停；0 不自动暂停。"},
                        "failure_backoff_seconds": {"type": "integer", "minimum": 0, "maximum": 604800, "description": "连续失败指数退避基数，最长七天；0 不退避。"}},
                    "additionalProperties": False},
                "skill_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32, "description": "固定到计划的当前可用 Skill 稳定 ID。"},
                "execution": EXECUTION_SCHEMA,
                "precheck": PRECHECK_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "list/history 的返回条数。"},
                "metadata_only": {"type": "boolean", "description": "list/get/history/status 只输出状态、时间和编号，排除名字、提示词、回复预览及原始错误正文。"},
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
                '{"tool":"schedule","action":"create","name":"资料更新","prompt":"汇总资料变化","schedule_kind":"every","every_seconds":600,"precheck":{"tool_name":"read_file","arguments":{"path":"source.txt"}}}',
                '{"tool":"schedule","action":"list"}',
                '{"tool":"schedule","action":"pause","job_id":"job_...","expected_version":2}',
            ),
            # 又大又少用：开关 tool_default_deferral_enabled 打开时前台回合不发 Schema，目录索引留下面这句用途，用到时 tool_search 加载。
            default_deferred=True,
            deferred_summary="创建、查看、暂停或删除定时任务与提醒（稍后、每天、每周、cron）",
        ),
    )


# LLM: 调度读写都经原 repository；元数据查询不得改变鉴权、生命周期或持久字段。
# 类用途: 管理当前 owner 的计划并查询运行和交付状态。
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

    # LLM: 元数据模式只读；已知拒绝零写入，异常消息可能带路径或任务正文，必须在失败投影前去掉。
    # 函数用途: 校验动作后调用调度账本，将存储失败转换成可诊断原因码。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        metadata_only = params.get("metadata_only", False)
        if not isinstance(metadata_only, bool):
            return _error("metadata_only 必须是布尔值", "TOOL_INVALID_ARGUMENTS")
        if metadata_only and action not in {"list", "get", "history", "status"}:
            return _error("纯元数据模式仅支持查询动作", "TOOL_INVALID_ARGUMENTS", metadata_only=True)
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
            return _error(str(exc), "SCHEDULER_INVALID_SCHEDULE", metadata_only=metadata_only)
        except SchedulerNotFoundError as exc:
            return _error(str(exc), "SCHEDULER_NOT_FOUND", metadata_only=metadata_only)
        except SchedulerConflictError as exc:
            return _error(str(exc), "SCHEDULER_CONFLICT", metadata_only=metadata_only)
        except OwnerQuotaExceeded as exc:
            return _error(str(exc), "OWNER_DISK_QUOTA_EXCEEDED", metadata_only=metadata_only)
        except OwnerQuotaUnavailable:
            return _error("owner 配额策略当前不可用，已拒绝写入", "OWNER_QUOTA_UNAVAILABLE")
        except (OSError, SchedulerRepositoryError, ValueError) as exc:
            # 存储异常可能发生在落盘期间，工具无法证明副作用是否已发生。
            return _error(str(exc), "SCHEDULER_UNAVAILABLE", effect_outcome="not_started" if metadata_only else "unknown", metadata_only=metadata_only)
        except Exception:  # noqa: BLE001 - 元数据查询的未知失败也不能回显自由文本
            if not metadata_only:
                raise
            return _error("", "SCHEDULER_UNAVAILABLE", metadata_only=True)

    # LLM: list/get/history 的元数据模式贯穿嵌套 run 投影；读取原账本，写入仍由 repository CAS 管理。
    # 函数用途: 执行定时动作，列表保留截断事实，诊断查询排除正文和名字。
    def _execute(self, repository, action: str, params: dict[str, object]) -> ToolHandlerOutcome:
        metadata_only = params.get("metadata_only", False) is True
        if action == "status":
            return _success(action, {"scheduler": repository.runtime_snapshot()})
        if action == "list":
            jobs, errors = repository.list_jobs()
            active_runs, run_errors = repository.active_runs_by_job()
            return _success(
                action,
                {
                    "jobs": [
                        _job_projection(
                            job,
                            include_prompt=False,
                            metadata_only=metadata_only,
                            active_run=active_runs.get(str(job["job_id"])),
                        )
                        for job in jobs[:_limit(params)]
                    ],
                    "total": len(jobs),
                    "has_more": len(jobs) > _limit(params),
                    "load_error_codes": [*errors, *run_errors],
                },
            )
        job_id = str(params.get("job_id") or "").strip()
        if action not in {"create"} and not job_id:
            return _error("job_id 必填", "TOOL_INVALID_ARGUMENTS")
        if action == "get":
            active_runs, run_errors = repository.active_runs_by_job()
            job = repository.get_job(job_id)
            return _success(
                action,
                {
                    "job": _job_projection(
                        job,
                        include_prompt=True,
                        metadata_only=metadata_only,
                        active_run=active_runs.get(str(job["job_id"])),
                    ),
                    "load_error_codes": run_errors,
                },
            )
        if action == "history":
            rows, errors = repository.history(job_id=job_id, limit=_limit(params))
            return _success(
                action,
                {
                    "job_id": job_id,
                    "runs": [_run_projection(row, metadata_only=metadata_only) for row in rows],
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

    # LLM: 先用原纯合同校验完整策略，再创建 IM 线程和 job；来源读可信绑定，模型不能改回送地址或凭据。
    # 函数用途: 保存可恢复计划，IM 保持后台、TUI 沿原行为；非法执行/预检/失败策略不留下空后台线程。
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
        current = time.time()
        schedule = _schedule_from_params(self.agent, params, now=current)
        execution = validate_execution_for_agent(self.agent, params.get("execution"))
        policy = normalize_policy(params.get("policy"))
        precheck = normalize_precheck(params.get("precheck"))
        execution = validate_execution_for_agent(self.agent, params.get("execution"))
        source_thread_id = thread_id
        source_request_id = _source_request_id(self.agent, params)
        from .execution_policy import schedule_execution_thread

        thread_id = schedule_execution_thread(self.agent, thread_id, source_request_id)
        job, deduped = repository.create_job(
            SchedulerJobCreateRequest(
                name=name,
                prompt=prompt,
                thread_id=thread_id,
                source_task_id=source_task_id,
                schedule=schedule,
                misfire_grace_seconds=_optional_int(params.get("misfire_grace_seconds")),
                skill_refs=skill_refs,
                source_request_id=source_request_id,
                source_thread_id=source_thread_id,
                now=current,
                policy=policy,
                execution=execution,
                precheck=precheck,
            )
        )
        return _success(
            "create",
            {"job": _job_projection(job, include_prompt=True), "deduplicated": deduped},
        )

    # LLM: 只更新显式给定字段，固定模型引用在写事务前校验，已经预留的 run 仍沿原冻结策略。
    # 函数用途: 将模型工具参数转换成原仓库的计划修改，保留 null 关闭预检的含义。
    def _update_patch(self, params: dict[str, object]) -> dict[str, object] | ToolHandlerOutcome:
        patch: dict[str, object] = {}
        for key in ("name", "prompt", "misfire_grace_seconds", "policy", "execution", "precheck"):
            if key in params:
                patch[key] = params[key]
        if "execution" in patch:
            patch["execution"] = validate_execution_for_agent(self.agent, patch["execution"])
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
    thread, error = store.tasks.thread_for_report(task_id)
    return (
        str(getattr(thread, "thread_id", "") or "") if error is None and thread else ""
    ), task_id


# LLM: 定时任务从当前权限快照固定引用；能力包保存完整激活身份，不能在后台启动时只按包名重新选择。
# 函数用途: 把用户选择的公开 Skill 或能力包规范化为原调度记录里的版本引用。
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
        entry = snapshot.resolve_reference(reference) if reference else None
        if entry is None:
            missing.append(reference)
        elif entry.stable_id not in seen:
            refs.append(entry.to_ref())
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


# LLM: 白名单投影原 job 与 active run；metadata_only 不读取 name/prompt/skill_refs，不从正文推导状态。
# 函数用途: 展示计划及在途执行，支持没有名称或正文的状态诊断。
def _job_projection(
    job: dict[str, object],
    *,
    include_prompt: bool,
    active_run: dict[str, object] | None = None,
    metadata_only: bool = False,
) -> dict[str, object]:
    from .policy import normalize_policy

    payload: dict[str, object] = {
        "job_id": job["job_id"],
        "execution_mode": "background" if normalize_execution(job.get("execution"))["mode"] == "isolated" or job.get("source_thread_id", job["thread_id"]) != job["thread_id"] else "current",
        "status": job["status"],
        "schedule": ({key: value for key, value in job["schedule"].items()
                      if key in {"kind", "at", "timezone", "every_seconds", "anchor_at", "expression"}}
                     if metadata_only else job["schedule"]),
        "next_run_at": iso_from_timestamp(job.get("next_run_at")),
        "misfire_grace_seconds": job["misfire_grace_seconds"],
        "version": job["version"],
        "last_run_at": iso_from_timestamp(job.get("last_run_at")),
        "last_run_status": job.get("last_run_status") or "",
        "last_error_code": job.get("last_error_code") or "",
        "policy": normalize_policy(job.get("policy")),
        "execution": normalize_execution(job.get("execution")),
        "precheck_enabled": job.get("precheck") is not None,
        "consecutive_failures": job.get("consecutive_failures", 0),
        "backoff_until": iso_from_timestamp(job.get("backoff_until")),
        "current_run": _run_projection(active_run, metadata_only=metadata_only) if active_run is not None else None,
    }
    if metadata_only:
        return payload
    payload["name"] = job["name"]
    payload["precheck"] = job.get("precheck")
    payload["skill_refs"] = job.get("skill_refs") or []
    prompt = str(job.get("prompt") or "")
    payload["prompt" if include_prompt else "prompt_preview"] = (
        prompt if include_prompt else prompt[:240]
    )
    return payload


# LLM: 白名单读取 run 状态、时间及预检原工具原因码；不读取回复/错误正文，执行成功不推导交付成功。
# 函数用途: 展示执行、交付和预检错误来源；纯元数据模式排除自由文本。
def _run_projection(run: dict[str, object], *, metadata_only: bool = False) -> dict[str, object]:
    payload = {
        "execution": normalize_execution(run.get("execution")),
        "origin_thread_id": run.get("origin_thread_id", run.get("thread_id")),
        "thread_id": run.get("thread_id"),
        "precheck_status": (run.get("precheck_observation") or {}).get("status", "pending" if run.get("precheck") else "disabled"),
        "precheck_observation": {key: value for key, value in (run.get("precheck_observation") or {}).items()
                                 if key in {"status", "call_id", "observed_at", "error_code", "error_type", "tool_error_code"}},
        "run_id": run["run_id"],
        "job_id": run["job_id"],
        "trigger": run.get("trigger") or "",
        "status": run["status"],
        "scheduled_for": iso_from_timestamp(run.get("scheduled_for")),
        "reserved_at": iso_from_timestamp(run.get("created_at")),
        "queue_deadline_at": iso_from_timestamp(run.get("queue_deadline_at")),
        "misfire_policy": run.get("misfire_policy", "skip"),
        "lateness_seconds": run.get("lateness_seconds"),
        "claimed_at": iso_from_timestamp(run.get("claimed_at")),
        "started_at": iso_from_timestamp(run.get("started_at")),
        "waiting_since": iso_from_timestamp(run.get("waiting_since")),
        "updated_at": iso_from_timestamp(run.get("updated_at")),
        "ended_at": iso_from_timestamp(run.get("ended_at")),
        "delivery_status": run.get("delivery_status") or "",
        "error_code": run.get("error_code") or "",
    }
    if not metadata_only:
        payload.update({
            "delivery_reason": run.get("delivery_reason") or "",
            "error_message": run.get("error_message") or "",
            "response_preview": str(run.get("response") or "")[:500],
        })
    return payload


def _success(action: str, payload: dict[str, object]) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "schedule",
        True,
        json.dumps({"ok": True, "action": action, **payload}, ensure_ascii=False),
    )


# LLM: 元数据错误只输出固定错误码；已知写前拒绝保持 not_started，写入异常才允许 unknown。
# 函数用途: 生成结构化失败；元数据查询不回显含正文或私有路径的异常说明。
def _error(
    message: str,
    code: str,
    *,
    effect_outcome: str = "not_started",
    metadata_only: bool = False,
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "schedule",
        False,
        json.dumps({"ok": False, **({"error_code": code, "metadata_only": True} if metadata_only else {"error": message})}, ensure_ascii=False),
        error_code=code,
        effect_outcome=effect_outcome,
    )


__all__ = ["ScheduleTool", "build_schedule_tool_model_spec"]
