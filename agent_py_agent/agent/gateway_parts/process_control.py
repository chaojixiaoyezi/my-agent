
from __future__ import annotations

"""wraps OS-level gateway process liveness and termination operations.

这个文件只处理进程层面的事情：pid 是否还活着、怎么礼貌停止、停止后等多久。
这样 gateway 的业务队列逻辑不用知道 Windows 和 Unix 的系统调用差异。
"""

import os
import signal
import time


def is_pid_alive(pid: int) -> bool:

    if pid <= 0:
        return False
    if os.name == "nt":
        from ..common.heartbeat import windows_process_alive

        return windows_process_alive(pid) is not False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:

    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def wait_for_pid_exit(pid: int, timeout: float) -> bool:

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)
