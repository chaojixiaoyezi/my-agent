from __future__ import annotations

"""Process-wide log redaction at the LogRecord creation boundary.

Business modules should log useful structured context without each one having to
remember every credential shape.  This module owns the mandatory last boundary
before a record reaches stderr, journald, a file handler, or a third-party
handler.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

_REDACTED = "<redacted>"
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "app_secret",
        "authorization",
        "client_secret",
        "credential",
        "encrypt_key",
        "id_token",
        "master_key",
        "password",
        "passwd",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "signature",
        "tenant_access_token",
        "ticket",
        "token",
        "verification_token",
        "x-amz-credential",
        "x-amz-signature",
    }
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_key|access_token|api_?key|client_secret|credential|id_token|"
    r"password|refresh_token|secret|signature|tenant_access_token|ticket|token|"
    r"verification_token|x-amz-credential|x-amz-signature)=)([^&#\s\]\)]+)"
)
_AUTHORIZATION_RE = re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)([^\s,;]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access_key|access_token|api_?key|app_secret|client_secret|master_key|"
    r"password|passwd|private_key|pwd|refresh_token|secret|tenant_access_token|ticket|token|"
    r"verification_token)\b\s*[:=]\s*)([\"']?)([^\s,;&\"']{4,})(\2)"
)
_SECRET_JSON_FIELD_RE = re.compile(
    r"(?i)([\"'](?:access_key|access_token|api_?key|app_secret|authorization|client_secret|"
    r"credential|id_token|master_key|password|passwd|private_key|pwd|refresh_token|secret|signature|"
    r"tenant_access_token|ticket|token|verification_token)[\"']\s*:\s*)"
    r"([\"'])([^\"'\r\n]{1,})(\2)"
)
_DB_PASSWORD_RE = re.compile(
    r"(?i)((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)"
)
_KNOWN_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{10,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"pypi-[A-Za-z0-9_-]{10,}|npm_[A-Za-z0-9]{10,})(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)


def redact_sensitive_text(
    value: object,
    *,
    code_file: bool = False,
    redacted_marker: str = _REDACTED,
    redact_assignment_labels: bool = False,
) -> str:
    """Return a safe string; preserve source-code assignments when requested.

    ``code_file`` follows 长期助手' file/code boundary: known credential shapes,
    authorization headers, private keys, URL credentials and database passwords
    are still removed, while generic ``TOKEN=value`` and JSON-field rules are
    skipped so ordinary source examples are not corrupted.
    """

    text = str(value)
    if not text:
        return text
    text = _SENSITIVE_QUERY_RE.sub(
        lambda match: (
            redacted_marker
            if redact_assignment_labels
            else match.group(1) + redacted_marker
        ),
        text,
    )
    text = _AUTHORIZATION_RE.sub(
        lambda match: (
            redacted_marker
            if redact_assignment_labels
            else match.group(1) + redacted_marker
        ),
        text,
    )
    if not code_file:
        text = _SECRET_JSON_FIELD_RE.sub(
            lambda match: (
                redacted_marker
                if redact_assignment_labels
                else match.group(1) + match.group(2) + redacted_marker + match.group(4)
            ),
            text,
        )
        text = _SECRET_ASSIGNMENT_RE.sub(
            lambda match: (
                redacted_marker
                if redact_assignment_labels
                else match.group(1) + match.group(2) + redacted_marker + match.group(4)
            ),
            text,
        )
    text = _DB_PASSWORD_RE.sub(
        lambda match: match.group(1) + redacted_marker + match.group(3),
        text,
    )
    text = _KNOWN_SECRET_RE.sub(redacted_marker, text)
    return _PRIVATE_KEY_RE.sub("<redacted-private-key>", text)


def redact_sensitive_value(
    value: Any,
    *,
    field_name: str = "",
    code_file: bool = False,
) -> Any:
    """Recursively redact strings and values under credential-named fields."""

    if _field_is_sensitive(field_name):
        return _REDACTED
    if isinstance(value, str):
        return redact_sensitive_text(value, code_file=code_file)
    if isinstance(value, BaseException):
        return redact_sensitive_text(value, code_file=code_file)
    if isinstance(value, Mapping):
        return {
            key: redact_sensitive_value(
                item,
                field_name=str(key),
                code_file=code_file,
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_value(item, code_file=code_file)
            for item in value
        )
    if isinstance(value, list):
        return [
            redact_sensitive_value(item, code_file=code_file)
            for item in value
        ]
    return value


def _field_is_sensitive(name: str) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES


def install_log_redaction() -> None:
    """Install the idempotent process-wide LogRecord redaction factory."""

    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_my_agent_secret_redactor", False):
        return

    def _redacting_factory(
        name: str,
        level: int,
        pathname: str,
        lineno: int,
        msg: object,
        args: object,
        exc_info: object,
        func: str | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        record = current_factory(name, level, pathname, lineno, msg, args, exc_info, func, sinfo)
        record.msg = redact_sensitive_value(record.msg)
        record.args = redact_sensitive_value(record.args)
        return record

    _redacting_factory._my_agent_secret_redactor = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_redacting_factory)


class RedactingFormatter(logging.Formatter):
    """Defense-in-depth formatter for handlers created by my-agent."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


__all__ = [
    "RedactingFormatter",
    "install_log_redaction",
    "redact_sensitive_text",
    "redact_sensitive_value",
]
