# LLM: 压缩阈值只消费正式新键、权威治理及线程settings，不读退役触发别名；回合快照沿ContextVar隔离，不保存第二策略账。
# 模块用途: 为CLI、Host及真实压缩提供同一四层比例和回合内冻结边界。
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

from ..settings.config_layers import compact_admin_forced, resolve_compact_trigger_policy

_TURN_COMPACT_POLICY = ContextVar("turn_compact_policy", default=None)


# LLM: thread只来自已授权Store，home来自宿主；治理地址只取_GovernanceControl.policy_path，损坏或身份拒绝不能降为自治。
#   只读 memory_compact_trigger_percent，缺失取正式默认80，绝不读取已删除的 auto_trigger_percent 别名。
# 函数用途: 读取四层比例及三个管理员开关名，返回独立的结构化快照。
def resolve_thread_compact_policy(agent, thread=None, *, use_frozen=True):
    frozen = _TURN_COMPACT_POLICY.get()
    if use_frozen and frozen is not None and frozen[0] is agent and (thread is None or frozen[1] == thread.thread_id):
        return dict(frozen[2])
    config = getattr(agent, "config", None)
    admin = getattr(config, "memory_compact_trigger_percent", 80)
    forced = compact_admin_forced(config)
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
            from ..user_space.local_user_store import LocalUserStore
            from ..user_space.managed_user_governance import (
                ManagedUserScopeDeniedError,
                resolve_managed_local_user,
            )

            try:
                uid, _identity, _owner = resolve_managed_local_user(home.root, home.owner_id)
            except ManagedUserScopeDeniedError:
                raw_uid = str(getattr(home, "owner_id", "") or "").removeprefix("local/").strip()
                gov_path = _GovernanceControl(home.root, raw_uid).policy_path
                if LocalUserStore(home.root).get(raw_uid) is not None or gov_path.exists():
                    raise
                uid = ""
            if uid:
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
# 函数用途: 解析/compact threshold的50..95或reset/default参数。
def parse_compact_threshold_command(instructions):
    from .control_commands import ConversationControlCommand

    parts = instructions.split(maxsplit=1)
    if not parts or parts[0] != "threshold":
        return None
    value = parts[1].strip() if len(parts) == 2 else ""
    valid = value in {"reset", "default"} or value.isascii() and value.isdigit() and 50 <= int(value) <= 95
    return ConversationControlCommand("compact", value=value, operation="threshold", valid=valid,
                                      usage="用法：/compact threshold <50-95|reset|default>")


# LLM: 只读取当前配置、治理和既有线程，不建线程、不进入模型scope或摘要，也不写任何状态。
# 函数用途: 给Gateway和CLI返回同一份压缩政策与闲置元数据，只读查询不会推进压缩代数。
def compact_status_result(agent, thread=None):
    import copy

    from ..settings.config_layers import compact_idle_parameters
    from .control_commands import ConversationControlResult

    policy = resolve_thread_compact_policy(agent, thread, use_frozen=False)
    metadata = getattr(thread, "metadata", {}) or {}
    threshold, seconds = compact_idle_parameters(getattr(agent, "config", None))
    public = {"effective_percent": policy["effective_trigger_percent"], "source": policy["effective_source"],
              "admin_forced": policy["force_locked"], "thread_override": policy["thread_override_percent"],
              "user_override": policy["user_default_percent"], "admin_default": policy["admin_default_percent"]}
    details = {"compact_policy": public, "compact_generation": getattr(thread, "compact_generation", 0),
               "idle_auto_compact": copy.deepcopy(metadata.get("idle_auto_compact", {})),
               "idle_policy": {"enabled": bool(threshold and seconds), "trigger_percent": threshold, "idle_seconds": seconds},
               "last_user_activity_at": metadata.get("last_user_activity_at", getattr(thread, "updated_at", None)),
               "user_activity_generation": metadata.get("user_activity_generation", 0)}
    return ConversationControlResult("compact", True,
        f"当前压缩阈值：{public['effective_percent']}%（{public['source']}）；压缩代数：{details['compact_generation']}。",
        details=details)
