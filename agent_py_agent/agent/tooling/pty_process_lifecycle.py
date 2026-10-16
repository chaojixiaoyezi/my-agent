# LLM: 原PTY退出身份与清退合同不变；Darwin结构长度/偏移明确为字节、SZOMB为平台状态码，改动需核原生命周期测试。
# 模块用途: 保留真实PTY组长的内核退出身份供原注册表清退，不用根退出推断整树死亡。
"""保留PTY组长的内核退出身份，直到原注册表完成树快照和清退。"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time

from .process_registry import ProcessTerminationReceipt, capture_process_birth_token


# LLM: 只包装本次Popen，未清退前poll/wait只观察WNOWAIT；不能把用户读状态变成回收PID或树退出证明。
# 类用途: 自然退出时暂不回收组长，保留它的PID/出生身份供原进程树清理器核对。
class PtyOwnedProcess:
    # LLM: raw必须尚未poll/wait；包装器不另开进程、不改变沙箱、命令、工作目录或终端描述符。
    # 函数用途: 冻结真实进程句柄与出生身份。
    def __init__(self, raw: subprocess.Popen) -> None:
        self.raw = raw
        self.pid = raw.pid
        self.birth_token = capture_process_birth_token(raw.pid)
        self._lock = threading.RLock()
        self._observed_code = None
        self.termination: ProcessTerminationReceipt | None = None
        self.retirement_processes: dict[int, str] = {}
        self.retirement_complete = True
        self._identity_intact = bool(self.birth_token)

    # LLM: 不回收原子进程；仅接受内核退出/信号终态，读取失败不伪造成功并取消后续树确认资格。
    # 函数用途: 返回真实父进程状态，同时让注册表继续负责尚未清退的进程树。
    def poll(self):
        if not self._lock.acquire(blocking=False):
            # 清退正在占锁时只读既有状态，不让输入的期限被后台清理延长。
            return self.raw.returncode if self.raw.returncode is not None else self._observed_code
        try:
            if self.raw.returncode is not None:
                return self.raw.returncode
            if self._observed_code is not None:
                return self._observed_code
            return self._observe_exit_code()
        finally:
            self._lock.release()

    # LLM: 调用方持状态锁；观察失败取消完整身份资格，绝不把raw.poll当整树清退证明。
    # 函数用途: 读取内核退出事件并保存真实退出码。
    def _observe_exit_code(self):
        if not hasattr(os, "waitid") and sys.platform == "darwin":
            return self._observe_darwin_exit_code()
        try:
            event = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (OSError, AttributeError):
            self._identity_intact = False
            return self.raw.poll()
        if event is None or event.si_pid != self.pid:
            return self._observed_code
        if event.si_code == os.CLD_EXITED:
            self._observed_code = event.si_status
        elif event.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
            self._observed_code = -event.si_status
        return self._observed_code

    # LLM: macOS 的 Python 没有 os.waitid；改读内核 kinfo_proc 的僵尸态和 wait 状态，同样不回收 PID。
    #   读不到或结构自检失败时与 waitid 失败同样处理：取消身份资格，绝不把 raw.poll 当整树清退证明。
    # 函数用途: 在 Mac 上不回收组长地观察自然退出，让终端会话能被正常确认清退、释放容量。
    def _observe_darwin_exit_code(self):
        try:
            status = darwin_zombie_wait_status(self.pid)
        except OSError:
            self._identity_intact = False
            return self.raw.poll()
        if status is not None:
            self._observed_code = os.waitstatus_to_exitcode(status)
        return self._observed_code

    # LLM: 兼容原Popen单根kill入口；持锁核验仍属本次未回收子进程，不回收PID、不伪造树清退回执。
    # 函数用途: 仅结束本次仍在运行的组长，后代仍必须由retire负责。
    def kill(self):
        import signal

        with self._lock:
            if self.poll() is not None:
                return
            if not self._identity_intact:
                raise ProcessLookupError("PTY process identity unavailable")
            os.kill(self.pid, signal.SIGKILL)

    # LLM: 与Popen.wait相同的退出码/TimeoutExpired合同，但原PID回收必须留给清退步骤；等待不持状态锁。
    # 函数用途: 有界等父进程自然退出，不提前丢掉后代核验所需的组长身份。
    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.raw.args, timeout)
            time.sleep(.01)
        return code

    # LLM: 原责任字典跨次传给唯一终止器；发信号前和补采时即保存后代，异常不丢责任，确认后才回收原Popen。
    # 函数用途: 重试必须结清此前观察的脱组后代，不能以本次小快照取代旧欠账。
    def retire(self, terminate):
        with self._lock:
            if self.termination is not None and self.termination.confirmed:
                return self.termination
            if not self._identity_intact or self.raw.returncode is not None:
                return ProcessTerminationReceipt("identity_unavailable", False, self.poll(), 0, (self.pid,))
            self.termination = terminate(self.pid, self, expected_birth_token=self.birth_token)
            if self.termination.confirmed:
                self.raw.wait(timeout=2)
            return self.termination


# 常量用途: 当前Darwin kinfo_proc的原ABI长度（字节），实际读取长度不符必须拒绝。
_DARWIN_KINFO_PROC_SIZE_BYTES = 648
# 常量用途: Darwin原p_stat在kinfo_proc中的字节偏移；与下方实际结构/PID自检共用。
_DARWIN_P_STAT_OFFSET_BYTES = 36
# 常量用途: Darwin原p_pid在kinfo_proc中的字节偏移，用来核实返回的确切PID。
_DARWIN_P_PID_OFFSET_BYTES = 40
# 常量用途: Darwin原p_xstat等待状态在kinfo_proc中的字节偏移，不是秒或容量。
_DARWIN_P_XSTAT_OFFSET_BYTES = 280
# 常量用途: Darwin SZOMB原平台进程状态码，不是可调数字；仅实际僵尸态允许读等待状态。
_DARWIN_ZOMBIE_STATE_CODE = 5


# LLM: 只读sysctl原kinfo_proc；长度/偏移均为原ABI字节、SZOMB为平台状态码，实际长度与p_pid自检不过报错，数值/观察语义不变。
#   返回 None 表示仍在运行；返回 int 是僵尸的原始 wait 状态；进程已被回收或读失败抛 OSError。
# 函数用途: 给 macOS 提供与 waitid(WNOWAIT) 等价的“已退出但未回收”观察，供 PTY 组长身份保留使用。
def darwin_zombie_wait_status(pid: int) -> int | None:
    raw = _darwin_kinfo_proc(pid)
    if len(raw) != _DARWIN_KINFO_PROC_SIZE_BYTES or struct.unpack_from("i", raw, _DARWIN_P_PID_OFFSET_BYTES)[0] != pid:
        raise OSError("DARWIN_KINFO_PROC_UNVERIFIED")
    if raw[_DARWIN_P_STAT_OFFSET_BYTES] != _DARWIN_ZOMBIE_STATE_CODE:
        return None
    return struct.unpack_from("H", raw, _DARWIN_P_XSTAT_OFFSET_BYTES)[0]


# LLM: 两次 sysctl（先取长度再取内容）；任何失败都抛 OSError，长度为 0 表示该 PID 已不存在。
# 函数用途: 读取单个进程的内核 kinfo_proc 原始字节，不起子进程、不改变进程状态。
def _darwin_kinfo_proc(pid: int) -> bytes:
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl kern.proc.pid size failed")
    buffer = ctypes.create_string_buffer(max(size.value, 1))
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl kern.proc.pid failed")
    return buffer.raw[:size.value]
