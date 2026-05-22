# LLM: Runtime card contracts keep long-task ownership, routing, and checkpoint facts machine-checkable.
# 模块用途: 提供最小运行时卡片校验，确保任务、worker、消息和通知路由具备可恢复所需的结构化字段。

from __future__ import annotations

from dataclasses import dataclass, field

from .contract_validation_recovery import recovery_for_findings

LONG_TASK_STATUSES = {"RUNNING", "WAITING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "REPAIRING"}
MESSAGE_TO_USER_DIRECTIONS = {"TO_USER", "USER"}


# LLM: RuntimeCard is the shared record shape for task/worker/message/runtime route facts.
# 类用途: 表示一张运行时卡片，承载任务、执行者、消息或通知路由的最小结构化身份字段。
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


# LLM: RuntimeCardValidation is the stable public result for runtime card checks.
# 类用途: 汇总运行时卡片校验结果，向调用方返回是否通过、错误码和逐项 finding。
@dataclass(frozen=True)
class RuntimeCardValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    recovery: dict[str, object] | None = None


# LLM: validate_runtime_card_set is the public runtime-card verifier used by tests and future runtime hooks.
# 函数用途: 校验一组运行时卡片是否满足长任务恢复、通知路由和 worker 归属的最小合同。
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


# LLM: _validate_common enforces fields that every runtime card must carry regardless of kind.
# 函数用途: 检查所有卡片共享的最小身份字段，例如 card_id 和 owner_user_id。
def _validate_common(card: RuntimeCard, findings: list[dict[str, str]]) -> None:
    if not card.card_id:
        findings.append(_finding("CARD_ID_MISSING", card.kind, "card_id is required"))
    if not card.owner_user_id:
        findings.append(_finding("CARD_OWNER_MISSING", card.card_id, "owner_user_id is required"))


# LLM: _validate_task adds long-task-specific recovery requirements on top of common card fields.
# 函数用途: 校验长任务卡片是否具备 checkpoint_ref 和 notification route，避免后台任务无法恢复或通知用户。
def _validate_task(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    if _status(card) not in LONG_TASK_STATUSES:
        return
    if not str(card.refs.get("checkpoint_ref") or ""):
        findings.append(_finding("TASK_CHECKPOINT_REF_MISSING", card.card_id, "long task needs checkpoint_ref"))
    if card.card_id not in route_task_ids:
        findings.append(_finding("TASK_NOTIFICATION_ROUTE_MISSING", card.card_id, "long task needs notification route"))


# LLM: _validate_worker ensures each worker card is anchored to a task before runtime dispatch.
# 函数用途: 检查 worker 是否绑定 task_id，避免游离执行者无法被任务账本接管。
def _validate_worker(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    if not card.task_id:
        findings.append(_finding("WORKER_TASK_ID_MISSING", card.card_id, "worker must be attached to a task"))


# LLM: _validate_message keeps user-facing messages routable without reading free-form chat text.
# 函数用途: 校验发给用户的消息卡片必须带 session_id 或 notification_route_id。
def _validate_message(card: RuntimeCard, route_task_ids: set[str], findings: list[dict[str, str]]) -> None:
    direction = str(card.refs.get("direction") or "").upper()
    if direction in MESSAGE_TO_USER_DIRECTIONS and not card.session_id and not card.refs.get("notification_route_id"):
        findings.append(_finding("MESSAGE_ROUTE_MISSING", card.card_id, "message to user needs session or route"))


# LLM: _kind canonicalizes runtime card kind values into a stable uppercase comparison key.
# 函数用途: 把 card.kind 归一成大写字符串，供验证逻辑按统一枚举比较。
def _kind(card: RuntimeCard) -> str:
    return str(card.kind or "").upper()


# LLM: _status canonicalizes runtime card status values before validation decisions.
# 函数用途: 把 card.status 归一成大写字符串，避免大小写差异影响合同判断。
def _status(card: RuntimeCard) -> str:
    return str(card.status or "").upper()


# LLM: _finding builds compact machine-readable runtime-card findings for reports and tests.
# 函数用途: 统一生成运行时卡片校验 finding，保持 code/location/detail 风格稳定。
def _finding(code: str, card_id: str, detail: str) -> dict[str, str]:
    return {"code": code, "card_id": card_id, "detail": detail}


# LLM: _route_task_ids indexes active notification routes so long-task checks can stay machine-driven.
# 函数用途: 收集当前有效通知路由关联的 task_id，供长任务校验判断是否具备可用通知出口。
def _route_task_ids(cards: list[RuntimeCard]) -> set[str]:
    return {
        card.task_id
        for card in cards
        if _kind(card) == "NOTIFICATION_ROUTE" and _status(card) in {"ACTIVE", "PENDING"} and card.task_id
    }


# LLM: _apply_kind_validation dispatches each card to its kind-specific validator without free-form branching elsewhere.
# 函数用途: 按卡片 kind 分派到任务、worker 或消息校验器，保持公共入口简洁且便于后续扩展。
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
