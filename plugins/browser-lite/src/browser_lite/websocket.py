# LLM: 纯标准库的最小 WebSocket 客户端，只服务本机 CDP：文本帧、客户端掩码、ping/pong、close、分片重组。
#   帧解析是无副作用的纯函数（便于单测），消息总长受 MAX_MESSAGE_BYTES 约束；不支持扩展、子协议或 TLS。
# 模块用途: 为 CDP 连接提供握手、帧编解码和按截止时间收发文本消息。

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024
OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_KNOWN_OPCODES = {OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG}
_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# LLM: 连接或协议层失败；调用方（CDP 层）统一转成“浏览器连接已断开”业务错误，不向用户暴露帧细节。
# 类用途: 表示 WebSocket 握手、帧格式或连接关闭错误。
class WebSocketError(Exception):
    pass


# LLM: 纯函数；按 RFC 6455 用 4 字节键循环异或，掩码与去掩码是同一操作。
# 函数用途: 对负载做 WebSocket 掩码变换。
def apply_mask(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b""
    repeated = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


# LLM: 客户端发出的帧必须掩码（RFC 6455 5.3）；mask_key 仅供测试固定，生产调用留空用随机键。只生成 FIN=1 的单帧。
# 函数用途: 把一条负载编码成带掩码的客户端帧，长度按 7 位 / 16 位 / 64 位三档写入。
def encode_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    key = os.urandom(4) if mask_key is None else mask_key
    if len(key) != 4 or opcode not in _KNOWN_OPCODES:
        raise ValueError("掩码键必须 4 字节，操作码必须已知")
    size = len(payload)
    header = bytearray([0x80 | opcode])
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", size)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", size)
    return bytes(header) + key + apply_mask(payload, key)


# LLM: 纯函数，不消费缓冲；数据不足返回 None，调用方补读后重试。expect_masked=False 时拒绝带掩码的服务端帧，
#   保留位非零、未知操作码、控制帧分片或超 125 字节、单帧超 limit 都抛 WebSocketError。
# 函数用途: 从字节缓冲头部解析一帧，返回 (fin, opcode, payload, 已用字节数)。
def parse_frame(data: bytes | bytearray, *, expect_masked: bool = False,
                limit: int = MAX_MESSAGE_BYTES) -> tuple[bool, int, bytes, int] | None:
    if len(data) < 2:
        return None
    first, second = data[0], data[1]
    fin, opcode, masked, size = bool(first & 0x80), first & 0x0F, bool(second & 0x80), second & 0x7F
    if first & 0x70 or opcode not in _KNOWN_OPCODES:
        raise WebSocketError("帧保留位非零或操作码未知")
    if masked != expect_masked:
        raise WebSocketError("帧掩码方向错误")
    offset = 2
    if size in (126, 127):
        width = 2 if size == 126 else 8
        if len(data) < offset + width:
            return None
        size = int.from_bytes(data[offset:offset + width], "big")
        offset += width
    if opcode >= OP_CLOSE and (not fin or size > 125):
        raise WebSocketError("控制帧不能分片或超过 125 字节")
    if size > limit:
        raise WebSocketError("消息超过大小上限")
    key = bytes(data[offset:offset + 4]) if masked else b""
    offset += len(key) if masked else 0
    if masked and len(key) < 4 or len(data) < offset + size:
        return None
    payload = bytes(data[offset:offset + size])
    return fin, opcode, apply_mask(payload, key) if masked else payload, offset + size


# LLM: 只接受文本消息；二进制消息、未开始就来的续帧、分片中途插入新数据帧、累计超限都拒绝。控制帧可插在分片之间。
# 类用途: 把一连串帧重组成完整文本消息，并把 ping/close 交还调用方处理。
class MessageAssembler:
    # LLM: 状态只属于一条连接；limit 约束重组后的总字节数。
    # 函数用途: 初始化空的分片状态。
    def __init__(self, limit: int = MAX_MESSAGE_BYTES):
        self.limit = limit
        self._opcode: int | None = None
        self._parts: list[bytes] = []
        self._size = 0

    # LLM: 返回 ("message", str) / ("ping", bytes) / ("close", bytes) / None（pong 或未完成的分片）。
    # 函数用途: 喂入一帧，按需返回完整消息或需要响应的控制帧。
    def feed(self, fin: bool, opcode: int, payload: bytes) -> tuple[str, object] | None:
        if opcode == OP_PING:
            return "ping", payload
        if opcode == OP_CLOSE:
            return "close", payload
        if opcode == OP_PONG:
            return None
        if opcode == OP_CONTINUATION and self._opcode is None:
            raise WebSocketError("收到没有起始帧的续帧")
        if opcode != OP_CONTINUATION:
            if self._opcode is not None:
                raise WebSocketError("分片未结束就开始新消息")
            self._opcode = opcode
        self._size += len(payload)
        if self._size > self.limit:
            raise WebSocketError("消息超过大小上限")
        self._parts.append(payload)
        if not fin:
            return None
        opcode, data = self._opcode, b"".join(self._parts)
        self._opcode, self._parts, self._size = None, [], 0
        if opcode != OP_TEXT:
            raise WebSocketError("不支持二进制消息")
        try:
            return "message", data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebSocketError("文本消息不是有效 UTF-8") from exc


# LLM: 只连本机 CDP（ws://，无 TLS、无 Origin 头）；一个实例只给一个线程用。收到对端 close 后回 close 并标记关闭，
#   之后收发都抛 WebSocketError。超时抛内置 TimeoutError，且不会丢失已读入缓冲的半帧。
# 类用途: 基于 socket 的 WebSocket 文本消息收发端。
class WebSocketClient:
    # LLM: 只保存已握手完成的 socket 和握手后剩余字节；构造本身不做网络操作。
    # 函数用途: 绑定连接与接收缓冲。
    def __init__(self, sock: socket.socket, leftover: bytes = b""):
        self.sock = sock
        self._buffer = bytearray(leftover)
        self._assembler = MessageAssembler()
        self.closed = False

    # LLM: 有副作用：建立 TCP 连接并发送 HTTP Upgrade；校验 101 状态与 Sec-WebSocket-Accept，失败关闭 socket。
    # 函数用途: 连接本机 CDP 地址并完成 WebSocket 握手。
    @classmethod
    def connect(cls, host: str, port: int, path: str, timeout: float) -> WebSocketClient:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                         .encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk or len(response) + len(chunk) > MAX_HANDSHAKE_BYTES:
                    raise WebSocketError("握手响应不完整或过长")
                response += chunk
            head, leftover = response.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            headers = {name.strip().lower(): value.strip() for name, _, value in
                       (line.partition(":") for line in lines[1:])}
            expected = base64.b64encode(hashlib.sha1((key + _ACCEPT_GUID).encode("ascii")).digest()).decode("ascii")
            if lines[0].split(" ")[1:2] != ["101"] or headers.get("sec-websocket-accept") != expected:
                raise WebSocketError("握手被拒绝")
            return cls(sock, leftover)
        except BaseException:
            sock.close()
            raise

    # LLM: 有副作用：写 socket。已关闭连接直接失败，不重连。
    # 函数用途: 发送一条文本消息。
    def send_text(self, text: str) -> None:
        if self.closed:
            raise WebSocketError("连接已关闭")
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8")))

    # LLM: 截止前拿不到完整消息抛 TimeoutError；途中自动回 pong；对端 close 时回 close 并抛 WebSocketError。
    # 函数用途: 按超时接收下一条完整文本消息。
    def recv_text(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            if self.closed:
                raise WebSocketError("连接已关闭")
            frame = parse_frame(self._buffer)
            if frame is None:
                self._fill(deadline)
                continue
            fin, opcode, payload, used = frame
            del self._buffer[:used]
            event = self._assembler.feed(fin, opcode, payload)
            if event is None:
                continue
            kind, value = event
            if kind == "message":
                return value
            if kind == "ping":
                self.sock.sendall(encode_frame(OP_PONG, value))
                continue
            self.close()
            raise WebSocketError("对端关闭了连接")

    # LLM: socket 超时只在截止时间内等待；对端 EOF 视为断开。
    # 函数用途: 从 socket 补读一块数据进接收缓冲。
    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("接收超时")
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(65536)
        if not chunk:
            self.closed = True
            raise WebSocketError("连接已断开")
        self._buffer += chunk

    # LLM: 幂等；尽力发送 close 帧（1000 正常关闭）后关闭 socket，发送失败忽略。
    # 函数用途: 关闭 WebSocket 连接。
    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
            except OSError:
                pass
        self.sock.close()
