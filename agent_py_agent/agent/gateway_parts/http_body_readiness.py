"""有界HTTP前缀只证明运输完整；原处理器仍负责解析、认证和业务。"""
from __future__ import annotations

import http.client
import io
import socket

# 这是预读预算而非payload上限；超出窗口的请求回原普通处理路径。
HTTP_READY_PREFIX_BYTES = 8192


def complete_request_length(prefix: bytearray, *, single_request: bool, search_from: int = 0) -> int | None:
    """正数为完整帧所需字节，0为原慢路径，None表示头尚未完整。"""
    boundary = prefix.find(b'\r\n\r\n', search_from)
    ending = 4
    if boundary < 0:
        boundary, ending = prefix.find(b'\n\n', search_from), 2
    if boundary < 0:
        return None
    head_end = boundary + ending
    line_end = prefix.find(b'\n')
    parts = bytes(prefix[:line_end]).rstrip(b'\r').split(b' ')
    if len(parts) != 3 or parts[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
        return 0
    try:
        headers = http.client.parse_headers(io.BytesIO(prefix[line_end + 1:head_end]))
    except (http.client.HTTPException, ValueError):
        return 0
    # Expect需由原handler及时握手；模糊framing不得被预读器签成完整。
    if headers.get('Transfer-Encoding') is not None or headers.get('Expect') is not None:
        return 0
    values = headers.get_all('Content-Length', [])
    if len(values) > 1 or (values and (not values[0].isascii() or not values[0].isdigit())):
        return 0
    connection = headers.get('Connection', '').lower()
    if not (single_request or connection == 'close' or (parts[2] == b'HTTP/1.0' and connection != 'keep-alive')):
        return 0
    if values and len(values[0].lstrip('0')) > len(str(HTTP_READY_PREFIX_BYTES)):
        return 0
    length = head_end + (int(values[0].lstrip('0') or '0') if values else 0)
    return length if length <= HTTP_READY_PREFIX_BYTES else 0


class PrefetchedSocket:
    """唯一持有原生socket；不转移FD，用标准SocketIO回放前缀。"""

    # socket.makefile是标准库Python实现；SocketIO只依赖recv_into/send/fileno和引用回收接口。
    makefile = socket.socket.makefile
    __enter__ = socket.socket.__enter__
    __exit__ = socket.socket.__exit__
    _decref_socketios = socket.socket._decref_socketios

    def __init__(self, original: socket.socket, prefix: bytearray):
        self._socket = original
        self._prefix = prefix
        self._prefix_offset = 0
        self._io_refs = 0
        self._closed = False

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_socket'), name)

    def settimeout(self, timeout):
        return self._socket.settimeout(timeout)

    def recv_into(self, buffer, nbytes=0, flags=0):
        if self._prefix:
            if flags & ~socket.MSG_PEEK:
                raise ValueError('prefetched_recv_flags_unsupported')
            view = memoryview(buffer)
            count = min(nbytes or len(view), len(view), len(self._prefix) - self._prefix_offset)
            view[:count] = self._prefix[self._prefix_offset:self._prefix_offset + count]
            if not flags & socket.MSG_PEEK:
                self._prefix_offset += count
                if self._prefix_offset == len(self._prefix):
                    self._prefix = None
            return count
        return self._socket.recv_into(buffer, nbytes, flags)

    def recv(self, buffersize, flags=0):
        if self._prefix:
            result = bytearray(min(buffersize, len(self._prefix) - self._prefix_offset))
            count = self.recv_into(result, flags=flags)
            return bytes(result[:count])
        return self._socket.recv(buffersize, flags)

    def detach(self):
        if self._prefix:
            raise ValueError('prefetched_bytes_not_consumed')
        descriptor = self._socket.detach()
        self._closed = True
        return descriptor

    def close(self):
        self._closed = True
        if self._io_refs <= 0:
            try:
                self._socket.close()
            finally:
                # Future可暂留票据，关闭后不随之保留正文；file reader存在时沿标准socket寿命延后。
                self._prefix = None
