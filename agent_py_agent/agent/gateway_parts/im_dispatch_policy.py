# LLM: 新 IM 入口在原回执提交前冻结当天偏好；冷宿主只读已解析 owner/config，不创建 Agent、Store、锁或缓存；恢复只消费原 prepared_request。
# 模块用途: 为已接受消息生成原委派快照，保留 TUI、策略关闭及跨零点重送语义；不签发 owner 或执行租约。
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..capability.runtime_config_reload import (
    capability_config_for_agent,
    capability_config_for_paths,
)
from ..conversation.channels import supports_transcript_delivery
from ..conversation.route_mode import route_setting_for_paths, route_thread_for_paths
from ..settings.config import AgentConfig
from ..user_space.home_layout import MyAgentHomePaths
from ..user_space.home_layout import home_paths as resolve_home_paths
from ..user_space.owner_policy import resolve_effective_owner_policy
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home

if TYPE_CHECKING:
    from .control_service import GatewayControlScope

IM_DISPATCH_KEY = "host_im_dispatch.v1"


def _space_enabled(config):
    return bool(getattr(config, "business_spaces_enabled", False))


# LLM: 原Agent入口保留可信身份与能力上界；仅按准确绑定读取route，已接受的请求不经过此入口重算。
# 函数用途: 为IM宿主冻结当前会话路由与来源，不从正文推断单轮覆盖。
def capture_gateway_input_context(base_agent, scope) -> tuple[dict | None, dict | None]:
    if base_agent is None:
        return None, None
    from .control_service import _request_agent_for_scope

    local_transcript = supports_transcript_delivery(scope.channel)
    agent = _request_agent_for_scope(base_agent, scope)
    if local_transcript and not _space_enabled(agent.config):
        return None, None
    store = getattr(agent, "conversation_store", None)
    if store is not None:
        thread, error = store.threads.resolve_report(channel=scope.channel,
            channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
        if error:
            raise ValueError("route thread binding is unreadable")
    else:
        thread = route_thread_for_paths(agent.home_paths, agent.config, scope)
    from .request_space import capture_request_space

    space = capture_request_space(agent.home_paths, thread, actor_id=scope.user_id) if _space_enabled(agent.config) else None
    dispatch = None if local_transcript else _capture_snapshot(
        agent.home_paths, agent.config, scope, capability_config_for_agent(agent),
        live_policy=getattr(agent, "owner_policy", None), thread=thread,
    )

    return dispatch, space


def capture_im_dispatch(base_agent, scope) -> dict | None:
    if base_agent is None or supports_transcript_delivery(scope.channel):
        return None
    return capture_gateway_input_context(base_agent, scope)[0]


# LLM: 只接受宿主已解析且与完整规范 home 一致的 owner；配置只缩小能力，不签权。capability 来源必须显式沿原构造 root/准确覆盖路径，不从 owner home 推断。
# 函数用途: 冷 Gateway 无需构造 Agent 或 Store 即可冻结原 host_im_dispatch.v1 字段；调用方仍负责租约、来源和实际配置解析。
def capture_gateway_input_context_for_owner(
    *, home_paths: MyAgentHomePaths, config: AgentConfig, scope: GatewayControlScope,
    capability_root: str | Path, capability_config_path: str | Path | None = None,
) -> tuple[dict | None, dict | None]:
    _require_scope_owner(home_paths, scope)
    from .request_space import capture_request_space

    local_transcript = supports_transcript_delivery(scope.channel)
    if local_transcript and not _space_enabled(config):
        return None, None
    thread = route_thread_for_paths(home_paths, config, scope)
    space = capture_request_space(home_paths, thread, actor_id=scope.user_id) if _space_enabled(config) else None
    dispatch = None if local_transcript else _capture_snapshot(
        home_paths, config, scope,
        capability_config_for_paths(root=capability_root, user_path=capability_config_path),
        thread=thread,
    )

    return dispatch, space


def capture_im_dispatch_for_owner(
    *, home_paths: MyAgentHomePaths, config: AgentConfig, scope: GatewayControlScope,
    capability_root: str | Path, capability_config_path: str | Path | None = None,
) -> dict | None:
    if supports_transcript_delivery(scope.channel):
        return None
    return capture_gateway_input_context_for_owner(home_paths=home_paths, config=config, scope=scope,
        capability_root=capability_root, capability_config_path=capability_config_path)[0]


# LLM: 不能从模型、role、原始 channel 或配置补造 owner；完整路径集合必须匹配纯规范工厂，避免只比较显示 ID 后读到别人的策略。
# 函数用途: 在任何偏好/策略读取前核对宿主冻结身份和 owner 路径，不创建目录或登记活跃用户。
def _require_scope_owner(home: MyAgentHomePaths, scope: GatewayControlScope) -> None:
    owner = scope.resolved_owner
    if not isinstance(home, MyAgentHomePaths) or not isinstance(owner, OwnerIdentity):
        raise ValueError("IM 委派要求已解析 owner 和规范路径")
    if owner == OwnerIdentity.local_main():
        canonical = OwnerIdentity.local_main()
    elif owner.owner_kind == "user":
        canonical = OwnerIdentity.provider_user(owner.provider, owner.owner_id)
    elif owner.owner_kind == "group":
        canonical = OwnerIdentity.provider_group(owner.provider, owner.owner_id)
    else:
        raise ValueError("IM 委派 owner 不是规范身份")
    expected = home_paths_with_owner(resolve_home_paths(home.root), resolve_owner_home(home.root, canonical))
    if canonical != owner or home != expected:
        raise ValueError("IM 委派 owner 与规范路径不一致")


# LLM: 唯一route设置决定新请求，能力/owner只收窄；结构化覆盖仅影响本轮mode，原route值仍冻结。
# 函数用途: 返回原受理快照的兼容字段和C-ROUTE-SNAPSHOT，不写任务或重算已接受的通知归属。
def _capture_snapshot(home, config, scope, capability, *, live_policy=None, thread=None) -> dict:
    preference = route_setting_for_paths(home, thread, actor_id=scope.user_id)
    policy = resolve_effective_owner_policy(home)
    enabled = (
        capability is not None and capability.im_task_delegation_enabled and config.enable_subagents
        and preference["enabled"] and scope.metadata.get("route_self_execution") is not True
        and _allows_delegation(policy) and _allows_delegation(live_policy)
    )
    snapshot = {"mode": "delegated" if enabled else "original", "channel": scope.channel,
                "actor_id": scope.user_id, "conversation_id": scope.conversation_id, "owner_id": str(home.owner_id),
                "preference_revision": preference["revision"], "expires_at": 0.0,
                "route_mode_frozen": preference["enabled"],
                "route_origin": {"channel": scope.channel, "chat_id": scope.conversation_id,
                                 "thread_id": thread.thread_id if thread is not None else "",
                                 "message_id": str(scope.metadata.get("message_id") or ""), "owner_id": str(home.owner_id)}}
    if getattr(scope, "metadata", None) and isinstance(scope.metadata, dict):
        if scope.metadata.get("space_id"):
            snapshot["space_id"] = str(scope.metadata["space_id"])
        target_agent = scope.metadata.get("target_agent_id") or scope.metadata.get("background_agent_id")
        if target_agent:
            snapshot["target_agent_id"] = str(target_agent)
    return snapshot


# LLM: 只消费原策略结构化错误及禁用列表；缺少可选 Agent 上界不等于越过刚读取的 owner policy。
# 函数用途: 判断一层既有策略是否允许委派，坏策略不能靠默认工具清单变为允许。
def _allows_delegation(policy) -> bool:
    return not getattr(policy, "load_errors", ()) and "create_subagents" not in getattr(policy, "disabled_tools", ())
