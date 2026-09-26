# LLM: audit_records 的 requests 主题：权限与范围裁决已由 audit_records 完成（audit_allowed；all_owners 需管理员且已开跨用户审计），
#   这里只按已裁决的范围收集 Gateway 请求结果（状态、错误码与处理建议、渠道、私聊/群聊、耗时、归属 owner），不读正文和日志。
#   请求记录只经宿主写入器读取（Gateway 运行时才有）；旧记录没有 owner_id 时按会话归属，需要列出范围内 owner 的会话编号。
#   调用方是本机管理员时附带管理员身份事实（是否设了管理员密码、哪些 IM 私聊已绑定），用于排查“飞书为什么报错”。
#   改动须同步 audit_records_tool、request_audit_records 与 test_audit_requests_topic.py。
# 模块用途: 让 my-agent 自己查出“哪些请求失败了、错误码是什么、属于哪个用户、该怎么处理”，不必由人去翻文件。
from __future__ import annotations

REQUEST_SOURCES = ("gateway_request_records", "error_taxonomy")


# LLM: 线程编号只来自各 owner 自己的会话库（只读打开）；读失败只计数。all_owners 最多覆盖 max_owners 个 owner。
# 函数用途: 按范围算出允许的 owner 集合与旧记录归属用的线程映射。
def _scope_owners(agent: object, query: object, max_owners: int) -> tuple[frozenset | None, dict, dict]:
    from ..user_space.owner_admin_controls import list_owner_home_paths
    from .audit_records_tool import _owner_host, _thread_ids

    home = agent.home_paths
    own_id = str(home.owner_id or "local/main")
    if query.scope != "all_owners":
        ids, errors = _thread_ids(agent.conversation_store)
        return frozenset({own_id}), dict.fromkeys(ids, own_id), {"unreadable_thread_records": errors}
    homes, truncated = list_owner_home_paths(home, limit=max_owners)
    thread_owners, errors = {}, 0
    for owner_id, owner_home in homes:
        ids, failed = _thread_ids(_owner_host(agent, owner_home).conversation_store)
        errors += failed
        thread_owners.update(dict.fromkeys(ids, owner_id))
    return None, thread_owners, {"owners_listed": len(homes), "owners_truncated": truncated, "unreadable_thread_records": errors}


# LLM: 只给本机管理员看，只含布尔与已绑定私聊的渠道、用户编号、绑定时间；读失败报告不可用，不影响其余审计。
# 函数用途: 汇总管理员身份现状，帮助判断 IM 私聊是否已按管理员运行。
def _admin_identity_facts(agent: object) -> dict | None:
    from ..user_space.admin_channel_identity import (
        AdminIdentityStoreError,
        list_admin_channel_identities,
    )
    from ..user_space.admin_password import admin_password_status
    from ..user_space.approval_mode import is_permission_admin

    home = getattr(agent, "home_paths", None)
    if not is_permission_admin(home):
        return None
    try:
        bound = [{"channel": item.channel, "user_id": item.user_id, "bound_at": round(item.bound_at, 3)}
                 for item in list_admin_channel_identities(home.root)]
    except AdminIdentityStoreError:
        bound = None
    return {"password_configured": bool(admin_password_status(home.root).get("configured")),
            "channel_identity_enabled": bool(getattr(agent.config, "admin_channel_identity_enabled", False)),
            "bound_private_chats": bound}


# LLM: 收集器签名与 audit_records 的 _TOPIC_COLLECTORS 一致 (agent, query)；返回值自带 sources。
# 函数用途: 按已裁决的范围读取请求结果，并在管理员调用时附带管理员身份事实。
def requests_topic(agent: object, query: object, *, max_owners: int) -> dict:
    from ..gateway_parts.request_audit_records import OutcomeQuery

    allowed, thread_owners, scope_facts = _scope_owners(agent, query, max_owners)
    writer = getattr(getattr(agent, "_current_run_params", None), "conversation_task_binding_callback", None)
    reader = getattr(writer, "request_audit_outcomes", None)
    if callable(reader):
        try:
            outcomes = reader(OutcomeQuery(allowed, thread_owners, query.thread_id, query.since, query.limit))
        except OSError:
            outcomes = {"available": False, "reason": "request_records_unreadable"}
    else:
        outcomes = {"available": False, "reason": "no_gateway_request_context"}
    result = {"requests": outcomes, **scope_facts, "sources": list(REQUEST_SOURCES)}
    admin = _admin_identity_facts(agent)
    if admin is not None:
        result["admin_identity"] = admin
        hints = _admin_hints(query.scope, admin)
        if hints:
            result["hints"] = hints
    return result


# LLM: 软提示只依据结构化事实（范围、管理员身份事实），给模型看的下一步建议，不做任何判定或授权。
# 函数用途: 告诉管理员的 my-agent：飞书私聊在绑定前属于另一个用户、以及还没有私聊绑定管理员时该怎么做。
def _admin_hints(scope: str, admin: dict) -> list[str]:
    hints = []
    if scope != "all_owners":
        hints.append("飞书等 IM 私聊在绑定管理员之前属于另一个用户，本人范围看不到；要查这些请求用 scope=all_owners。")
    if admin.get("password_configured") and admin.get("channel_identity_enabled") and not admin.get("bound_private_chats"):
        hints.append("已设管理员密码但还没有任何 IM 私聊绑定管理员：管理员本人在飞书私聊发 /admin <管理员密码> 后，"
                     "那个私聊才按管理员运行（使用管理员的模型和设置）。")
    return hints


__all__ = ["REQUEST_SOURCES", "requests_topic"]
