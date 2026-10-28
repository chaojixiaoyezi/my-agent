# LLM: 持久计划经原owner/typed actor提交；成功回执的姓名沿原展示投影且在policy锁外补充，名字/回复不授权。
# 模块用途: 统一登记管理计划及多通道通知；投递引用只认当前owner认证目录，展示资料不改变权限。
from __future__ import annotations

import json
import time
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING

from ..conversation.host_actor import HostActorError, resolve_host_actor
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
from ..user_space.group_authorization import GroupAuthorizationError
from ..user_space.group_authorization_context import group_capability_guard
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .delivery_policy import (
    DELIVERY_SCHEMA,
    ScheduleDeliveryTargetUnauthorized,
    available_delivery_targets,
    creation_delivery,
    delivery_from_params,
    normalize_delivery,
    validate_delivery_targets,
)
from .event_source_tool import (
    EVENT_SOURCE_INPUT_SCHEMA,
    EventSourceInputError,
    prepare_event_sources,
    schedule_from_event_input,
)
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
from .rules import RULES_SCHEMA
from .schedule import ScheduleValidationError, build_schedule, iso_from_timestamp

if TYPE_CHECKING:
    from ..core import SimpleAgent

_ACTIONS = ("create", "list", "get", "update", "update_delivery", "delivery_targets", "pause", "resume", "delete", "takeover", "execution", "cancel", "recover", "run_now", "history", "status")
_MUTATING_EXISTING_ACTIONS = frozenset({"update", "update_delivery", "pause", "resume", "delete", "takeover", "cancel", "recover"})


# LLM: 公开单计划与每条规则只选择消息引用，原宿主解析canonical来源；发现元数据不路由/授权，完成只认repository回执。
# 函数用途: 声明计划、认证投递目录与更新参数；新目标必须引用delivery_targets，不接受隐藏身份或任意地址。
def build_schedule_tool_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="schedule",
        description=(
            "管理当前用户自己的持久定时任务或提醒。IM 创建的任务在独立后台会话执行，默认只记录内部结果，TUI 创建沿原会话执行，"
            "网关重启后仍会恢复；支持一次、固定间隔、五段 cron 以及已授权群内准确机器人的消息订阅。"
            "群内新建、扩展、恢复或手动运行须先由群主/真实邀请人通过 /group 显式授权；停止取消仍走原创建者权限。"
            "订阅机器人时用 schedule_kind=event_source 与 event_source.source_message_refs 原样指定本请求的引用；"
            "多规则在每条 rules[].schedule 填 kind=event_source 和同样的 source_message_refs，勿填写应用或租户身份。"
            "宿主核验来源，机器人正文只是外部资料。自然语言条件写 prompt；暂停或删除即可取消订阅。"
            "create 只登记未来计划，不会立即运行正文；run_now 才是立即触发。用户要求建好定时计划但不现在执行时，"
            "应创建计划，不需要再索要一次确认，也不能只用 task_progress 保存计划草稿冒充定时计划。"
            "可显式设置独立上下文、固定模型、重叠与失败策略；需要变化检查时在 precheck 对象填写 tool_name 和 arguments。"
            "用户要求提醒、告警或定时报表时，明确设置 delivery.mode=origin；纯巡检设置 local，正常内部总结不逐轮发消息。"
            "只有出现事项才通知时，使用 delivery={mode:origin,reply_policy:explicit}，运行内按结构化通知意图决定外发。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                    "description": "create/list/get/update/pause/resume/delete/takeover/execution/cancel/recover/run_now/history/status。takeover 只接管本机计划未来通知到当前绑定管理员私聊，不迁移执行。",
                },
                "job_id": {"type": "string", "description": "除 create/list/status 外，使用 list/get 返回的精确 job_id。"},
                "expected_version": {"type": "integer", "minimum": 1, "description": "更新现有任务时使用 get/list 返回的 version。"},
                "name": {"type": "string", "description": "create 必填的简短名称；update 可选。"},
                "prompt": {"type": "string", "description": "到点后执行的完整用户要求，执行与通知身份由宿主固定。"},
                "schedule_kind": {"type": "string", "enum": ["at", "every", "cron", "event_source"], "description": "计划类型。"},
                "event_source": EVENT_SOURCE_INPUT_SCHEMA,
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
                "rules": RULES_SCHEMA,
                "rule_id": {"type": "string", "description": "多规则监控 run_now 必须选择准确规则编号。"},
                "execution": EXECUTION_SCHEMA,
                "precheck": PRECHECK_SCHEMA,
                "delivery": DELIVERY_SCHEMA,
                "delivery_policy": DELIVERY_SCHEMA,
                "notify_mode": {"enum": ["origin", "local", "none"], "description": "旧通知模式输入，与delivery_policy冲突时拒绝。"},
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
                "在已授权群内订阅引用的机器人后续消息，交原监控分析并按规则通知",
            ),
            avoid_when=(
                "当前长任务内部等待使用 wait",
                "用户没有要求跨当前执行轮持续存在时不要创建计划",
                "不能指定其他用户、群或任意会话",
            ),
            keywords=("提醒", "定时", "schedule", "cron", "每天", "每周", "稍后", "周期任务", "监控", "消息订阅", "机器人消息"),
            examples=(
                '{"tool":"schedule","action":"create","name":"稍后提醒","prompt":"提醒用户检查备份","schedule_kind":"at","after_seconds":90,"delivery":{"mode":"origin"}}',
                '{"tool":"schedule","action":"create","name":"周报提醒","prompt":"整理本周项目进展并发给我","schedule_kind":"cron","cron":"0 18 * * 5","timezone":"Asia/Shanghai","delivery":{"mode":"origin"}}',
                '{"tool":"schedule","action":"create","name":"资料更新","prompt":"汇总资料变化","schedule_kind":"every","every_seconds":600,"precheck":{"tool_name":"read_file","arguments":{"path":"source.txt"}}}',
                '{"tool":"schedule","action":"list"}',
                '{"tool":"schedule","action":"pause","job_id":"job_...","expected_version":2}',
            ),
            # 又大又少用：开关 tool_default_deferral_enabled 打开时前台回合不发 Schema，目录索引留下面这句用途，用到时 tool_search 加载。
            default_deferred=True,
            deferred_summary="管理定时任务、提醒与已授权群机器人消息订阅（event_source）；宿主核验引用来源",
        ),
    )


# LLM: 调度读写都经原 repository；元数据查询不得改变鉴权、生命周期或持久字段。
# 类用途: 管理当前 owner 的计划并查询运行和交付状态。
class ScheduleTool(BaseTool):
    # LLM: 新控制动作沿同一工具副作用和幂等策略；显式 ask 保持原权限裁决，不通过 slash 入口改变模型调用策略。
    # host_authorizer只由认证宿主适配器注入，不进入模型参数或修改Agent共享run上下文。
    # 函数用途: 装配计划工具与投递控制副作用；认证目录查询只读，更新通知仍需原CAS/creator授权。
    def __init__(self, agent: SimpleAgent, *, host_authorizer=None):
        self.agent = agent
        self._host_authorizer = host_authorizer
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
                        ("delivery_targets", "read_only"),
                        ("create", "mutating"),
                        ("update", "mutating"),
                        ("update_delivery", "mutating"),
                        ("pause", "mutating"),
                        ("resume", "mutating"),
                        ("delete", "dangerous"),
                        ("takeover", "mutating"),
                        ("execution", "read_only"),
                        ("cancel", "dangerous"),
                        ("recover", "mutating"),
                        ("run_now", "dangerous"),
                    ),
                ),),
            ),
            idempotency_policy=IdempotencyPolicy("operation"),
            resource_scopes=ResourceScopePolicy(parameter_names=("job_id",),
            parameter_kinds={"job_id": "logical"}),
            input_policy=ToolInputPolicy(internal_parameters=("__tool_call_id", "__run_scope")),
        )

    # LLM: 群新工作在真实 actor 与原策略锁内写入；只读/停止不受新建授权门限制，失败不泄露正文。
    # 函数用途: 校验动作后调用调度账本，未认证目标保持专用错误码和not_started，不降级为其它路由。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        metadata_only = params.get("metadata_only", False)
        if not isinstance(metadata_only, bool):
            return _error("metadata_only 必须是布尔值", "TOOL_INVALID_ARGUMENTS")
        if metadata_only and action not in {"list", "get", "history", "status"}:
            return _error("纯元数据模式仅支持查询动作", "TOOL_INVALID_ARGUMENTS", metadata_only=True)
        if action not in _ACTIONS:
            return _error("action 不受支持", "TOOL_INVALID_ARGUMENTS")
        if action == "update_delivery" and set(params) - {"action", "job_id", "expected_version", "delivery", "delivery_policy",
                "notify_mode", "metadata_only", "__tool_call_id", "__run_scope"}:
            return _error("update_delivery 只能修改通知策略", "SCHEDULE_DELIVERY_UPDATE_FIELDS_INVALID")
        repository = getattr(self.agent, "scheduler_repository", None)
        if repository is None:
            return _error("当前 owner 的 scheduler 未装配", "TOOL_UNAVAILABLE")
        try:
            return self._execute(repository, action, params)
        except (GroupAuthorizationError, EventSourceInputError) as exc:
            return _error(str(exc), exc.error_code, metadata_only=metadata_only)
        except ScheduleDeliveryTargetUnauthorized as exc:
            return _error(str(exc), exc.error_code, metadata_only=metadata_only)
        except ScheduleValidationError as exc:
            return _error(str(exc), "SCHEDULER_INVALID_SCHEDULE", metadata_only=metadata_only)
        except SchedulerNotFoundError as exc:
            return _error(str(exc), "SCHEDULER_NOT_FOUND", metadata_only=metadata_only)
        except HostActorError as exc:
            return _error(str(exc), exc.error_code, metadata_only=metadata_only)
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

    # LLM: 元数据查询原样早返回；写入沿原CAS，成功后退出policy锁再补本工具结构化job的姓名展示，不改变权限或效果字段。
    # 函数用途: 执行查询和监控控制，投递目录与policy别名沿同一owner校验，旧诊断查询仍排除正文和名字。
    def _execute(self, repository, action: str, params: dict[str, object]) -> ToolHandlerOutcome:
        metadata_only = params.get("metadata_only", False) is True
        if action == "delivery_targets":
            return _success(action, {"delivery_targets": available_delivery_targets(self.agent)})
        if action == "status":
            facts = repository.runtime_snapshot()
            if getattr(getattr(self.agent, "home_paths", None), "owner_kind", "") == "group":
                jobs = _visible_jobs(self.agent, repository.list_jobs()[0])
                ids={job['job_id'] for job in jobs}
                runs=repository._valid_runs(repository._store_snapshot())[0]
                facts.update(active_jobs=sum(j["status"] == "active" for j in jobs), paused_jobs=sum(j["status"] == "paused" for j in jobs),
                    active_runs=sum(run['job_id'] in ids for run in runs),
                    schedule_kinds=sorted({rule['schedule']['kind'] for job in jobs for rule in job.get('rules') or [job]}))
            return _success(action, {"scheduler": facts})
        if action == "list":
            jobs, errors = repository.list_jobs()
            jobs = _visible_jobs(self.agent, jobs)
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
        if action != "create" and not _visible_jobs(self.agent, [repository.get_job(job_id, include_deleted=action == 'delete')]):
            return _error("目标监控不属于当前发起人，已拒绝操作", "SCHEDULER_NOT_AUTHORIZED")
        if action not in {'create', 'get', 'history', 'execution', 'delete', 'cancel', 'takeover', 'recover'}:
            from .control import authorize_job
            if self._host_authorizer is not None:
                self._host_authorizer(repository.get_job(job_id), action)
            else:
                thread_id, _task = _conversation_scope(self.agent)
                authorize_job(repository, repository.get_job(job_id, include_deleted=action == 'delete'),
                    resolve_host_actor(self.agent, thread_id), home_root=getattr(getattr(self.agent, 'home_paths', None), 'root', None),
                    agent=self.agent, operation=action)
        if action == 'execution':
            from .control import inspect_monitor_execution
            thread_id, _task = _conversation_scope(self.agent)
            return _success(action, inspect_monitor_execution(self.agent, repository, job_id,
                actor=resolve_host_actor(self.agent, thread_id)))
        if action == "get":
            active_runs, run_errors = repository.active_runs_by_job()
            job = _visible_jobs(self.agent, [repository.get_job(job_id)])[0]
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
        prepared = prepare_event_sources(self.agent, params) if action in {'create', 'update'} else None
        with _monitor_work_guard(self.agent, action):
            if action == "create":
                outcome = self._create(repository, params, prepared)
            else:
                outcome = self._mutate_existing(repository, action, params, prepared)
        return _with_creator_display(self.agent, outcome)

    # LLM: 已核原 job/creator 后才持群新工作门；写入沿原 CAS，未知编号不触发 provider 查询或泄露其它 owner。
    # 函数用途: 执行已授权监控变更；显式整体通知更新清除旧规则覆盖，只改未来策略，不重置执行统计。
    def _mutate_existing(self, repository, action, params, prepared):
        job_id = str(params.get('job_id') or '').strip()
        if action == "run_now":
            run = repository.reserve_manual_run(job_id, rule_id=str(params.get("rule_id") or ""))
            return _success(action, {"run": _run_projection(run)})
        expected_version = _expected_version(params, required=action in _MUTATING_EXISTING_ACTIONS)
        if isinstance(expected_version, ToolHandlerOutcome):
            return expected_version
        if action == "pause":
            job = repository.pause_job(job_id, expected_version=expected_version)
        elif action == "resume":
            job = repository.resume_job(job_id, expected_version=expected_version)
        elif action in {'delete', 'takeover', 'cancel', 'recover'}:
            return self._control_existing(repository, action, job_id, expected_version)
        else:
            patch = self._update_patch(params, prepared)
            if isinstance(patch, ToolHandlerOutcome):
                return patch
            if not patch:
                return _error("update 至少需要一个可修改字段", "TOOL_INVALID_ARGUMENTS")
            if "delivery" in patch and "rules" not in patch and (action == "update_delivery"
                    or "delivery_policy" in params or "notify_mode" in params):
                from .monitor_state import _RUNTIME_FIELDS
                current = repository.get_job(job_id)
                if current.get("rules"):
                    patch["rules"] = [{key: value for key, value in rule.items()
                        if key not in _RUNTIME_FIELDS and key != "delivery"} for rule in current["rules"]]
            job = repository.update_job(
                job_id,
                patch=patch,
                expected_version=expected_version,
            )
        return _success(action, {"job": _job_projection(job, include_prompt=True)})

    # LLM: 原 tool actor 与命令共用控制服务；不可从参数替换收件人、身份或取消范围，仍由原 job CAS 验证。
    # 函数用途: 执行删除、当前取消、恢复核对和未来通知接管，返回原账本准确事实。
    def _control_existing(self, repository, action, job_id, expected_version):
        from .control import cancel_monitor, delete_monitor, recover_monitor, takeover_notifications
        thread_id, _task = _conversation_scope(self.agent)
        actor = resolve_host_actor(self.agent, thread_id)
        if action == 'recover':
            return _success(action, recover_monitor(self.agent, repository, job_id, expected_version, actor=actor))
        if action in {'delete', 'cancel'}:
            handler = delete_monitor if action == 'delete' else cancel_monitor
            facts = handler(self.agent, repository, job_id, expected_version, actor=actor)
            return _success(action, {**facts, 'job': _job_projection(facts['job'], include_prompt=True)})
        job = takeover_notifications(self.agent, repository, job_id, expected_version, actor)
        return _success(action, {'job': _job_projection(job, include_prompt=True)})

    # LLM: 锁外事件证明与锁内群授权仍守原门；job source沿原ExecutionOrigin/typed actor规范，工作任务编号仅作原执行审计。
    # 函数用途: 保存原聊天来源与后台位置，认证目标在创建线程前验证；非法策略不留下空后台线程。
    def _create(self, repository, params: dict[str, object], prepared) -> ToolHandlerOutcome:
        name = str(params.get("name") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        rules = prepared.rules
        if rules is not None:
            for rule in rules:
                validate_execution_for_agent(self.agent, rule["execution"])
        if not name or (rules is None and (not prompt or not str(params.get("schedule_kind") or "").strip())):
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
        schedule = rules[0]["schedule"] if rules else prepared.schedule or _schedule_from_params(self.agent, params, now=current)
        execution = validate_execution_for_agent(self.agent, params.get("execution"))
        policy = normalize_policy(params.get("policy"))
        precheck = normalize_precheck(params.get("precheck"))
        delivery = validate_delivery_targets(self.agent, creation_delivery(self.agent, thread_id, delivery_from_params(params)))
        from .execution_policy import schedule_execution_thread, schedule_source_thread_id

        source_thread_id = schedule_source_thread_id(self.agent, thread_id)
        creator = _creator_identity(self.agent, thread_id)
        source_request_id = _source_request_id(self.agent, params)
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
                delivery=delivery,
                rules=rules,
                creator=creator,
            )
        )
        return _success(
            "create",
            {"job": _job_projection(job, include_prompt=True), "deduplicated": deduped},
        )

    # LLM: 只更新显式字段，事件来源使用同次锁外网络预检；已预留run保持原冻结策略，写入仍持原群权限锁。
    # 函数用途: 将公开policy别名转换成唯一delivery字段，并在原CAS前验证目标，保留null关闭预检的含义。
    def _update_patch(self, params: dict[str, object], prepared) -> dict[str, object] | ToolHandlerOutcome:
        patch: dict[str, object] = {}
        for key in ("name", "prompt", "misfire_grace_seconds", "policy", "execution", "precheck", "delivery", "rules"):
            if key in params:
                patch[key] = params[key]
        delivery = delivery_from_params(params)
        if delivery is not None:
            patch["delivery"] = validate_delivery_targets(self.agent, delivery)
        if "execution" in patch:
            patch["execution"] = validate_execution_for_agent(self.agent, patch["execution"])
        if "rules" in patch:
            patch["rules"] = prepared.rules
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
            "event_source",
        }
        if any(key in params for key in schedule_keys):
            if not str(params.get("schedule_kind") or "").strip():
                return _error(
                    "修改执行时间时必须同时提供完整 schedule_kind 及对应参数",
                    "TOOL_INVALID_ARGUMENTS",
                )
            patch["schedule"] = prepared.schedule or _schedule_from_params(self.agent, params, now=time.time())
        return patch


# LLM: 原 owner/creator 校验后才取群能力；查/停/取消/删除不申请新建许可，策略锁覆盖真正副作用。
# 函数用途: 更新通知也受原群监控能力门约束，避免新动作绕过群授权。
def _monitor_work_guard(agent, action):
    if action in {'create', 'update', 'update_delivery', 'resume', 'run_now'} and getattr(getattr(agent, 'home_paths', None), 'owner_kind', '') == 'group':
        return group_capability_guard(agent, 'monitors.create', thread_id=_conversation_scope(agent)[0])
    return nullcontext()


# LLM: 事件来源只接受准确消息引用并由宿主核验；普通定时保持原时间合同，不能混用两类参数。
# 函数用途: 生成原调度计划，事件订阅不根据文字或名字推断来源。
def _schedule_from_params(
    agent: object, params: dict[str, object], *, now: float
) -> dict[str, object]:
    if params.get('schedule_kind') == 'event_source':
        if any(params.get(key) not in (None, '') for key in ('at', 'after_seconds', 'every_seconds', 'anchor_at', 'cron', 'timezone')):
            raise ScheduleValidationError('事件订阅不能同时填写定时参数。')
        return schedule_from_event_input(agent, params.get('event_source'))
    if 'event_source' in params:
        raise ScheduleValidationError('event_source 需要 schedule_kind=event_source。')
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
# 函数用途: 展示唯一delivery策略及delivery_policy公开别名；元数据模式不公开目标地址。
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
        "alias": job.get("alias", job["job_id"]),
        "run_count": job.get("run_count"),
        "rules": ([{key: value for key, value in rule.items() if key in {"id", "use_llm", "enabled", "next_run_at", "last_run_at", "last_run_status", "run_count"} or not metadata_only} for rule in job.get("rules", [])]),
        "schedule": ({key: value for key, value in job.get("schedule", {}).items()
                      if key in {"kind", "at", "timezone", "every_seconds", "anchor_at", "expression"}}
                     if metadata_only else job.get("schedule")),
        "next_run_at": iso_from_timestamp(job.get("next_run_at")),
        "misfire_grace_seconds": job["misfire_grace_seconds"],
        "version": job["version"],
        "last_run_at": iso_from_timestamp(job.get("last_run_at")),
        "last_run_status": job.get("last_run_status") or "",
        "last_error_code": job.get("last_error_code") or "",
        "policy": normalize_policy(job.get("policy")),
        "execution": normalize_execution(job.get("execution")),
        "delivery": normalize_delivery(job.get("delivery")),
        "notification_binding_version": int((job.get('notification_binding') or {}).get('version') or 0),
        "precheck_enabled": job.get("precheck") is not None,
        "consecutive_failures": job.get("consecutive_failures", 0),
        "backoff_until": iso_from_timestamp(job.get("backoff_until")),
        "current_run": _run_projection(active_run, metadata_only=metadata_only) if active_run is not None else None,
    }
    if metadata_only:
        payload["delivery"] = {key: value for key, value in payload["delivery"].items() if key in {"mode", "reply_policy", "include_origin"}}
        return payload
    payload["delivery_policy"] = normalize_delivery(job.get("delivery"))
    payload["creator"] = job.get("creator")
    payload["name"] = job["name"]
    payload["creator_display"] = job.get("creator_display") or "创建者身份待核验"
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
        "rule_id": run.get("rule_id", "rule1"),
        "use_llm": run.get("use_llm", True),
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


# LLM: 仅消费本工具_success的结构化JSON.job，不读模型正文；调用方已退出policy锁，仅替换output，原权限/效果及其它回执不变。
# 函数用途: 给成功创建或修改回执补同一创建者展示；可选有界姓名刷新失败沿原未知姓名，不改原job或成功状态。
def _with_creator_display(agent, outcome):
    if not outcome.ok:
        return outcome
    payload = json.loads(outcome.output)
    if not isinstance(payload.get('job'), dict):
        return outcome
    from .creator_access import creator_display_rows

    payload['job'] = creator_display_rows(agent, [payload['job']])[0]
    return replace(outcome, output=json.dumps(payload, ensure_ascii=False))


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


# LLM: 创建与查询共用宿主专有 actor/不可变来源；模型 attrs 与群最新发言者不提供身份，冲突不降级。
# 函数用途: 取得准确监控创建人，本机任务保留 local 标记，未知 IM 来源明确拒绝。
def _creator_identity(agent, thread_id):
    actor = resolve_host_actor(agent, thread_id)
    return {'channel': actor.channel, 'actor_id': actor.actor_id} if actor else {'local': True}


# LLM: 查询只使用已经装配的当前owner仓库，全群创建者可见；写授权在authorize_job统一核对，显示名不授予权限。
# 函数用途: 给当前空间的监控添加可信创建者展示，与slash同空间，不跨群合并。
def _visible_jobs(agent, jobs):
    from .creator_access import creator_display_rows
    if getattr(getattr(agent, 'home_paths', None), 'owner_kind', '') == 'group':
        thread_id, _task = _conversation_scope(agent)
        _creator_identity(agent, thread_id)
    return creator_display_rows(agent, jobs)
