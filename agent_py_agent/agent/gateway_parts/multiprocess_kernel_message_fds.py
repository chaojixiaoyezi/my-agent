# LLM: FD只随同次内核消息运输，不授业务写权限；失败关闭已收到资源，原endpoint creator门仍在公开层。
# 模块用途: 供生产执行桥传递真实标准流，保留Linux逐包凭据和Darwin AUDIT来源。
import array
import os
import socket
import time

from . import multiprocess_kernel_message as km

# 参数用途: 同消息最多运输16个FD副本；拒绝超限且失败关闭已接收资源。
MAX_FDS_COUNT = 16


# LLM: 只接真实整数FD；在任何发送前核有效性，不能把路径或JSON字段打开为描述符。
# 函数用途: 验证调用者已经拥有的有限FD元组。
def validate_fds(descriptors):
    if (type(descriptors) is not tuple or len(descriptors) > MAX_FDS_COUNT
            or any(type(fd) is not int or fd < 0 for fd in descriptors)):
        raise km.KernelMessageError('KERNEL_MESSAGE_INVALID', 'fds', 'invalid descriptor tuple')
    for fd in descriptors:
        os.fstat(fd)
    return descriptors


# LLM: 只清理本次receive已经copyout的FD；不能关闭sender原资源或其它业务对象。
# 函数用途: 失败时释放已领取的描述符。
def close_fds(descriptors):
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


# LLM: 使用原native endpoint，不建旁路socket，不回退身份；send不转移调用者的原FD所有权。
# 函数用途: 将完整payload与FD作为同一条原生消息发送。
def send_fds(native, payload, descriptors, deadline):
    if type(native) is km._DarwinMessage:
        from .multiprocess_kernel_fd_darwin import send_fds as send_darwin

        return send_darwin(native, payload, descriptors, deadline)
    if type(native) is not km._LinuxMessage:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'fds', 'unsupported native backend')
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors))] if descriptors else []
    while True:
        native._wait(deadline, True)
        try:
            sent = native.sock.sendmsg([payload], ancillary, 0, native.address)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise km.KernelMessageError('KERNEL_MESSAGE_TIMEOUT', 'send', 'queue full') from None
            continue
        if sent != len(payload):
            raise km.KernelMessageError('KERNEL_MESSAGE_IO', 'send', 'partial datagram')
        return


# LLM: 同次recvmsg的SCM_CREDENTIALS和SCM_RIGHTS必须共同完整；解码helper关闭坏信封全部copyout FD，接收/期限/原sender不变；同步kernel FD测试。
# 函数用途: 从原endpoint接收字节、真实sender和本次FD副本，兼容原无FD消息。
def receive_fds(native, deadline):
    if type(native) is km._DarwinMessage:
        from .multiprocess_kernel_fd_darwin import receive_fds as receive_darwin

        return receive_darwin(native, deadline)
    if type(native) is not km._LinuxMessage:
        raise km.KernelMessageError('KERNEL_MESSAGE_UNAVAILABLE', 'fds', 'unsupported native backend')
    while True:
        native._wait(deadline, False)
        try:
            payload, ancillary, flags, _ = native.sock.recvmsg(
                km.MAX_PAYLOAD_BYTES, socket.CMSG_SPACE(12) + socket.CMSG_SPACE(MAX_FDS_COUNT * 4),
                socket.MSG_CMSG_CLOEXEC)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise km.KernelMessageError('KERNEL_MESSAGE_TIMEOUT', 'receive', 'queue empty') from None
            continue
        return _decode_linux_controls(native, payload, ancillary, flags)



# LLM: 同次SCM凭据/FD独立解码；无论未知controls/数量/凭据错误都释放本次copyout，不改变接收端点或sender权威。
# 函数用途: 拆平消息资源验证，坏帧不遗留FD。
def _decode_linux_controls(native, payload, ancillary, flags):
    descriptors, credentials, malformed = [], [], False
    try:
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array('i')
                values.frombytes(data[:len(data) // values.itemsize * values.itemsize])
                descriptors.extend(values)
                malformed |= len(data) % values.itemsize != 0
            elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                credentials.append((level, kind, data))
            else:
                malformed = True
        peer = native._credentials(credentials, flags)
        if malformed or len(descriptors) > MAX_FDS_COUNT:
            raise km.KernelMessageError('KERNEL_MESSAGE_MALFORMED', 'receive', 'invalid FD controls')
        return payload, peer, tuple(descriptors)
    except BaseException:
        close_fds(descriptors)
        raise
