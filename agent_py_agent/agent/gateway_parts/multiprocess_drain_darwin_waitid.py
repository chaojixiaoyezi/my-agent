# LLM: 仅原C直接child的P6 waitid公开LP64 ABI；同原WNOWAIT zombie完整sysctl核出生/parent后才能消费actualreap，不读Popen缓存、不证明外native整树；同步ABI/CLI drain tests。
# 模块用途: 用标准库调用macOS原内核waitid，给已有直接worker观察保留真实退出状态。
import ctypes
import errno
import os
import signal
import sys
from typing import NamedTuple

# 以下固定值属于 Darwin 公开 ABI 协议，不属于参数中心的可调数值；名称按原 TYPE/PROTOCOL/CODE 规则标明。
# 公开 waitid 的 PID 查询类型；是固定 ABI 类型码，不是可调 PID 或预算。
_DARWIN_WAIT_PID_TYPE = 1
# 公开 waitid 的退出事件选项位；保持 SDK 原值。
_DARWIN_WEXITED_PROTOCOL_OPTION = 0x04
# 公开 waitid 的非阻塞选项位；不表示轮询时间。
_DARWIN_WNOHANG_PROTOCOL_OPTION = 0x01
# 公开 waitid 的保留子进程选项位；原观察阶段禁止提前回收。
_DARWIN_WNOWAIT_PROTOCOL_OPTION = 0x20
# 公开 siginfo.si_code 的正常退出状态码。
_DARWIN_CLD_EXITED_CODE = 1
# 公开 siginfo.si_code 的信号终止状态码。
_DARWIN_CLD_KILLED_CODE = 2
# 公开 siginfo.si_code 的信号终止并转储状态码。
_DARWIN_CLD_DUMPED_CODE = 3


# LLM: SDK sys/signal.h完整siginfo_t自然LP64布局，零初始化si_pid；不以部分bytes或未知32bit布局解码。
# 类用途: 接收同一次真实waitid系统调用返回的公开结构。
class _DarwinSigInfo(ctypes.Structure):
    _fields_ = [('si_signo', ctypes.c_int), ('si_errno', ctypes.c_int), ('si_code', ctypes.c_int),
                ('si_pid', ctypes.c_int), ('si_uid', ctypes.c_uint), ('si_status', ctypes.c_int),
                ('si_addr', ctypes.c_void_p), ('si_value', ctypes.c_uint64), ('si_band', ctypes.c_long),
                ('reserved', ctypes.c_ulong * 7)]


# LLM: 只有原waitid调用生产本结果；此DTO不授来源或执行权，原observer仍要求原对象/活C和同Popen出生。
# 类用途: 给原P6消费同一次内核返回的PID、code和status。
class DarwinWaitInfo(NamedTuple):
    si_pid: int
    si_code: int
    si_status: int
    wait_status: int | None = None


# LLM: 只在正count原observer安装前调用；符号或公开LP64布局缺失即不可用，不对平台缺能力伪confirmed。
# 函数用途: 核本平台真实waitid入口，不spawn、signal或reap。
def darwin_waitid_available():
    try:
        _waitid_query()
        return True
    except (OSError, ValueError, AttributeError):
        return False


# LLM: 明文SDK接口及sizeof/offset核验，不试另一路wait或返回码；Count0不调用本入口。
# 函数用途: 绑定当前进程内Darwin公开waitid函数，拒绝未知ABI。
def _waitid_query():
    if (sys.platform != 'darwin' or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(_DarwinSigInfo) != 104
            or _DarwinSigInfo.si_pid.offset != 12 or _DarwinSigInfo.si_status.offset != 20):
        raise ValueError('drain_darwin_waitid_abi_unavailable')
    query = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).waitid
    query.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_DarwinSigInfo), ctypes.c_int]
    query.restype = ctypes.c_int
    return query


# LLM: WNOHANG前置si_pid=0；peek保zombie、False用原waitpid实际raw收据；ECHILD/权限/布局损坏未知，None只表示尚无EXIT。
# 函数用途: 对原直接child执行一次真实waitid，不从Popen.returncode回填事实。
def darwin_waitid(pid, *, nowait):
    query = _waitid_query()
    if type(pid) is not int or pid <= 0 or type(nowait) is not bool:
        raise ValueError('drain_original_child_pid_required')
    if not nowait:
        return _darwin_reap_child(pid)
    info = _DarwinSigInfo()
    options = _DARWIN_WEXITED_PROTOCOL_OPTION | _DARWIN_WNOHANG_PROTOCOL_OPTION | (_DARWIN_WNOWAIT_PROTOCOL_OPTION if nowait else 0)
    ctypes.set_errno(0)
    if query(_DARWIN_WAIT_PID_TYPE, pid, ctypes.byref(info), options) != 0:
        number = ctypes.get_errno()
        raise OSError(number, 'drain_darwin_waitid_failed')
    if info.si_pid == 0:
        return None
    if (info.si_pid != pid or info.si_signo != signal.SIGCHLD
            or info.si_code not in {_DARWIN_CLD_EXITED_CODE, _DARWIN_CLD_KILLED_CODE, _DARWIN_CLD_DUMPED_CODE}):
        raise OSError(errno.EPROTO, 'drain_darwin_waitstatus_invalid')
    return DarwinWaitInfo(info.si_pid, info.si_code, info.si_status)


# LLM: 原libproc birth不支持zombie，必须消费公开KERN_PROC_PID同原类型的完整SZOMB/PID/直接C parent/birth；不可缺省或以消失签死亡。
# 函数用途: 在WNOWAIT仍保原孩子时准确核出生，不枚举、发送信号或回收。
def darwin_zombie_birth(pid):
    from .multiprocess_takeover_darwin_types import KinfoProc

    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(KinfoProc) != 648:
        raise ValueError('drain_darwin_child_abi_unavailable')
    query = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).sysctl
    query.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    mib, value = (ctypes.c_int * 4)(1, 14, 1, pid), KinfoProc()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    ctypes.set_errno(0)
    if query(mib, 4, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'drain_darwin_child_birth_unavailable')
    stamp = value.kp_proc.p_un.p_starttime
    if (size.value != ctypes.sizeof(value) or value.kp_proc.p_pid != pid
            or value.kp_eproc.e_ppid != os.getpid() or ord(value.kp_proc.p_stat) != 5
            or stamp.tv_sec <= 0 or not 0 <= stamp.tv_usec < 1000000):
        raise ValueError('drain_darwin_original_zombie_required')
    return f'{stamp.tv_sec}:{stamp.tv_usec:06d}'


# LLM: 仅同原observer/Popen锁调用；waitpid raw收据保标准低8bit正常/SIG/core语义，ECHILD不算退出，不从缓存生成结果。
# 函数用途: 真正回收原直接孩子并保其实际系统调用状态。
def _darwin_reap_child(pid):
    actual_pid, status = os.waitpid(pid, os.WNOHANG)
    if actual_pid == 0:
        return None
    if actual_pid != pid:
        raise OSError(errno.EPROTO, 'drain_darwin_reap_pid_changed')
    if os.WIFEXITED(status):
        return DarwinWaitInfo(actual_pid, _DARWIN_CLD_EXITED_CODE, os.WEXITSTATUS(status), status)
    if os.WIFSIGNALED(status):
        code = _DARWIN_CLD_DUMPED_CODE if os.WCOREDUMP(status) else _DARWIN_CLD_KILLED_CODE
        return DarwinWaitInfo(actual_pid, code, os.WTERMSIG(status), status)
    raise OSError(errno.EPROTO, 'drain_darwin_reap_not_exit')
