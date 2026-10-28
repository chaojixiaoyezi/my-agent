# LLM: 来源沿原ThreadStore/HostActor与run；旧work双引用证明仅交原queued CAS规范，不产生第二个权限、迁移或模型账。
# 模块用途: 冻结规范定时来源并物化独立线程；同通道通知沿显式继任链解析，执行来源不迁移，失败重投不重执业务。
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, replace

from .delivery_policy import TARGET_DELIVERY_MODES
from .execution_policy import (
    migrate_queued_execution_origin,
    normalize_execution,
    scheduled_execution_metadata,
)


# LLM: 只关闭原 run 精确绑定且尚未开始的 TaskLink；缺失或损坏不补建，跨线程记录不修改，运行收口仍由 scheduler CAS。
# 函数用途: 排队过期或执行前失败时收回已准备的活动链接，避免留下没有执行者的任务。
def close_unstarted_task(service, run: dict, *, now: float) -> None:
    if run.get("started_at"):
        return
    link, error = service.conversation_store.tasks.load_report(str(run["run_id"]))
    if error is None and link is not None and link.thread_id == run["thread_id"] and link.status == "active":
        service.conversation_store.tasks.update_status({"task_id": link.task_id, "status": "cancelled",
                                                        "expected_status": "active", "now": now})


# LLM: 原claim/车道不变；已冻结需处理义务只通知、不重业务，预检与NO沿同一工具执行器，首次拒绝转原零工具表达。
# 函数用途: 在同一领取范围执行一次工作，失败通知重投不会再次认证或执行写操作。
def with_scheduled_work(dependencies, agent, service, claim, signal, *, now: float):
    # LLM: 原wake义务优先，只有新工作写started；通知的原TaskLink保持active到确认或明确结束，不另建任务。
    # 函数用途: 分离一次实际工作与其失败通知，重启后不重新执行失败业务。
    def run_once(kwargs):
        from ..conversation.required_user_action import required_notice
        from ..conversation.runtime import _run_request
        from .precheck import admit_prechecked_work

        if required_notice(agent, _run_request(kwargs)) is not None:
            return dependencies.run_once(kwargs)
        service.repository.mark_run_running(claim.run_id, claim.claim_id, now=now)
        if not claim.run.get("use_llm", True):
            from .tool_work import execute_scheduled_tool
            return execute_scheduled_tool(agent, service, claim, signal, kwargs, notify=dependencies.run_once)
        admission = admit_prechecked_work(agent, service, claim, signal, run_kwargs=kwargs, now=now, notify=dependencies.run_once)
        if admission is True:
            return dependencies.run_once(kwargs)
        return admission if admission is not False else None

    return replace(dependencies, run_once=run_once)


# LLM: 来源按原creator冻结；旧委派work只在原双引用同源且queued未claim时显式规范，owner/topic门不放宽，终态不可复活。
# 函数用途: 入队前规范合法旧来源并冻结交付/宿主身份，原store记录迁移证明，只为isolated创建运行线程。
def prepare_execution(service, run: dict, *, now: float) -> dict:
    if not service.repository.event_run_is_current(run):
        raise ValueError('事件订阅已停止或更新')
    execution = normalize_execution(run.get("execution"))
    from ..conversation.models import ConversationThread
    from ..conversation.unbound_thread_store import ensure_unbound_thread

    origin, migration = _execution_origin(service, run)
    context = {
        "origin_thread_id": origin.thread_id,
        "origin_bindings": _origin_bindings(service, {**run, 'origin_thread_id': origin.thread_id}, origin),
        "defined_model_policy": execution["model_policy"],
        "source_selection_revision": origin.model_selection_revision,
    }
    if (run.get("delivery") or {}).get("follow_latest_channel_session") is False and run["delivery"].get("targets"):
        from .delivery_policy import repository_delivery_targets
        selected = {spec.get("binding_id") for spec in run["delivery"]["targets"]}
        context["delivery_target_threads"] = {row["binding_id"]: row["thread_id"]
            for row in repository_delivery_targets(service.repository) if row["binding_id"] in selected}
    if migration is not None:
        context['origin_migration'] = migration
    run = service.repository.prepare_run_context(str(run["run_id"]), context, origin_migration=migration)
    if execution["mode"] != "isolated":
        return run
    identity = {"thread_kind": "scheduled", "scheduler_run_id": run["run_id"],
                "origin_thread_id": origin.thread_id, **scheduled_execution_metadata(service, run)}
    if run.get('event_source') is not None:
        identity['scheduler_event_source'] = run['event_source']
    seed = ConversationThread(
        thread_id=run["thread_id"], canonical_user_id=origin.canonical_user_id,
        owner_id=origin.owner_id, owner_home=origin.owner_home, title=run["name"],
        created_at=now, updated_at=now, cwd=origin.cwd,
        runtime_workspace_roots=origin.runtime_workspace_roots,
        model_profile_id="", metadata=identity,
    )
    migrate_queued_execution_origin(service, run, seed)
    ensure_unbound_thread(service.conversation_store.threads, seed, identity=identity)
    return run


# LLM: 旧run/source/job须同一work，两份原引用指向同topic/creator且三线程owner/home均属原quota owner_root；不写或猜来源。
# 函数用途: 为旧合法委派计划生成显式old/new及引用证明，真正修改由原queued/context CAS一次完成。
def _execution_origin(service, run):
    from ..conversation.execution_threads import EXECUTION_ORIGIN_KEY, execution_origin_thread
    from ..runtime_errors import DataCorruptionError

    threads = service.conversation_store.threads
    original = threads.load(run['origin_thread_id'])
    if original is None or (original.owner_id and original.owner_id != run['owner']['id']):
        raise KeyError('定时来源会话不可用')
    origin = execution_origin_thread(threads.require, original)
    if origin.thread_id == original.thread_id:
        return origin, None
    job = service.repository.get_job(run['job_id'], include_deleted=True)
    plan = threads.require(job['thread_id'])
    source = execution_origin_thread(threads.require, plan)
    before, planned = original.metadata[EXECUTION_ORIGIN_KEY], plan.metadata.get(EXECUTION_ORIGIN_KEY)
    owner_home = service.repository.quota_enforcer.owner_root
    if (any(row.owner_id != run['owner']['id'] or row.owner_home != str(owner_home) for row in (original, plan, origin))
            or planned is None or source.thread_id != origin.thread_id or before['binding'] != planned['binding']
            or job['source_thread_id'] != original.thread_id or run['source_thread_id'] != original.thread_id
            or job['source_task_id'] != run['source_task_id']
            or run.get('creator') != {'channel': before['binding']['channel'], 'actor_id': before['binding']['actor_id']}):
        raise DataCorruptionError('定时运行与原计划来源不一致')
    return origin, {'schema_version': 'scheduler_origin_migration.v1', 'from_thread_id': original.thread_id,
        'to_thread_id': origin.thread_id, 'owner': run['owner'], 'owner_home': str(owner_home), 'source_task_id': run['source_task_id'],
        'plan_thread_id': plan.thread_id, 'source_reference': before, 'plan_reference': planned}


# LLM: 原 IM 计划来源与 run.creator 必须一致；关闭后台开关时也只冻结准确 creator，缺证据的旧记录不补最新成员。
# 函数用途: 将原布置者交给本次运行，不让群内后来的发送者替换通知或审批对象。
def _origin_bindings(service, run, origin):
    from ..conversation.execution_threads import EXECUTION_ORIGIN_KEY, execution_origin_thread
    from ..runtime_errors import DataCorruptionError

    job = service.repository.get_job(run["job_id"], include_deleted=True)
    plan_thread = service.conversation_store.threads.require(job["thread_id"])
    reference = plan_thread.metadata.get(EXECUTION_ORIGIN_KEY)
    if reference is None:
        creator = run.get("creator")
        if isinstance(creator, dict) and set(creator) == {"channel", "actor_id"}:
            group = run["owner"]["kind"] == "group"
            selected = [asdict(replace(row, channel_user_id=creator["actor_id"])) for row in origin.channel_bindings
                if row.channel == creator["channel"] and (group or row.channel_user_id == creator["actor_id"])]
            if not selected:
                raise DataCorruptionError("定时运行创建人没有准确原通道来源")
            return selected
        return [asdict(binding) for binding in origin.channel_bindings]
    source = execution_origin_thread(service.conversation_store.threads.require, plan_thread)
    if source.thread_id != origin.thread_id:
        raise DataCorruptionError("定时运行与原计划来源不一致")
    frozen = reference["binding"]
    creator = run.get("creator")
    if isinstance(creator, dict) and creator.get("actor_id") and creator != {"channel": frozen["channel"], "actor_id": frozen["actor_id"]}:
        raise DataCorruptionError("定时运行创建人与原计划身份冲突")
    for binding in source.channel_bindings:
        if binding.channel == frozen["channel"] and (
                binding.channel_conversation_id == frozen["conversation_id"] if frozen["conversation_id"]
                else binding.channel_user_id == frozen["actor_id"]):
            return [asdict(replace(binding, channel_user_id=frozen["actor_id"],
                                  channel_conversation_id=frozen["conversation_id"]))]
    raise DataCorruptionError("定时计划来源绑定不可用")


# LLM: 只读当前任务精确 run，不信 wake 里的模型配置或正文；run/线程不匹配必须报错，不扩大到其他任务。
# 函数用途: 读取原活动运行；终态多目标只允许原wake冻结欠账定位策略，不恢复业务执行。
def execution_run(agent, request) -> dict | None:
    repository = getattr(agent, "scheduler_repository", None)
    task_id = str(getattr(request, "task_id", "") or "")
    if repository is None or not task_id:
        return None
    run = repository.get_active_run(task_id)
    if run is None and task_id.startswith('srun_'):
        candidate = repository.effect_run(task_id)
        wake = getattr(request, 'wake_signal', None) or {}
        cached = (wake.get('metadata') or {}).get('owner_delivery')
        if candidate is not None and (candidate.get('event_source') is not None or cached
                and (candidate.get('delivery') or {}).get('mode') in TARGET_DELIVERY_MODES):
            run = candidate
    if run is not None and str(run["thread_id"]) != request.thread_id:
        raise ValueError("定时执行线程与任务不匹配")
    return run


# LLM: 固定档案在每片开始解析，片内不消费原会话选模；继承策略的每请求安全点仍由 selection_refresh 唯一采用。
# 函数用途: 为后台执行绑定原模型依赖，并将模型选择来源与实际执行线程分开记录。
@contextmanager
def scheduled_model_scope(agent, thread, request):
    from ..settings.model_profiles import selected_model_config
    from ..settings.model_scope import (
        ModelSelectionSnapshot,
        model_dependencies_scope,
        model_selection_snapshot,
        prepare_model_dependencies,
        selected_model_scope,
    )
    from ..settings.thread_model_selection import _require_owner

    run = execution_run(agent, request)
    if run is None:
        with selected_model_scope(agent, thread_id=thread.thread_id):
            yield
        return
    policy = normalize_execution(run.get("execution"))
    origin_id = str(run.get("origin_thread_id") or run["thread_id"])
    origin = agent.conversation_store.threads.load(origin_id)
    if origin is None:
        raise ValueError("定时模型来源会话不存在")
    _require_owner(agent, origin)
    profile_id = policy["profile_id"] if policy["model_policy"] == "fixed" else origin.model_profile_id
    config = selected_model_config(agent, profile_id=profile_id)
    dependencies = prepare_model_dependencies(agent, config)
    if dependencies is None:
        if policy["model_policy"] == "fixed" or policy["mode"] == "isolated":
            raise ValueError("当前执行器不支持调度模型作用域")
        with selected_model_scope(agent, thread_id=thread.thread_id):
            yield
        return
    if policy["model_policy"] == "fixed":
        selection = ModelSelectionSnapshot(
            thread.thread_id, profile_id, profile_id, int(run["job_version"]), 0, None, time.time(),
            selection_source="scheduler_fixed", scheduler_run_id=str(run["run_id"]),
        )
    else:
        selection = replace(model_selection_snapshot(origin, config), thread_id=thread.thread_id,
                            selection_thread_id=origin_id, selection_source="scheduler_inherit",
                            scheduler_run_id=str(run["run_id"]))
    with model_dependencies_scope(agent, replace(dependencies, selection=selection), thread_id=thread.thread_id, active=True):
        yield


# LLM: 两种定时执行模式都使用触发时冻结的绑定或明确通知接管引用；旧线程缺 owner 字段时只投影原 run 的准确 owner，不能回落管理员。
# 函数用途: 为后台路由器提供同通道最新继任投影；执行线程和冻结来源记录均不修改。
def delivery_thread(agent, thread, request, *, channel=""):
    from ..conversation.models import ChannelBinding

    run = execution_run(agent, request)
    if run is None:
        return thread
    context = run.get("execution_context") or {}
    follow = (run.get("delivery") or {}).get("follow_latest_channel_session", True)
    notification = run.get('notification_binding')
    if notification is not None:
        from .notification_binding import bound_notification_thread
        origin = bound_notification_thread(agent, notification, follow_latest_channel_session=follow)
        binding = notification['binding']
        selected = tuple(row for row in origin.channel_bindings if row.channel == binding['channel']
            and row.channel_user_id == binding['actor_id'] and row.channel_conversation_id == binding['conversation_id'])
        return replace(origin, channel_bindings=selected)
    origin = agent.conversation_store.threads.load(str(context.get("origin_thread_id") or ""))
    if origin is None:
        raise KeyError("定时交付来源会话不存在")
    bindings = tuple(ChannelBinding(**item) for item in context.get("origin_bindings", ()))
    origin = _follow_delivery_origin(agent, origin, bindings, follow=follow, channel=channel)
    return replace(origin, channel_bindings=bindings, owner_id=origin.owner_id or str(run['owner']['id']))


# LLM: 仅原冻结三元组与/new范围相同才沿最多16跳的共享校验；其它通道仍留原线程，不能使用owner最近会话。
# 函数用途: 为origin投递选择同通道继任，不改定时任务的执行、creator或模型来源。
def _follow_delivery_origin(agent, origin, bindings, *, follow=True, channel=""):
    from ..conversation.store_threads import resolve_thread_successor_chain

    if not follow:
        return origin
    binding = next((row for row in bindings if row.channel == channel), None) if channel else next(iter(bindings), None)
    if binding is None:
        return origin
    return agent.conversation_store.threads.require(resolve_thread_successor_chain(agent.conversation_store, origin.thread_id, binding=binding))


# LLM: 普通请求保持原 route；所有定时执行只以冻结来源绑定调用原路由器，不能沿用 wake 中的错误目标。
# 函数用途: 将执行线程与交付会话分开，首次发送与冻结重投共用同一解析规则。
def resolve_delivery(agent, thread, request, channel: str, target: str):
    from ..conversation.background_routing import (
        BackgroundRouteDependencies,
        resolve_background_route,
    )

    if scheduled_local_delivery(agent, request):
        return thread, "internal", thread.thread_id
    origin = delivery_thread(agent, thread, request, channel=channel)
    if execution_run(agent, request) is None:
        return origin, channel, target
    channel, target = resolve_background_route(BackgroundRouteDependencies(
        load_thread=lambda _key: origin, owner_paths=lambda: (), owner_identity=lambda: ("", "")), origin.thread_id)
    return origin, channel, target


# LLM: 只读原冻结绑定/代次/目标；此快照不能替代运输前原begin CAS。admitted_completion仅用于已external_sent冻结回执补canonical，不授新发送。
# 函数用途: 核原来源后继或多线认证目标与事件代次；已发送目标只按原outbox补账，不授权任意目标。
def validate_delivery_origin(agent, request, context, *, admitted_completion=False) -> bool:
    run = execution_run(agent, request)
    if run is not None and not admitted_completion and not agent.scheduler_repository.event_run_is_current(run):
        return False
    if run is not None and scheduled_local_delivery(agent, request):
        return context.thread_id == run["thread_id"] and context.channel == "internal" and context.target == run["thread_id"]
    if run is None:
        return True
    follow = (run.get('delivery') or {}).get('follow_latest_channel_session', True)
    if (run.get('delivery') or {}).get('mode') in TARGET_DELIVERY_MODES:
        from .alert_delivery import validate_alert_delivery
        return validate_alert_delivery(agent, request, context, run, admitted_completion=admitted_completion)
    if run.get('notification_binding') is not None:
        from ..conversation.channels import binding_delivery_target
        from .notification_binding import bound_notification_thread
        try:
            notification = run['notification_binding']
            origin = bound_notification_thread(agent, notification, follow_latest_channel_session=follow)
            binding = notification['binding']
            return (context.thread_id == origin.thread_id and context.channel == binding['channel']
                and context.target == binding_delivery_target(binding['channel'], binding['actor_id'], binding['conversation_id'], group=False))
        except (ValueError, KeyError, OSError):
            return False
    frozen = run.get("execution_context") or {}
    origin_id = str(frozen.get("origin_thread_id") or "")
    store = agent.conversation_store
    origin = store.threads.load(origin_id)
    if origin is None or (origin.owner_id and origin.owner_id != run['owner']['id']):
        return False
    from ..conversation.models import ChannelBinding
    try:
        origin = _follow_delivery_origin(agent, origin, tuple(ChannelBinding(**row) for row in frozen.get("origin_bindings", ())),
            follow=follow, channel=context.channel)
    except (ValueError, KeyError):
        return False
    origin_id = origin.thread_id
    if context.thread_id != origin_id:
        return False
    for binding in frozen.get("origin_bindings", ()):
        if binding["channel"] != context.channel:
            continue
        from ..conversation.channels import binding_delivery_target

        expected_target = binding_delivery_target(binding['channel'], binding['channel_user_id'], binding['channel_conversation_id'],
            group='/groups/' in str(run['owner']['id']))
        if context.target != expected_target:
            return False
        current, error = store.threads.resolve_report(channel=binding["channel"],
            channel_conversation_id=binding["channel_conversation_id"], channel_user_id=binding["channel_user_id"])
        return error is None and current is not None and (current.thread_id == origin_id if follow else
            current.owner_id == origin.owner_id and current.owner_home == origin.owner_home)
    return context.channel in {"", "internal"} and context.target in {"", origin_id}


# LLM: 正常结果静默读取原run策略；原wake首次认证拒绝义务独立，仍须原来源和发送前资格门，不据正文取消或外发。
# 函数用途: 区分正常周期结果与必须由原用户处理的通知，local/none不吞掉准确宿主义务。
def scheduled_local_delivery(agent, request) -> bool:
    from ..conversation.required_user_action import required_notice
    from .delivery_policy import delivery_specs, normalize_delivery

    run = execution_run(agent, request)
    if run is not None and required_notice(agent, request) is not None:
        return False
    if run is None:
        return False
    policy = normalize_delivery(run.get("delivery"))
    return policy["mode"] in {"local", "none"} or policy["mode"] == "severity_routed" and not delivery_specs(policy, request)
