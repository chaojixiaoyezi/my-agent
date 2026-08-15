"""滑动窗口签名计数:按桶滚动,窗口外惰性过期。纯计数,无语义。"""

from __future__ import annotations

from typing import Any


class SlidingWindowCounter:
    __slots__ = ("window_seconds", "bucket_seconds", "_buckets", "_totals")

    def __init__(self, window_seconds: int, bucket_seconds: int) -> None:
        self.window_seconds = max(int(window_seconds), 1)
        self.bucket_seconds = max(int(bucket_seconds), 1)
        self._buckets: dict[int, dict[str, int]] = {}
        self._totals: dict[str, int] = {}

    def observe(self, signature: str, now: float) -> int:
        """记一次并返回该签名当前窗口内计数(含本次)。"""
        self._evict(now)
        bucket = self._bucket_start(now)
        per_bucket = self._buckets.setdefault(bucket, {})
        per_bucket[signature] = per_bucket.get(signature, 0) + 1
        self._totals[signature] = self._totals.get(signature, 0) + 1
        return self._totals[signature]

    def window_count(self, signature: str, now: float) -> int:
        self._evict(now)
        return self._totals.get(signature, 0)

    def census(self, now: float) -> dict[str, int]:
        self._evict(now)
        return dict(self._totals)

    def _bucket_start(self, now: float) -> int:
        return int(now) - (int(now) % self.bucket_seconds)

    def _evict(self, now: float) -> None:
        horizon = self._bucket_start(now) - self.window_seconds
        expired = [start for start in self._buckets if start <= horizon]
        for start in expired:
            self._drop_bucket(start)

    def _drop_bucket(self, start: int) -> None:
        for signature, count in self._buckets.pop(start).items():
            remaining = self._totals.get(signature, 0) - count
            if remaining > 0:
                self._totals[signature] = remaining
            else:
                self._totals.pop(signature, None)

    def snapshot(self, now: float) -> dict[str, Any]:
        return {"window_totals": self.census(now)}

    def restore(self, payload: dict[str, Any], now: float) -> None:
        """恢复到单一当前桶(粗粒度:重启后窗口从当前时刻重新滚动,计数不凭空丢)。"""
        totals = {str(k): int(v) for k, v in dict(payload.get("window_totals") or {}).items()}
        if not totals:
            return
        self._buckets[self._bucket_start(now)] = dict(totals)
        self._totals = dict(totals)


__all__ = ["SlidingWindowCounter"]
