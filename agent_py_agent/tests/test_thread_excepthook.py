"""审计 #19(excepthook 部分,medium/稳定)真测:后台线程未捕获异常落日志,不再静默死亡。

守护线程(supervisor/worker/心跳/reaper/唤醒器)抛未捕获异常时默认只打 stderr、生产里常被吞 →
任务不再被消费却无信号。真起会抛异常的后台线程,断言 hook 把它落到 logging(可告警);SystemExit
正常退出不误告警;重复安装幂等。学 代理运行时 process.on('uncaughtException')。
"""

from __future__ import annotations

import logging
import threading

from agent_py_agent.agent.common import thread_hooks


def _reset_hook():
    original = threading.excepthook
    thread_hooks._installed = False
    return original


def test_thread_excepthook_logs_uncaught(caplog) -> None:
    original = _reset_hook()
    try:
        thread_hooks.install_thread_excepthook()
        assert threading.excepthook is thread_hooks._thread_excepthook

        def boom() -> None:
            raise RuntimeError("后台炸了")

        with caplog.at_level(logging.ERROR, logger="agent.threads"):
            t = threading.Thread(target=boom, name="boom-thread")
            t.start()
            t.join()
        messages = [r.getMessage() for r in caplog.records]
        assert any("boom-thread" in m and "后台炸了" in m for m in messages)  # 未捕获异常被落日志
    finally:
        threading.excepthook = original
        thread_hooks._installed = False


def test_system_exit_not_logged(caplog) -> None:
    original = _reset_hook()
    try:
        thread_hooks.install_thread_excepthook()

        def clean_exit() -> None:
            raise SystemExit(0)

        with caplog.at_level(logging.ERROR, logger="agent.threads"):
            t = threading.Thread(target=clean_exit, name="exit-thread")
            t.start()
            t.join()
        assert not any("exit-thread" in r.getMessage() for r in caplog.records)  # 正常退出不误告警
    finally:
        threading.excepthook = original
        thread_hooks._installed = False


def test_install_is_idempotent() -> None:
    original = _reset_hook()
    try:
        thread_hooks.install_thread_excepthook()
        hook1 = threading.excepthook
        thread_hooks.install_thread_excepthook()  # 第二次无操作
        assert threading.excepthook is hook1
    finally:
        threading.excepthook = original
        thread_hooks._installed = False
