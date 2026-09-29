# LLM: 会话互通第一期的“列本 owner 会话”模型工具。只投影结构化事实：thread_id、状态、最近活动时间、渠道、
#   是否调用方自己、按唯一判定函数 decide_session_messaging 得出的可用发送类型；绝不输出标题、摘要或任何消息正文。
#   可见性与其它会话工具同源：只给管理员（owner_kind=main），且消息或派活的管理员开关至少开一个；配置统一经
#   capability_config_for_agent(...) or CapabilityConfig() 读，直接读字段，不自带兜底值。
#   数据只来自本 owner 的 conversation_store，并按 owner_home 再核一次，别的 owner 的会话既不列出也不计数。
#   子代理内部线程（metadata.thread_kind=agent）不是会话，不列出。改动时联查 core._register_orchestration_tools、
#   background_tool_policy.SESSION_TASK_WAKE_ALLOWED_TOOLS 与 test_list_owner_sessions_tool。
# 模块用途: 让管理员会话在发消息或派活前，拿到本 owner 其他会话的结构化清单，不再靠人工 /sessions threads 查。
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from ....capability.config import CapabilityConfig
from ....conversation.session_messaging import (
    OWNER_KIND_MAIN,
    SESSION_IDENTITY_UNAVAILABLE,
    SESSION_KIND_MESSAGE,
    SESSION_KIND_TASK,
    SessionMessagingRequest,
    decide_session_messaging,
)
from ....runtime_errors import runtime_error_report
from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ....user_space.owner_resolver import OwnerIdentity

if TYPE_CHECKING:
    from ....core import SimpleAgent

LIST_OWNER_SESSIONS_TOOL = "list_owner_sessions"
# 默认与上限只约束一次返回的条数；0 或负数、非整数、超过上限都按参数错误拒绝，不静默截断成别的值。
_LIST_SESSIONS_DEFAULT_LIMIT = 20
_LIST_SESSIONS_MAX_LIMIT = 100


# LLM: 与 session_messaging_tool_visible / session_task_tool_visible 同源：只看结构化 owner_kind 与开关；
#   config 必须是调用方用 `capability_config_for_agent(...) or CapabilityConfig()` 取得的对象，这里直接读字段。
# 函数用途: 判断当前 owner 是否应该看到“列本 owner 会话”工具；返回 False 时工具不进 registry。
def list_owner_sessions_tool_visible(home_paths: object, config: CapabilityConfig) -> bool:
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip()
    return owner_kind == OWNER_KIND_MAIN and (
        config.session_messaging_admin_enabled or config.session_task_admin_enabled
    )


# LLM: 只读、可并行；资源域是当前 owner 的会话清单，不写任何状态。
# 类用途: 列出本 owner 的其他会话，供 send_session_message / create_session_task 选目标。
class ListOwnerSessionsTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("current_owner_sessions",)),
        mutates_workspace=False,
        promotes_task=False,
    )

    # LLM: model_spec 是实例属性，保持与其它会话工具一致的装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的参数说明。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent
        self.model_spec = build_list_owner_sessions_model_spec()

    # LLM: 顺序固定：校验 limit → 取调用方结构化身份（缺失 fail closed）→ 读本 owner 会话 → 逐条投影。
    #   读会话失败返回结构化失败；损坏记录只计数，不输出内容。
    # 函数用途: 返回本 owner 其他会话的结构化清单。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        limit = _requested_limit(params)
        if isinstance(limit, ToolHandlerOutcome):
            return limit
        identity = _caller_identity(self.agent)
        if identity is None:
            return _failure("无法确定当前会话的 owner 身份；没有列出任何会话。", SESSION_IDENTITY_UNAVAILABLE)
        threads = getattr(getattr(self.agent, "conversation_store", None), "threads", None)
        if threads is None:
            return _failure("当前运行环境没有会话存储，不能列出会话。", "TOOL_EXECUTION_FAILED")
        try:
            items, load_errors = threads.list_report(limit=0)
        except Exception as exc:  # noqa: BLE001 - 统一转成结构化失败
            return _failure("读取会话清单失败。", "TOOL_EXECUTION_FAILED",
                            runtime_error_report(exc, context="list_owner_sessions"))
        from ....capability.runtime_config_reload import capability_config_for_agent

        config = capability_config_for_agent(self.agent) or CapabilityConfig()
        current = _current_thread_id(self.agent)
        owner_home = _owner_home(self.agent)
        rows = [_session_row(thread, identity, current, config) for thread in items if _listable(thread, owner_home)]
        rows.sort(key=lambda row: row["last_activity_at"], reverse=True)
        payload = {"sessions": rows[:limit], "current_thread_id": current, "returned": min(len(rows), limit),
                   "truncated": len(rows) > limit, "unreadable_records": len(load_errors)}
        return ToolHandlerOutcome(LIST_OWNER_SESSIONS_TOOL, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: 只接受 1..上限的整数；bool 不算整数。缺省用默认值。
# 函数用途: 读取并校验 limit 参数。
def _requested_limit(params: dict[str, object]) -> int | ToolHandlerOutcome:
    value = params.get("limit", _LIST_SESSIONS_DEFAULT_LIMIT)
    if type(value) is not int or not 1 <= value <= _LIST_SESSIONS_MAX_LIMIT:
        return _failure(f"limit 必须是 1 到 {_LIST_SESSIONS_MAX_LIMIT} 之间的整数。", "TOOL_INVALID_ARGUMENTS")
    return value


# LLM: 与 send_session_message 同一口径：身份只从 agent.home_paths 的三元组取，任一缺失返回 None（fail closed）。
# 函数用途: 取调用方的结构化 owner 身份。
def _caller_identity(agent: object) -> OwnerIdentity | None:
    home = getattr(agent, "home_paths", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip()
    owner_kind = str(getattr(home, "owner_kind", "") or "").strip()
    owner_id = str(getattr(home, "owner_id", "") or "").strip()
    if not (provider and owner_kind and owner_id):
        return None
    return OwnerIdentity(provider=provider, owner_kind=owner_kind, owner_id=owner_id)


# LLM: 当前会话编号只来自宿主写入的结构化任务属性；没有时为空串（独立命令），清单照常返回、没有“当前”标记。
# 函数用途: 读取调用方所在会话的 thread_id。
def _current_thread_id(agent: object) -> str:
    from ....conversation.authority import current_conversation_task_attributes

    return str(current_conversation_task_attributes(agent).get("conversation_thread_id") or "").strip()


# LLM: owner 家目录只取宿主解析的 home_paths；取不到时返回空串，此时只依赖 store 本身的 owner 边界。
# 函数用途: 返回当前 owner 家目录的规范绝对路径，用于逐条核对会话归属。
def _owner_home(agent: object) -> str:
    root = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    return os.path.realpath(str(root)) if root else ""


# LLM: 子代理内部线程不是会话；记录里写了别的 owner_home 的会话一律跳过且不计数，不泄露其存在。
# 函数用途: 判断一条会话记录是否应出现在清单里。
def _listable(thread: object, owner_home: str) -> bool:
    metadata = getattr(thread, "metadata", None) or {}
    if isinstance(metadata, dict) and str(metadata.get("thread_kind") or "").strip() == "agent":
        return False
    recorded = str(getattr(thread, "owner_home", "") or "").strip()
    return not (recorded and owner_home and os.path.realpath(recorded) != owner_home)


# LLM: 只输出结构化字段，不输出 title/summary/消息正文；allowed_kinds 由唯一判定函数逐类算出，
#   调用方自己的会话一律为空并标 is_current，不作为可发送目标。
# 函数用途: 把一条会话投影成清单中的一行。
def _session_row(thread: object, identity: OwnerIdentity, current: str, config: CapabilityConfig) -> dict[str, object]:
    thread_id = str(getattr(thread, "thread_id", "") or "")
    channel = _thread_channel(thread)
    is_current = bool(current) and thread_id == current
    kinds = [] if is_current else [kind for kind in (SESSION_KIND_MESSAGE, SESSION_KIND_TASK)
                                   if decide_session_messaging(SessionMessagingRequest(
                                       sender_identity=identity, sender_thread_id=current, target_thread_id=thread_id,
                                       target_owner_identity=identity, kind=kind,
                                       messaging_admin_enabled=config.session_messaging_admin_enabled,
                                       messaging_user_enabled=config.session_messaging_user_enabled,
                                       task_admin_enabled=config.session_task_admin_enabled,
                                       target_channel=channel)).allowed]
    return {"thread_id": thread_id, "status": str(getattr(thread, "status", "") or ""),
            "last_activity_at": float(getattr(thread, "updated_at", 0.0) or 0.0), "channel": channel,
            "is_current": is_current, "allowed_kinds": kinds}


# LLM: 与 send_session_message 同一口径：取 channel_bindings 里第一个非空渠道，没有绑定为空串（本机会话）。
# 函数用途: 读取会话渠道，供可发送类型判定与展示。
def _thread_channel(thread: object) -> str:
    for binding in getattr(thread, "channel_bindings", ()) or ():
        channel = str(getattr(binding, "channel", "") or "").strip()
        if channel:
            return channel
    return ""


# LLM: 所有失败带稳定 error_code，effect_outcome=not_started（只读工具没有副作用）。
# 函数用途: 统一生成机器可读失败结果。
def _failure(message: str, error_code: str, details: dict | None = None) -> ToolHandlerOutcome:
    payload: dict[str, object] = {"error": message, "error_code": error_code}
    if details:
        payload["details"] = details
    return ToolHandlerOutcome(LIST_OWNER_SESSIONS_TOOL, False, json.dumps(payload, ensure_ascii=False),
                              error_code=error_code, effect_outcome="not_started")


# LLM: 参数只有可选 limit；描述里写清“不含正文、自己的会话不是目标”，避免模型误发。
# 函数用途: 定义 list_owner_sessions 的参数与语义。
def build_list_owner_sessions_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=LIST_OWNER_SESSIONS_TOOL,
        description=(
            "列出本 owner 的会话清单：thread_id、状态、最近活动时间（Unix 秒）、渠道、是否你当前所在会话，"
            "以及能对它用的发送类型 allowed_kinds（message / task）。只读，不含任何会话正文或标题。"
            "is_current=true 的是你自己的会话，不要把它当作发送目标。"
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": _LIST_SESSIONS_MAX_LIMIT,
                                     "description": f"最多返回几条，按最近活动倒序，默认 {_LIST_SESSIONS_DEFAULT_LIMIT}。"}},
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("给另一个会话发消息或派活前，先查有哪些会话可选", "确认目标会话是否还在活动"),
            avoid_when=("要看会话里的具体内容（本工具不返回正文）",),
            keywords=("会话列表", "其他会话", "list sessions", "thread id"),
            examples=('{"tool":"list_owner_sessions"}', '{"tool":"list_owner_sessions","limit":5}'),
        ),
    )


__all__ = [
    "LIST_OWNER_SESSIONS_TOOL",
    "ListOwnerSessionsTool",
    "build_list_owner_sessions_model_spec",
    "list_owner_sessions_tool_visible",
]
