"""Pure redacted config-parse facts; parsing/writer integration belongs to W11."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..contracts.diagnostics import DIAGNOSTIC_DETAILS_MAX_BYTES, redact_diagnostic_text


@dataclass(frozen=True)
class ConfigParseDiagnostic:
    """Source and 1-based location without raw configuration or disk writes."""

    source: str
    line: int | None
    column: int | None
    code: str
    message: str
    severity: Literal["warning", "error"]
    value_redacted: bool

    # LLM: 与DiagnosticEnvelope共用文本脱敏边界；只保存有界事实，不读配置或落盘。
    # 函数用途: 校验位置和严重度，遮住错误消息中可能出现的秘密值。
    def __post_init__(self) -> None:
        for key in ("source", "code", "message", "severity"):
            if type(getattr(self, key)) is not str:
                raise TypeError("config diagnostic text must be str")
        if self.severity not in ("warning", "error"):
            raise ValueError("config diagnostic severity must be warning or error")
        if type(self.value_redacted) is not bool:
            raise TypeError("config diagnostic value_redacted must be bool")
        for value in (self.line, self.column):
            if value is not None:
                if type(value) is not int:
                    raise TypeError("config diagnostic location must be int or None")
                if value < 1:
                    raise ValueError("config diagnostic locations are 1-based")
        redacted = self.value_redacted
        for key in ("source", "code", "message"):
            value = getattr(self, key)
            safe = redact_diagnostic_text(value)
            safe = safe.encode("utf-8", errors="replace")[:DIAGNOSTIC_DETAILS_MAX_BYTES // 4].decode(
                "utf-8", errors="ignore")
            object.__setattr__(self, key, safe)
            redacted = redacted or safe != value
        object.__setattr__(self, "value_redacted", redacted)
