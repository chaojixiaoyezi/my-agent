# LLM: Darwin原C的无FD workloop保留R前PROC/FD关闭历史；guard仅为条件清理，不能认证代次。SPI失败保持unknown，调用者须核原source/attempt；同步native域及原故障测试。
# 模块用途: 让全部数字FD复用之后仍能读取原内核历史，并原子拒绝关闭复用的陌生FD。
import ctypes
import errno
import os
import secrets
import select

# Apple kevent_id选项是ABI位掩码，无量纲；只访问已显式创建的workloop，不补建。
_WORKLOOP_FLAGS = 0x400 | 0x20000 | 1 | 2
# Apple EV_DISPATCH2/EV_VANISHED是ABI位掩码，无量纲；关闭记录在原workloop留存。
_DISPATCH2, _VANISHED = 0x180, 0x200
# Apple knote status是ABI位掩码，无量纲；禁用且请求关闭通知，不允许消失记录认证原FD。
_DISABLED, _REQVANISH, _KN_VANISHED = 4, 0x200, 0x400
# Apple GUARD_DUP是ABI位掩码，无量纲；不加GUARD_CLOSE，裸close不能杀原C。
# 参数用途: _GUARD_DUP_PROTOCOL是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_GUARD_DUP_PROTOCOL = 2


# LLM: Apple event_private.h固定72字节ABI；仅用于原workloop请求/读回，不从wire重建历史。
# 类用途: 表示kevent_qos_s的原生布局。
class _Event(ctypes.Structure):
    _fields_ = [('ident', ctypes.c_uint64), ('filter', ctypes.c_int16), ('flags', ctypes.c_uint16),
                ('qos', ctypes.c_int32), ('udata', ctypes.c_uint64), ('fflags', ctypes.c_uint32),
                ('xflags', ctypes.c_uint32), ('data', ctypes.c_int64), ('ext', ctypes.c_uint64 * 4)]


# LLM: Apple proc_info_private.h固定104字节ABI；count按条数，零行/errno不能解释为完整空域。
# 类用途: 读出原knote的累积事件和关闭状态。
class _ExtInfo(ctypes.Structure):
    _fields_ = [('event', _Event), ('sdata', ctypes.c_uint64), ('status', ctypes.c_int),
                ('sfflags', ctypes.c_int), ('reserved', ctypes.c_uint64 * 2)]


# LLM: Apple packed workloop params固定36字节ABI，version传结构尺寸；仅创建本域生命周期，不改全局配置。
# 类用途: 显式持有不随FD关闭而销毁的workloop。
class _Params(ctypes.Structure):
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [('version', ctypes.c_int), ('flags', ctypes.c_int), ('id', ctypes.c_uint64),
                ('sched_pri', ctypes.c_int), ('sched_pol', ctypes.c_int), ('cpu_percent', ctypes.c_int),
                ('cpu_refillms', ctypes.c_int), ('wi_port', ctypes.c_uint32)]


# LLM: Apple kevent64_s为48字节ABI；仅适配既有kevent控制接口，Python不持有会裸close的kqueue对象。
# 类用途: 在有条件所有权包装里调用真正kevent64。
class _Event64(ctypes.Structure):
    _fields_ = [('ident', ctypes.c_uint64), ('filter', ctypes.c_int16), ('flags', ctypes.c_uint16),
                ('fflags', ctypes.c_uint32), ('data', ctypes.c_int64), ('udata', ctypes.c_uint64),
                ('ext', ctypes.c_uint64 * 2)]


# LLM: 固定真实Apple SPI签名，符号不存在立即失败，不回退到标签挑战；同步Darwin发行身份探针。
# 函数用途: 配置内核历史与条件关闭接口。
def _native():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.kevent_id.argtypes = [ctypes.c_uint64, ctypes.POINTER(_Event), ctypes.c_int, ctypes.POINTER(_Event),
                             ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_uint]
    lib.kevent_id.restype = ctypes.c_int
    lib.__kqueue_workloop_ctl.argtypes = [ctypes.c_size_t, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
    lib.__kqueue_workloop_ctl.restype = ctypes.c_int
    lib.proc_piddynkqueueinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_piddynkqueueinfo.restype = ctypes.c_int
    lib._pthread_qos_class_encode.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    lib._pthread_qos_class_encode.restype = ctypes.c_ulong
    lib.guarded_kqueue_np.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint]
    lib.guarded_kqueue_np.restype = ctypes.c_int
    lib.guarded_close_np.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
    lib.guarded_close_np.restype = ctypes.c_int
    lib.change_fdguard_np.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
    lib.change_fdguard_np.restype = ctypes.c_int
    lib.kevent64.argtypes = [ctypes.c_int, ctypes.POINTER(_Event64), ctypes.c_int,
                            ctypes.POINTER(_Event64), ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    lib.kevent64.restype = ctypes.c_int
    return lib


# LLM: guard仅比较当前descriptor清理权，历史由独立workloop；无Python kqueue析构器，未知实例不裸close。两个独立原队列无需GUARD_DUP之后dup。
# 类用途: 原子guard出生的内核队列，显式条件关闭避免误关复用的陌生管道。
class GuardedQueue:
    # LLM: 内核一次分配并附guard，不存在普通alias再guard的出生空窗；不调用GUARD_CLOSE。
    # 函数用途: 创建只由原C条件清理的队列。
    def __init__(self):
        self._origin = self
        self.lib = _native()
        self.guard = ctypes.c_uint64(secrets.randbits(63) or 1)
        self.creator_pid = os.getpid()
        self.closed = False
        self.descriptor = self.lib.guarded_kqueue_np(ctypes.byref(self.guard), _GUARD_DUP_PROTOCOL)
        if self.descriptor < 0:
            raise OSError(ctypes.get_errno(), 'guarded_kqueue_np')
        self._bind_original()

    # LLM: 只在真实本C分配/guard成功后固定清理对象，外来FD不能由wire构造；核pipe与queue调用方。
    # 函数用途: 冻结原guard/FD/library，防浅复制或字段替换消费。
    def _bind_original(self):
        self._descriptor_origin = self.descriptor
        self._guard_origin = self.guard
        self._guard_value = self.guard.value
        self._lib_origin = self.lib

    # LLM: 数字定位仅供真实系统调用，不是原内核身份或退出证据。
    # 函数用途: 返回当前自有数字FD。
    def fileno(self):
        self._require_original()
        return self.descriptor

    # LLM: 原包装和guard/数字FD不可被浅复制或重绑定消费，不把其数值授给别的domain。
    # 函数用途: 在真实系统调用之前核对原清理对象。
    def _require_original(self):
        if (self._origin is not self or self.creator_pid != os.getpid()
                or self.descriptor != self._descriptor_origin or self.guard is not self._guard_origin
                or self.guard.value != self._guard_value or self.lib is not self._lib_origin):
            raise OSError(errno.EPERM, 'original_guarded_queue_required')

    # LLM: 兼容原测试观察接口但不用于代次权威；只允许非阻塞操作，实际guarded FD可被故障注入关闭。
    # 函数用途: 调用真实kevent64，保留原marker/PROC观测接口。
    def control(self, changes, max_events, timeout):
        self._require_original()
        if self.closed or timeout != 0:
            raise OSError(errno.EBADF, 'original_guarded_queue_required')
        values = [_Event64(ident=e.ident, filter=e.filter, flags=e.flags, fflags=e.fflags,
                           data=e.data, udata=e.udata or 0) for e in (changes or ())]
        incoming = (_Event64 * len(values))(*values) if values else None
        outgoing = (_Event64 * max_events)() if max_events else None
        immediate = (ctypes.c_long * 2)(0, 0)
        count = self.lib.kevent64(self.descriptor, incoming, len(values), outgoing, max_events, 0, immediate)
        if count < 0:
            raise OSError(ctypes.get_errno(), 'kevent64')
        return [select.kevent(e.ident, filter=e.filter, flags=e.flags, fflags=e.fflags,
                             data=e.data, udata=e.udata) for e in outgoing[:count]] if outgoing is not None else []

    # LLM: 内核锁下比较guard并关闭，一次失败不重试旧数字；不调用裸close或会裸close的Python析构器。
    # 函数用途: 只关闭仍由本包装持有的FD，复用成陌生管道时返回普通错误。
    def close(self):
        self._require_original()
        if self.closed:
            return
        if self.lib.guarded_close_np(self.descriptor, ctypes.byref(self.guard)) < 0:
            raise OSError(ctypes.get_errno(), 'guarded_close_np')
        self.closed = True


# LLM: 仅接受同构造路径真实os.pipe刚返回的自有端点，soft guard条件清理，原workloop关闭日志仍守历史；无GUARD_CLOSE或裸close析构。
# 类用途: 为原私有管道提供与队列相同的条件关闭，避免数字复用时误伤。
class GuardedPipeDescriptor(GuardedQueue):
    # LLM: 按真实os.pipe出生立即附soft guard，失败隔离，不尝试关闭无guard的旧数字；同步partial-init与stamp复用测试。
    # 函数用途: 捕获原管道的清理所有权，不运输给W。
    def __init__(self, descriptor):
        self._origin = self
        self.lib = _native()
        self.guard = ctypes.c_uint64(secrets.randbits(63) or 1)
        self.creator_pid = os.getpid()
        self.closed = False
        self.descriptor = descriptor
        previous = ctypes.c_int()
        if self.lib.change_fdguard_np(descriptor, None, 0, ctypes.byref(self.guard), _GUARD_DUP_PROTOCOL,
                                      ctypes.byref(previous)) < 0:
            raise OSError(ctypes.get_errno(), 'guard_private_pipe')
        self._bind_original()


# LLM: 无FD显式workloop的唯一创建/登记位于R前，后续仅快照/原note删除/销毁；它保存原PROC和每个原FD关闭历史，不依赖重放标签。
# 类用途: 为一个原C命令保留不可被数字FD复用抹去的历史。
class KernelHistory:
    # LLM: CREATE冲突拒绝接管；disabled原PROC在真实fork/exit后仍保留累积位。登记成功后必须原快照正向读回。
    # 函数用途: 在命令仍等待R时登记原根和两个真实队列。
    def __init__(self, pid, descriptors, stamp=()):
        self._origin = self
        self.lib = _native()
        self.creator_pid = os.getpid()
        self.identifier = secrets.randbits(63) or 1
        self.pid = pid
        watches = [(fd, -1) for fd in descriptors] + list(zip(stamp, (-1, -2)))
        self.descriptors = tuple(fd for fd, _filter in watches)
        self.closed = False
        self._identity_origin = (self.identifier, self.pid, self.descriptors, self.lib)
        self.params = _Params(version=ctypes.sizeof(_Params), flags=1, id=self.identifier, sched_pri=31)
        self._ctl(1)
        qos = self.lib._pthread_qos_class_encode(0x15, 0, 0)
        self.notes = [_Event(ident=fd, filter=filter_kind, flags=1 | 8 | _DISPATCH2 | _VANISHED | 0x40,
                             qos=qos, udata=index + 1) for index, (fd, filter_kind) in enumerate(watches)]
        self.notes.append(_Event(ident=pid, filter=-5, flags=1 | 8 | 0x40, qos=qos,
                                 udata=len(self.descriptors) + 1, fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT))
        self._changes(self.notes)
        forked, exited = self.snapshot()
        if forked or exited:
            raise OSError(errno.EIO, 'kernel_history_not_pre_release')

    # LLM: 私有生命周期只在原C操作，不从host请求或missing状态重建同id；create/destroy均有真实系统调用结果。
    # 函数用途: 持有或释放本域workloop创建引用。
    def _ctl(self, command):
        self._require_original()
        if getattr(self.lib, "__kqueue_workloop_ctl")(command, 0, ctypes.byref(self.params), ctypes.sizeof(self.params)) < 0:
            raise OSError(ctypes.get_errno(), 'kqueue_workloop_ctl')

    # LLM: 无FD对象也不能按id重绑定/浅复制，原字段来自唯一R前构造，不从磁盘或wire恢复。
    # 函数用途: 在快照和资源回收前确认原C历史对象。
    def _require_original(self):
        if (self._origin is not self or self.creator_pid != os.getpid()
                or (self.identifier, self.pid, self.descriptors, self.lib) != self._identity_origin):
            raise OSError(errno.EPERM, 'original_kernel_history_required')

    # LLM: 只在构造时ADD、正常回收时DELETE；MUST_EXIST不补建，逐条EV_RECEIPT errno均必须0。
    # 函数用途: 登记或删除本域原knote，拒绝部分成功。
    def _changes(self, values):
        self._require_original()
        incoming, outgoing = (_Event * len(values))(*values), (_Event * len(values))()
        count = self.lib.kevent_id(self.identifier, incoming, len(values), outgoing, len(values),
                                  None, None, _WORKLOOP_FLAGS)
        if count != len(values) or any(not e.flags & 0x4000 or e.data for e in outgoing):
            raise OSError(ctypes.get_errno() or errno.EIO, 'kernel_history_registration')

    # LLM: EXTINFO读取原disabled knote，不消费后重登记；每个READ未VANISHED和PROC兴趣/身份必须正向存在，0/截断/EPERM全失败。
    # 函数用途: 读取原根FORK/EXIT，同时检测任一原队列被关闭或换代。
    def snapshot(self):
        self._require_original()
        if self.closed:
            raise OSError(errno.EPERM, 'original_kernel_history_required')
        rows = (_ExtInfo * (len(self.notes) + 1))()
        ctypes.set_errno(0)
        count = self.lib.proc_piddynkqueueinfo(os.getpid(), 1, self.identifier, rows, ctypes.sizeof(rows))
        if count != len(self.notes) or ctypes.get_errno():
            raise OSError(ctypes.get_errno() or errno.EIO, 'kernel_history_snapshot_incomplete')
        indexed = {(row.event.ident, row.event.filter, row.event.udata): row for row in rows[:count]}
        if len(indexed) != count:
            raise OSError(errno.EIO, 'kernel_history_duplicate_note')
        for expected in self.notes:
            row = indexed.get((expected.ident, expected.filter, expected.udata))
            if row is None or not row.status & _DISABLED or row.sfflags & 0xffffffff != expected.fflags:
                raise OSError(errno.EIO, 'kernel_history_original_note_missing')
            if expected.filter != -5 and (not row.status & _REQVANISH or row.status & _KN_VANISHED):
                raise OSError(errno.EIO, 'kernel_history_descriptor_vanished')
        flags = indexed[(self.pid, -5, len(self.descriptors) + 1)].event.fflags
        if flags & ~(select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT):
            raise OSError(errno.EIO, 'kernel_history_unexpected_proc_event')
        return bool(flags & select.KQ_NOTE_FORK), bool(flags & select.KQ_NOTE_EXIT)

    # LLM: 回收只删除原notes并释放CREATE引用；未知状态不销毁/补建证明，原C进程退出由内核回收保留资源。
    # 函数用途: 正常已验证退出后清理本域workloop。
    def close(self):
        self._require_original()
        if self.closed:
            return
        values = [_Event(ident=e.ident, filter=e.filter, flags=2 | 4 | 0x40 | (_DISPATCH2 if e.filter != -5 else 0),
                         udata=e.udata) for e in self.notes]
        self._changes(values)
        self._ctl(2)
        self.closed = True
