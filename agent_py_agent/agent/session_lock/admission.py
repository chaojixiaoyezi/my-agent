"""私聊执行准入只投影原 SessionLockStore；不缓存或另存认证状态。"""
from __future__ import annotations

from dataclasses import dataclass

from .service import DEFAULT_IDLE_SECONDS, UnlockService
from .store import SessionLockStore


@dataclass(frozen=True)
class SessionAdmission:
    allowed: bool
    reason_code: str = ''
    locked: bool = False
    requires_password_setup: bool = False


# LLM: 每次调用重读原服务，不建立第二份锁；业务入口须在执行前再次调用。
# 函数用途: 让 local 和 scale 用同一状态判定私聊是否放行，未知故障一律拒绝。
def check_session_admission(service, user_id: str, *, is_group: bool = False,
                            enabled: bool = True) -> SessionAdmission:
    if is_group or not enabled:
        return SessionAdmission(True)
    if service is None or not user_id:
        return SessionAdmission(False, 'FEISHU_SESSION_LOCK_UNAVAILABLE')
    try:
        status = service.status(user_id, is_group=False)
        return SessionAdmission(not (status.locked or status.requires_password_setup),
            'FEISHU_SESSION_LOCK_UNAVAILABLE' if status.locked or status.requires_password_setup else '',
            status.locked, status.requires_password_setup)
    except Exception:
        return SessionAdmission(False, 'FEISHU_SESSION_LOCK_UNAVAILABLE')


# LLM: root 来自原宿主配置，ro 构造不建库；缺库不能当作已解锁。
# 函数用途: 规模 worker 在业务执行前读取与本地通道相同的原锁库。
def admission_from_config(config, user_id: str, *, is_group: bool = False) -> SessionAdmission:
    enabled = str(getattr(config, 'feishu_session_lock_enabled', True)).lower() not in {'false', '0', 'off', 'no'}
    if is_group or not enabled:
        return check_session_admission(None, user_id, is_group=is_group, enabled=enabled)
    try:
        from ..user_space.home_layout import resolve_my_agent_home
        from ..user_space.home_root import configured_home_root

        root = resolve_my_agent_home(configured_home_root(config))
        service = UnlockService(SessionLockStore(root / 'session_lock' / 'private_chat_locks.db', read_only=True),
            idle_limit_seconds=int(getattr(config, 'feishu_personal_idle_lock_seconds', DEFAULT_IDLE_SECONDS) or DEFAULT_IDLE_SECONDS))
        return check_session_admission(service, user_id)
    except Exception:
        return SessionAdmission(False, 'FEISHU_SESSION_LOCK_UNAVAILABLE')
