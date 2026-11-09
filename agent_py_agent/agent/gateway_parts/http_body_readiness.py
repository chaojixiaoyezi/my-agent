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


class PrefetchedSocket(socket.socket):
    """接管同一FD，消费过的唯一前缀先原样回放，再从内核读取；无第二份正文。"""

    def __init__(self, original: socket.socket, prefix: bytearray):
        timeout = original.gettimeout()
        super().__init__(original.family, original.type, original.proto, fileno=original.detach())
        try:
            self.settimeout(timeout)
            self._prefix = prefix
            self._prefix_offset = 0
        except BaseException as error:
            try:
                super().close()
            except BaseException as cleanup:
                error.socket_cleanup_error = cleanup
            raise

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
        return super().recv_into(buffer, nbytes, flags)

    def recv(self, buffersize, flags=0):
        if self._prefix:
            result = bytearray(min(buffersize, len(self._prefix) - self._prefix_offset))
            count = self.recv_into(result, flags=flags)
            return bytes(result[:count])
        return super().recv(buffersize, flags)

    def close(self):
        try:
            super().close()
        finally:
            # Future回调可暂留已结束票据；已无file reader时不把其正文前缀一起保留。
            if not getattr(self, '_io_refs', 0):
                self._prefix = None
