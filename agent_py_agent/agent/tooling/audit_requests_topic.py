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
    own_id = str(agent.home_paths.owner_id or "local/main")
    # 仅实际候选线程做规范点读；未证归属不猜，不预列所有线程或 owner。
    class CandidateOwners(dict):
        def get(self, key, default=""):
            if key in self:
                return super().get(key, default)
            if query.scope == "all_owners":
                return default
            thread, error = agent.conversation_store.threads.load_report(key)
            if thread is None or error is not None:
                return default
            self[key] = own_id
            return own_id
    mapping = CandidateOwners()
    if query.thread_id:
        mapping[query.thread_id] = own_id
    return (None if query.scope == "all_owners" else frozenset({own_id}), mapping,
            {"legacy_attribution_coverage": "candidate_point_lookup" if query.scope != "all_owners" else "unknown"})


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

    allowed, thread_owners, scope_facts = _scope_owners(agent, query, max_owners)
    reader = request_reader(agent, "request_audit_outcomes")
    if callable(reader):
        try:
            outcomes = reader(outcome_query(query, allowed, thread_owners))
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


def outcome_query(query, allowed, thread_owners):
    from ..gateway_parts.request_audit_records import OutcomeQuery
    names = ("request_id", "error_code", "channel", "until", "cursor", "message_ref", "request_ref", "tool_name", "status", "min_duration_ms")
    return OutcomeQuery(allowed, thread_owners, query.thread_id, query.since, query.limit,
                        **{name: getattr(query, name, None if name in ("until", "min_duration_ms") else "") for name in names})


def request_reader(agent, name):
    """运行中只信已领取 writer；本机主管理员可沿已配置宿主路径只读查询。"""
    writer = getattr(getattr(agent, "_current_run_params", None), "conversation_task_binding_callback", None)
    if writer is not None:
        return getattr(writer, name, None)
    from ..user_space.approval_mode import is_permission_admin
    if str(agent.home_paths.owner_id or "local/main") != "local/main" or not is_permission_admin(agent.home_paths):
        return None
    from ..gateway_parts import request_audit_records
    from ..gateway_parts.paths import gateway_paths
    function = {"request_audit_outcomes": request_audit_records.request_outcome_records,
                "message_lifecycle_records": request_audit_records.message_lifecycle_records}.get(name)
    return (lambda query: function(gateway_paths(agent), query)) if function else None
