
from __future__ import annotations

"""通用韧性原语:断路器 + 滑动窗口速率检测。

对照五个 agent 项目(长期助手/终端交互/通道运行时/工具运行时/会话运行时)长跑韧性实现提炼的通用版:
- CircuitBreaker ← 长期助手 MCP 断路器(连续 3 次失败→open→60s 冷却→half-open)
  + 终端交互 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3 断路器 + 工具运行时 doom-loop 3 连检测。
- RateWindow ← 通道运行时 fixed-window-rate-limit(windowMs+maxRequests)
  + 长期助手 process watch 限流(8 次/10s 滑动窗口)。

纯内存状态机,无 IO、无全局时钟依赖(now 由调用方传入,便于测试与确定性回放),
可被任何常驻能力(日志 daemon / 网关 / 采集器 / 多代理调度)复用。
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 断路器:把"连续失败"从静默累积变成可观测、会快速失败、能自动恢复的状态机。
# ---------------------------------------------------------------------------
@dataclass
class CircuitBreaker:
    """连续失败到阈值就 open(快速失败 + 告警一次),冷却后 half-open 放一次试探,成功则 close。

    用途:外部依赖(数据源接口/网络/数据库)连续失败时,不再静默重试烧资源,而是
    熔断 + 主动暴露,冷却后自愈。对照 长期助手 打开期直接返回错误而非让模型继续烧 token。
    """

    threshold: int = 3              # 连续失败几次后熔断(open)
    cooldown_seconds: float = 60.0  # open 后多久允许 half-open 试探
    consecutive_failures: int = 0   # 当前连续失败数(成功即清零)
    total_failures: int = 0         # 累计失败数(不清零,供观测/告警文案)
    state: str = "closed"           # closed(正常) / open(熔断) / half_open(试探)
    opened_at: float = 0.0          # 最近一次熔断的时刻

    def on_success(self) -> None:
        """一次成功:清零连续失败,回到 closed。"""
        self.consecutive_failures = 0
        self.state = "closed"

    def on_failure(self, *, now: float) -> bool:
        """记一次失败。返回 True 当且仅当这次失败【刚好】触发熔断(closed/half_open → open),
        供调用方"刚熔断"时主动告警一次(避免每拍重复告警)。"""
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.state != "open" and self.consecutive_failures >= self.threshold:
            self.state = "open"
            self.opened_at = now
            return True
        return False

    def allow(self, *, now: float) -> bool:
        """现在是否允许尝试。open 且冷却已到→转 half_open 放一次试探;open 且冷却未到→False(快速失败)。"""
        if self.state == "open" and now - self.opened_at >= self.cooldown_seconds:
            self.state = "half_open"
        return self.state != "open"

    def snapshot(self) -> dict[str, object]:
        """落盘/上报用的可序列化快照。"""
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "opened_at": self.opened_at,
        }


# ---------------------------------------------------------------------------
# 滑动窗口速率:把"单位时间出现频率"变成可判突变的量(暴力破解/扫描/DDoS 的本质特征)。
# ---------------------------------------------------------------------------
@dataclass
class RateWindow:
    """滑动时间窗口事件计数。同一主体(IP/用户/事件类型)在窗口内的次数超阈值 = 突发。

    与"新实体/罕见值"基线互补:新实体抓"没见过的",速率窗口抓"见过但突然高频的"
    (如已知 IP 突然每秒上百次登录失败 = 爆破)。对照 通道运行时 固定窗口 + 长期助手 watch 窗口。
    """

    window_seconds: float = 10.0
    timestamps: deque = field(default_factory=deque)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()

    def observe(self, *, now: float) -> int:
        """记一次事件并淘汰窗口外的,返回当前窗口内计数(含本次)。"""
        self.timestamps.append(now)
        self._evict(now)
        return len(self.timestamps)

    def count(self, *, now: float) -> int:
        """只读当前窗口内计数(先淘汰过期),不记新事件。"""
        self._evict(now)
        return len(self.timestamps)

    def is_burst(self, *, now: float, limit: int) -> bool:
        """当前窗口内计数是否达到/超过 limit(即突发)。"""
        return self.count(now=now) >= limit


@dataclass
class BurstTracker:
    """按实体(IP/用户/事件类型)的滑动窗口频率突变检测。逻辑时钟由 observe 自增(不依赖 wall clock,
    便于按"日志记录流"判频率)。某实体在 window 个事件内出现 >= threshold 次 = 突发——抓"已知实体
    突然高频"(暴力破解/端口扫描/DDoS),与基线"新实体/罕见值"互补。LRU 限活跃实体数防内存爆。
    对照 通道运行时 fixed-window-rate-limit / 长期助手 watch 窗口(8 次/10s)。"""

    window: int = 200
    threshold: int = 20
    capacity: int = 1024
    _windows: "OrderedDict[str, RateWindow]" = field(default_factory=OrderedDict)
    _seq: int = 0

    def observe(self, entity: str) -> bool:
        """记一次实体出现(逻辑时钟自增),返回 True 当该实体在当前滑动窗口内达到突发阈值。"""
        self._seq += 1
        rw = self._windows.get(entity)
        if rw is None:
            if len(self._windows) >= self.capacity:
                self._windows.popitem(last=False)  # LRU:淘汰最久未活动的实体
            rw = RateWindow(window_seconds=float(self.window))
            self._windows[entity] = rw
        else:
            self._windows.move_to_end(entity)
        return rw.observe(now=self._seq) >= self.threshold
