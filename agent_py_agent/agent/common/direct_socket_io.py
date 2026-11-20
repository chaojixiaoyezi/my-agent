"""直接套接字读取器：与 socket.makefile("rb") 读语义一致，但 readinto 不经两次 C 层检查。

socket.SocketIO.readinto 每次先调 _checkClosed() 与 _checkReadable()；在 python3.14t 关闭 GIL 时，
这两次检查要经类型查找取 _io 模块状态，SSE/HTTP 读循环在 128 线程下会落到全局类型锁上（R3 §2.6、§4.8）。
DirectSocketIO 只用实例属性做同样的判定：已关闭抛 ValueError、不可读抛 UnsupportedOperation、
超时后再读抛 OSError、EAGAIN/EWOULDBLOCK 返回 None。依赖的 SocketIO 私有属性 _sock、_reading、
_timeout_occurred 与套接字的 _io_refs 由单元测试钉住。PrefetchedSocket、SSLSocket 都走各自的 recv_into。
"""

from __future__ import annotations

import errno
import http.client
import io
import socket

# 非阻塞套接字“暂无数据”的 errno 集合，与 socket._blocking_errnos 相同。
_BLOCKING_ERRNOS = frozenset((errno.EAGAIN, errno.EWOULDBLOCK))


# 类用途: socket.SocketIO 的只读快路径子类；写、关闭、引用计数等其余行为全部沿用父类。
class DirectSocketIO(socket.SocketIO):
    # LLM: 判定顺序与 SocketIO.readinto 相同（关闭 → 可读 → 超时标记 → recv_into）；SocketIO.close() 会把
    #   _sock 置 None，所以 _sock 为空即等价于已关闭。
    # 函数用途: 从套接字读入缓冲区，返回读到的字节数；非阻塞且无数据时返回 None。
    def readinto(self, b) -> int | None:
        sock = self._sock
        if sock is None:
            raise ValueError("I/O operation on closed file.")
        if not self._reading:
            raise io.UnsupportedOperation("File or stream is not readable.")
        if self._timeout_occurred:
            raise OSError("cannot read from timed out object")
        try:
            return sock.recv_into(b)
        except TimeoutError:
            self._timeout_occurred = True
            raise
        except OSError as exc:
            if exc.errno in _BLOCKING_ERRNOS:
                return None
            raise


# LLM: 与 socket.socket.makefile("rb", buffering) 一一对应：先建原始读取器、再给套接字 _io_refs 加一（关闭
#   读取器时由 SocketIO.close 减回）；buffering 为 None 或负数用 io.DEFAULT_BUFFER_SIZE，为 0 时返回原始读取器。
# 函数用途: 为套接字（含 SSLSocket、PrefetchedSocket）建立二进制缓冲读取器。
def makefile_rb(sock: socket.socket, buffering: int | None = -1) -> io.BufferedReader | DirectSocketIO:
    raw = DirectSocketIO(sock, "r")
    sock._io_refs += 1
    if buffering is None or buffering < 0:
        buffering = io.DEFAULT_BUFFER_SIZE
    if buffering == 0:
        return raw
    return io.BufferedReader(raw, buffering)


# 类用途: 只把 makefile("rb") 换成 makefile_rb 的套接字外壳，供 HTTPResponse 构造时使用；其它模式原样转交。
class _RbMakefile:
    __slots__ = ("_sock",)

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def makefile(self, mode: str = "r", buffering: int | None = None, *, encoding: str | None = None,
                 errors: str | None = None, newline: str | None = None):
        if mode == "rb" and encoding is None and errors is None and newline is None:
            return makefile_rb(self._sock, -1 if buffering is None else buffering)
        return self._sock.makefile(mode, buffering, encoding=encoding, errors=errors, newline=newline)


# LLM: 标准库 HTTPResponse.__init__ 只调用一次 sock.makefile("rb")；这里显式调用父类构造（不用零参 super），
#   传入只替换该调用的外壳，响应解析、keep-alive 与 fp.peek 探测全部沿用标准库实现。
# 类用途: 读通道走 DirectSocketIO 的 HTTP 响应类，可直接赋给 HTTPConnection.response_class。
class DirectReadHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, debuglevel: int = 0, method: str | None = None, url: str | None = None) -> None:
        http.client.HTTPResponse.__init__(self, _RbMakefile(sock), debuglevel, method, url)


__all__ = ["DirectReadHTTPResponse", "DirectSocketIO", "makefile_rb"]
