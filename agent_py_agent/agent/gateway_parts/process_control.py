from __future__ import annotations

"""LLM: wraps OS-level gateway process liveness and termination operations.

给人看的解释：
这个文件只处理进程层面的事情：pid 是否还活着、怎么礼貌停止、停止后等多久。
这样 gateway 的业务队列逻辑不用知道 Windows 和 Unix 的系统调用差异。
"""

import ctypes
import os
import signal
import time


def is_pid_alive(pid: int) -> bool:
    """LLM contract: check whether a pid currently belongs to a live process.

    Human version:
    `gateway status` 和 `gateway stop` 都需要知道后台进程是不是真的还活着。
    Windows 和 Unix 的检查方式不一样，所以统一收口在这里。
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:
    """LLM contract: send a normal termination signal to a live pid.

    Human version:
    这是“礼貌关停”，不是强杀。进程不存在时直接返回，让 stop/restart 命令保持幂等。
    """

    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    """LLM contract: poll for process exit until timeout.

    Human version:
    发出停止信号后等一会儿，看 gateway 有没有自己退出。超过时间还活着，调用方再决定
    要不要强制处理。
    """

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)
