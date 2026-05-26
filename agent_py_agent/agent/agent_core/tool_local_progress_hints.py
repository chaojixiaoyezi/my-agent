# LLM: Local-progress hints are soft rework guidance, not a hidden task-specific gate.
# 模块用途: 计算本地进展提醒轮次、百分比和中文返工提示，并记录已提示轮次。

from __future__ import annotations

import json
from dataclasses import dataclass

from .exploration_fuse_config import ExplorationFuseConfig


@dataclass(frozen=True)
class HintDeliveryInput:
    agent: object
    state: dict[str, object]
    payload: dict[str, object]
    config: ExplorationFuseConfig
    hint_round: int
    write_state: object


def should_prompt_or_block(
    payload: dict[str, object],
    config: ExplorationFuseConfig,
    state: dict[str, object],
    count: int,
) -> bool:
    threshold = exploration_round_threshold(payload, config)
    if threshold <= 0:
        return due_hint_round(payload, config, state, count) is not None
    return due_hint_round(payload, config, state, count) is not None or count >= threshold


def due_hint_round(
    payload: dict[str, object],
    config: ExplorationFuseConfig,
    state: dict[str, object],
    count: int,
) -> int | None:
    threshold = exploration_round_threshold(payload, config)
    if threshold > 0 and count >= threshold:
        return None
    delivered = delivered_hint_rounds(state)
    if threshold <= 0:
        return _unlimited_due_hint(config, delivered, count)
    crossed = [hint for hint in hint_rounds(threshold, config) if hint <= count and hint not in delivered]
    return max(crossed) if crossed else None


def _unlimited_due_hint(config: ExplorationFuseConfig, delivered: set[int], count: int) -> int | None:
    interval = max(1, int(config.local_progress_unlimited_hint_interval))
    hint_round = (count // interval) * interval
    if hint_round > 0 and hint_round not in delivered:
        return hint_round
    return None


def hint_rounds(threshold: int, config: ExplorationFuseConfig) -> tuple[int, ...]:
    del config
    if threshold <= 0:
        return ()
    first = max(1, threshold // 3)
    second = max(first + 1, (threshold * 2) // 3)
    return tuple(item for item in (first, second) if item < threshold)


def used_percent(threshold: int, hint_round: int) -> int:
    if threshold <= 0:
        return 0
    hints = hint_rounds(threshold, ExplorationFuseConfig())
    if hints and hint_round == hints[0]:
        return 33
    if len(hints) > 1 and hint_round == hints[1]:
        return 66
    return int((hint_round * 100) / threshold)


def hint_message(threshold: int, count: int, hint_round: int, percent: int) -> str:
    action_hint = (
        "请优先做一次本地落地动作，例如补充检查点、写阶段草稿、保存结构化数据、更新目标产物或调用构建工具；"
    )
    if threshold <= 0:
        return (
            f"本地进展门配置为 0，不会因次数阻断；这是第 {hint_round} 轮固定提醒"
            f"（当前已连续只读/无本地推进 {count} 轮）。"
            f"{action_hint}"
            "如果还需要继续检索，也要同步留下可恢复、可验收的本地进展。"
        )
    return (
        f"结构化交付状态显示你已消耗 {percent}% 的本地进展返工额度"
        f"（{count}/{threshold} 轮）。"
        "当前 closeout 仍未通过，且本地进展指纹没有变化。"
        f"{action_hint}"
        "如果还需要继续检索，也要同步留下来源索引、草稿或阶段数据。"
    )


def mark_hint_delivered(request: HintDeliveryInput) -> None:
    delivered = delivered_hint_rounds(request.state)
    threshold = exploration_round_threshold(request.payload, request.config)
    if threshold <= 0:
        delivered.add(request.hint_round)
    else:
        delivered.update(item for item in hint_rounds(threshold, request.config) if item <= request.hint_round)
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


def exploration_round_threshold(payload: dict[str, object], config: ExplorationFuseConfig) -> int:
    if "no_progress_block_threshold" in payload and payload.get("no_progress_block_threshold") is not None:
        return _threshold_value(payload.get("no_progress_block_threshold"), config)
    return config.local_progress_round_threshold


def _threshold_value(raw: object, config: ExplorationFuseConfig) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = config.local_progress_round_threshold
    return max(0, value)
