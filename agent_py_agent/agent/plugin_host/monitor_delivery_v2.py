# LLM: 仅桥接当前owner原schedule服务；授权由门户capability和原工具owner/creator/群能力门共同核验，参数不得选择owner。
# 模块用途: 为Web提供认证通知目标及策略读写，不创建第二监控库、不停止或重执工作。
from __future__ import annotations

import json

from .errors import HostAPIError


# LLM: agent由组合根注入，不可从payload构造；缺准确owner运行时返回503，不能借main或其它用户执行器。
# 函数用途: 复用原schedule的结构化结果、CAS及安全错误码。
def _call(context, payload, agent, action, authorized=None):
    from ..scheduler.tool import ScheduleTool

    home = getattr(agent, "home_paths", None)
    if (home is None or home.owner_id != context.owner_home.owner_id
            or str(home.owner_home_dir) != str(context.owner_home.home_dir)):
        raise HostAPIError("HOST_NOT_READY", 503)
    authorizer = None
    if authorized is not None:
        from .conversations import read_context
        from .schemas import thaw
        context = read_context(authorized)
        expected_action = {"delivery_targets": "monitors.delivery.targets.list", "get": "monitors.delivery.get",
            "update_delivery": "monitors.delivery.update"}[action]
        if authorized.call.action != expected_action or thaw(authorized.call.payload) != payload:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        agent, authorizer = _host_schedule_context(agent, context, payload, authorized)
    params = {"action": action}
    if "monitor_id" in payload:
        params["job_id"] = payload["monitor_id"]
    for key in ("expected_version", "delivery_policy"):
        if key in payload:
            params[key] = payload[key]
    result = ScheduleTool(agent, host_authorizer=authorizer).execute(params)
    if not result.ok:
        raise HostAPIError(result.error_code or "HOST_NOT_READY", 403 if result.error_code in {
            "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED", "CHANNEL_ACTOR_SCOPE_MISMATCH", "CAPABILITY_DENIED"} else 409)
    return json.loads(result.output)


# LLM: 目录由当前owner真实绑定投影，不允许自行提交任意收件地址或选择别人的绑定。
# 函数用途: 列出可用的已认证通知路线。
def delivery_targets(context, payload, *, agent=None, authorized=None):
    return _call(context, payload, agent, "delivery_targets", authorized)


# LLM: get沿原工具只读owner授权，不写账或拉起新Agent，公开策略只有原delivery的投影别名。
# 函数用途: 读取一个原监控的通知策略和版本。
def get_delivery(context, payload, *, agent=None, authorized=None):
    return _call(context, payload, agent, "get", authorized)


# LLM: 更新沿原creator授权、群能力、expected_version和目标认证；只影响未来运行，不重放已冻结业务。
# 函数用途: 在原调度账本切换静默、本地、绑定或双线通知。
def update_delivery(context, payload, *, agent=None, authorized=None):
    return _call(context, payload, agent, "update_delivery", authorized)


# LLM: 显式请求视图只覆盖本调用的宿主上下文，属性仍委托原owner Agent；不修改线程局部/共享运行参数。
# 类用途: 让非管理员冷请求沿原creator及群能力门使用已认证调用者。
class _HostScheduleAgent:
    # LLM: params来自宿主Owner身份和准确job来源，不接受模型载荷提供actor。
    # 函数用途: 固定本次请求的原Agent和身份视图。
    def __init__(self, agent, params):
        self._agent, self._current_run_params = agent, params

    # LLM: 仓库和服务仅取原Agent，不创建备用执行器或跨owner资源。
    # 函数用途: 委托本调用未覆盖的原服务。
    def __getattr__(self, name):
        return getattr(self._agent, name)


# LLM: local/main的真实宿主管理员可管理自己的监控；普通owner仍按原creator与群授权，缺成员身份拒绝而不伪装创建者。
# 函数用途: 从鲜活Host调用构造独立认证上下文和写前重验器。
def _host_schedule_context(agent, context, payload, authorized):
    from types import SimpleNamespace

    from ..conversation.host_actor import HostActorError, actor_from_scope, resolve_host_actor
    from ..scheduler.control import authorize_job
    from ..user_space.owner_resolver import OwnerIdentity
    from .conversations import read_context

    home = agent.home_paths
    main = context.identity == OwnerIdentity.local_main() and context.source_binding.actor == context.identity
    job = agent.scheduler_repository.get_job(payload["monitor_id"]) if payload.get("monitor_id") else None
    if job is not None and job["owner"] != agent.scheduler_repository.owner:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    scoped = agent
    source_id = (job or {}).get("source_thread_id", "")
    if not main and job is not None:
        actor = None
        if home.owner_provider != "local":
            caller = context.source_binding.source.caller_owner if home.owner_kind == "group" else context.identity
            if caller is None or caller.owner_kind != "user" or caller.provider != home.owner_provider:
                raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
            scope = SimpleNamespace(channel=home.owner_provider, channel_user_id=caller.owner_id,
                channel_conversation_id=context.identity.owner_id if home.owner_kind == "group" else "")
            actor = actor_from_scope(agent, scope, source_id, authorized.call.idempotency_key or "host-v2-read")
        scoped = _HostScheduleAgent(agent, SimpleNamespace(host_channel_actor=actor, task_id="",
            task_attributes={"conversation_thread_id": source_id}, request_id=authorized.call.idempotency_key or "host-v2-read"))
    # LLM: 每次真实写前重验原issuer，管理员例外仅限准确local/main；普通用户不借job.creator反造身份。
    # 函数用途: 保持owner、原creator及群策略校验，不依赖模型回合存在。
    def authorize(current_job, operation):
        fresh = read_context(authorized)
        if (fresh.identity != context.identity or fresh.owner_home.owner_id != home.owner_id
                or str(fresh.owner_home.home_dir) != str(home.owner_home_dir)
                or current_job["owner"] != agent.scheduler_repository.owner):
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        if not main:
            authorize_job(agent.scheduler_repository, current_job, resolve_host_actor(scoped, source_id),
                home_root=home.root, agent=scoped, operation=operation)
    return scoped, authorize
