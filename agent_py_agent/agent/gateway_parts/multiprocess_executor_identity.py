# LLM: P1a 出生身份只读内核；未知绝不退回 PID，不调用 ps，不改变 daemon_metadata 旧合同。
# 模块用途: 为等待垫片读取可跨 exec 保持的进程身份，未知由 P0 校验拒绝。
import ctypes
import sys
from pathlib import Path


# LLM: Linux 为 stat 第22字段 ticks，macOS 为 proc_bsdinfo 的秒:微秒；任何取值失败保持 None。
# 函数用途: 读取 PID 的内核出生标识。
def process_birth(pid: int):
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform == "darwin":
        return _darwin_birth(pid)
    if sys.platform == "linux":
        try:
            return linux_stat_birth(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None
    return None


# LLM: comm 允许空格和右括号，故从最后一个右括号后计数；读不到完整第22字段就未知。
# 函数用途: 解析 Linux stat 的出生 ticks。
def linux_stat_birth(content: str):
    try:
        birth = int(content.rsplit(")", 1)[1].split()[19])
        return birth if birth >= 0 else None
    except (IndexError, ValueError, TypeError):
        return None


# LLM: Darwin libproc.h 的 proc_bsdinfo 固定136字节布局，出生 timeval 位于120/128；不枚举进程。
# 类用途: 接收内核的进程出生时间，不读取用户命令、环境或凭据。
class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [("prefix", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32), ("scope", ctypes.c_uint32 * 6),
                ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


# LLM: PROC_PIDTBSDINFO=3 只读调用必须完整返回结构，失败或零出生值为未知；不启动辅助进程。
# 函数用途: 用原生 proc_pidinfo 读取 macOS 精确出生身份。
def _darwin_birth(pid: int):
    try:
        query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = _ProcBSDInfo()
        size = ctypes.sizeof(info)
        if query(pid, 3, 0, ctypes.byref(info), size) != size or info.start_sec == 0:
            return None
        return f"{info.start_sec}:{info.start_usec:06d}"
    except (OSError, AttributeError, OverflowError):
        return None
