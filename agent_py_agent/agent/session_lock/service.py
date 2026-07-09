"""个人私聊闲置锁 + 密码解锁服务(

个人私聊闲置超过阈值(默认 1h)后锁定,需密码解锁;群聊不锁。首次没密码时引导设置。
暴破防护:连续失败超阈值后每次失败指数退避锁定(monotonic 计时,防墙钟前跳绕过)。
解锁失败/成功都发事件(经 events 回调),但绝不携带密码内容。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .passwords import PasswordPolicyError, hash_password, verify_password
from .store import SessionLockStore

DEFAULT_IDLE_SECONDS = 3600
# 解锁口令暴破节流:前几次失败不锁(容忍手误),超阈值后每次失败指数退避(封顶),
# 把在线暴破从"只受 scrypt 单次成本约束"压到每窗口 ~1 次;成功即清零。
UNLOCK_MAX_FAILURES = 5
UNLOCK_LOCKOUT_BASE_SECONDS = 30.0
UNLOCK_LOCKOUT_CAP_SECONDS = 900.0
MAX_FAILURE_ENTRIES = 8192  # _failures 上限(防大量不同 user 撑爆内存),满淘汰最老


@dataclass(frozen=True)
class UnlockStatus:
    locked: bool
    idle_seconds: float
    requires_password_setup: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "locked": self.locked,
            "idle_seconds": round(self.idle_seconds, 1),
            "requires_password_setup": self.requires_password_setup,
        }


class UnlockService:
    def __init__(
        self,
        store: SessionLockStore,
        *,
        idle_limit_seconds: int = DEFAULT_IDLE_SECONDS,
        events: Callable[[str, dict], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._idle_limit_seconds = max(60, int(idle_limit_seconds or DEFAULT_IDLE_SECONDS))
        self._events = events
        # 暴破锁定窗用 monotonic:墙钟前跳(NTP/容器迁移/DST)会让 locked_until 瞬间过期→绕过。
        # _failures 内存态、重启即空,monotonic 同样进程级、重启同步重置 → 二者一致;锁定本不跨重启。
        self._monotonic = monotonic
        self._fail_lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}  # user_id → (失败数, 锁定至[mono])

    def _emit(self, kind: str, payload: dict) -> None:
        if self._events is None:
            return
        try:
            self._events(kind, payload)
        except Exception:
            pass  # 事件外呼失败绝不影响锁逻辑

    # -- 活跃 / 状态 ------------------------------------------------------------

    def record_activity(self, user_id: str, *, is_group: bool = False) -> None:
        """记录私聊活跃(群聊永不触碰锁状态)。"""
        if is_group or not user_id:
            return
        self.store.record_activity(user_id)

    def status(self, user_id: str, *, is_group: bool = False) -> UnlockStatus:
        """群聊永不闲置锁;个人私聊闲置超阈值后锁定。"""
        if is_group or not user_id:
            return UnlockStatus(locked=False, idle_seconds=0.0, requires_password_setup=False)
        last = self.store.last_activity(user_id)
        if last is None:
            # 首次接触:还没锁,活跃从现在起算。
            return UnlockStatus(locked=False, idle_seconds=0.0, requires_password_setup=False)
        idle = time.time() - last
        if idle <= self._idle_limit_seconds:
            return UnlockStatus(locked=False, idle_seconds=idle, requires_password_setup=False)
        return UnlockStatus(
            locked=True,
            idle_seconds=idle,
            requires_password_setup=not self.store.has_password(user_id),
        )

    # -- 暴破防护 --------------------------------------------------------------

    def lockout_remaining(self, user_id: str, *, now: float | None = None) -> float:
        """当前暴破锁定窗内的剩余秒数(>0 表示此刻应拒绝尝试)。now 为 monotonic 时刻。"""
        current = now if now is not None else self._monotonic()
        with self._fail_lock:
            _count, locked_until = self._failures.get(user_id, (0, 0.0))
        return max(0.0, locked_until - current)

    def _record_failure(self, user_id: str, now: float) -> None:
        """now 为 monotonic 时刻;locked_until 也按 monotonic 存(防墙钟前跳绕过)。"""
        with self._fail_lock:
            count = self._failures.get(user_id, (0, 0.0))[0] + 1
            locked_until = 0.0
            if count >= UNLOCK_MAX_FAILURES:  # 超阈值后每次失败指数退避(封顶)
                over = count - UNLOCK_MAX_FAILURES
                backoff = min(UNLOCK_LOCKOUT_CAP_SECONDS, UNLOCK_LOCKOUT_BASE_SECONDS * (2.0**over))
                locked_until = now + backoff
            # LRU 封顶:满且是新 user 时淘汰最老(dict 保插入序,首个即最老)。
            if len(self._failures) >= MAX_FAILURE_ENTRIES and user_id not in self._failures:
                self._failures.pop(next(iter(self._failures)), None)
            self._failures[user_id] = (count, locked_until)

    # -- 密码 set / change / unlock --------------------------------------------

    def set_password(self, user_id: str, password: str) -> tuple[bool, str]:
        """首次设解锁密码。已设过的不能直接覆盖(防有人趁会话未锁改你密码反锁你)——改密码走
        change_password。返回 (是否成功, 中文提示)。打字命令与卡片输入共用此逻辑。"""
        if self.store.has_password(user_id):
            return False, "你已设过解锁密码,不能直接覆盖;改密码请用「更改密码」(先输旧密码再输新的)。"
        try:
            self.store.set_password_hash(user_id, hash_password(password))
        except PasswordPolicyError:
            return False, "密码不符合要求(需 ≥8 位、含大小写字母和数字),请重设。"
        self.record_activity(user_id)
        self._emit("session_lock.password.set", {"user_id": user_id})
        return True, "✅ 解锁密码已设置。以后会话闲置锁定后用它解锁。"

    def change_password(self, user_id: str, old_password: str, new_password: str) -> tuple[bool, str]:
        """改密码:必须先验旧密码(复用 unlock 的验证 + 防爆破锁定),再设新。返回 (成功, 提示)。"""
        if not self.store.has_password(user_id):
            return False, "你还没设过解锁密码;请先设置(不用改)。"
        if not self.unlock(user_id, old_password):  # 验旧密码(失败计入防爆破锁定)
            return False, "旧密码不正确(多次错误会临时锁定)。"
        try:
            self.store.set_password_hash(user_id, hash_password(new_password))
        except PasswordPolicyError:
            return False, "新密码不符合要求(需 ≥8 位、含大小写字母和数字)。"
        self.record_activity(user_id)
        self._emit("session_lock.password.changed", {"user_id": user_id})
        return True, "✅ 密码已修改。"

    def unlock(self, user_id: str, password: str) -> bool:
        """/unlock <密码>:验证并恢复被锁的个人会话。"""
        now = self._monotonic()  # 锁定窗用 monotonic,免墙钟前跳绕过
        remaining = self.lockout_remaining(user_id, now=now)
        if remaining > 0:
            # 锁定窗内:直接拒(不验密码、不增计数),给暴破上硬约束。
            self._emit("session_lock.unlock.throttled", {"user_id": user_id, "retry_after": round(remaining, 1)})
            return False
        stored = self.store.password_hash(user_id)
        ok = bool(stored) and verify_password(password, stored)
        if ok:
            with self._fail_lock:  # 成功即清零,合法用户偶尔手误不受影响
                self._failures.pop(user_id, None)
            self.record_activity(user_id)
            self._emit("session_lock.unlock.succeeded", {"user_id": user_id})
        else:
            self._record_failure(user_id, now)
            self._emit("session_lock.unlock.failed", {"user_id": user_id})
        return ok


__all__ = ["UnlockService", "UnlockStatus", "DEFAULT_IDLE_SECONDS"]
