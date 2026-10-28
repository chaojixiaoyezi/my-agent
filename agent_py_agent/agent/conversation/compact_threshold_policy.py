# LLM: 压缩阈值只消费宿主配置、权威治理及线程settings，回合快照沿ContextVar隔离，不保存第二策略账。
# 模块用途: 为CLI、Host及真实压缩提供同一四层比例和回合内冻结边界。
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

from ..settings.config_layers import resolve_compact_trigger_policy

_TURN_COMPACT_POLICY = ContextVar("turn_compact_policy", default=None)


# LLM: thread只来自已授权Store，home来自宿主；治理损坏必须传播，不能把锁定静默降为用户自治。
# 函数用途: 读取四层比例，返回独立的结构化快照。
def resolve_thread_compact_policy(agent, thread=None, *, use_frozen=True):
    frozen = _TURN_COMPACT_POLICY.get()
    if use_frozen and frozen is not None and frozen[0] is agent and (thread is None or frozen[1] == thread.thread_id):
        return dict(frozen[2])
    config = getattr(agent, "config", None)
    admin = getattr(config, "memory_compact_trigger_percent",
                    getattr(config, "memory_compact_auto_trigger_percent", 80))
    forced = getattr(config, "force_compact_trigger_percent", False) is True
    user = {}
    source = (getattr(config, "config_sources", None) or {}).get("memory_compact_trigger_percent", {})
    if type(source.get("compact_admin_default_percent")) is int:
        user["default_trigger_percent"] = admin
        admin = source["compact_admin_default_percent"]
    home = getattr(agent, "home_paths", None)
    if home is not None and getattr(home, "root", None) is not None:
        from ..user_space.managed_user_governance import (
            _GovernanceControl,
            read_global_compact_force_policy,
        )

        global_policy = read_global_compact_force_policy(home.root)
        if global_policy is not None:
            if global_policy.get("trigger_percent") is not None:
                admin = global_policy["trigger_percent"]
            forced = global_policy["force_compact_trigger_percent"]
        if getattr(home, "owner_provider", "") == "local" and getattr(home, "owner_kind", "") == "user":
            from ..user_space.managed_user_governance import resolve_managed_local_user

            uid, _identity, _owner = resolve_managed_local_user(home.root, home.owner_id)
            governed = _GovernanceControl(home.root, uid).policy().get("user_compact_policy", {})
            if governed.get("default_trigger_percent") is not None or governed.get("force_locked"):
                user.update(governed)
    metadata = getattr(thread, "metadata", {}) or {}
    settings = metadata.get("settings", {})
    override = settings.get("compact_trigger_percent") if isinstance(settings, dict) else None
    return resolve_compact_trigger_policy(admin_default_percent=admin, admin_forced=forced,
                                           thread_override_percent=override, user_policy=user)


# LLM: 同一Agent的本次回合比例冻结，finally恢复上层上下文；不同线程/子Agent不共享这个快照。
# 函数用途: 让preflight、live及finalization在一次回合内使用同一生效比例。
@contextmanager
def compact_policy_scope(agent, thread):
    resolution = resolve_thread_compact_policy(agent, thread)
    token = _TURN_COMPACT_POLICY.set((agent, thread.thread_id, dict(resolution)))
    try:
        yield resolution
    finally:
        _TURN_COMPACT_POLICY.reset(token)


# LLM: 必须在原线程update_atomic锁内调用且读取新鲜治理，不用本轮冻结值绕过管理员刚开启的锁；不自行写盘。
# 函数用途: 合并或清除会话比例并推进原设置修订，强制锁定时连恢复默认也返回403。
def thread_with_compact_threshold(agent, thread, value):
    from dataclasses import replace

    from ..plugin_host.errors import HostAPIError

    if resolve_thread_compact_policy(agent, thread, use_frozen=False)["force_locked"]:
        raise HostAPIError("COMPACT_POLICY_LOCKED_BY_ADMIN", 403)
    if value is not None and (type(value) is not int or not 50 <= value <= 95):
        raise HostAPIError("INVALID_SCHEMA")
    metadata = dict(thread.metadata or {})
    settings = dict(metadata.get("settings", {}))
    if value is None:
        settings.pop("compact_trigger_percent", None)
    else:
        settings["compact_trigger_percent"] = value
    return replace(thread, metadata={**metadata, "settings": settings}, revision=thread.revision + 1, updated_at=time.time())


# LLM: 只解析显式threshold子树，不猜测普通摘要说明；数值非法保持结构化用法错误，不调用模型。
# 函数用途: 解析/compact threshold的50..95或default参数。
def parse_compact_threshold_command(instructions):
    from .control_commands import ConversationControlCommand

    parts = instructions.split(maxsplit=1)
    if not parts or parts[0] != "threshold":
        return None
    value = parts[1].strip() if len(parts) == 2 else ""
    valid = value == "default" or value.isascii() and value.isdigit() and 50 <= int(value) <= 95
    return ConversationControlCommand("compact", value=value, operation="threshold", valid=valid,
                                      usage="用法：/compact threshold <50-95|default>")
