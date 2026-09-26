# LLM: 统一审计工具：以后新的审计主题一律加进 topic 枚举与 _TOPIC_COLLECTORS，不再为每类审计新建工具。
#   权限：当前 owner 的 audit_allowed（管理员可关，读失败按不允许）；scope=all_owners 另要求当前 owner 是本机管理员且
#   管理员自己开启了 cross_owner_audit_allowed（owner_admin_controls）。owner 与会话身份只来自宿主解析的 home_paths 与
#   运行上下文，不接受模型给的 owner/thread。只读权威结构化记录（设置读取、model_usage 账本、Gateway 请求记录），
#   不 grep 日志、不读正文。子代理不可用。各主题收集器自带 sources。改动须同步 decision_audit、request_audit_records、
#   audit_requests_topic、错误码块与 test_decision_audit_controls、test_audit_requests_topic。
# 模块用途: 让用户和 my-agent 查询自己的决策使用与请求成败（管理员许可时可查全部用户），回答"调没调、成没成、为什么失败"。
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

from ..runtime_context import current_subagent_run_id
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

TOOL_NAME = "audit_records"
_TOPICS = ("decision", "requests")
_SCOPES = ("current_thread", "owner", "all_owners")
_DEFAULT_HOURS, _MAX_HOURS = 24, 24 * 30
_DEFAULT_LIMIT, _MAX_LIMIT = 20, 100
# 跨用户审计一次最多覆盖的用户数
_MAX_OWNERS = 64


# LLM: 一次审计已校验的结构化参数：thread_id 只在 scope=current_thread 时取自宿主运行上下文，其余为空；since 为时间窗起点。
# 类用途: 在各审计主题收集器之间传递同一份请求参数。
@dataclass(frozen=True)
class AuditQuery:
    topic: str
    scope: str
    thread_id: str
    since: float
    limit: int


# LLM: 线程只取宿主运行上下文（与 user_config 同一裁决函数），不接受模型参数；没有可信会话时返回空。
# 函数用途: 取得当前可信会话编号，供 scope=current_thread 使用。
def _trusted_thread(agent: object) -> str:
    from .user_config_tool import _decision_thread_scope

    return _decision_thread_scope(agent)[0]


# LLM: 读线程目录失败只计数，不补猜；返回全部线程编号（顺序不影响统计）。
# 函数用途: 列出一个会话库里的全部会话编号。
def _thread_ids(store: object) -> tuple[list[str], int]:
    threads, errors = store.threads.list_report(limit=0)
    return [thread.thread_id for thread in threads], len(errors)


# LLM: 其它 owner 的宿主只带 home/config/会话库与共享能力配置，不物化冷 Agent、不创建目录（initialize=False）。
# 函数用途: 为跨用户审计构造某个 owner 的只读宿主。
def _owner_host(agent: object, home: object) -> SimpleNamespace:
    from ..gateway_parts.owner_conversation_store import owner_conversation_store

    return SimpleNamespace(home_paths=home, config=agent.config, capability_router=getattr(agent, "capability_router", None),
                           conversation_store=owner_conversation_store(agent, home, initialize=False))


# LLM: 设置读取失败只记结构化原因（设置忙或配置不可读），不影响用量统计。points 取 owner 决策结果日志的按点位汇总，
#   是判断某接入点是否被调用、被冷却/期限挡住的唯一来源；用量账只按用途汇总，不能拿它推断单个点位。
# 函数用途: 汇总一个 owner 的决策设置、管理员控制、时间窗内的调用统计和各接入点的决策结果。
def _decision_owner_report(owner_id: str, host: object, thread_ids: list[str], query: AuditQuery) -> dict:
    from ..conversation.decision_audit import decision_settings_summary, decision_usage_summary
    from ..conversation.decision_outcome_log import decision_outcome_summary
    from ..user_space.owner_admin_controls import read_owner_admin_controls

    try:
        settings = decision_settings_summary(host, query.thread_id)
    except BlockingIOError:
        settings = {"unavailable": "settings_busy"}
    except Exception:  # noqa: BLE001 设置读不到只影响这一段，审计其余部分照常输出
        settings = {"unavailable": "settings_unreadable"}
    controls = read_owner_admin_controls(host.home_paths)
    return {"owner_id": owner_id, "admin_controls": {key: controls[key] for key in ("decision_model_allowed", "audit_allowed")},
            "settings": settings, "usage": decision_usage_summary(host.conversation_store, thread_ids, since=query.since),
            "points": decision_outcome_summary(host.home_paths, since=query.since)}


# LLM: 请求记录只经宿主写入器读取（Gateway 运行时才有），读取器不存在即报告不可用，不改从日志或配置推导队列位置。
# 函数用途: 读取这些会话在 Gateway 请求记录里的决策观察。
def _observations(agent: object, thread_owners: dict[str, str], query: AuditQuery) -> dict:
    writer = getattr(getattr(agent, "_current_run_params", None), "conversation_task_binding_callback", None)
    reader = getattr(writer, "decision_audit_observations", None)
    if not callable(reader):
        return {"available": False, "reason": "no_gateway_request_context"}
    try:
        return reader(thread_owners=thread_owners, since=query.since, limit=query.limit)
    except OSError:
        return {"available": False, "reason": "request_records_unreadable"}


# LLM: current_thread/owner 只看当前 owner；all_owners 由调用方先完成管理员许可检查。owner 编号取宿主 home 的规范编号。
# 函数用途: 按范围收集决策审计：每个 owner 的设置与用量，以及全部相关会话的观察记录。
def _decision_topic(agent: object, query: AuditQuery) -> dict:
    from ..user_space.owner_admin_controls import list_owner_home_paths

    home = agent.home_paths
    if query.scope == "all_owners":
        homes, truncated = list_owner_home_paths(home, limit=_MAX_OWNERS)
        targets = [(owner_id, _owner_host(agent, owner_home)) for owner_id, owner_home in homes]
    else:
        truncated, targets = False, [(str(home.owner_id or "local/main"), agent)]
    owners, thread_owners, unreadable_threads = [], {}, 0
    for owner_id, host in targets:
        ids, errors = ([query.thread_id], 0) if query.scope == "current_thread" else _thread_ids(host.conversation_store)
        unreadable_threads += errors
        thread_owners.update(dict.fromkeys(ids, owner_id))
        owners.append(_decision_owner_report(owner_id, host, ids, query))
    return {"owners": owners, "owners_truncated": truncated, "unreadable_thread_records": unreadable_threads,
            "observations": _observations(agent, thread_owners, query),
            "sources": ["decision_settings", "model_usage_ledger", "decision_outcome_log", "gateway_request_records"]}


# LLM: requests 主题实现在 audit_requests_topic（延迟导入避免循环）；范围与许可已由 execute 裁决。
# 函数用途: 收集 Gateway 请求结果（成败、错误码与处理建议、渠道、归属 owner）。
def _requests_topic(agent: object, query: AuditQuery) -> dict:
    from .audit_requests_topic import requests_topic

    return requests_topic(agent, query, max_owners=_MAX_OWNERS)


_TOPIC_COLLECTORS = {"decision": _decision_topic, "requests": _requests_topic}


# LLM: 只做结构校验，越界不夹取；非法参数按 TOOL_INVALID_ARGUMENTS 返回，不读任何记录。
# 函数用途: 解析 topic/scope/since_hours/limit，返回规范值或参数错误回执。
def _parse(params: dict) -> tuple | ToolHandlerOutcome:
    topic, scope = params.get("topic"), params.get("scope", "owner")
    hours, limit = params.get("since_hours", _DEFAULT_HOURS), params.get("limit", _DEFAULT_LIMIT)
    valid = (topic in _TOPICS and scope in _SCOPES
             and type(hours) in (int, float) and 0 < hours <= _MAX_HOURS
             and type(limit) is int and 1 <= limit <= _MAX_LIMIT)
    if not valid:
        return _refuse("invalid_arguments", f"topic 须为 {'/'.join(_TOPICS)}，scope 须为 {'/'.join(_SCOPES)}，"
                                            f"since_hours 为 (0, {_MAX_HOURS}]，limit 为 1 到 {_MAX_LIMIT}。", "TOOL_INVALID_ARGUMENTS")
    return topic, scope, float(hours), limit


# LLM: 拒绝都在读记录之前（not_started），带稳定原因码；权限类用 AUDIT_ACCESS_DENIED。
# 函数用途: 生成统一的拒绝回执。
def _refuse(reason: str, message: str, code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(TOOL_NAME, False, json.dumps({"reason": reason, "message": message}, ensure_ascii=False),
                              error_code=code, effect_outcome="not_started", result_envelope={TOOL_NAME: {"reason": reason}})


# LLM: 与 core 注册条件配套（main_agent/user owner 注册，子代理隐藏）；只读，不需审批，可并行。
# 类用途: 按主题与范围读取权威结构化记录，输出可核对的审计结果。
class AuditRecordsTool(BaseTool):
    model_spec = ToolModelSpec(
        name=TOOL_NAME,
        description=(
            "统一审计入口：查询当前用户自己的结构化运行记录，回答'到底调没调用、成功失败几次、用了多少 token、设置是什么、为什么失败'。"
            "topic=decision 汇总决策模型（Jev）：有效设置与各接入点模式、会话用量账本里的调用次数/成功/失败/超时/已报输入 token、"
            "各接入点的决策结果（成功/超时/冷却跳过等次数与最近几条；某点没出现只说明窗口内没触发），"
            "Gateway 请求记录里的选模型与能力推荐观察。topic=requests 列出 Gateway 请求结果：状态、错误码及处理建议、渠道、"
            "私聊/群聊、耗时和归属用户；用户说'飞书/IM/TUI 发消息报错、没回复'时先用它查，管理员调用时还附带管理员密码是否已设、"
            "哪些 IM 私聊已绑定管理员。scope=current_thread 只看当前会话，owner（默认）看本人全部会话，"
            "all_owners 仅管理员且已明确开启跨用户审计时可用。只读权威记录，不读日志和对话正文；"
            "回答决策是否被调用时必须以本工具结果为准，不要 grep 日志或凭文件名下结论。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": list(_TOPICS),
                          "description": "审计主题：decision（决策模型使用）或 requests（请求成败与错误码）。"},
                "scope": {"type": "string", "enum": list(_SCOPES), "description": "默认 owner（本人全部会话）。"},
                "since_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_HOURS,
                                "description": f"只看最近多少小时，默认 {_DEFAULT_HOURS}。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT,
                          "description": f"最多返回多少条观察记录，默认 {_DEFAULT_LIMIT}。"},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(category="system", use_cases=(
            "用户问决策模型/Jev 今天有没有被调用、成功几次、失败原因",
            "用户问决策花了多少 token、哪些会话调用最多",
            "排查决策设置是否生效（总开关、各点模式、等待时间）",
            "用户说在飞书/IM/TUI 发消息报错或没回复：topic=requests 查错误码与处理建议（别的用户的请求要 scope=all_owners）",
        ), avoid_when=("需要修改设置时（用 user_config）",)),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("audit_records:current_owner",)),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # 函数用途: 保存宿主 agent，身份与会话在执行时从它读取。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 审计按主会话 owner 身份读取，子代理运行里隐藏，避免下级代理代表 owner 读全部会话。
    # 函数用途: 子代理运行中报告本工具不可用。
    def availability(self) -> ToolAvailability:
        if current_subagent_run_id(self._agent):
            return ToolAvailability.unavailable("audit records are read only by the main conversation agent")
        return ToolAvailability.ready()

    # LLM: 先校验参数，再按 owner 控制与管理员许可裁决范围，最后才读记录；只读，没有副作用。
    # 函数用途: 执行一次审计查询并返回 JSON 结果。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        from ..user_space.owner_admin_controls import (
            cross_owner_audit_allowed,
            read_owner_admin_controls,
        )

        parsed = _parse(params)
        if isinstance(parsed, ToolHandlerOutcome):
            return parsed
        topic, scope, hours, limit = parsed
        home = getattr(self._agent, "home_paths", None)
        controls = read_owner_admin_controls(home)
        if not controls["audit_allowed"]:
            reason = "policy_unreadable" if controls["source"] == "unreadable" else "audit_disabled_by_admin"
            return _refuse(reason, "当前用户不能使用审计工具。", "AUDIT_ACCESS_DENIED")
        if scope == "all_owners" and not cross_owner_audit_allowed(home):
            return _refuse("cross_owner_not_allowed", "跨用户审计需要管理员在 admin_controls 里明确开启。", "AUDIT_ACCESS_DENIED")
        thread_id = _trusted_thread(self._agent) if scope == "current_thread" else ""
        if scope == "current_thread" and not thread_id:
            return _refuse("no_trusted_thread", "当前运行没有可信会话；请改用 scope=owner。", "TOOL_INVALID_ARGUMENTS")
        query = AuditQuery(topic, scope, thread_id, time.time() - hours * 3600, limit)
        result = _TOPIC_COLLECTORS[topic](self._agent, query)
        report = {"topic": topic, "scope": scope, "window": {"since_hours": hours, "since": round(query.since, 3)},
                  **result, "not_read": "日志与对话/记忆正文不在审计读取范围内"}
        return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(report, ensure_ascii=False))


__all__ = ["AuditRecordsTool", "TOOL_NAME"]
