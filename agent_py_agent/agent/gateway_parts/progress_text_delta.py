"""可撤回的正文候选流；有界保留同调用投影上下文，不拥有模型或请求终态。"""
from __future__ import annotations

import re
from collections.abc import Callable

from ..common import log_redaction
from ..conversation import user_visible_text
from ..conversation.channels import _HOST_ABSOLUTE_PATH_RE

# 沿用共用秘密语法，只补流切点，不另设一套凭据识别口径。
_OPEN_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]*\Z")
_KEY_TOKEN = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]+)")
_UNCLOSED_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_calls?|tool_results?|function_calls?|function_responses?|function)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _retention_literals(identifiers: tuple) -> dict[str, None]:
    return dict.fromkeys(
        value for value in (*(value for value, _ in identifiers), *log_redaction._CONFIGURED_SECRET_VALUES.get())
        if isinstance(value, str) and value
    )


def _literal_prefix_table(needle: str) -> list[int]:
    table = [0] * len(needle)
    matched = 0
    for index in range(1, len(needle)):
        while matched and needle[index] != needle[matched]:
            matched = table[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
        table[index] = matched
    return table


def _literal_suffix_length(raw: str, needle: str, table: list[int]) -> int:
    matched = 0
    # 完整匹配由 find 处理；这里只保留可能和下一块拼成完整字面的后缀。
    start = max(0, len(raw) - len(needle) + 1)
    for char in raw[start:]:
        while matched and char != needle[matched]:
            matched = table[matched - 1]
        if char == needle[matched]:
            matched += 1
    return matched


class IncrementalRetentionProjector:
    """只对还可能跨切点的输入投影，已消费前缀不再进入扫描。"""

    def __init__(self, *, project: Callable[[str], str] = log_redaction.redact_sensitive_text,
                 holdback_chars: int = 256, flush_chars: int = 128,
                 identifiers: tuple[tuple[object, str], ...] = ()) -> None:
        self.project = project
        self.holdback_chars = holdback_chars
        self.flush_chars = flush_chars
        self.identifiers = identifiers
        self.work = 0
        self._tail: list[str] = []
        self._size = 0
        self._anchor = ""
        self._next_scan = holdback_chars + flush_chars
        self._structured_document = False
        self._literal_prefixes: dict[str, list[int]] = {}

    def advance(self, new_text: str) -> str:
        if not new_text or self._structured_document:
            return ""
        self._tail.append(new_text)
        self._size += len(new_text)
        if self._size < self._next_scan:
            return ""

        raw = self._anchor + "".join(self._tail)
        anchor_size = len(self._anchor)
        self.work += 3 * len(raw)
        if not self._anchor:
            # 完整 JSON 的整根解析可能重写前面的字段与空白；交给原终态投影。
            initial = raw.lstrip()
            if initial.startswith(("{", "[")):
                opening, following = initial[0], initial[1:].lstrip()[:1]
                if not following or following in ('"}' if opening == '{' else '[{"-0123456789tfn]'):
                    self._structured_document = True
                    self._tail.clear()
                    self._size = 0
                    return ""

        literals = _retention_literals(self.identifiers)
        if self._literal_prefixes.keys() != literals.keys():
            self._literal_prefixes = {
                needle: self._literal_prefixes[needle] if needle in self._literal_prefixes else _literal_prefix_table(needle)
                for needle in literals
            }
        cut = _safe_cut(raw, len(raw) - self.holdback_chars, self.identifiers,
                        literal_prefixes=self._literal_prefixes)
        candidate = raw[:cut] if cut and not raw[cut - 1].isspace() else ""
        consumed = 0
        delta = ""
        if len(candidate) > anchor_size:
            projected = self.project(candidate)
            end = len(candidate) - 1
            while end > anchor_size and not candidate[end - 1].isspace():
                end -= 1
            # 等到结尾的正文词可见才消费。以协议块结束时，原投影会裁掉的
            # 换行仍可能位于两段正文之间，须和后续正文一起处理。
            if projected.startswith(self._anchor) and projected.endswith(candidate[end:]):
                consumed = len(candidate) - anchor_size
                delta = projected[anchor_size:]
                final_char = candidate[-1]
                # 保留词边界和非行首状态，同时避免把已发布的原词带回候选。
                self._anchor = "x" if final_char.isalnum() or final_char == "_" else "."

        pending = raw[anchor_size + consumed:]
        self._tail = [pending] if pending else []
        self._size = len(pending)
        # 未闭合词元/协议可长于保留窗口。等到新输入足以支付再次扫描，
        # 避免每个回调都重新遍历整段未决材料。
        self._next_scan = self._size + max(self.flush_chars, self._size - self.holdback_chars)
        return delta


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
        self._source: list[str] = []
        self._emitted: list[str] = []
        self._projector = self._new_projector()
        self._size = 0
        self._suspended = False

    def _new_projector(self) -> IncrementalRetentionProjector:
        return IncrementalRetentionProjector(project=self.project, holdback_chars=self.holdback_chars,
                                             flush_chars=self.flush_chars, identifiers=self.identifiers)

    def _clear_candidate(self) -> None:
        self._source.clear()
        self._emitted.clear()
        self._projector = self._new_projector()

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
        self._source.append(text)
        self._projector.project = self.project
        self._projector.identifiers = self.identifiers
        previous_work = self._projector.work
        delta = self._projector.advance(text)
        self._projection_remaining -= self._projector.work - previous_work
        if self._projection_remaining < 0:
            return self._suspend()
        return self._delta(delta)

    # LLM: reset 是显示撤销，不是取消执行；retry 可保留同调用身份，其余边界拒绝后续旧写入。
    # 函数用途: 清掉重试、取消、工具调用或被替换的候选。
    def reset(self, reason: str, *, model_call_id: str = "") -> list[dict[str, object]]:
        if reason not in {"retry", "cancel", "tool_call", "superseded"}:
            raise ValueError("invalid_text_reset_reason")
        if not self.model_call_id or (model_call_id and model_call_id != self.model_call_id):
            return []
        event = self._reset_event(reason)
        self._clear_candidate()
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
            raw = text if text is not None else "".join(self._source)
            if len(raw) > self.max_buffer_chars or 3 * len(raw) > self._projection_remaining:
                events.extend(self._suspend())
            else:
                self._projection_remaining -= 3 * len(raw)
                full = self.project(raw)
                emitted = "".join(self._emitted)
                if not full.startswith(emitted):
                    events.append(self._reset_event("retry"))
                    self.seq = 0
                    self._emitted.clear()
                    emitted = ""
                events.extend(self._delta(full[len(emitted):]))
        events.append({"kind": "assistant_text_done", "model_call_id": model_call_id, "seq": max(0, self.seq - 1)})
        self.model_call_id = ""
        self._clear_candidate()
        self._size = 0
        return events

    # LLM: 只撤销显示，不改变执行；容量或累计处理预算耗尽后，本调用只允许 done，终稿仍沿 result 获取。
    # 函数用途: 清理有界候选并停止后续投影，防止逐字回调导致无限累计扫描。
    def _suspend(self) -> list[dict[str, object]]:
        event = self._reset_event("superseded")
        self._clear_candidate()
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
        self._emitted.append(text)
        return [event]


# LLM: 固定holdback外保留完整秘密、URL、标识和宿主路径；复用原投影语法及调用方预算，不新增状态或认证I/O。
# 函数用途: 寻找仍保有完整脱敏上下文的前缀，找不到就继续等待，不强行刷新。
def _safe_cut(raw: str, limit: int, identifiers: tuple, *,
              literal_prefixes: dict[str, list[int]] | None = None) -> int:
    cut = max(0, limit)
    if cut <= 0:
        return 0
    # 不把一个 ASCII 词元（如 sk-、JWT、URL 或字段名）拆成两段分别脱敏。
    if cut < len(raw) and "!" <= raw[cut] <= "~":
        while cut > 0 and "!" <= raw[cut - 1] <= "~":
            cut -= 1
    while cut and raw[cut - 1].isspace():
        cut -= 1
    guarded_spans = []
    for pattern in (log_redaction._SECRET_JSON_FIELD_RE, log_redaction._SECRET_QUOTED_ASSIGNMENT_RE,
                    log_redaction._SECRET_ASSIGNMENT_RE, log_redaction._PRIVATE_KEY_RE):
        guarded_spans.extend((match.start(), match.end()) for match in pattern.finditer(raw) if match.start() < cut)
    for pattern in (log_redaction._AUTHORIZATION_KEY_RE, log_redaction._COOKIE_KEY_RE):
        matches = list(pattern.finditer(raw))
        for index, match in enumerate(matches):
            end = log_redaction._authorization_value_end(
                raw, match, matches[index + 1].start() if index + 1 < len(matches) else len(raw))
            if match.start() < cut:
                guarded_spans.append((match.start(), end))
    log_redaction._redact_cli_arguments(raw, "", True, spans=guarded_spans)
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
    protocol_spans = []
    for pattern in (user_visible_text._BRACKET_TOOL_BLOCK_RE, user_visible_text._FENCED_TOOL_BLOCK_RE,
                    user_visible_text._XML_TOOL_BLOCK_RE, user_visible_text._BARE_FUNCTION_BLOCK_RE,
                    user_visible_text._MEMORY_CONTEXT_BLOCK_RE):
        for match in pattern.finditer(raw):
            if match.start() < cut:
                protocol_spans.append((match.start(), match.end()))
    # 原投影的独占协议行与直接工具标签依赖行首和成对闭合。
    for match in user_visible_text._PLAIN_TEXT_TOOL_LINE_RE.finditer(raw):
        if match.start() < cut:
            protocol_spans.append((match.start(), match.end()))
    direct_end = 0
    for match in user_visible_text._DIRECT_XML_TOOL_OPEN_RE.finditer(raw):
        if match.start() >= cut:
            break
        if match.start() < direct_end:
            continue
        close = re.compile(rf"</{re.escape(match.group('tag'))}\s*>", re.IGNORECASE).search(raw, match.end())
        if close is None or cut < close.end():
            cut = match.start()
            break
        direct_end = close.end()
        protocol_spans.append((match.start(), direct_end))
    guarded_spans.extend(protocol_spans)
    protocol_spans.sort()
    spans = iter(protocol_spans)
    span = next(spans, None)
    covered_until = 0
    for marker in user_visible_text._LINE_LEADING_MARKER_RE.finditer(raw):
        if marker.start() >= cut:
            break
        while span is not None and span[0] <= marker.start():
            covered_until = max(covered_until, span[1])
            span = next(spans, None)
        if marker.start() >= covered_until:
            cut = marker.start()
            break
    unclosed_xml_probe = user_visible_text._XML_TOOL_BLOCK_RE.sub(
        lambda match: " " * (match.end() - match.start()), raw
    )
    if (match := _UNCLOSED_XML_TOOL_BLOCK_RE.search(unclosed_xml_probe)) and match.start() < cut:
        guarded_spans.append((match.start(), match.end()))
    folded = raw.casefold()
    for marker in user_visible_text._LINE_LEADING_TOOL_MARKERS:
        if marker.startswith("[/"):
            continue
        closing_start = folded.rfind(("[/" + marker[1:]).casefold())
        for match in re.finditer(r"(?im)^[ \t]*" + re.escape(marker), raw):
            if closing_start < match.end():
                cut = min(cut, match.start())
                break
    if literal_prefixes is None:
        literal_prefixes = {needle: _literal_prefix_table(needle) for needle in _retention_literals(identifiers)}
    for needle, table in literal_prefixes.items():
        cut = min(cut, len(raw) - _literal_suffix_length(raw, needle, table))
    for needle in literal_prefixes:
        position = raw.find(needle)
        while 0 <= position < cut:
            end = position + len(needle)
            guarded_spans.append((position, end))
            position = raw.find(needle, end)
    # 复用投影路径语法；虚拟末段只补齐未完整的POSIX首段或末尾斜杠，不进入实际投影。
    path_probe = raw + ("_" if raw.endswith("/") else "/_")
    guarded_spans.extend(
        (match.start(), match.end()) for match in _HOST_ABSOLUTE_PATH_RE.finditer(path_probe) if match.start() < cut
    )
    # 从右向左收拢相交边界；较早的协议、路径或字面值可覆盖后续切点。
    for start, end in sorted(guarded_spans, reverse=True):
        if start < cut < end:
            cut = start
    return cut
