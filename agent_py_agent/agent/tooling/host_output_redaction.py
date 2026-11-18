"""宿主调用的临时凭据投影与回合级脱敏快照；值不进入回执、持久账或环境变量。"""
from __future__ import annotations

import base64
import codecs
import hashlib
import json
import re
import secrets
import threading
import weakref
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from ..common.log_redaction import (
    _CREDENTIAL_REDACTED,
    _PROTOCOL_CONTROL_KEYS,
    bind_active_credential_log_values,
    configured_secret_redaction,
    redact_sensitive_output_text,
    redact_sensitive_text,
    register_active_turn_log_values,
    unregister_active_turn_log_values,
)
from ..settings.secret_ref import HostCredentialSnapshot, configured_credential_snapshot

# 宿主脱敏会处理的结果字段；封记摘要只覆盖这些字段，任何一个变化都会重新脱敏。
_REDACTED_OUTCOME_FIELDS = (
    'output', 'result_envelope', 'recommended_action', 'recovery_hint', 'host_error',
    'source_effect', 'next_action', 'component_receipts')
# 已脱敏结果上的封记属性名：保存 (快照令牌, force_default, 字段内容摘要)，不含任何凭据或其摘要。
_REDACTION_SEAL_ATTR = '_host_redaction_seal'
# 凭据快照 -> 本进程随机令牌；快照对象释放即失效，序列化后的封记在别处永远匹配不上。
_SNAPSHOT_TOKENS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SNAPSHOT_TOKENS_LOCK = threading.Lock()
_CREDENTIAL_REDACTED_BYTES = _CREDENTIAL_REDACTED.encode('utf-8')
_TURN_REDACTION_ATTR = '_turn_credential_redaction'
_MAX_TURN_RAW_VALUES = 256
_MAX_TURN_RAW_BYTES = 512 * 1024
_MAX_ACTIVE_TURNS_PER_SCOPE = 64
_MAX_ACTIVE_SCOPES = 1024
_UNICODE_ESCAPE_RE = re.compile(r'\\u([0-9a-f]{4})')
_PERCENT_HEX_RE = re.compile(r'%[0-9A-F]{2}')


class _CredentialSecretTuple(tuple):
    """兼容既有 tuple 接口，同时向流/结果/日志净化层标记携卡凭据变体集合。"""

    def __new__(cls, values: Iterable[str], *, credential_values: tuple[str, ...] = ()):
        instance = super().__new__(cls, values)
        instance.credential_values = tuple(v for v in dict.fromkeys(credential_values) if v)
        return instance

    def __repr__(self) -> str:
        return '_CredentialSecretTuple()'

    def __reduce__(self):
        raise TypeError('credential secret snapshot is non-serializable')


def credential_echo_variants(values: Iterable[str]) -> tuple[str, ...]:
    """生成携卡字段值的确定性回显变体（原文、JSON转义、URL/表单编码、标准与URL-safe Base64含去padding），按长度降序。"""
    variants: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw or not raw.strip():
            continue
        variants.add(raw)
        ascii_escaped = json.dumps(raw, ensure_ascii=True)[1:-1]
        variants.add(ascii_escaped)
        if '\\u' in ascii_escaped:
            variants.add(_UNICODE_ESCAPE_RE.sub(lambda m: '\\u' + m.group(1).upper(), ascii_escaped))
        variants.add(json.dumps(raw, ensure_ascii=False)[1:-1])
        pct = quote(raw, safe='')
        variants.add(pct)
        if '%' in pct:
            variants.add(_PERCENT_HEX_RE.sub(lambda m: m.group(0).lower(), pct))
        plus = quote_plus(raw, safe='')
        variants.add(plus)
        if '%' in plus:
            variants.add(_PERCENT_HEX_RE.sub(lambda m: m.group(0).lower(), plus))
        utf8 = raw.encode('utf-8')
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(utf8).decode('ascii')
            variants.add(encoded)
            stripped = encoded.rstrip('=')
            if stripped:
                variants.add(stripped)
    variants.discard('')
    return tuple(sorted(variants, key=len, reverse=True))


def _material_raw_values(material: object) -> tuple[str, ...]:
    """从 CredentialMaterial 提取全部字段原值及 HTTP Basic 组合，供变体扩展与回合注册共用。"""
    raw_map = getattr(material, 'values', None)
    if not isinstance(raw_map, Mapping):
        return ()
    collected: list[str] = []
    users: list[str] = []
    secrets_list: list[str] = []
    for key, val in raw_map.items():
        if not isinstance(val, str) or not val:
            continue
        collected.append(val)
        lowered = str(key or '').strip().lower()
        if lowered in {'username', 'user', 'account', 'login', 'email'}:
            users.append(val)
        elif lowered in {'secret', 'password', 'passwd', 'pass', 'token', 'api_key', 'access_token'}:
            secrets_list.append(val)
    for user in users:
        for sec in secrets_list:
            collected.append(f'{user}:{sec}')
    return tuple(dict.fromkeys(collected))


@dataclass(frozen=True, kw_only=True)
class HostOutputRedaction:
    snapshot: HostCredentialSnapshot = field(default_factory=HostCredentialSnapshot, repr=False)
    force_default: bool = False
    _credential_use: object | None = field(default=None, repr=False, compare=False)

    def redact(self, text: object) -> str:
        return redact_host_text(text, secret_values=self.snapshot.secret_values)

    def with_credential_values(self, values: Iterable[str]) -> HostOutputRedaction:
        cred_variants = credential_echo_variants(values)
        existing_cred = getattr(self.snapshot.secret_values, 'credential_values', ())
        merged_cred = tuple(sorted(set(existing_cred) | set(cred_variants), key=len, reverse=True))
        if not merged_cred:
            return self
        merged_all = tuple(dict.fromkeys((*merged_cred, *self.snapshot.secret_values)))
        wrapped = _CredentialSecretTuple(merged_all, credential_values=merged_cred)
        return replace(self, snapshot=replace(self.snapshot, secret_values=wrapped))


def redact_host_text(text: object, *, secret_values: tuple[str, ...] = (), code_file: bool = False) -> str:
    value = str(text)
    cred_values = tuple(getattr(secret_values, 'credential_values', ()) or ())
    cred_set = frozenset(cred_values)
    for secret in sorted(cred_set, key=len, reverse=True):
        if secret:
            value = value.replace(secret, _CREDENTIAL_REDACTED)
    for secret in sorted(set(secret_values) - cred_set, key=len, reverse=True):
        if secret:
            value = value.replace(secret, '<redacted>')
    marker = _CREDENTIAL_REDACTED if cred_set else '<redacted>'
    with configured_secret_redaction(secret_values):
        return redact_sensitive_output_text(value, code_file=code_file, redacted_marker=marker)


class HostOutputStreamRedactor:
    """仅保留最大凭据长度的尾部；输出前遮住跨块匹配，独立用于每条流。"""

    def __init__(self, *, secret_values: tuple[str, ...] = ()):
        cred_values = tuple(v for v in getattr(secret_values, 'credential_values', ()) if v)
        self._credential_mode = bool(cred_values)
        self._cred_set = frozenset(v.encode('utf-8') for v in cred_values)
        self._edges: list[dict[int, int]] = [{}]
        self._failure = [0]
        self._depth = [0]
        self._matches = [0]
        self._terminal_kind: list[int] = [0]  # 0=none, 1=legacy_mask, 2=credential_redacted
        self._max_pattern_len = 0
        for value in dict.fromkeys(secret_values):
            if not value:
                continue
            encoded = value.encode('utf-8')
            self._max_pattern_len = max(self._max_pattern_len, len(encoded))
            node = 0
            for byte in encoded:
                if byte not in self._edges[node]:
                    self._edges[node][byte] = len(self._edges)
                    self._edges.append({})
                    self._failure.append(0)
                    self._depth.append(self._depth[node] + 1)
                    self._matches.append(0)
                    self._terminal_kind.append(0)
                node = self._edges[node][byte]
            self._matches[node] = max(self._matches[node], len(encoded))
            kind = 2 if encoded in self._cred_set else 1
            self._terminal_kind[node] = max(self._terminal_kind[node], kind)
        if not self._credential_mode:
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
        self._cred_buf = bytearray()
        self._masked_suffix = 0
        self._line = bytearray()
        self._line_limit = 65536
        self._suppress_line = False
        self._line_emitted = b''
        self._private_key = False

    def __repr__(self) -> str:
        return 'HostOutputStreamRedactor()'

    def reset_discard(self) -> None:
        """丢弃尚未公开的跨块前缀与行缓冲，绝不在 reset/重连/关闭时提前释放疑似秘密前缀。"""
        self._state = 0
        self._pending.clear()
        self._cred_buf.clear()
        self._masked_suffix = 0
        self._line.clear()
        self._line_emitted = b''
        self._suppress_line = False
        self._private_key = False

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
        marker = _CREDENTIAL_REDACTED if self._credential_mode else '<redacted>'
        if self._private_key or private_start:
            self._private_key = not ('-----END ' in text and 'PRIVATE KEY-----' in text)
            safe = '<redacted-private-key>' + ('\n' if text.endswith(('\n', '\r')) else '')
        else:
            safe = redact_sensitive_output_text(text, redacted_marker=marker)
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

    def _probe_trie_at(self, buf: bytearray, start: int, *, final: bool) -> tuple[int, int, bool]:
        """从 start 沿前缀树扫描，返回 (最远匹配结束下标, 匹配种类, 是否仍有未决前缀需等待更多字节)。"""
        node = 0
        best_end = -1
        best_kind = 0
        n = len(buf)
        for idx in range(start, n):
            nxt = self._edges[node].get(buf[idx])
            if nxt is None:
                return best_end, best_kind, False
            node = nxt
            t_kind = self._terminal_kind[node]
            if t_kind:
                best_end = idx + 1
                best_kind = max(best_kind, t_kind)
        still_extendable = bool(self._edges[node]) and not final
        if node != 0 and not final:
            return best_end, best_kind, (still_extendable or best_end < 0)
        return best_end, best_kind, False

    def _drain_credential_buf(self, *, final: bool) -> bytes:
        buf = self._cred_buf
        if not buf:
            return b''
        out = bytearray()
        pos = 0
        n = len(buf)
        max_span = max(65536, self._max_pattern_len * 4)
        while pos < n:
            end, kind, need_more = self._probe_trie_at(buf, pos, final=final)
            if need_more:
                break
            if end < 0:
                if final:
                    # 检查 pos 是否是某个已知秘密的未完成前缀（仅在真实前缀命中时遮蔽尾段）
                    node = 0
                    prefix_alive = True
                    for idx in range(pos, n):
                        nxt = self._edges[node].get(buf[idx])
                        if nxt is None:
                            prefix_alive = False
                            break
                        node = nxt
                    if prefix_alive and node != 0:
                        out.extend(_CREDENTIAL_REDACTED_BYTES)
                        pos = n
                        break
                out.append(buf[pos])
                pos += 1
                continue
            # 合并区间内重叠的后续秘密命中（例如 username 与 username_secret 或交叠变体）
            scan = pos + 1
            paused = False
            while scan < end and (end - pos) < max_span:
                sub_end, sub_kind, sub_more = self._probe_trie_at(buf, scan, final=final)
                if sub_more:
                    paused = True
                    break
                if sub_end > end:
                    end = sub_end
                    kind = max(kind, sub_kind)
                scan += 1
            if paused:
                break
            if kind == 2:
                out.extend(_CREDENTIAL_REDACTED_BYTES)
            else:
                out.extend(bytes(42 if b not in (10, 13) else b for b in buf[pos:end]))
            pos = end
        if pos > 0:
            del buf[:pos]
        return bytes(out)

    def feed(self, chunk: bytes) -> bytes:
        if self._credential_mode:
            if chunk:
                self._cred_buf.extend(chunk)
            settled = self._drain_credential_buf(final=False)
            if self._cred_set:
                with configured_secret_redaction(
                    _CredentialSecretTuple(
                        tuple(v.decode('utf-8', 'ignore') for v in self._cred_set),
                        credential_values=tuple(v.decode('utf-8', 'ignore') for v in self._cred_set),
                    )
                ):
                    return self._generic_feed(settled, final=False)
            return self._generic_feed(settled, final=False)
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
        if self._credential_mode:
            settled = self._drain_credential_buf(final=True)
            self._cred_buf.clear()
            if self._cred_set:
                with configured_secret_redaction(
                    _CredentialSecretTuple(
                        tuple(v.decode('utf-8', 'ignore') for v in self._cred_set),
                        credential_values=tuple(v.decode('utf-8', 'ignore') for v in self._cred_set),
                    )
                ):
                    return self._generic_feed(settled, final=True)
            return self._generic_feed(settled, final=True)
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


# LLM: 同一凭据快照、同一 force_default 下，内容与上次脱敏输出逐字段一致（摘要相同）的结果直接返回，不再重复整轮
#   正则；结果被替换字段或原位改写任何受脱敏字段后摘要失配，照旧完整脱敏。保留所有调用点，任何一层都不靠外部证明。
# 函数用途: 对一次工具结果做宿主凭据脱敏，并为输出打上内容封记。
def redact_host_tool_outcome(outcome, *, snapshot: HostCredentialSnapshot | None = None,
        force_default: bool = False):
    if snapshot is None:
        return outcome
    token = _snapshot_token(snapshot)
    seal = getattr(outcome, _REDACTION_SEAL_ATTR, None)
    if (token and isinstance(seal, tuple) and seal[:2] == (token, bool(force_default))
            and seal[2:] == (_outcome_digest(outcome),)):
        return outcome
    result = _redact_host_tool_outcome(outcome, snapshot=snapshot, force_default=force_default)
    digest = _outcome_digest(result)
    if token and digest:
        try:
            object.__setattr__(result, _REDACTION_SEAL_ATTR, (token, bool(force_default), digest))
        except (AttributeError, TypeError):
            pass
    return result


# LLM: 令牌只是本进程随机串，与凭据值无关；不可弱引用的快照返回空串（永不封记，照旧每次脱敏）。
# 函数用途: 取得某个凭据快照在本进程内的封记令牌。
def _snapshot_token(snapshot: HostCredentialSnapshot) -> str:
    try:
        with _SNAPSHOT_TOKENS_LOCK:
            token = _SNAPSHOT_TOKENS.get(snapshot)
            if token is None:
                token = secrets.token_hex(16)
                _SNAPSHOT_TOKENS[snapshot] = token
            return token
    except TypeError:
        return ''


# LLM: 摘要基于受脱敏字段的 repr；repr 失败（自定义对象异常、过深嵌套）返回空串，调用方不封记也不跳过。
# 函数用途: 计算结果受脱敏字段的内容摘要。
def _outcome_digest(outcome) -> str:
    try:
        values = tuple(getattr(outcome, name) for name in _REDACTED_OUTCOME_FIELDS)
        return hashlib.sha256(repr(values).encode('utf-8', 'surrogatepass')).hexdigest()
    except Exception:  # noqa: BLE001 - 摘要失败只放弃跳过，不能改变脱敏结果
        return ''


# LLM: 原逐字段脱敏实现；字符串走宿主凭据替换与通用模式，容器递归，协议控制键保留键名，动态键照常脱敏。
# 函数用途: 生成一份脱敏后的工具结果副本。
def _redact_host_tool_outcome(outcome, *, snapshot: HostCredentialSnapshot, force_default: bool):
    is_code = (
        not force_default
        and outcome.result_envelope.get('tool_output_policy', {}).get('redaction') == 'source_code'
    )

    def clean_key(key):
        if isinstance(key, str) and key not in _PROTOCOL_CONTROL_KEYS:
            return redact_host_text(key, secret_values=snapshot.secret_values, code_file=is_code)
        return key

    def clean(value):
        if isinstance(value, str):
            return redact_host_text(value, secret_values=snapshot.secret_values, code_file=is_code)
        if isinstance(value, dict):
            return {clean_key(k): clean(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return type(value)(clean(v) for v in value)
        return value

    updates = {name: clean(getattr(outcome, name)) for name in _REDACTED_OUTCOME_FIELDS}
    envelope = updates['result_envelope']
    existing_receipt = envelope.get('credential_redaction')
    if not (isinstance(existing_receipt, Mapping) and existing_receipt.get('content_status') == 'withheld'):
        envelope['credential_redaction'] = credential_projection_receipt(snapshot)
    if force_default:
        envelope['tool_output_policy'] = {**envelope.get('tool_output_policy', {}), 'redaction': 'default'}
    return replace(outcome, **updates)


class TurnCredentialStreamFilter:
    """为单条模型输出流或思考流提供跨块凭据回显净化，reset/关闭时绝不提前吐出未决前缀。"""

    def __init__(self, turn_redaction: TurnCredentialRedaction) -> None:
        self._turn = turn_redaction
        self._version = -1
        self._redactor: HostOutputStreamRedactor | None = None
        self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._pending_tail = b''

    def _sync(self) -> HostOutputStreamRedactor | None:
        version, snapshot = self._turn._snapshot_state()
        if not self._turn.active:
            return None
        if self._redactor is None or self._version != version:
            # 升级快照时不能泄漏旧状态机里尚未结算的字节，将其转入新状态机重喂。
            carry = b''
            if self._redactor is not None:
                carry = bytes(self._redactor._cred_buf) + bytes(self._redactor._line)
                self._redactor.reset_discard()
            self._redactor = HostOutputStreamRedactor(secret_values=snapshot.secret_values)
            self._version = version
            if carry:
                out = self._redactor.feed(carry)
                if out:
                    self._pending_tail += out
        return self._redactor

    def feed_text(self, text: str, *, final: bool = False) -> str:
        if self._turn.withheld:
            return '[内容因凭据脱敏额度超限已扣留]' if final or text else ''
        redactor = self._sync()
        if redactor is None:
            return text
        raw = (text or '').encode('utf-8')
        out = self._pending_tail
        self._pending_tail = b''
        if raw:
            out += redactor.feed(raw)
        if final:
            out += redactor.finish()
        return self._decoder.decode(out, final=final)

    def discard(self) -> None:
        self._pending_tail = b''
        self._decoder.reset()
        if self._redactor is not None:
            self._redactor.reset_discard()


# 按 (scope_root, thread_id) 隔离的活动回合表；两级有界 OrderedDict + 显式 close / weakref 自动清理。
_ACTIVE_TURN_SCOPES_LOCK = threading.RLock()
_ACTIVE_TURN_SCOPES: OrderedDict[tuple[str, str], OrderedDict[str, TurnCredentialRedaction]] = OrderedDict()
_PARAMS_TURN_REDACTIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _normalize_scope_path(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return text


def _register_active_scope(keys: Iterable[tuple[str, str]], turn_key: str, item: TurnCredentialRedaction) -> None:
    evicted_log_keys: list[str] = []
    with _ACTIVE_TURN_SCOPES_LOCK:
        for scope_key in keys:
            if not scope_key[0]:
                continue
            bucket = _ACTIVE_TURN_SCOPES.get(scope_key)
            if bucket is None:
                bucket = OrderedDict()
                _ACTIVE_TURN_SCOPES[scope_key] = bucket
                while len(_ACTIVE_TURN_SCOPES) > _MAX_ACTIVE_SCOPES:
                    _, dropped_bucket = _ACTIVE_TURN_SCOPES.popitem(last=False)
                    for dropped in dropped_bucket.values():
                        dropped._clear_secrets_unlocked()
                        evicted_log_keys.append(f"{dropped.owner_key}::{dropped.turn_key}")
            else:
                _ACTIVE_TURN_SCOPES.move_to_end(scope_key)
            bucket[turn_key] = item
            bucket.move_to_end(turn_key)
            while len(bucket) > _MAX_ACTIVE_TURNS_PER_SCOPE:
                _, evicted = bucket.popitem(last=False)
                evicted._clear_secrets_unlocked()
                evicted_log_keys.append(f"{evicted.owner_key}::{evicted.turn_key}")
    for log_key in evicted_log_keys:
        unregister_active_turn_log_values(log_key)


def _unregister_active_scope(keys: Iterable[tuple[str, str]], turn_key: str, item: TurnCredentialRedaction) -> None:
    with _ACTIVE_TURN_SCOPES_LOCK:
        for scope_key in keys:
            bucket = _ACTIVE_TURN_SCOPES.get(scope_key)
            if bucket is None:
                continue
            if bucket.get(turn_key) is item:
                bucket.pop(turn_key, None)
            if not bucket:
                _ACTIVE_TURN_SCOPES.pop(scope_key, None)


class TurnCredentialRedaction:
    """按 (owner, thread, run/request) 隔离的回合级凭据脱敏对象，跨线程显式传递并在回合收尾立即释放。"""

    def __init__(
        self,
        *,
        owner_key: str,
        conversation_root: str = '',
        thread_id: str = '',
        turn_key: str = '',
        base_redaction: HostOutputRedaction | None = None,
    ) -> None:
        self.owner_key = str(owner_key or 'anonymous')
        self.conversation_root = _normalize_scope_path(conversation_root)
        self.thread_id = str(thread_id or '')
        self.turn_key = str(turn_key or secrets.token_hex(8))
        self._lock = threading.RLock()
        self._base = base_redaction or HostOutputRedaction()
        self._current = self._base
        self._raw_values: list[str] = []
        self._raw_bytes = 0
        self._variants: tuple[str, ...] = ()
        self._withheld = False
        self._closed = False
        self._version = 0

    def __repr__(self) -> str:
        return 'TurnCredentialRedaction()'

    def __reduce__(self):
        raise TypeError('TurnCredentialRedaction is non-serializable')

    def __enter__(self) -> TurnCredentialRedaction:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _scope_keys(self) -> tuple[tuple[str, str], ...]:
        keys: list[tuple[str, str]] = [(f'owner:{self.owner_key}', self.thread_id)]
        if self.conversation_root:
            keys.append((f'conv:{self.conversation_root}', self.thread_id))
            if self.thread_id:
                keys.append((f'conv:{self.conversation_root}', ''))
        if self.thread_id:
            keys.append((f'owner:{self.owner_key}', ''))
        return tuple(dict.fromkeys(keys))

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._closed and (bool(self._variants) or self._withheld)

    @property
    def withheld(self) -> bool:
        with self._lock:
            return self._withheld

    @property
    def redaction(self) -> HostOutputRedaction:
        with self._lock:
            return self._current

    def _snapshot_state(self) -> tuple[int, HostCredentialSnapshot]:
        with self._lock:
            return self._version, self._current.snapshot

    def new_stream_filter(self) -> TurnCredentialStreamFilter:
        return TurnCredentialStreamFilter(self)

    def register(self, material: object) -> HostOutputRedaction:
        return self.register_values(_material_raw_values(material))

    def register_values(self, values: Iterable[str]) -> HostOutputRedaction:
        with self._lock:
            if self._closed:
                raise ValueError('当前回合凭据脱敏作用域已关闭，拒绝注册新凭据')
            added = False
            seen = set(self._raw_values)
            for raw in values:
                if not isinstance(raw, str) or not raw or not raw.strip() or raw in seen:
                    continue
                size = len(raw.encode('utf-8'))
                if len(self._raw_values) + 1 > _MAX_TURN_RAW_VALUES or self._raw_bytes + size > _MAX_TURN_RAW_BYTES:
                    self._withheld = True
                    self._version += 1
                    break
                seen.add(raw)
                self._raw_values.append(raw)
                self._raw_bytes += size
                added = True
            if added or not self._variants and self._raw_values:
                self._variants = credential_echo_variants(self._raw_values)
                self._current = self._base.with_credential_values(self._variants)
                self._version += 1
            variants = self._variants
            current = self._current
        if variants or self._withheld:
            _register_active_scope(self._scope_keys(), self.turn_key, self)
        if variants:
            bind_active_credential_log_values(variants)
            register_active_turn_log_values(f"{self.owner_key}::{self.turn_key}", variants)
        return current

    def public_text(self, text: str) -> str:
        with self._lock:
            withheld = self._withheld
            variants = self._variants
            snapshot = self._current.snapshot
        if withheld:
            return '[内容因凭据脱敏额度超限已扣留]'
        if not variants:
            return redact_sensitive_text(str(text))
        return redact_host_text(str(text), secret_values=snapshot.secret_values, code_file=False)

    def redact_value(self, value: Any) -> Any:
        with self._lock:
            withheld = self._withheld
            variants = self._variants
            snapshot = self._current.snapshot
        if withheld:
            return '[内容因凭据脱敏额度超限已扣留]'
        if not variants:
            return value
        return _clean_public_structure(value, snapshot.secret_values)

    def contains_credential_echo(self, text: str) -> bool:
        with self._lock:
            if self._withheld:
                return True
            variants = self._variants
        if not variants or not text:
            return False
        return any(secret in text for secret in variants)

    def _clear_secrets_unlocked(self) -> None:
        self._raw_values.clear()
        self._raw_bytes = 0
        self._variants = ()
        self._current = self._base
        self._closed = True
        self._version += 1

    def close(self) -> None:
        keys = self._scope_keys()
        log_key = f"{self.owner_key}::{self.turn_key}"
        with self._lock:
            self._clear_secrets_unlocked()
        _unregister_active_scope(keys, self.turn_key, self)
        unregister_active_turn_log_values(log_key)


def _clean_public_structure(value: Any, secret_values: tuple[str, ...], *, depth: int = 0) -> Any:
    if depth >= 64:
        return _CREDENTIAL_REDACTED
    if isinstance(value, str):
        return redact_host_text(value, secret_values=secret_values, code_file=False)
    if isinstance(value, BaseException):
        return redact_host_text(str(value), secret_values=secret_values, code_file=False)
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            safe_k = (
                redact_host_text(k, secret_values=secret_values, code_file=False)
                if isinstance(k, str) and k not in _PROTOCOL_CONTROL_KEYS
                else k
            )
            out[safe_k] = _clean_public_structure(v, secret_values, depth=depth + 1)
        return out
    if isinstance(value, tuple):
        return tuple(_clean_public_structure(v, secret_values, depth=depth + 1) for v in value)
    if isinstance(value, list):
        return [_clean_public_structure(v, secret_values, depth=depth + 1) for v in value]
    return value


def _extract_turn_identity(agent: object, params: object) -> tuple[str, str, str, str]:
    home = getattr(agent, 'home_paths', None)
    owner_dir = _normalize_scope_path(getattr(home, 'owner_home_dir', None))
    owner_id = str(getattr(home, 'owner_id', '') or '')
    owner_key = owner_dir or owner_id or f'agent:{id(agent)}'
    store = getattr(agent, 'conversation_store', None)
    conv_root = _normalize_scope_path(getattr(getattr(store, 'storage', None), 'root', None))
    attrs = getattr(params, 'task_attributes', None)
    attrs = attrs if isinstance(attrs, Mapping) else {}
    thread_id = str(
        attrs.get('conversation_thread_id')
        or getattr(params, 'thread_id', '')
        or getattr(getattr(params, 'on_chunk', None), 'conversation_thread_id', '')
        or ''
    ).strip()
    turn_key = str(
        getattr(params, 'request_id', '')
        or getattr(params, 'run_id', '')
        or getattr(params, 'task_id', '')
        or ''
    ).strip()
    return owner_key, conv_root, thread_id, turn_key


def _base_redaction_for_agent(agent: object) -> HostOutputRedaction:
    config = getattr(agent, 'config', None)
    try:
        snapshot = configured_credential_snapshot(config)
    except Exception:  # noqa: BLE001
        snapshot = HostCredentialSnapshot(snapshot_error_code='CREDENTIAL_SNAPSHOT_UNAVAILABLE')
    return HostOutputRedaction(snapshot=snapshot, force_default=False)


def turn_credential_redaction(agent: object, params: object = None) -> TurnCredentialRedaction:
    """获取或建立与当前 (owner, thread, run/request) 绑定的回合脱敏对象，并显式挂接到流写入器。"""
    resolved_params = params if params is not None else getattr(agent, '_current_run_params', None)
    existing: TurnCredentialRedaction | None = None
    if resolved_params is not None:
        existing = getattr(resolved_params, _TURN_REDACTION_ATTR, None)
        if existing is None:
            try:
                with _ACTIVE_TURN_SCOPES_LOCK:
                    existing = _PARAMS_TURN_REDACTIONS.get(resolved_params)
            except TypeError:
                existing = None
    owner_key, conv_root, thread_id, turn_key = _extract_turn_identity(agent, resolved_params)
    if existing is not None and not existing._closed and existing.owner_key == owner_key:
        if thread_id and not existing.thread_id:
            existing.thread_id = thread_id
        if conv_root and not existing.conversation_root:
            existing.conversation_root = conv_root
        _bind_writer_if_present(resolved_params, existing)
        return existing
    if turn_key:
        with _ACTIVE_TURN_SCOPES_LOCK:
            bucket = _ACTIVE_TURN_SCOPES.get((f'owner:{owner_key}', thread_id))
            found = bucket.get(turn_key) if bucket is not None else None
            if found is not None and not found._closed:
                existing = found
    if existing is None:
        existing = TurnCredentialRedaction(
            owner_key=owner_key,
            conversation_root=conv_root,
            thread_id=thread_id,
            turn_key=turn_key or f'turn-{secrets.token_hex(8)}',
            base_redaction=_base_redaction_for_agent(agent),
        )
    if resolved_params is not None:
        try:
            with _ACTIVE_TURN_SCOPES_LOCK:
                _PARAMS_TURN_REDACTIONS[resolved_params] = existing
            weakref.finalize(resolved_params, existing.close)
        except TypeError:
            try:
                object.__setattr__(resolved_params, _TURN_REDACTION_ATTR, existing)
            except (AttributeError, TypeError):
                pass
    _bind_writer_if_present(resolved_params, existing)
    return existing


def _bind_writer_if_present(params: object, turn_redaction: TurnCredentialRedaction) -> None:
    writer = getattr(params, 'on_chunk', None) if params is not None else None
    binder = getattr(writer, 'bind_credential_redaction', None)
    if callable(binder):
        try:
            binder(turn_redaction)
        except Exception:  # noqa: BLE001
            pass


def active_turn_redactions_for_scope(
    *,
    owner_key: str = '',
    conversation_root: str | Path | None = None,
    thread_id: str = '',
    turn_keys: Iterable[str] = (),
) -> tuple[TurnCredentialRedaction, ...]:
    """按严格 owner/存储根和线程检索活动中的凭据脱敏绑定（O(1) 内存查找，零磁盘扫描，跨 owner 绝对隔离）。"""
    conv = _normalize_scope_path(conversation_root)
    candidate_scopes: list[tuple[str, str]] = []
    if owner_key:
        if thread_id:
            candidate_scopes.append((f'owner:{owner_key}', thread_id))
        candidate_scopes.append((f'owner:{owner_key}', ''))
    if conv:
        if thread_id:
            candidate_scopes.append((f'conv:{conv}', thread_id))
        candidate_scopes.append((f'conv:{conv}', ''))
    wanted_turns = {str(k).strip() for k in turn_keys if str(k or '').strip()}
    matched: list[TurnCredentialRedaction] = []
    exact_turn_matched: list[TurnCredentialRedaction] = []
    with _ACTIVE_TURN_SCOPES_LOCK:
        for scope_key in candidate_scopes:
            bucket = _ACTIVE_TURN_SCOPES.get(scope_key)
            if not bucket:
                continue
            for t_key, item in list(bucket.items()):
                if not item.active:
                    continue
                if thread_id and item.thread_id and item.thread_id != thread_id:
                    continue
                if item not in matched:
                    matched.append(item)
                if wanted_turns and t_key in wanted_turns and item not in exact_turn_matched:
                    exact_turn_matched.append(item)
    return tuple(exact_turn_matched or matched)


__all__ = [
    'HostOutputRedaction',
    'HostOutputStreamRedactor',
    'TurnCredentialRedaction',
    'TurnCredentialStreamFilter',
    'active_turn_redactions_for_scope',
    'credential_echo_variants',
    'credential_projection_receipt',
    'redact_host_text',
    'redact_host_tool_outcome',
    'redact_source_window',
    'turn_credential_redaction',
]
