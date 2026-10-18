# LLM: fileport仅复制真实FD；同消息AUDIT证据仍由内核给出，FD/port持有不授业务权限。
# 模块用途: 为Darwin原endpoint增添有限FD消息，坏消息和失败转换必须释放copyout资源。
import ctypes
import os
import struct

from . import multiprocess_kernel_message as km
from .multiprocess_kernel_message_fds import MAX_FDS_COUNT, close_fds

_PORT = struct.Struct('=IIHBB')
# 参数用途: _PORT_SEND_TYPE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_PORT_SEND_TYPE = 17
# 参数用途: _PORT_DESCRIPTOR_TYPE是系统协议固定标记或选项值，无量纲；保持公开ABI值，不作为可调预算。
_PORT_DESCRIPTOR_TYPE = 0
_MAX_MESSAGE = 32 + MAX_FDS_COUNT * _PORT.size + km.MAX_PAYLOAD_BYTES


# LLM: 公开SDK fileport ABI，不从JSON、路径或PID构造FD。
# 函数用途: 配置真实fd↔Mach fileport转换签名。
def _configure(native):
    try:
        native.lib.fileport_makeport.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
        native.lib.fileport_makeport.restype = ctypes.c_int
        native.lib.fileport_makefd.argtypes = [ctypes.c_uint32]
        native.lib.fileport_makefd.restype = ctypes.c_int
    except AttributeError as exc:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'fileport', 'fileport ABI absent') from exc


# LLM: 发COPY_SEND副本，finally只释放本次makeport的自有sendright，不关闭caller原FD。
# 函数用途: 发送同一Mach消息的字节及port descriptors。
def send_fds(native, payload, descriptors, deadline):
    if not descriptors:
        return native.send(payload, deadline)
    _configure(native)
    ports = []
    try:
        for fd in descriptors:
            port = ctypes.c_uint32()
            native._check(native.lib.fileport_makeport(fd, ctypes.byref(port)), 'fileport_makeport')
            if not port.value:
                raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'fileport', 'null fileport')
            ports.append(port.value)
        body = struct.pack('=I', len(ports))
        body += b''.join(_PORT.pack(port, 0, 0, km._MACH_COPY_SEND_TYPE, _PORT_DESCRIPTOR_TYPE) for port in ports)
        body += km._MACH_LENGTH.pack(len(payload)) + payload
        total = (km._MACH_HEADER.size + len(body) + 3) & ~3
        header = km._MACH_HEADER.pack(km._MACH_COPY_SEND_TYPE | km._MACH_COMPLEX_PROTOCOL,
                                     total, native.port, 0, 0, km._MACH_MESSAGE_CODE)
        message = ctypes.create_string_buffer((header + body).ljust(total, b'\0'))
        km._mach_result(native.lib.mach_msg(message, km._MACH_SEND_OPTIONS, total, 0, 0,
                                           km._milliseconds(deadline), 0), 'send')
    finally:
        for port in ports:
            native.lib.mach_port_deallocate(native.task, port)


# LLM: 同一次真实mach_msg AUDIT经公开BSM函数提PID及pidversion，exec会变代；不从body/当前PID/last-peer壳补历史，核原SDK/exec/EXIT探针。
# 函数用途: 提取本消息内核sender。
def _sender(native, message, size, capacity):
    trailer_offset = (size + 3) & ~3
    if trailer_offset + km._MACH_AUDIT_BYTES > capacity:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'receive', 'AUDIT trailer truncated')
    trailer_type, trailer_size = struct.unpack_from('=II', message.raw, trailer_offset)
    if trailer_type != 0 or trailer_size != km._MACH_AUDIT_BYTES:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'receive', 'expected kernel AUDIT trailer')
    token = km._AuditToken.from_buffer_copy(message.raw, trailer_offset + 20)
    peer = int(native.bsm.audit_token_to_pid(token))
    if peer <= 0:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'receive', 'kernel sender absent')
    version = int(native.bsm.audit_token_to_pidversion(token))
    return peer, ('darwin_audit_pidversion.v1', peer, version)


# LLM: descriptor类型、个数和长度都在convert之前校验；失败不把剩余权利留给下次消息。
# 函数用途: 按同消息复杂标志确定payload位置与有限fileport列表。
def _body(message, bits, size):
    if not bits & km._MACH_COMPLEX_PROTOCOL:
        return 24, []
    if size < 32:
        raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'complex body absent')
    count = struct.unpack_from('=I', message.raw, 24)[0]
    offset = 28 + count * _PORT.size
    if not 0 < count <= MAX_FDS_COUNT or offset + 4 > size:
        raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'invalid descriptor count')
    ports = []
    for index in range(count):
        port, pad1, pad2, disposition, kind = _PORT.unpack_from(message.raw, 28 + index * _PORT.size)
        if not port or pad1 or pad2 or disposition != _PORT_SEND_TYPE or kind != _PORT_DESCRIPTOR_TYPE:
            raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'invalid port descriptor')
        ports.append(port)
    return offset, ports


# LLM: 原AUDIT PID+pidversion和全部FD同次receive，以不可变三元tuple共同返回；FD副本CLOEXEC，成功/失败均destroy消息权利，旧三元解包保持。
# 函数用途: 返回payload、内核sender和调用者拥有的真实FD副本，兼容简单无FD帧。
def receive_fds(native, deadline):
    capacity = _MAX_MESSAGE + km._MACH_AUDIT_BYTES
    message = ctypes.create_string_buffer(capacity)
    km._mach_result(native.lib.mach_msg(message, km._MACH_RECEIVE_OPTIONS, 0, capacity, native.port,
                                       km._milliseconds(deadline), 0), 'receive')
    bits, size, remote, local, voucher, message_id = km._MACH_HEADER.unpack_from(message.raw)
    descriptors = []
    try:
        if size > _MAX_MESSAGE:
            raise km.KernelMessageError('KERNEL_MESSAGE_TOO_LARGE', 'receive', 'wire FD frame too large')
        if size < 28 or size % 4 or local != native.port or remote or voucher or message_id != km._MACH_MESSAGE_CODE:
            raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'unexpected Mach FD envelope')
        offset, ports = _body(message, bits, size)
        length = km._MACH_LENGTH.unpack_from(message.raw, offset)[0]
        if length > km.MAX_PAYLOAD_BYTES:
            raise km.KernelMessageError('KERNEL_MESSAGE_TOO_LARGE', 'receive', 'payload exceeds 4096 bytes')
        if size != ((offset + 4 + length + 3) & ~3):
            raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'FD payload boundary mismatch')
        peer, instance = _sender(native, message, size, capacity)
        if ports:
            _configure(native)
        for port in ports:
            fd = native.lib.fileport_makefd(port)
            if fd < 0:
                raise km.KernelMessageError('KERNEL_MESSAGE_IO', 'fileport_makefd', 'FD copyout failed')
            descriptors.append(fd)
            os.set_inheritable(fd, False)
        return km.KernelReceivedMessage(message.raw[offset + 4:offset + 4 + length], peer, tuple(descriptors), instance)
    except BaseException:
        close_fds(descriptors)
        raise
    finally:
        if remote or voucher or bits & km._MACH_COMPLEX_PROTOCOL:
            native.lib.mach_msg_destroy(message)
