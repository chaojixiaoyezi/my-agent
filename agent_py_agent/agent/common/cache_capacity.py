"""从现有并发槽位配置推导缓存容量，避免新增配置和重复维护容量默认值。

未登记时返回调用方原有下限，保持 CLI 和单测行为；max_subagents=0 按
1 + 0 保守计算，只保留主代理槽位，不将缓存放大为无界。
"""

import threading

_LOCK = threading.Lock()
_SLOTS = {"owners": 0, "owner_sessions": 0, "owner_agents": 0, "global_inflight": 0}


def _count(config: object, name: str) -> int:
    try:
        return max(0, int(getattr(config, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def configure_from_config(config: object) -> None:
    owners = _count(config, "gateway_owner_inflight_limit") or _count(config, "owner_agent_pool_max_agents")
    sessions = _count(config, "gateway_user_inflight_limit") + max(
        _count(config, "background_threads_per_owner"),
        _count(config, "background_admin_threads_per_owner"),
    )
    agents = sessions * (1 + _count(config, "max_subagents"))
    inflight = _count(config, "gateway_global_inflight_limit")
    with _LOCK:
        _SLOTS.update(owners=owners, owner_sessions=sessions, owner_agents=agents, global_inflight=inflight)


def resident_owners(floor: int) -> int:
    return max(floor, _SLOTS["owners"])


def owner_sessions(floor: int) -> int:
    return max(floor, _SLOTS["owner_sessions"])


def owner_agents(floor: int) -> int:
    return max(floor, _SLOTS["owner_agents"])


def owners_scaled(floor: int) -> int:
    return floor * max(1, _SLOTS["owners"])


def global_inflight(floor: int) -> int:
    return max(floor, _SLOTS["global_inflight"])


def reset_for_tests() -> None:
    with _LOCK:
        _SLOTS.update(owners=0, owner_sessions=0, owner_agents=0, global_inflight=0)
