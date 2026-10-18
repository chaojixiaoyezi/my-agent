# LLM: 原payload/PID/FD形状不变，Darwin同次AUDIT附不可变pidversion事实，仅供原EXIT后末回送认证；locator/正文/环境不授信，同步kernel/host/exec探针。
# 模块用途: Darwin Mach AUDIT trailer与Linux SCM_CREDENTIALS消息transport。
import array
import ctypes
import errno
import math
import numbers
import os
import select
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager

# 参数用途: 原生消息业务载荷上限4096字节；系统头和凭据不计入业务载荷。
MAX_PAYLOAD_BYTES = 4096

# LLM: 以下Darwin ABI经本机SDK静态断言核对，不从请求或配置修改。
# 常量用途: 固定Mach wire格式与receive AUDIT trailer选项。
_MACH_HEADER = struct.Struct("=IIIIii")
_MACH_LENGTH = struct.Struct("=I")
# 参数用途: _MACH_MESSAGE_CODE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_MESSAGE_CODE = 0x4B4D
# 参数用途: _MACH_COPY_SEND_TYPE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_COPY_SEND_TYPE = 19
# 参数用途: _MACH_MAKE_SEND_TYPE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_MAKE_SEND_TYPE = 20
# 参数用途: _MACH_RECEIVE_RIGHT_TYPE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_RECEIVE_RIGHT_TYPE = 1
# 参数用途: _MACH_COMPLEX_PROTOCOL是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_COMPLEX_PROTOCOL = 0x80000000
_MACH_SEND_OPTIONS = 0x00000001 | 0x00000010
_MACH_RECEIVE_OPTIONS = 0x00000002 | 0x00000100 | 0x03000000
# 参数用途: _MACH_SEND_TIMED_OUT_CODE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_SEND_TIMED_OUT_CODE = 0x10000004
# 参数用途: _MACH_RECEIVE_TIMED_OUT_CODE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_RECEIVE_TIMED_OUT_CODE = 0x10004003
# 参数用途: _MACH_RECEIVE_TOO_LARGE_CODE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_MACH_RECEIVE_TOO_LARGE_CODE = 0x10004004
_MACH_MAX_MESSAGE = 24 + 4 + MAX_PAYLOAD_BYTES
# 参数用途: Mach内核AUDIT trailer固定52字节；完整性认证必须逐字节边界检查。
_MACH_AUDIT_BYTES = 52
# 参数用途: 原Mach接收超时最大4294967.294秒，按32位无符号毫秒ABI换算；保持原上限，区别业务失败分析的等待预算。
_KERNEL_RECEIVE_MAX_TIMEOUT_SECONDS = (0xFFFFFFFF - 1) / 1000


# LLM: 错误必须显式传播，不生成替代PID或静默成功。
# 类用途: 对调用方提供稳定错误码、操作名和原生错误。
class KernelMessageError(RuntimeError):
    # LLM: 保留原生错误供诊断，业务准入不能从异常文字推断。
    # 函数用途: 建立结构化transport异常。
    def __init__(self, error_code, operation, detail="", native_error=None):
        self.error_code = error_code
        self.operation = operation
        self.native_error = native_error
        self.detail = detail
        super().__init__(
            f"{error_code}: {operation}: {detail}"
            + (
                f" (native={native_error})"
                if native_error is not None
                else ""
            )
        )


# LLM: 不接受无限、负数或字符串timeout；原内核接收秒上限保持ABI值，不使用业务失败分析预算，联测原sender/receiver超时。
# 函数用途: 验证原内核运输timeout并保留有限秒数，避免毫秒转换溢出。
def _seconds(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, numbers.Real):
        raise KernelMessageError(
            "KERNEL_MESSAGE_INVALID", "timeout", "expected finite seconds"
        )
    value = float(timeout)
    if not math.isfinite(value) or value < 0 or value > _KERNEL_RECEIVE_MAX_TIMEOUT_SECONDS:
        raise KernelMessageError(
            "KERNEL_MESSAGE_INVALID", "timeout", "out of supported range"
        )
    return value


# LLM: Mach超时字段为uint32毫秒，向上取整避免提前结束正timeout。
# 函数用途: 将剩余deadline转换成原生Mach timeout。
def _milliseconds(deadline):
    remaining = max(0.0, deadline - time.monotonic())
    return min(0xFFFFFFFF - 1, math.ceil(remaining * 1000))


# LLM: 地址只定位；不从地址中解析任何sender或owner身份。
# 函数用途: 验证公开locator的类型与基本格式。
def _address(address):
    if not isinstance(address, str) or not address or "\0" in address:
        raise KernelMessageError(
            "KERNEL_MESSAGE_INVALID", "address", "nonempty string required"
        )
    return address


# LLM: 公开API只接收bytes，拒绝隐式文本编码和超限传输。
# 函数用途: 验证payload字节边界。
def _payload(payload):
    if not isinstance(payload, bytes):
        raise KernelMessageError(
            "KERNEL_MESSAGE_INVALID", "send", "payload must be bytes"
        )
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise KernelMessageError(
            "KERNEL_MESSAGE_TOO_LARGE", "send", "maximum is 4096 bytes"
        )
    return payload


# LLM: 内核失败与超时分开，禁止使用当前PID补齐失败结果。
# 函数用途: 将mach_msg返回值映射为结构化异常。
def _mach_result(result, operation):
    code = result & 0xFFFFFFFF
    if code == 0:
        return
    if code in (_MACH_SEND_TIMED_OUT_CODE, _MACH_RECEIVE_TIMED_OUT_CODE):
        error = "KERNEL_MESSAGE_TIMEOUT"
    elif code == _MACH_RECEIVE_TOO_LARGE_CODE:
        error = "KERNEL_MESSAGE_TOO_LARGE"
    else:
        error = "KERNEL_MESSAGE_IO"
    raise KernelMessageError(error, operation, "mach_msg failed", code)


# LLM: audit token按BSM公开函数解释，不自行把val字段当稳定PID布局。
# 类用途: 与SDK audit_token_t一致的按值FFI结构。
class _AuditToken(ctypes.Structure):
    _fields_ = [("val", ctypes.c_uint32 * 8)]


# LLM: receive资源是本对象拥有的Mach receive right，不是连接peer观察。
# 类用途: Darwin原生消息backend；bootstrap名称只做sendright查找。
class _DarwinMessage:
    # LLM: 不接受能力缺失时的备用身份来源。
    # 函数用途: 加载ABI、获取bootstrap port并创建receiver或sender资源。
    def __init__(self, address, receiver):
        self.port = 0
        self.bootstrap = 0
        self.registered = False
        self.receiver = receiver
        self.name = address.encode("utf-8")
        if len(self.name) >= 128:
            raise KernelMessageError(
                "KERNEL_MESSAGE_INVALID", "address",
                "Mach service name must fit name_t"
            )

        try:
            self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            self.bsm = ctypes.CDLL("/usr/lib/libbsm.dylib")
            self._configure()
            self.task = ctypes.c_uint32.in_dll(
                self.lib, "mach_task_self_"
            ).value
            if (
                not self.task
                or _MACH_HEADER.size != 24
                or ctypes.sizeof(_AuditToken) != 32
            ):
                raise KernelMessageError(
                    "KERNEL_MESSAGE_UNAVAILABLE", "open", "ABI unavailable"
                )
            bootstrap = ctypes.c_uint32()
            self._check(
                self.lib.task_get_special_port(
                    self.task, 4, ctypes.byref(bootstrap)
                ),
                "task_get_special_port",
            )
            self.bootstrap = bootstrap.value
            if not self.bootstrap:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_UNAVAILABLE", "open",
                    "no bootstrap capability"
                )
            self._open()
        except KernelMessageError:
            self._cleanup()
            raise
        except (OSError, AttributeError, ValueError, UnicodeError) as exc:
            self._cleanup()
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "open", str(exc)
            ) from exc

    # LLM: 显式声明Mach与公开BSM PID/pidversion固定宽度，不取AUDIT私有字段；pidversion在exec后变化，仅原READY+P1冻结后用于精确末回送，同步真探针及host tests。
    # 函数用途: 配置libSystem与libbsm函数签名。
    def _configure(self):
        u32 = ctypes.c_uint32
        p32 = ctypes.POINTER(u32)
        self.lib.mach_msg.argtypes = [
            ctypes.c_void_p, u32, u32, u32, u32, u32, u32
        ]
        self.lib.mach_msg.restype = ctypes.c_int32
        self.lib.mach_msg_destroy.argtypes = [ctypes.c_void_p]
        self.lib.mach_msg_destroy.restype = None
        self.lib.mach_port_allocate.argtypes = [u32, ctypes.c_int32, p32]
        self.lib.mach_port_allocate.restype = ctypes.c_int32
        self.lib.mach_port_insert_right.argtypes = [u32, u32, u32, u32]
        self.lib.mach_port_insert_right.restype = ctypes.c_int32
        self.lib.mach_port_destroy.argtypes = [u32, u32]
        self.lib.mach_port_destroy.restype = ctypes.c_int32
        self.lib.mach_port_deallocate.argtypes = [u32, u32]
        self.lib.mach_port_deallocate.restype = ctypes.c_int32
        self.lib.task_get_special_port.argtypes = [
            u32, ctypes.c_int32, p32
        ]
        self.lib.task_get_special_port.restype = ctypes.c_int32
        self.lib.bootstrap_register.argtypes = [
            u32, ctypes.c_char_p, u32
        ]
        self.lib.bootstrap_register.restype = ctypes.c_int32
        self.lib.bootstrap_look_up.argtypes = [
            u32, ctypes.c_char_p, p32
        ]
        self.lib.bootstrap_look_up.restype = ctypes.c_int32
        self.bsm.audit_token_to_pid.argtypes = [_AuditToken]
        self.bsm.audit_token_to_pid.restype = ctypes.c_int32
        self.bsm.audit_token_to_pidversion.argtypes = [_AuditToken]
        self.bsm.audit_token_to_pidversion.restype = ctypes.c_int32

    # LLM: bootstrap/port创建失败不等于获得可用sender身份。
    # 函数用途: 检查资源创建或查找的原生返回值。
    def _check(self, result, operation):
        if result != 0:
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", operation,
                "native capability unavailable", result & 0xFFFFFFFF
            )

    # LLM: 注册冲突时不删除既有服务，sender只取得自己的一份sendright。
    # 函数用途: 创建receive endpoint或查找对应sendright。
    def _open(self):
        port = ctypes.c_uint32()
        if self.receiver:
            self._check(
                self.lib.mach_port_allocate(
                    self.task, _MACH_RECEIVE_RIGHT_TYPE, ctypes.byref(port)
                ),
                "mach_port_allocate",
            )
            self.port = port.value
            self._check(
                self.lib.mach_port_insert_right(
                    self.task, self.port, self.port, _MACH_MAKE_SEND_TYPE
                ),
                "mach_port_insert_right",
            )
            self._check(
                self.lib.bootstrap_register(
                    self.bootstrap, self.name, self.port
                ),
                "bootstrap_register",
            )
            self.registered = True
        else:
            self._check(
                self.lib.bootstrap_look_up(
                    self.bootstrap, self.name, ctypes.byref(port)
                ),
                "bootstrap_look_up",
            )
            self.port = port.value
        if not self.port:
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "open", "null Mach port"
            )

    # LLM: wire body不含可信身份字段；AUDIT信息只能来自内核receive trailer。
    # 函数用途: 发送长度前缀加payload的简单Mach消息。
    def send(self, payload, deadline):
        total = (28 + len(payload) + 3) & ~3
        header = _MACH_HEADER.pack(
            _MACH_COPY_SEND_TYPE, total, self.port, 0, 0, _MACH_MESSAGE_CODE
        )
        message = ctypes.create_string_buffer(
            (header + _MACH_LENGTH.pack(len(payload)) + payload).ljust(
                total, b"\0"
            )
        )
        _mach_result(
            self.lib.mach_msg(
                message, _MACH_SEND_OPTIONS, total, 0, 0,
                _milliseconds(deadline), 0
            ),
            "send",
        )

    # LLM: 不启用MACH_RCV_LARGE保留超大消息，避免坏消息卡住队列。
    # 函数用途: 带AUDIT trailer接收并验证逐消息sender PID。
    def receive(self, deadline):
        capacity = _MACH_MAX_MESSAGE + _MACH_AUDIT_BYTES
        message = ctypes.create_string_buffer(capacity)
        _mach_result(
            self.lib.mach_msg(
                message, _MACH_RECEIVE_OPTIONS, 0, capacity, self.port,
                _milliseconds(deadline), 0
            ),
            "receive",
        )
        bits, size, remote, local, voucher, message_id = (
            _MACH_HEADER.unpack_from(message.raw)
        )
        try:
            if size > _MACH_MAX_MESSAGE:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_TOO_LARGE", "receive",
                    "wire message exceeds payload limit"
                )
            if (
                size < 28
                or size % 4
                or local != self.port
                or remote != 0
                or voucher != 0
                or bits & _MACH_COMPLEX_PROTOCOL
                or message_id != _MACH_MESSAGE_CODE
            ):
                raise KernelMessageError(
                    "KERNEL_MESSAGE_MALFORMED", "receive",
                    "unexpected Mach envelope"
                )
            length = _MACH_LENGTH.unpack_from(message.raw, 24)[0]
            if length > MAX_PAYLOAD_BYTES:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_TOO_LARGE", "receive",
                    "declared payload exceeds 4096 bytes"
                )
            if size != ((28 + length + 3) & ~3):
                raise KernelMessageError(
                    "KERNEL_MESSAGE_MALFORMED", "receive",
                    "payload length does not match message boundary"
                )

            trailer_offset = (size + 3) & ~3
            if trailer_offset + _MACH_AUDIT_BYTES > capacity:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                    "AUDIT trailer absent or truncated"
                )
            trailer_type, trailer_size = struct.unpack_from(
                "=II", message.raw, trailer_offset
            )
            if trailer_type != 0 or trailer_size != _MACH_AUDIT_BYTES:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                    "expected kernel AUDIT trailer"
                )
            token = _AuditToken.from_buffer_copy(
                message.raw, trailer_offset + 20
            )
            sender_pid = int(self.bsm.audit_token_to_pid(token))
            if sender_pid <= 0:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                    "kernel trailer has no userspace sender PID"
                )
            return message.raw[28:28 + length], sender_pid
        finally:
            # LLM: 坏消息携带的reply/voucher/descriptor权利不能泄漏到进程。
            # 清理用途: 释放被拒消息copyout的资源，不销毁本receiver的local port。
            if remote or voucher or bits & _MACH_COMPLEX_PROTOCOL:
                self.lib.mach_msg_destroy(message)

    # LLM: 只撤销本对象成功注册的名字及自己持有的port references。
    # 函数用途: 清理构造失败和正常close中的自有Mach资源。
    def _cleanup(self):
        errors = []
        lib = getattr(self, "lib", None)
        task = getattr(self, "task", 0)
        if lib is None or not task:
            return errors

        # 销毁本对象receive right会使launchd收到dead-name并自动释放服务名。
        # 不能先bootstrap_register(name, MACH_PORT_NULL)：活服务返回NAME_IN_USE，不能当撤销API。
        self.registered = False

        port, self.port = self.port, 0
        if port:
            operation = (
                lib.mach_port_destroy
                if self.receiver
                else lib.mach_port_deallocate
            )
            rc = operation(task, port)
            if rc:
                errors.append(("mach_port_release", rc))

        bootstrap, self.bootstrap = self.bootstrap, 0
        if bootstrap:
            rc = lib.mach_port_deallocate(task, bootstrap)
            if rc:
                errors.append(("bootstrap_port_release", rc))
        return errors

    # LLM: 清理失败必须报告，但不得对已释放资源重复操作。
    # 函数用途: 关闭Darwin backend的自有资源。
    def close(self):
        errors = self._cleanup()
        if errors:
            operation, code = errors[0]
            raise KernelMessageError(
                "KERNEL_MESSAGE_IO", operation,
                "resource cleanup failed", code & 0xFFFFFFFF
            )


# LLM: 不接受连接peer凭据；只读取此datagram的SCM_CREDENTIALS。
# 类用途: Linux AF_UNIX datagram逐包凭据backend。
class _LinuxMessage:
    # LLM: Linux仅使用抽象地址，禁止需按pathname删除的资源；所需内核接口缺失仍失败关闭。
    # 函数用途: 在任何I/O前拒绝文件地址，再创建nonblocking datagram及receiver的SO_PASSCRED。
    def __init__(self, address, receiver):
        self.sock = None
        self.receiver = receiver
        self.address = (
            "\0" + address[1:] if address.startswith("@") else address
        )
        if not self.address.startswith('\0') or len(self.address) <= 1:
            raise KernelMessageError(
                'KERNEL_MESSAGE_UNSUPPORTED_LOCATOR', 'open',
                'Linux requires a nonempty abstract socket address'
            )
        required = (
            "SO_PASSCRED", "SCM_CREDENTIALS", "MSG_TRUNC",
            "MSG_CTRUNC", "MSG_CMSG_CLOEXEC"
        )
        if (
            not hasattr(socket.socket, "recvmsg")
            or any(not hasattr(socket, name) for name in required)
        ):
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "open",
                "per-datagram credentials unavailable"
            )
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            if receiver:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                self.sock.bind(self.address)
        except (OSError, ValueError) as exc:
            self._cleanup()
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "open", str(exc),
                getattr(exc, "errno", None)
            ) from exc

    # LLM: 内核队列等待使用deadline，不把socket无响应解释成权限拒绝。
    # 函数用途: 等待可读/可写且处理EINTR。
    def _wait(self, deadline, writing):
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                read, write, _ = select.select(
                    [] if writing else [self.sock],
                    [self.sock] if writing else [],
                    [],
                    remaining,
                )
            except InterruptedError:
                if time.monotonic() >= deadline:
                    raise KernelMessageError(
                        "KERNEL_MESSAGE_TIMEOUT",
                        "send" if writing else "receive",
                        "deadline expired",
                    ) from None
                continue
            if read or write:
                return
            raise KernelMessageError(
                "KERNEL_MESSAGE_TIMEOUT",
                "send" if writing else "receive",
                "deadline expired",
            )

    # LLM: datagram必须完整发送，不能以partial或queue-full冒充成功。
    # 函数用途: 带deadline的Linux原子datagram发送。
    def send(self, payload, deadline):
        while True:
            self._wait(deadline, True)
            try:
                sent = self.sock.sendto(payload, self.address)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise KernelMessageError(
                        "KERNEL_MESSAGE_TIMEOUT", "send", "queue full"
                    ) from None
                continue
            if sent != len(payload):
                raise KernelMessageError(
                    "KERNEL_MESSAGE_IO", "send", "partial datagram"
                )
            return

    # LLM: 未知ancillary不能成为身份，恶意传入的FD也不能泄漏。
    # 函数用途: 释放收到的SCM_RIGHTS并拒绝不在协议内的control message。
    def _credentials(self, ancillary, flags):
        unexpected = False
        credentials = []
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors = array.array("i")
                width = descriptors.itemsize
                descriptors.frombytes(data[:len(data) // width * width])
                for descriptor in descriptors:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                unexpected = True
            elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                credentials.append(data)
            else:
                unexpected = True

        if flags & socket.MSG_TRUNC:
            raise KernelMessageError(
                "KERNEL_MESSAGE_TOO_LARGE", "receive", "datagram truncated"
            )
        if flags & socket.MSG_CTRUNC:
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                "ancillary credentials truncated"
            )
        if unexpected:
            raise KernelMessageError(
                "KERNEL_MESSAGE_MALFORMED", "receive",
                "unexpected ancillary data"
            )
        if len(credentials) != 1 or len(credentials[0]) != 12:
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                "exactly one kernel ucred required"
            )
        sender_pid, _, _ = struct.unpack("=iII", credentials[0])
        if sender_pid <= 0:
            raise KernelMessageError(
                "KERNEL_MESSAGE_UNAVAILABLE", "receive",
                "invalid kernel sender PID"
            )
        return sender_pid

    # LLM: recvmsg同时领取payload和该条消息的凭据，不查询后来的last_pid。
    # 函数用途: 带deadline接收完整payload及SCM_CREDENTIALS sender。
    def receive(self, deadline):
        while True:
            self._wait(deadline, False)
            try:
                payload, ancillary, flags, _ = self.sock.recvmsg(
                    MAX_PAYLOAD_BYTES,
                    socket.CMSG_SPACE(12) + socket.CMSG_SPACE(256),
                    socket.MSG_CMSG_CLOEXEC,
                )
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise KernelMessageError(
                        "KERNEL_MESSAGE_TIMEOUT", "receive", "queue empty"
                    ) from None
                continue
            sender_pid = self._credentials(ancillary, flags)
            return payload, sender_pid

    # LLM: 只释放本对象的FD；抽象地址由内核随FD销毁，不按路径检查/删除任何资源。
    # 函数用途: 关闭自有socket并保留真实清理错误。
    def _cleanup(self):
        errors = []
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError as exc:
                errors.append(exc)
        return errors

    # LLM: 子进程副本不得调用此方法清理父endpoint，公开层先检查creator。
    # 函数用途: 关闭Linux backend的自有资源。
    def close(self):
        errors = self._cleanup()
        if errors:
            exc = errors[0]
            raise KernelMessageError(
                "KERNEL_MESSAGE_IO", "close", str(exc), exc.errno
            ) from exc


# LLM: 不因平台不支持而回退到peer PID、JSON、环境或MAC。
# 函数用途: 按实际平台选择具有逐消息凭据的backend。
def _backend(address, receiver):
    if sys.platform == "darwin":
        return _DarwinMessage(address, receiver)
    if sys.platform == "linux":
        return _LinuxMessage(address, receiver)
    raise KernelMessageError(
        "KERNEL_MESSAGE_UNAVAILABLE", "open",
        "no per-message kernel sender backend for this platform"
    )


# LLM: creator guard必须在任何锁之前执行；fork副本close不碰父资源。
# 类用途: 共享公开transport生命周期、deadline及结构化异常处理。
class _Endpoint:
    # LLM: creator取真实本进程PID，self身份只保护原endpoint资源，两者都不作为消息sender权限。
    # 函数用途: 初始化原对象资源归属及原生backend。
    def __init__(self, address, receiver):
        self._origin = self
        self.creator_pid = os.getpid()
        self._lock = threading.Lock()
        self._closed = False
        self._native = _backend(_address(address), receiver)

    # LLM: creator/self都在锁前核验；拒fork使用及同PID浅拷贝借原native资源，身份检查不等于消息授权。
    # 函数用途: 仅允许创建进程中的原endpoint调用收发与关闭。
    def _check_creator(self):
        if os.getpid() != self.creator_pid:
            raise KernelMessageError(
                "KERNEL_MESSAGE_FORKED", "lifecycle",
                "endpoint belongs to its creator process"
            )
        if self._origin is not self:
            raise KernelMessageError('KERNEL_MESSAGE_COPIED', 'lifecycle', 'original endpoint required')

    # LLM: 锁等待也算入timeout；拿锁后再次检查closed状态。
    # 函数用途: 对一个公开send/receive操作提供有界串行区。
    @contextmanager
    def _operation(self, timeout):
        self._check_creator()
        seconds = _seconds(timeout)
        deadline = time.monotonic() + seconds
        if not self._lock.acquire(timeout=seconds):
            raise KernelMessageError(
                "KERNEL_MESSAGE_TIMEOUT", "lock", "deadline expired"
            )
        try:
            self._check_creator()
            if self._closed:
                raise KernelMessageError(
                    "KERNEL_MESSAGE_CLOSED", "lifecycle",
                    "endpoint is closed"
                )
            yield deadline
        except OSError as exc:
            code = (
                "KERNEL_MESSAGE_TIMEOUT"
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                else "KERNEL_MESSAGE_IO"
            )
            raise KernelMessageError(
                code, "transport", str(exc), exc.errno
            ) from exc
        finally:
            self._lock.release()

    # LLM: child close保留锁前no-op；同PID copy须锁前拒绝，原对象重复close不影响后来endpoint。
    # 函数用途: 仅由creator中的原对象关闭自己持有的资源。
    def close(self):
        if os.getpid() != self.creator_pid:
            return
        self._check_creator()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._native.close()


# LLM: 三元tuple的现有FD/payload/PID语义不变，sender_instance只由同次内核AUDIT产生；不可变返回值不会从last-peer壳另读漂移事实，字段不作为写许可。
# 类用途: 把同包内核PID代次与原收到的数据/FD一起交给需要EXIT后认证的消费者。
class KernelReceivedMessage(tuple):
    # LLM: 构造只冻结内存事实，无I/O；实际生产调用仅由原kernel receive在锁内消费刚返回的AUDIT元数据。
    # 函数用途: 保留三字段解包兼容，并携带本条消息独立的内核发送者代次。
    def __new__(cls, payload, peer, descriptors, sender_instance=None):
        result = tuple.__new__(cls, (payload, peer, descriptors))
        object.__setattr__(result, 'sender_instance', sender_instance)
        return result

    # LLM: 不允许调用方改代次或追加字段把旧收到的packet冒充另一内核sender；原wire不能提供此Python对象。
    # 函数用途: 使同包发送者代次随返回消息不可重绑。
    def __setattr__(self, name, value):
        raise AttributeError('kernel_received_message_is_immutable')


# LLM: 只输出内核事实；上层仍须核原Popen/P0/P4/controller/lease。
# 类用途: 公开逐消息receiver。
class KernelMessageReceiver(_Endpoint):
    # LLM: address只注册定位服务，不颁发权限。
    # 函数用途: 创建本进程拥有的receive endpoint。
    def __init__(self, address):
        super().__init__(address, True)

    # LLM: 成功结果来自同一次原生receive，不接受caller声明sender PID。
    # 函数用途: 返回(payload_bytes, kernel_sender_pid)。
    def receive(self, *, timeout=5):
        with self._operation(timeout) as deadline:
            return self._native.receive(deadline)

    # LLM: FD和sender来自同一次原生接收；复制对象先在锁前拒绝，不把FD转换为业务许可。
    # 函数用途: 返回字节、内核sender及本次拥有的FD副本，调用者负责关闭FD。
    def receive_fds(self, *, timeout=5):
        with self._operation(timeout) as deadline:
            from .multiprocess_kernel_message_fds import receive_fds

            return receive_fds(self._native, deadline)


# LLM: 持有sendright/FD本身不等于获得业务权限。
# 类用途: 公开逐消息sender。
class KernelMessageSender(_Endpoint):
    # LLM: 查找address只获得传输定位能力。
    # 函数用途: 创建sender自有原生资源。
    def __init__(self, address):
        super().__init__(address, False)

    # LLM: 不传用户声明的身份参数，内核在真实发送时生成sender证据。
    # 函数用途: 发送最多4096字节的完整payload。
    def send(self, payload, *, timeout=5):
        self._check_creator()
        payload = _payload(payload)
        with self._operation(timeout) as deadline:
            self._native.send(payload, deadline)

    # LLM: 先锁前核creator再验证真实FD，发送只复制资源，不消费或关闭调用者原FD。
    # 函数用途: 同一原生消息运输字节与最多16个已拥有的FD。
    def send_fds(self, payload, descriptors=(), *, timeout=5):
        self._check_creator()
        payload = _payload(payload)
        with self._operation(timeout) as deadline:
            from .multiprocess_kernel_message_fds import send_fds, validate_fds

            send_fds(self._native, payload, validate_fds(descriptors), deadline)
