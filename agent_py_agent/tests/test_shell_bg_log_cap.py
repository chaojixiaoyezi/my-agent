"""审计 #16(部分)修复真测:后台命令日志字节上限——失控/恶意命令写满磁盘前被 killpg 杀掉。

真起一个无限写日志的后台进程,给小的 max_bytes,断言 watchdog 超限即杀整组(进程终止);正常命令不受影响。
学 终端交互 sizeWatchdog。补 #14 留的"后台日志无字节上限"缺口。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agent_py_agent.agent.tooling.background_process_host import (
    start_background_process_host,
)
from agent_py_agent.agent.tooling.shell import _kill_process_group, _LogSizeWatchdog


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组语义(用户:不管 Windows)")
def test_watchdog_kills_runaway_log(tmp_path) -> None:
    log = tmp_path / "j.log"
    handle = log.open("wb")
    proc = subprocess.Popen(  # 无限写日志,模拟失控/恶意命令
        "while true; do echo spamspamspam; done", shell=True, stdout=handle, start_new_session=True,
    )
    try:
        _LogSizeWatchdog(proc, log, max_bytes=50_000, interval=0.05).start()
        end = time.monotonic() + 5.0
        while proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert proc.poll() is not None  # 被 watchdog 超限杀掉(没无限写满磁盘)
    finally:
        handle.close()
        if proc.poll() is None:
            _kill_process_group(proc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX")
def test_watchdog_lets_normal_command_finish(tmp_path) -> None:
    log = tmp_path / "j.log"
    handle = log.open("wb")
    proc = subprocess.Popen("echo hi-bg", shell=True, stdout=handle, start_new_session=True)
    _LogSizeWatchdog(proc, log, max_bytes=50_000, interval=0.05).start()
    proc.wait(timeout=5)
    handle.close()
    assert proc.returncode == 0  # 正常小输出命令不被杀,正常结束
    assert "hi-bg" in log.read_text(encoding="utf-8")


def test_detached_host_keeps_enforcing_log_cap_after_launcher_scope(tmp_path) -> None:
    """独立 host 自己守日志上限，不依赖已经结束的子代理 watchdog 线程。"""

    log = tmp_path / "hosted.log"
    hosted = start_background_process_host(
        [
            sys.executable,
            "-u",
            "-c",
            "while True: print('x' * 1000, flush=True)",
        ],
        cwd=tmp_path,
        log_path=log,
        env=dict(os.environ),
        max_log_bytes=20_000,
    )
    try:
        hosted.process.wait(timeout=6)
        state = json.loads(hosted.state_file.read_text(encoding="utf-8"))
        assert state["status"] == "exited"
        assert state["reason"] == "log_limit_exceeded"
    finally:
        if hosted.process.poll() is None:
            _kill_process_group(hosted.process)
