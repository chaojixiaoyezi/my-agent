"""网关后台线程循环的永不停机硬保障单测。

真机实锤(1.10):网关进程被 systemd 拉着 active,但干活的后台主循环线程死了再也不回来。
`_gateway_background_main_loop` 的 tick 编排缝隙(种子重扫/owner 池同步/提交/汇报)原先
不在任何保护内,一个异常就杀死整条线程。撤修复即 FAIL:异常直接传播出循环函数。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent.backends.errors import ProviderTransientError


def test_background_main_loop_survives_tick_exceptions_and_reports_supply_error(monkeypatch, capsys) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    ticks = {"count": 0}

    class _ExplodingSupervisor:
        poll_interval = 0.01

        def __init__(self, context) -> None:
            pass

        def tick(self) -> bool:
            ticks["count"] += 1
            if ticks["count"] >= 3:
                stop_event.set()
            raise ProviderTransientError('HTTP 429: rate_limit_error "已达到 Token Plan 用量上限"')

    monkeypatch.setattr(gateway_loops, "_BackgroundMainSupervisor", _ExplodingSupervisor)
    monkeypatch.setattr(gateway_loops, "_start_orphan_reconcile_loop", lambda _context, _stop: None)
    monkeypatch.setattr(gateway_loops, "_start_owner_maintenance_loop", lambda _context, _stop: None)

    gateway_loops._gateway_background_main_loop(SimpleNamespace(), stop_event)  # 撤修复:异常在这里炸出

    assert ticks["count"] >= 3  # 循环在异常后继续跑,只随 stop_event 退出
    err = capsys.readouterr().err
    assert "[gateway-loop-error]" in err
    assert '"category": "provider_transient"' in err  # 联动分类修复:429 不再误标 programmer_bug
    assert '"recoverable": true' in err
    assert "programmer_bug" not in err


def test_heartbeat_loop_survives_write_failure(monkeypatch) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    writes = {"count": 0}

    def _failing_write(paths, agent, options, *, status, pid) -> None:
        writes["count"] += 1
        if writes["count"] >= 2:
            stop_event.set()
        raise OSError("disk full")

    monkeypatch.setattr(gateway_loops, "_write_gateway_heartbeat", _failing_write)
    context = SimpleNamespace(
        paths=SimpleNamespace(),
        agent=SimpleNamespace(config=SimpleNamespace(gateway_heartbeat_interval=0)),
        options=SimpleNamespace(),
    )

    gateway_loops._gateway_heartbeat_loop(context, stop_event)  # 撤修复:OSError 在这里炸出

    assert writes["count"] >= 2  # 写失败不杀心跳线程
