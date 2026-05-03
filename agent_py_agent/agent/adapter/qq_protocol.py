"""WebSocket frame protocol (RFC 6455) — text frames only."""

from __future__ import annotations

import os
import struct


class WebSocketFrame:
    """WebSocket frame parsing and construction (text frames only)."""

    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    @staticmethod
    def build_text_frame(payload: bytes, masked: bool = True) -> bytes:
        """Construct a text data frame (client -> server, must be masked)."""
        length = len(payload)
        first = 0x81

        if masked:
            mask_key = os.urandom(4)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if length < 126:
                second = 0x80 | length
                return bytes([first, second]) + mask_key + masked_payload
            elif length < 65536:
                second = 0x80 | 126
                return bytes([first, second]) + struct.pack(">H", length) + mask_key + masked_payload
            else:
                second = 0x80 | 127
                return bytes([first, second]) + struct.pack(">Q", length) + mask_key + masked_payload
        else:
            if length < 126:
                return bytes([first, length]) + payload
            elif length < 65536:
                return bytes([first, 126]) + struct.pack(">H", length) + payload
            else:
                return bytes([first, 127]) + struct.pack(">Q", length) + payload

    @staticmethod
    def build_close_frame() -> bytes:
        """Construct a close frame."""
        return bytes([0x88, 0x00])

    @staticmethod
    def build_ping_frame() -> bytes:
        """Construct a ping frame."""
        return bytes([0x89, 0x00])

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

        offset = 2
        if length == 126:
            if len(data) < 4:
                return None
            length = struct.unpack(">H", data[2:4])[0]
            offset = 4
        elif length == 127:
            if len(data) < 10:
                return None
            length = struct.unpack(">Q", data[2:10])[0]
            offset = 10

        if has_mask:
            if len(data) < offset + 4:
                return None
            mask_key = data[offset : offset + 4]
            offset += 4

        if len(data) < offset + length:
            return None

        payload = data[offset : offset + length]
        if has_mask:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload