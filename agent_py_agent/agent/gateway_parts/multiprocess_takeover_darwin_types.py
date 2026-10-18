# LLM: 按Darwin SDK sys/proc.h、sys/sysctl.h、sys/vm.h声明公开LP64 ABI；字段偏移由ctypes对齐，不用字节切片魔数。
# 模块用途: 描述KERN_PROC_ALL完整记录；调用方须先检查LP64和sizeof，不能把未知ABI当完整表。
import ctypes as c


# LLM: sys/time.h的timeval，Darwin long秒与int微秒；保留尾部自然对齐。
# 类用途: 描述内核出生时间，转换时与P1秒:六位微秒一致。
class Timeval(c.Structure):
    _fields_ = [("tv_sec", c.c_long), ("tv_usec", c.c_int)]


# LLM: extern_proc.p_un的链指针与timeval共用布局，不把指针误当出生信息。
# 类用途: 表达公开内核记录的出生时间联合体。
class ProcUnion(c.Union):
    _fields_ = [("links", c.c_void_p * 2), ("p_starttime", Timeval)]


# LLM: itimerval由两个timeval构成；仅为保证后续字段自然偏移，不消费计时器。
# 类用途: 描述进程记录的定时器占位类型。
class Itimerval(c.Structure):
    _fields_ = [("it_interval", Timeval), ("it_value", Timeval)]


# LLM: sys/proc.h extern_proc全部公开字段按声明顺序排列；MAXCOMLEN=16，sigset_t/fixpt_t均32位。
# 类用途: 描述进程PID、状态、出生时间及其余ABI字段，不读取命令或内核指针内容。
class ExternProc(c.Structure):
    _fields_ = [
        ("p_un", ProcUnion), ("p_vmspace", c.c_void_p), ("p_sigacts", c.c_void_p),
        ("p_flag", c.c_int), ("p_stat", c.c_char), ("p_pid", c.c_int),
        ("p_oppid", c.c_int), ("p_dupfd", c.c_int), ("user_stack", c.c_void_p),
        ("exit_thread", c.c_void_p), ("p_debugger", c.c_int), ("sigwait", c.c_int),
        ("p_estcpu", c.c_uint), ("p_cpticks", c.c_int), ("p_pctcpu", c.c_uint),
        ("p_wchan", c.c_void_p), ("p_wmesg", c.c_void_p), ("p_swtime", c.c_uint),
        ("p_slptime", c.c_uint), ("p_realtimer", Itimerval), ("p_rtime", Timeval),
        ("p_uticks", c.c_uint64), ("p_sticks", c.c_uint64), ("p_iticks", c.c_uint64),
        ("p_traceflag", c.c_int), ("p_tracep", c.c_void_p), ("p_siglist", c.c_int),
        ("p_textvp", c.c_void_p), ("p_holdcnt", c.c_int), ("p_sigmask", c.c_uint),
        ("p_sigignore", c.c_uint), ("p_sigcatch", c.c_uint), ("p_priority", c.c_ubyte),
        ("p_usrpri", c.c_ubyte), ("p_nice", c.c_char), ("p_comm", c.c_char * 17),
        ("p_pgrp", c.c_void_p), ("p_addr", c.c_void_p), ("p_xstat", c.c_ushort),
        ("p_acflag", c.c_ushort), ("p_ru", c.c_void_p)]


# LLM: sys/sysctl.h _pcred pc_lock公开为72字节opaque；uid/gid均32位，无用户身份过滤。
# 类用途: 保留eproc凭据结构的ABI，不向证据输出凭据。
class Pcred(c.Structure):
    _fields_ = [("pc_lock", c.c_char * 72), ("pc_ucred", c.c_void_p),
                ("p_ruid", c.c_uint), ("p_svuid", c.c_uint), ("p_rgid", c.c_uint),
                ("p_svgid", c.c_uint), ("p_refcnt", c.c_int)]


# LLM: sys/sysctl.h _ucred NGROUPS=16；有效UID不决定执行树成员资格，防setuid漏查。
# 类用途: 描述系统表凭据占位，任何用户的组/会话成员都必须保留。
class Ucred(c.Structure):
    _fields_ = [("cr_ref", c.c_int32), ("cr_uid", c.c_uint),
                ("cr_ngroups", c.c_short), ("cr_groups", c.c_uint * 16)]


# LLM: sys/vm.h用户态vmspace占位声明，指针自然对齐；不解引用地址。
# 类用途: 保持eproc父PID之前的ABI布局。
class Vmspace(c.Structure):
    _fields_ = [("dummy", c.c_int32), ("dummy2", c.c_void_p),
                ("dummy3", c.c_int32 * 5), ("dummy4", c.c_void_p * 3)]


# LLM: sys/sysctl.h eproc，segsz_t/dev_t/pid_t均32位；WMESGLEN=7、COMAPT_MAXLOGNAME=12。
# 类用途: 读取内核父PID与进程组，保留公开记录其余布局。
class Eproc(c.Structure):
    _fields_ = [("e_paddr", c.c_void_p), ("e_sess", c.c_void_p), ("e_pcred", Pcred),
                ("e_ucred", Ucred), ("e_vm", Vmspace), ("e_ppid", c.c_int),
                ("e_pgid", c.c_int), ("e_jobc", c.c_short), ("e_tdev", c.c_int32),
                ("e_tpgid", c.c_int), ("e_tsess", c.c_void_p), ("e_wmesg", c.c_char * 8),
                ("e_xsize", c.c_int32), ("e_xrssize", c.c_short), ("e_xccount", c.c_short),
                ("e_xswrss", c.c_short), ("e_flag", c.c_int32), ("e_login", c.c_char * 12),
                ("e_spare", c.c_int32 * 4)]


# LLM: KERN_PROC_ALL公开数组元素，完整类型供sizeof/数组解析共同使用；不硬编码字段偏移。
# 类用途: 组合macOS完整进程记录，禁止部分记录当完整事实。
class KinfoProc(c.Structure):
    _fields_ = [("kp_proc", ExternProc), ("kp_eproc", Eproc)]
