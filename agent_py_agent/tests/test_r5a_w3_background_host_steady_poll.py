"""R5A-W3：托管后台进程运行满一分钟后按常态间隔观察，长驻服务的 host 不再每秒4次读记录。"""
from __future__ import annotations

from agent_py_agent.agent.tooling import background_process_host as host

# 模拟的托管时长（秒）：一个长驻 dev server 运行一小时
SIMULATED_SECONDS = 3600.0
# 一分钟后每分钟允许的最多观察次数
STEADY_WAKEUPS_PER_MINUTE_MAX = 60


# 函数用途: 按 host 的睡眠退避函数累加，统计模拟时长内的观察（唤醒）次数。
def _wakeups(start: float, end: float) -> int:
    elapsed, count = start, 0
    while elapsed < end:
        elapsed += host._monitor_poll_interval(elapsed)
        count += 1
    return count


# 函数用途: 前一分钟保持原灵敏度（首秒50ms、十秒内150ms、一分钟内250ms），之后切到常态间隔。
def test_first_minute_keeps_short_command_sensitivity():
    assert host._monitor_poll_interval(0.0) == host._HOST_POLL_SECONDS
    assert host._monitor_poll_interval(5.0) == 0.15
    assert host._monitor_poll_interval(59.9) == 0.25
    assert host._monitor_poll_interval(60.0) == host._HOST_STEADY_POLL_SECONDS >= 1.0


# 函数用途: 一小时长驻服务在一分钟后的观察次数不超过每分钟60次（旧实现每分钟240次）。
def test_long_running_host_wakeups_are_bounded_after_the_first_minute():
    steady = _wakeups(60.0, SIMULATED_SECONDS)
    assert steady <= STEADY_WAKEUPS_PER_MINUTE_MAX * (SIMULATED_SECONDS - 60.0) / 60.0, steady
