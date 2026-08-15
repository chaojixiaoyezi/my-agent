
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class OfflineContractValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    ignored_event_ids: tuple[str, ...] = ()
    recovery: dict[str, object] | None = None


def validation_report(
    findings: list[dict[str, object]],
    *,
    ignored_event_ids: tuple[str, ...] = (),
) -> OfflineContractValidation:
    return OfflineContractValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        ignored_event_ids=ignored_event_ids,
        recovery=recovery_for_findings("offline_contract", findings),
    )


def finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


def dict_items(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(item_text for item in value for item_text in (text(item),) if item_text)


def positive_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineContractValidation", "dict_items", "finding", "positive_int", "string_tuple", "text", "validation_report"]
