
from __future__ import annotations

import json
from dataclasses import dataclass

from ..exploration_fuse_config import ExplorationFuseConfig


@dataclass(frozen=True)
class HintDeliveryInput:
    agent: object
    state: dict[str, object]
    config: ExplorationFuseConfig
    hint_round: int
    write_state: object


def should_prompt(
    config: ExplorationFuseConfig,
    state: dict[str, object],
    count: int,
) -> bool:
    return due_hint_round(config, state, count) is not None


def due_hint_round(
    config: ExplorationFuseConfig,
    state: dict[str, object],
    count: int,
) -> int | None:
    delivered = delivered_hint_rounds(state)
    return _due_interval_hint(config, delivered, count)


def _due_interval_hint(config: ExplorationFuseConfig, delivered: set[int], count: int) -> int | None:
    interval = max(1, int(config.local_progress_unlimited_hint_interval))
    hint_round = (count // interval) * interval
    if hint_round > 0 and hint_round not in delivered:
        return hint_round
    return None


def hint_message(count: int, hint_round: int) -> str:
    action_hint = (
        "请优先做一次本地落地动作，例如补充检查点、写阶段草稿、保存结构化数据、更新目标产物或调用构建工具；"
    )
    return (
        f"这是第 {hint_round} 轮本地进展固定提醒"
        f"（当前已连续只读/无本地推进 {count} 轮）。"
        "当前 closeout 仍未通过，且本地进展指纹没有变化。"
        f"{action_hint}"
        "如果还需要继续检索，也要同步留下可恢复、可验收的来源索引、草稿或阶段数据。"
    )


def mark_hint_delivered(request: HintDeliveryInput) -> None:
    delivered = delivered_hint_rounds(request.state)
    delivered.add(request.hint_round)
    next_state = dict(request.state)
    next_state["delivered_hint_rounds"] = sorted(delivered)
    request.write_state(request.agent, next_state)


def delivered_hint_rounds(state: dict[str, object]) -> set[int]:
    raw = state.get("delivered_hint_rounds")
    if not isinstance(raw, list | tuple):
        return set()
    delivered: set[int] = set()
    for item in raw:
        _append_positive_int(delivered, item)
    return delivered


def _append_positive_int(values: set[int], item: object) -> None:
    try:
        number = int(item)
    except (TypeError, ValueError):
        return
    if number > 0:
        values.add(number)


def recovery_signature(params) -> str:
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    recovery = contract.get("recovery") if isinstance(contract.get("recovery"), dict) else {}
    if not recovery:
        return ""
    payload = {
        "case_id": recovery.get("case_id") or "",
        "packet_ref": recovery.get("packet_ref") or "",
        "recommended_action": recovery.get("recommended_action") or "",
        "reason_codes": recovery.get("reason_codes") if isinstance(recovery.get("reason_codes"), list) else [],
        "schema_version": recovery.get("schema_version") or "",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
