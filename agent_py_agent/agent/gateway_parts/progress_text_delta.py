"""可撤回的正文候选流；有界保留同调用投影上下文，不拥有模型或请求终态。"""
from __future__ import annotations

import re
from collections.abc import Callable

from ..common import log_redaction
from ..conversation import user_visible_text

# 沿用共用秘密语法，只补流切点，不另设一套凭据识别口径。
_OPEN_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]*\Z")
_KEY_TOKEN = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]+)")


# LLM: 每条请求单独持有；候选有内存上限，所有公开字符串先走原脱敏，终态仍由 result 决定。
# 类用途: 保留跨块安全尾部，按模型调用编号产出可撤回、可重放的增量事件。
class TextDeltaBuffer:
    def __init__(self, *, holdback_chars: int = 256, max_buffer_chars: int = 1_048_576,
                 project: Callable[[str], str] = log_redaction.redact_sensitive_text,
                 identifiers: tuple[tuple[object, str], ...] = (), flush_chars: int = 128,
                 max_projection_chars: int = 16_777_216) -> None:
        self.holdback_chars = max(1, int(holdback_chars))
        self.max_buffer_chars = max(self.holdback_chars, int(max_buffer_chars))
        self.flush_chars = max(1, min(int(flush_chars), self.holdback_chars))
        self.max_projection_chars = max(1, int(max_projection_chars))
        self._projection_remaining = self.max_projection_chars
        self.project = project
        self.identifiers = identifiers
        self.model_call_id = ""
        self.seq = 0
        self._pending: list[str] = []
        self._pending_chars = 0
        self._source = ""
        self._emitted = ""
        self._size = 0
        self._suspended = False

    # LLM: 只由绑定真实调用身份的宿主调用；切换时先撤回旧候选，不让旧回调自动选择新调用。
    # 函数用途: 开始一个模型调用，并返回需要清掉的旧显示事件。
    def begin(self, model_call_id: str) -> list[dict[str, object]]:
        if not isinstance(model_call_id, str) or not model_call_id.strip():
            return []
        if model_call_id == self.model_call_id:
            return []
        events = self.reset("superseded")
        self.model_call_id = model_call_id
        self.seq = 0
        self._suspended = False
        self._projection_remaining = self.max_projection_chars
        return events

    # LLM: 迟到写入不改变当前身份或序号；超预算撤回并静默该调用余下候选，防止丢掉秘密前缀后泄露尾段。
    # 函数用途: 接收一块真实候选，只发布已越过安全切点的部分。
    def append(self, text: str, *, model_call_id: str) -> list[dict[str, object]]:
        if not self.model_call_id or model_call_id != self.model_call_id or self._suspended or not text:
            return []
        self._size += len(text)
        if self._size > self.max_buffer_chars:
            return self._suspend()
        self._pending.append(text)
        self._pending_chars += len(text)
        if self._pending_chars < self.flush_chars:
            return []
        raw = self._source + "".join(self._pending)
        # 切点扫描、累计投影和前缀比较按三份字符计费；预算不足不再扫描或拼接后续候选。
        if 3 * len(raw) > self._projection_remaining:
            return self._suspend()
        self._projection_remaining -= 3 * len(raw)
        self._source, self._pending, self._pending_chars = raw, [], 0
        cut = _safe_cut(raw, len(raw) - self.holdback_chars, self.identifiers)
        if cut <= 0:
            return []
        # 整段投影保留已刷出键名和内部协议的上下文；逐块净化会漏掉后续值或错误拼接空白。
        projected = self.project(raw[:cut])
        events = []
        if not projected.startswith(self._emitted):
            events.append(self._reset_event("retry"))
            self.seq, self._emitted = 0, ""
        events.extend(self._delta(projected[len(self._emitted):]))
        return events

    # LLM: reset 是显示撤销，不是取消执行；retry 可保留同调用身份，其余边界拒绝后续旧写入。
    # 函数用途: 清掉重试、取消、工具调用或被替换的候选。
    def reset(self, reason: str, *, model_call_id: str = "") -> list[dict[str, object]]:
        if reason not in {"retry", "cancel", "tool_call", "superseded"}:
            raise ValueError("invalid_text_reset_reason")
        if not self.model_call_id or (model_call_id and model_call_id != self.model_call_id):
            return []
        event = self._reset_event(reason)
        self._pending, self._pending_chars = [], 0
        self._source = self._emitted = ""
        self._size = self.seq = 0
        self._projection_remaining = self.max_projection_chars
        self._suspended = False
        if reason != "retry":
            self.model_call_id = ""
        return [event]

    # LLM: 只有同调用完整采纳边界才能结束；若终稿校正了候选，先 reset 再发安全替代，绝不人工拆成假 token。
    # 函数用途: 排空安全尾部并标明该调用流结束；超限候选保持回退到权威 result。
    def finish(self, model_call_id: str, *, text: str | None = None) -> list[dict[str, object]]:
        if not self.model_call_id or model_call_id != self.model_call_id:
            return []
        events: list[dict[str, object]] = []
        if not self._suspended:
            raw = text if text is not None else self._source + "".join(self._pending)
            if len(raw) > self.max_buffer_chars or 3 * len(raw) > self._projection_remaining:
                events.extend(self._suspend())
            else:
                self._projection_remaining -= 3 * len(raw)
                full = self.project(raw)
                if not full.startswith(self._emitted):
                    events.append(self._reset_event("retry"))
                    self.seq, self._emitted = 0, ""
                events.extend(self._delta(full[len(self._emitted):]))
        events.append({"kind": "assistant_text_done", "model_call_id": model_call_id, "seq": max(0, self.seq - 1)})
        self.model_call_id = ""
        self._pending, self._pending_chars = [], 0
        self._source = self._emitted = ""
        self._size = 0
        return events

    # LLM: 只撤销显示，不改变执行；容量或累计处理预算耗尽后，本调用只允许 done，终稿仍沿 result 获取。
    # 函数用途: 清理有界候选并停止后续投影，防止逐字回调导致无限累计扫描。
    def _suspend(self) -> list[dict[str, object]]:
        event = self._reset_event("superseded")
        self._pending, self._pending_chars = [], 0
        self._source = self._emitted = ""
        self.seq = 0
        self._suspended = True
        return [event]

    def _reset_event(self, reason: str) -> dict[str, object]:
        return {"kind": "assistant_text_reset", "model_call_id": self.model_call_id, "reason": reason}

    def _delta(self, text: str) -> list[dict[str, object]]:
        if not text:
            return []
        event = {"kind": "assistant_text_delta", "model_call_id": self.model_call_id,
                 "seq": self.seq, "text": text, "level": "stream"}
        self.seq += 1
        self._emitted += text
        return [event]


# LLM: 固定 holdback 之外还保留跨切点的完整秘密语法、未闭合 URL 和可信标识；长凭据不能因超过 256 字而漏尾部。
# 函数用途: 寻找仍保有完整脱敏上下文的前缀，找不到就继续等待，不强行刷新。
def _safe_cut(raw: str, limit: int, identifiers: tuple) -> int:
    cut = max(0, limit)
    if cut <= 0:
        return 0
    # 不把一个 ASCII 词元（如 sk-、JWT、URL 或字段名）拆成两段分别脱敏。
    if cut < len(raw) and "!" <= raw[cut] <= "~":
        while cut > 0 and "!" <= raw[cut - 1] <= "~":
            cut -= 1
    for pattern in (log_redaction._SECRET_JSON_FIELD_RE, log_redaction._SECRET_QUOTED_ASSIGNMENT_RE,
                    log_redaction._SECRET_ASSIGNMENT_RE, log_redaction._PRIVATE_KEY_RE):
        for match in pattern.finditer(raw):
            if match.start() < cut < match.end():
                cut = match.start()
    for pattern in (log_redaction._AUTHORIZATION_KEY_RE, log_redaction._COOKIE_KEY_RE):
        matches = list(pattern.finditer(raw))
        for index, match in enumerate(matches):
            end = log_redaction._authorization_value_end(
                raw, match, matches[index + 1].start() if index + 1 < len(matches) else len(raw))
            if match.start() < cut < end:
                cut = match.start()
    if match := _OPEN_URL.search(raw):
        cut = min(cut, match.start())
    # 所有未闭合的角括号片段先等待完整标签；最终识别和移除仍只用原协议语法，普通 HTML 不另行删除。
    if (opening := raw.find("<", raw.rfind(">", 0, cut) + 1)) >= 0:
        cut = min(cut, opening)
    tail_start = len(raw.rstrip(" \t\r\n\"':="))
    for match in _KEY_TOKEN.finditer(raw):
        key = match.group(1)
        header = any(pattern.fullmatch(key + ":") for pattern in (
            log_redaction._AUTHORIZATION_KEY_RE, log_redaction._COOKIE_KEY_RE))
        if (log_redaction._is_credential_key_name(key) or header) and match.end() >= tail_start:
            cut = min(cut, match.start())
    # 与原公开正文共用协议语法：成对块不能切开，未闭方括号块等完整后再投影，不能先泄露参数。
    for pattern in (user_visible_text._BRACKET_TOOL_BLOCK_RE, user_visible_text._FENCED_TOOL_BLOCK_RE,
                    user_visible_text._XML_TOOL_BLOCK_RE, user_visible_text._UNCLOSED_XML_TOOL_BLOCK_RE,
                    user_visible_text._BARE_FUNCTION_BLOCK_RE, user_visible_text._MEMORY_CONTEXT_BLOCK_RE):
        for match in pattern.finditer(raw):
            if match.start() < cut < match.end():
                cut = match.start()
    folded = raw.casefold()
    for marker in user_visible_text._LINE_LEADING_TOOL_MARKERS:
        if marker.startswith("[/"):
            continue
        closing_start = folded.rfind(("[/" + marker[1:]).casefold())
        for match in re.finditer(r"(?im)^[ \t]*" + re.escape(marker), raw):
            if closing_start < match.end():
                cut = min(cut, match.start())
                break
    for needle, _ in identifiers:
        if not isinstance(needle, str) or not needle:
            continue
        position = raw.find(needle)
        while position >= 0:
            if position < cut < position + len(needle):
                cut = position
            position = raw.find(needle, position + len(needle))
        for length in range(1, min(len(needle), len(raw) + 1)):
            if raw.endswith(needle[:length]):
                cut = min(cut, len(raw) - length)
    return cut
