# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""wraps OS-level gateway process liveness and termination operations.

这个文件只处理进程层面的事情：pid 是否还活着、怎么礼貌停止、停止后等多久。
这样 gateway 的业务队列逻辑不用知道 Windows 和 Unix 的系统调用差异。
"""

import ctypes
import os
import signal
import time


# LLM: is_pid_alive 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 判断pidalive条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def is_pid_alive(pid: int) -> bool:

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


# LLM: terminate_pid 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理terminatepid相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def terminate_pid(pid: int) -> None:

    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


# LLM: wait_for_pid_exit 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进pidexit的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def wait_for_pid_exit(pid: int, timeout: float) -> bool:

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)
