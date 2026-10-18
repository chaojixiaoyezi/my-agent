# LLM: Darwin全用户sysctl枚举，逐PID核对不减；只把函数绑定移出循环，同源birth和消失拒绝不改。
# 模块用途: 获取macOS整树身份，运输消失最后事实但不放宽不一致拒绝。
import ctypes
import errno
import os

from .multiprocess_executor_identity import _ProcBSDInfo
from .multiprocess_takeover_darwin_types import KinfoProc
from .multiprocess_takeover_probe import DisappearedProcessSnapshot, UnstableProcessSnapshot


# LLM: 全用户KERN_PROC_ALL逐PID补SID和同源birth，不过滤UID/PPID；每轮绑定reader而非每PID，无信号。
# 函数用途: 完整读取整机身份及消失诊断，省Python绑定开销但不省内核核对。
def darwin_process_rows() -> dict:
    values = _sysctl_table()
    pids = [value.kp_proc.p_pid for value in values]
    if len(pids) != len(set(pids)) or any(pid < 0 for pid in pids):
        raise ValueError("darwin_process_table_invalid")
    rows, lost = {}, []
    birth_reader = _birth_reader(_sysctl_query())
    for value in values:
        if value.kp_proc.p_pid > 0:
            _append_snapshot_row(value, rows, lost, birth_reader)
    if lost:
        raise DisappearedProcessSnapshot(rows, lost)
    return rows


# LLM: 使用本轮绑定reader逐PID核验，消失只追加birth/关系诊断，复用/权限/ABI仍整表失败。
# 函数用途: 保留完整核验行和消失进程最后一次同源事实，不补猜SID。
def _append_snapshot_row(value, rows: dict, lost: list, birth_reader=None) -> None:
    try:
        row = _sysctl_row(value, birth_reader)
    except DisappearedProcessSnapshot:
        row = None
    if row is None:
        lost.append({"pid": value.kp_proc.p_pid, "birth": _kernel_birth(value),
                     "ppid": value.kp_eproc.e_ppid, "pgid": value.kp_eproc.e_pgid})
    else:
        rows[row["pid"]] = row


# LLM: 只支持SDK公开LP64 648字节ABI，绑定一次query作完整重试；未知ABI不得按整除放行。
# 函数用途: 在内核读前核对声明，三次完整两阶段重试后仍增长则报告未知。
def _sysctl_table():
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(KinfoProc) != 648:
        raise ValueError("darwin_process_abi_unconfirmed")
    query = _sysctl_query()
    # 每次增长都从大小查询重新开始，最多三次防止永远追赶进程创建。
    for _ in range(3):
        values = _read_sysctl_table(query)
        if values is not None:
            return values
    raise ValueError("sysctl_enumeration_unstable")


# LLM: ENOMEM只表示两阶段增长，成功也须核对真实长度/容量/整除；其它失败不能变成空。
# 函数用途: 完整读取一次内核表；16MiB仅拒绝上限，不截取或输出原始缓冲。
def _read_sysctl_table(query):
    mib, size = (ctypes.c_int * 4)(1, 14, 0, 0), ctypes.c_size_t()
    ctypes.set_errno(0)
    if query(mib, 4, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl_enumeration_failed")
    if not 0 < size.value <= 16 * 1024 * 1024:
        raise ValueError("sysctl_enumeration_size_invalid")
    buffer = ctypes.create_string_buffer(size.value)
    ctypes.set_errno(0)
    if query(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        if ctypes.get_errno() == errno.ENOMEM:
            return None
        raise OSError(ctypes.get_errno(), "sysctl_enumeration_failed")
    if size.value > ctypes.sizeof(buffer):
        return None
    if size.value == 0 or size.value % ctypes.sizeof(KinfoProc):
        raise ValueError("sysctl_enumeration_incomplete")
    return (KinfoProc * (size.value // ctypes.sizeof(KinfoProc))).from_buffer_copy(buffer)


# LLM: 原SZOMB/ESRCH及同源birth/PGID义务不变，注入reader只省绑定，不省任一SID后的内核复核。
# 函数用途: 转成六字段事实，僵尸不虚构会话，活行必须核对出生身份。
def _sysctl_row(value, birth_reader=None):
    proc, extra = value.kp_proc, value.kp_eproc
    birth = _kernel_birth(value)
    state = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}.get(ord(proc.p_stat))
    if state is None:
        raise ValueError("darwin_process_identity_invalid")
    try:
        session = os.getsid(proc.p_pid)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
        if state != "Z":
            return None
        session = None
    if session is not None and (birth_reader or _sysctl_birth)(proc.p_pid, extra.e_pgid) != birth:
        raise UnstableProcessSnapshot("process_reused_during_observation")
    return {"pid": proc.p_pid, "ppid": extra.e_ppid, "pgid": extra.e_pgid, "sid": session,
            "birth": birth, "state": state}


# LLM: 同源KERN_PROC_PID逐PID不减，显式本轮query省绑定；PGID/出生/消失/权限/ABI拒绝不变，无信号。
# 函数用途: 在getsid之后复核出生和原组身份，防新PID或新会话接到旧快照。
def _sysctl_birth(pid: int, group: int, query=None) -> str:
    query = _sysctl_query() if query is None else query
    mib, value = (ctypes.c_int * 4)(1, 14, 1, pid), KinfoProc()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    ctypes.set_errno(0)
    result = query(mib, 4, ctypes.byref(value), ctypes.byref(size), None, 0)
    if result != 0:
        if ctypes.get_errno() in {errno.ESRCH, errno.ENOENT}:
            raise DisappearedProcessSnapshot()
        raise OSError(ctypes.get_errno(), "sysctl_identity_failed")
    if size.value == 0:
        raise DisappearedProcessSnapshot()
    if size.value != ctypes.sizeof(value) or value.kp_proc.p_pid != pid:
        raise ValueError("sysctl_identity_incomplete")
    if value.kp_eproc.e_pgid != group:
        raise UnstableProcessSnapshot("process_group_changed_during_observation")
    return _kernel_birth(value)


# LLM: 每次观察独立绑定errno-aware函数，不跨轮缓存事实或共享可变reader；无内核读取副作用。
# 函数用途: 初始化SDK sysctl签名，让同一轮逐PID复核复用绑定。
def _sysctl_query():
    query = ctypes.CDLL(None, use_errno=True).sysctl
    query.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    return query


# LLM: 闭包只冻结当前轮函数，不冻结内核事实；每次调用仍真实KERN_PROC_PID核birth/PGID。
# 函数用途: 建立双参数出生读取器，省每PID的CDLL加载与原型声明。
def _birth_reader(query):
    return lambda pid, group: _sysctl_birth(pid, group, query)


# LLM: 全表与单PID复核共用同一出生格式/合法性，不读取UID或命令，不以未知出生签发stopped。
# 函数用途: 把内核timeval转成P1秒:六位微秒身份。
def _kernel_birth(value) -> str:
    birth = value.kp_proc.p_un.p_starttime
    if birth.tv_sec <= 0 or not 0 <= birth.tv_usec < 1000000:
        raise ValueError("darwin_process_identity_invalid")
    return f"{birth.tv_sec}:{birth.tv_usec:06d}"


# LLM: 复用P1固定136字节结构与秒:微秒出生格式；BSDInfo pid/ppid/pgid均是内核字段。
# 函数用途: 读取一个macOS进程的精确身份；只有明确已消失可从快照略去。
def _darwin_row(library, pid: int):
    query = library.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = _ProcBSDInfo()
    ctypes.set_errno(0)
    count = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if count != ctypes.sizeof(info):
        if ctypes.get_errno() == errno.ESRCH:
            return None
        raise OSError(ctypes.get_errno(), "proc_pidinfo_incomplete")
    if info.prefix[3] != pid or info.start_sec == 0:
        raise ValueError("darwin_process_identity_invalid")
    try:
        session = os.getsid(pid)
    except ProcessLookupError:
        return None
    return {"pid": pid, "ppid": int(info.prefix[4]), "pgid": int(info.scope[1]), "sid": session,
            "birth": f"{info.start_sec}:{info.start_usec:06d}", "state": "Z" if info.prefix[1] == 5 else "S"}
