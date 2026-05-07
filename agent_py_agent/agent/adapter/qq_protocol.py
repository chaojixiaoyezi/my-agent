# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。

"""WebSocket frame protocol (RFC 6455) — text frames only."""

from __future__ import annotations

import os
import struct


# LLM: WebSocketFrame 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 封装websocketframe相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class WebSocketFrame:
    """WebSocket frame parsing and construction (text frames only)."""

    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    # LLM: build_text_frame 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 构建文本frame所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_text_frame(payload: bytes, masked: bool = True) -> bytes:
        """Construct a text data frame (client -> server, must be masked)."""
        length = len(payload)
        first = 0x81

        if masked:
            mask_key = os.urandom(4)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            return _frame_header(first, length, masked=True) + mask_key + masked_payload
        return _frame_header(first, length, masked=False) + payload

    # LLM: build_close_frame 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 构建closeframe所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_close_frame() -> bytes:
        """Construct a close frame."""
        return bytes([0x88, 0x00])

    # LLM: build_ping_frame 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 构建pingframe所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_ping_frame() -> bytes:
        """Construct a ping frame."""
        return bytes([0x89, 0x00])

    # LLM: parse_frame 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 解析并归一化frame的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def parse_frame(data: bytes) -> tuple[int, bytes] | None:
        """Parse a server response frame. Returns (opcode, payload) or None."""
        if len(data) < 2:
            return None

        first = data[0]
        second = data[1]
        opcode = first & 0x0F
        has_mask = bool(second & 0x80)
        length = second & 0x7F

        length_info = _parse_payload_length(data, length)
        if length_info is None:
            return None
        length, offset = length_info

        mask_key, offset = _parse_mask(data, offset, has_mask)
        if mask_key is None and has_mask:
            return None

        if len(data) < offset + length:
            return None

        payload = data[offset : offset + length]
        if has_mask:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload


# LLM: _frame_header 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理frameheader相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _frame_header(first: int, length: int, *, masked: bool) -> bytes:
    mask_bit = 0x80 if masked else 0
    if length < 126:
        return bytes([first, mask_bit | length])
    if length < 65536:
        return bytes([first, mask_bit | 126]) + struct.pack(">H", length)
    return bytes([first, mask_bit | 127]) + struct.pack(">Q", length)


# LLM: _parse_payload_length 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 解析并归一化载荷length的输入形态，让下游只处理稳定结构；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _parse_payload_length(data: bytes, length: int) -> tuple[int, int] | None:
    if length == 126:
        if len(data) < 4:
            return None
        return struct.unpack(">H", data[2:4])[0], 4
    if length == 127:
        if len(data) < 10:
            return None
        return struct.unpack(">Q", data[2:10])[0], 10
    return length, 2


# LLM: _parse_mask 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 解析并归一化mask的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _parse_mask(data: bytes, offset: int, has_mask: bool) -> tuple[bytes | None, int]:
    if not has_mask:
        return b"", offset
    if len(data) < offset + 4:
        return None, offset
    return data[offset : offset + 4], offset + 4
