"""宿主调用的临时凭据投影；值不进入回执、持久账或环境变量。"""
from __future__ import annotations

import codecs
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..common.log_redaction import redact_sensitive_output_text
from ..settings.secret_ref import HostCredentialSnapshot


@dataclass(frozen=True, kw_only=True)
class HostOutputRedaction:
    snapshot: HostCredentialSnapshot = field(default_factory=HostCredentialSnapshot, repr=False)
    force_default: bool = False

    def redact(self, text: object) -> str:
        return redact_host_text(text, secret_values=self.snapshot.secret_values)


def redact_host_text(text: object, *, secret_values: tuple[str, ...] = (), code_file: bool = False) -> str:
    value = str(text)
    for secret in sorted(set(secret_values), key=len, reverse=True):
        if secret:
            value = value.replace(secret, '<redacted>')
    return redact_sensitive_output_text(value, code_file=code_file)


class HostOutputStreamRedactor:
    """仅保留最大凭据长度的尾部；输出前遮住跨块匹配，独立用于每条流。"""
    def __init__(self, *, secret_values: tuple[str, ...] = ()):
        self._edges: list[dict[int, int]] = [{}]
        self._failure = [0]
        self._depth = [0]
        self._matches = [0]
        for value in dict.fromkeys(secret_values):
            node = 0
            for byte in value.encode('utf-8'):
                if byte not in self._edges[node]:
                    self._edges[node][byte] = len(self._edges)
                    self._edges.append({})
                    self._failure.append(0)
                    self._depth.append(self._depth[node] + 1)
                    self._matches.append(0)
                node = self._edges[node][byte]
            self._matches[node] = max(self._matches[node], len(value.encode('utf-8')))
        queue = deque(self._edges[0].values())
        while queue:
            node = queue.popleft()
            for byte, child in self._edges[node].items():
                fallback = self._failure[node]
                while fallback and byte not in self._edges[fallback]:
                    fallback = self._failure[fallback]
                self._failure[child] = self._edges[fallback].get(byte, 0)
                self._matches[child] = max(self._matches[child], self._matches[self._failure[child]])
                queue.append(child)
        self._state = 0
        self._pending: deque[int] = deque()
        self._masked_suffix = 0
        self._line = bytearray()
        self._line_limit = 65536
        self._suppress_line = False
        self._line_emitted = b''
        self._private_key = False

    def __repr__(self) -> str:
        return 'HostOutputStreamRedactor()'

    def _generic_feed(self, data: bytes, *, final: bool = False) -> bytes:
        # 未闭合词先保留，防止 sk-/Bearer/赋值凭据在下一块才完整成形；
        # 已确认的普通空白分隔前缀可立即展示，交互提示不必等待换行。
        output = bytearray()
        for part in data.splitlines(keepends=True):
            ended = part.endswith((b'\n', b'\r'))
            if not self._suppress_line:
                if len(part) > self._line_limit - len(self._line):
                    self._line.clear()
                    self._suppress_line = True
                    output.extend(b'<redacted-overlong-output-line>')
                else:
                    self._line.extend(part)
                    output.extend(self._project_line(complete=ended))
            if ended:
                if self._suppress_line:
                    output.extend(b'\n')
                self._line.clear()
                self._line_emitted = b''
                self._suppress_line = False
        if final and self._line:
            output.extend(self._project_line(complete=True))
            self._line.clear()
            self._line_emitted = b''
        return bytes(output)

    def _project_line(self, *, complete: bool) -> bytes:
        text = codecs.getincrementaldecoder('utf-8')(errors='replace').decode(bytes(self._line), final=complete)
        private_start = '-----BEGIN ' in text and 'PRIVATE KEY-----' in text
        if self._private_key or private_start:
            self._private_key = not ('-----END ' in text and 'PRIVATE KEY-----' in text)
            safe = '<redacted-private-key>' + ('\n' if text.endswith(('\n', '\r')) else '')
        else:
            safe = redact_sensitive_output_text(text)
        if not complete and safe == text:
            boundary = max((index + 1 for index, char in enumerate(text) if char.isspace()), default=0)
            # 常见无尾空格交互提示的标点不是凭据正文，保留即可交互。
            if text.endswith((':', '>', '$')):
                boundary = len(text)
            safe = safe[:boundary]
        encoded = safe.encode('utf-8')
        if not encoded.startswith(self._line_emitted):
            # 后续字符改变先前模式边界时不公开无法证明安全的续段。
            return b'\n' if complete and text.endswith(('\n', '\r')) else b''
        output = encoded[len(self._line_emitted):]
        self._line_emitted = encoded
        return output

    def feed(self, chunk: bytes) -> bytes:
        # Aho-Corasick 前缀状态只保留可能跨块命中的尾部，普通提示立即通过。
        output = bytearray()
        for byte in chunk:
            while self._state and byte not in self._edges[self._state]:
                self._state = self._failure[self._state]
            self._state = self._edges[self._state].get(byte, 0)
            self._pending.append(byte)
            matched = self._matches[self._state]
            if matched:
                start = (len(self._pending) - 1 if self._masked_suffix >= matched - 1
                         else max(0, len(self._pending) - matched))
                for index in range(start, len(self._pending)):
                    if self._pending[index] not in (10, 13):
                        self._pending[index] = 42
                self._masked_suffix = max(matched, self._masked_suffix + 1)
            else:
                self._masked_suffix = 0
            count = len(self._pending) - self._depth[self._state]
            if count:
                for _ in range(count):
                    output.append(self._pending.popleft())
                self._masked_suffix = min(self._masked_suffix, len(self._pending))
        return self._generic_feed(bytes(output))

    def finish(self) -> bytes:
        # EOF、取消或读错都不能证明这个前缀不是凭据；收尾只释放原长遮蔽值。
        # 普通非敏感尾部早已通过前缀状态机交给 generic 行缓冲，不受此处影响。
        tail = bytes(byte if byte in (10, 13) else 42 for byte in self._pending)
        self._pending.clear()
        self._state = 0
        return self._generic_feed(tail, final=True)


def redact_source_window(read_window: Callable[[int, int], str], *, offset: int,
        limit: int, total_chars: int, secret_values: tuple[str, ...] = ()) -> str:
    context = max((len(v) for v in secret_values if v), default=1) - 1
    start = max(0, offset - context)
    text = read_window(start, min(total_chars, offset + limit + context) - start)
    safe = list(text)
    for secret in secret_values:
        if not secret:
            continue
        position = text.find(secret)
        while position >= 0:
            for index in range(position, position + len(secret)):
                if safe[index] not in '\r\n':
                    safe[index] = '*'
            position = text.find(secret, position + 1)
    return ''.join(safe[offset - start:offset - start + limit])


def credential_projection_receipt(snapshot: HostCredentialSnapshot, *, withheld: bool = False) -> dict:
    result = {'coverage': snapshot.coverage, 'unavailable_sources': [
        {'field': status.field_name, 'reason_code': status.reason_code}
        for status in snapshot.unavailable_sources], 'content_status': 'withheld' if withheld else 'redacted'}
    if snapshot.snapshot_error_code:
        result['snapshot_error_code'] = snapshot.snapshot_error_code
    if withheld:
        result.update(reason_code='CREDENTIAL_SOURCE_PROJECTION_UNAVAILABLE',
            next_step='修复已声明的凭据来源后重新读取；其它宿主诊断可继续执行。')
    return result


def redact_host_tool_outcome(outcome, *, snapshot: HostCredentialSnapshot | None = None,
        force_default: bool = False):
    if snapshot is None:
        return outcome
    def clean(value):
        if isinstance(value, str):
            return redact_host_text(value, secret_values=snapshot.secret_values,
                code_file=not force_default and outcome.result_envelope.get('tool_output_policy', {}).get('redaction') == 'source_code')
        if isinstance(value, dict):
            return {clean(k): clean(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return type(value)(clean(v) for v in value)
        return value
    updates = {name: clean(getattr(outcome, name)) for name in (
        'output', 'result_envelope', 'recommended_action', 'recovery_hint', 'host_error',
        'source_effect', 'next_action', 'component_receipts')}
    envelope = updates['result_envelope']
    envelope.setdefault('credential_redaction', credential_projection_receipt(snapshot))
    if force_default:
        envelope['tool_output_policy'] = {**envelope.get('tool_output_policy', {}), 'redaction': 'default'}
    return replace(outcome, **updates)
