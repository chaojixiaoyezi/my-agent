"""签名已核验请求的有界进程内LRU；重启或容量淘汰后不承诺跨进程防重放。"""
from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict


class FeishuReplayClockRollbackError(ValueError):
    """回拨越过已遗忘窗口时无法安全判断重放。"""


class FeishuReplayGuard:
    # LLM: 只存摘要和截止时间；实例与入口同生命周期，不持有原始签名或正文。
    # 函数用途: 创建线程安全、有限容量的短期重放缓存。
    def __init__(self, max_entries: int = 4096, window_seconds: int = 300):
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError('max_entries must be positive')
        if type(window_seconds) is not int or window_seconds < 0:
            raise ValueError('window_seconds must be nonnegative')
        self._max_entries = max_entries
        self._window_seconds = window_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._retired_until: float | None = None

    # LLM: 只观察缓存，不通过len清理或延长窗口。
    # 函数用途: 返回当前槽位数。
    def __len__(self):
        with self._lock:
            return len(self._entries)

    # LLM: 调用方先验签和验时间；锁内原子去重，期限包含端点且覆盖未来时间戳的整个有效期。
    # 函数用途: 首次记录返回True，重复返回False；回拨到已清理的窗口时保守拒绝。
    def check_and_record(self, timestamp, nonce, signature, *, now, valid_until):
        now, valid_until = float(now), float(valid_until)
        expires = max(now + self._window_seconds, valid_until)
        if not all(math.isfinite(value) for value in (now, valid_until, expires)) or valid_until < now:
            raise ValueError('invalid replay time')
        key = hashlib.sha256('|'.join((timestamp, nonce, signature)).encode()).hexdigest()
        with self._lock:
            if self._retired_until is not None and now <= self._retired_until:
                raise FeishuReplayClockRollbackError('clock crossed retired replay window')
            # LRU不是到期顺序，不能只检查首项；扫描规模始终受max_entries限制。
            for old in [item for item, deadline in self._entries.items() if deadline < now]:
                deadline = self._entries.pop(old)
                self._retired_until = max(self._retired_until or deadline, deadline)
            if key in self._entries:
                self._entries.move_to_end(key)
                return False
            self._entries[key] = expires
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return True
