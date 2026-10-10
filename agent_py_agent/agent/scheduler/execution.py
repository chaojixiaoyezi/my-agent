# LLM: 调度模型与线程执行沿原 ThreadStore、模型作用域和 run 身份；独立上下文不产生第二个权限或模型账本。
# 模块用途: 物化独立定时线程，解析原会话继承或固定档案，并回到原会话交付。
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, replace

from .execution_policy import normalize_execution


# LLM: 只关闭原 run 精确绑定且尚未开始的 TaskLink；缺失或损坏不补建，跨线程记录不修改，运行收口仍由 scheduler CAS。
# 函数用途: 排队过期或执行前失败时收回已准备的活动链接，避免留下没有执行者的任务。
def close_unstarted_task(service, run: dict, *, now: float) -> None:
    if run.get("started_at"):
        return
    link, error = service.conversation_store.tasks.load_report(str(run["run_id"]))
    if error is None and link is not None and link.thread_id == run["thread_id"] and link.status == "active":
        service.conversation_store.tasks.update_status({"task_id": link.task_id, "status": "cancelled",
                                                        "expected_status": "active", "now": now})


# LLM: 只包装原后台 run_once；拿到车道后才写 started_at，预检接收同一原后台参数以保留 cwd、Skill 和任务身份。
# 函数用途: 在同一领取、中断和心跳范围内执行预检及模型工作，不重建一份工作区合同。
def with_scheduled_work(dependencies, agent, service, claim, signal, *, now: float):
    # LLM: 原 claim 已经通过准入和恢复检查；开始、预检、模型仍用一个 run，异常交原后台领取器收尾。
    # 函数用途: 记录实际取得车道的时刻，然后按预检结果继续模型或静默收口。
    def run_once(kwargs):
        from .precheck import admit_prechecked_work

        service.repository.mark_run_running(claim.run_id, claim.claim_id, now=now)
        if admit_prechecked_work(agent, service, claim, signal, run_kwargs=kwargs, now=now):
            return dependencies.run_once(kwargs)
        return None

    return replace(dependencies, run_once=run_once)


# LLM: run 已由本 owner 仓库校验；先冻结来源绑定，重启只复用同一快照，不把后来重置的会话猜作原接收者。
# 函数用途: 入队前在原存储创建无通道的独立线程，保留来源工作区权限上界。
def prepare_execution(service, run: dict, *, now: float) -> dict:
    execution = normalize_execution(run.get("execution"))
    if execution["mode"] != "isolated":
        return run
    from ..conversation.models import ConversationThread
    from ..conversation.unbound_thread_store import ensure_unbound_thread

    origin = service.conversation_store.threads.load(run["origin_thread_id"])
    if origin is None or (origin.owner_id and origin.owner_id != run["owner"]["id"]):
        raise KeyError("定时来源会话不可用")
    run = service.repository.prepare_run_context(str(run["run_id"]), {
        "origin_thread_id": origin.thread_id,
        "origin_bindings": [asdict(binding) for binding in origin.channel_bindings],
        "defined_model_policy": execution["model_policy"],
        "source_selection_revision": origin.model_selection_revision,
    })
    identity = {"thread_kind": "scheduled", "scheduler_run_id": run["run_id"],
                "origin_thread_id": origin.thread_id}
    seed = ConversationThread(
        thread_id=run["thread_id"], canonical_user_id=origin.canonical_user_id,
        owner_id=origin.owner_id, owner_home=origin.owner_home, title=run["name"],
        created_at=now, updated_at=now, cwd=origin.cwd,
        runtime_workspace_roots=origin.runtime_workspace_roots,
        model_profile_id="", metadata=identity,
    )
    ensure_unbound_thread(service.conversation_store.threads, seed, identity=identity)
    return run


# LLM: 只读当前任务精确 run，不信 wake 里的模型配置或正文；run/线程不匹配必须报错，不扩大到其他任务。
# 函数用途: 在原调度账本定位本次后台执行的冻结策略。
def execution_run(agent, request) -> dict | None:
    repository = getattr(agent, "scheduler_repository", None)
    task_id = str(getattr(request, "task_id", "") or "")
    if repository is None or not task_id:
        return None
    run = repository.get_active_run(task_id)
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


# LLM: 独立线程只负责执行，路由候选使用触发时冻结的绑定；当前索引验证放在每次真正交付前，重置不改接收者。
# 函数用途: 为原后台路由器提供原会话投影，不给独立线程添加通道绑定。
def delivery_thread(agent, thread, request):
    from ..conversation.models import ChannelBinding

    run = execution_run(agent, request)
    if run is None or normalize_execution(run.get("execution"))["mode"] != "isolated":
        return thread
    context = run.get("execution_context") or {}
    origin = agent.conversation_store.threads.load(str(context.get("origin_thread_id") or ""))
    if origin is None:
        raise KeyError("定时交付来源会话不存在")
    bindings = tuple(ChannelBinding(**item) for item in context.get("origin_bindings", ()))
    return replace(origin, channel_bindings=bindings)


# LLM: 普通请求保持原 route；独立执行只以冻结来源绑定调用原路由器，不用 owner 兜底猜收件者。
# 函数用途: 将执行线程与交付会话分开，首次发送与冻结重投共用同一解析规则。
def resolve_delivery(agent, thread, request, channel: str, target: str):
    from ..conversation.background_routing import (
        BackgroundRouteDependencies,
        resolve_background_route,
    )

    origin = delivery_thread(agent, thread, request)
    if origin.thread_id == thread.thread_id:
        return origin, channel, target
    channel, target = resolve_background_route(BackgroundRouteDependencies(
        load_thread=lambda _key: origin, owner_paths=lambda: (), owner_identity=lambda: ("", "")), origin.thread_id)
    return origin, channel, target


# LLM: 只裁决原 run 的冻结绑定，不搜索新通道；返回失败供既有 outbox 保存，禁止重跑模型来弥补交付失败。
# 函数用途: 投递前核对原会话未重置，并给 canonical 消息提供准确的归属会话。
def validate_delivery_origin(agent, request, context) -> bool:
    run = execution_run(agent, request)
    if run is None or normalize_execution(run.get("execution"))["mode"] != "isolated":
        return True
    frozen = run.get("execution_context") or {}
    origin_id = str(frozen.get("origin_thread_id") or "")
    if context.thread_id != origin_id:
        return False
    store = agent.conversation_store
    if store.threads.load(origin_id) is None:
        return False
    for binding in frozen.get("origin_bindings", ()):
        if binding["channel"] != context.channel:
            continue
        current, error = store.threads.resolve_report(channel=binding["channel"],
            channel_conversation_id=binding["channel_conversation_id"], channel_user_id=binding["channel_user_id"])
        return error is None and current is not None and current.thread_id == origin_id
    return context.channel in {"", "internal"} and context.target in {"", origin_id}
