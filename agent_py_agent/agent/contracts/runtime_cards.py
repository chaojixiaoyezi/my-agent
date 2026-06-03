
from __future__ import annotations

from dataclasses import dataclass, field

from .contract_validation_recovery import recovery_for_findings

LONG_TASK_STATUSES = {"RUNNING", "WAITING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "REPAIRING"}
MESSAGE_TO_USER_DIRECTIONS = {"TO_USER", "USER"}


@dataclass(frozen=True)
class RuntimeCard:
    kind: str
    card_id: str
    owner_user_id: str
    status: str
    session_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    refs: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeCardValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    recovery: dict[str, object] | None = None


def validate_runtime_card_set(cards: list[RuntimeCard]) -> RuntimeCardValidation:
    findings: list[dict[str, str]] = []
    route_task_ids = _route_task_ids(cards)
    for card in cards:
        _validate_common(card, findings)
        _apply_kind_validation(card, route_task_ids, findings)
    return RuntimeCardValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("runtime_cards", findings),
    )


def _validate_common(card: RuntimeCard, findings: list[dict[str, str]]) -> None:
    if not card.card_id:
        findings.append(_finding("CARD_ID_MISSING", card.kind, "card_id is required"))
    if not card.owner_user_id:
        findings.append(_finding("CARD_OWNER_MISSING", card.card_id, "owner_user_id is required"))


def _validate_task(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    if _status(card) not in LONG_TASK_STATUSES:
        return
    if not str(card.refs.get("checkpoint_ref") or ""):
        findings.append(_finding("TASK_CHECKPOINT_REF_MISSING", card.card_id, "long task needs checkpoint_ref"))
    if card.card_id not in route_task_ids:
        findings.append(_finding("TASK_NOTIFICATION_ROUTE_MISSING", card.card_id, "long task needs notification route"))


def _validate_worker(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    if not card.task_id:
        findings.append(_finding("WORKER_TASK_ID_MISSING", card.card_id, "worker must be attached to a task"))


def _validate_message(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    direction = str(card.refs.get("direction") or "").upper()
    if direction in MESSAGE_TO_USER_DIRECTIONS and not card.session_id and not card.refs.get("notification_route_id"):
        findings.append(_finding("MESSAGE_ROUTE_MISSING", card.card_id, "message to user needs session or route"))


def _kind(card: RuntimeCard) -> str:
    return str(card.kind or "").upper()


def _status(card: RuntimeCard) -> str:
    return str(card.status or "").upper()


def _finding(code: str, card_id: str, detail: str) -> dict[str, str]:
    return {"code": code, "card_id": card_id, "detail": detail}


def _route_task_ids(cards: list[RuntimeCard]) -> set[str]:
    return {
        card.task_id
        for card in cards
        if _kind(card) == "NOTIFICATION_ROUTE" and _status(card) in {"ACTIVE", "PENDING"} and card.task_id
    }


def _apply_kind_validation(
    card: RuntimeCard,
    route_task_ids: set[str],
    findings: list[dict[str, str]],
) -> None:
    validator = {
        "TASK": _validate_task,
        "WORKER": _validate_worker,
        "MESSAGE": _validate_message,
    }.get(_kind(card))
    if validator is not None:
        validator(card, route_task_ids, findings)


__all__ = ["RuntimeCard", "RuntimeCardValidation", "validate_runtime_card_set"]
