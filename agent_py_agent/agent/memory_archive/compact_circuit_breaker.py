from __future__ import annotations

"""compact 连续失败熔断：防 thrash loop。

连续失败熔断：compact 连续失败累计达阈值即
open，一段冷却期内不再自动 compact，避免反复失败空烧 API（终端交互 实测 thrash
可日烧 250K calls）。冷却期过后 half-open 允许重试一次；一次成功即清零回到 closed。

状态持久化在 workspace/compact/circuit_breaker.json，跨 turn 生效。所有函数显式
接收 now，便于测试；写状态失败只吞掉，绝不打断主链路。
"""

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COMPACT_FAILURE_THRESHOLD = 3
DEFAULT_COMPACT_COOLDOWN_SECONDS = 300.0


@dataclass(frozen=True)
class CompactCircuitState:
    consecutive_failures: int = 0
    opened_at: float = 0.0
    last_status: str = ""
    total_failures: int = 0


def compact_circuit_path(workspace: Path | str) -> Path:
    return Path(workspace) / "compact" / "circuit_breaker.json"


def read_compact_circuit(workspace: Path | str) -> CompactCircuitState:
    try:
        data = json.loads(compact_circuit_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CompactCircuitState()
    if not isinstance(data, dict):
        return CompactCircuitState()
    return CompactCircuitState(
        consecutive_failures=max(0, int(data.get("consecutive_failures") or 0)),
        opened_at=float(data.get("opened_at") or 0.0),
        last_status=str(data.get("last_status") or ""),
        total_failures=max(0, int(data.get("total_failures") or 0)),
    )


def compact_circuit_open(
    state: CompactCircuitState,
    *,
    now: float,
    threshold: int = DEFAULT_COMPACT_FAILURE_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COMPACT_COOLDOWN_SECONDS,
) -> bool:
    """连续失败达阈值且仍在冷却期内 → open（跳过 compact）。冷却过后 half-open 放行重试。"""
    if threshold <= 0 or state.consecutive_failures < threshold:
        return False
    return (now - state.opened_at) < cooldown_seconds


def record_compact_outcome(
    workspace: Path | str,
    *,
    ok: bool,
    status: str,
    now: float,
) -> CompactCircuitState:
    """记录一次 compact 结果：成功清零回 closed，失败累计；达阈值标记 opened_at。"""
    state = read_compact_circuit(workspace)
    if ok:
        new_state = CompactCircuitState(last_status=status, total_failures=state.total_failures)
    else:
        failures = state.consecutive_failures + 1
        opened = failures >= DEFAULT_COMPACT_FAILURE_THRESHOLD
        new_state = CompactCircuitState(
            consecutive_failures=failures,
            opened_at=now if opened else state.opened_at,
            last_status=status,
            total_failures=state.total_failures + 1,
        )
    _write_compact_circuit(workspace, new_state)
    return new_state


def compact_circuit_blocker_payload(state: CompactCircuitState) -> dict[str, object]:
    """熔断 open 时给上层的结构化说明（next_action 人工介入，而非无限重试）。"""
    return {
        "code": "COMPACT_CIRCUIT_OPEN",
        "consecutive_failures": state.consecutive_failures,
        "last_status": state.last_status,
        "message": (
            f"compact 连续失败 {state.consecutive_failures} 次已熔断，冷却期内暂停自动 compact；"
            "请检查 compact 失败原因（self-check / action guard / 磁盘）后再重试。"
        ),
    }


def _write_compact_circuit(workspace: Path | str, state: CompactCircuitState) -> None:
    path = compact_circuit_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "consecutive_failures": state.consecutive_failures,
                    "opened_at": state.opened_at,
                    "last_status": state.last_status,
                    "total_failures": state.total_failures,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 熔断状态写失败不应打断主链路


__all__ = [
    "CompactCircuitState",
    "DEFAULT_COMPACT_COOLDOWN_SECONDS",
    "DEFAULT_COMPACT_FAILURE_THRESHOLD",
    "compact_circuit_blocker_payload",
    "compact_circuit_open",
    "compact_circuit_path",
    "read_compact_circuit",
    "record_compact_outcome",
]
