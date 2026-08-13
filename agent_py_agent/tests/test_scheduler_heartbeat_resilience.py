from __future__ import annotations

"""HANDOFF P0-2 验收测试: 调度心跳守护线程瞬时错误不退出。

HANDOFF_reliability-gaps-20260813.md P0-2: SchedulerRunHeartbeat._loop 的
except Exception: return 让任何瞬时 DB 错误(连接抖动/锁冲突)直接退出守护
线程且无日志——此后该 run 租约无人续期。

修复语义: 瞬时异常记录结构化日志并继续循环(指数退避防紧循环);
alive=False(租约被回收)仍退出; stop 语义不变。
"""

import threading
import time

from agent_py_agent.agent.scheduler.service import SchedulerRunClaim, SchedulerRunHeartbeat


class _FlakyRepo:
    def __init__(self, *, fail_first: int = 0, alive: bool = True) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.alive = alive

    def heartbeat_run(self, run_id: str, claim_id: str, **kwargs: object) -> bool:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("db 瞬时抖动(测试注入)")
        return self.alive


def _claim() -> SchedulerRunClaim:
    return SchedulerRunClaim(
        run_id="run-heartbeat-test-0001",
        claim_id="claim-1",
        run={"name": "heartbeat-test"},
    )


def test_heartbeat_survives_transient_errors() -> None:
    """瞬时异常后守护线程不退出, 恢复后续续租(旧代码 except return 直接死)。"""
    repo = _FlakyRepo(fail_first=2)
    hb = SchedulerRunHeartbeat(repo, _claim(), lease_seconds=3)
    hb.start()
    # 覆盖: interval=1s, 失败退避 2^1=2s + 2^2=4s(退避后回 while 再等
    # interval), 第三次续租在 t≈9s
    time.sleep(10.5)
    assert repo.calls >= 3  # 前 2 次异常后仍继续调用(线程存活续租)
    hb.stop()


def test_heartbeat_exits_when_lease_lost() -> None:
    """租约失效(alive=False) -> 线程正常退出(正确退出语义不变)。"""
    repo = _FlakyRepo(alive=False)
    hb = SchedulerRunHeartbeat(repo, _claim(), lease_seconds=3)
    hb.start()
    hb._thread.join(timeout=3.0)  # type: ignore[attr-defined]
    assert not hb._thread.is_alive()  # type: ignore[attr-defined]
    hb.stop()


def test_heartbeat_stop_event_still_exits() -> None:
    """stop 语义不变: stop event 仍能退出循环(即使 heartbeat 正常)。"""
    repo = _FlakyRepo(alive=True)
    hb = SchedulerRunHeartbeat(repo, _claim(), lease_seconds=3)
    hb.start()
    hb.stop()
    assert not hb._thread.is_alive()  # type: ignore[attr-defined]


def test_heartbeat_stop_interrupts_failure_backoff() -> None:
    """失败退避期间 stop 仍生效(不因退避 sleep 卡住退出)。"""
    repo = _FlakyRepo(fail_first=100)  # 持续失败 -> 进入退避
    hb = SchedulerRunHeartbeat(repo, _claim(), lease_seconds=3)
    hb.start()
    time.sleep(1.2)  # 至少进入一次失败退避
    hb.stop()
    assert not hb._thread.is_alive()  # type: ignore[attr-defined]
