
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from ....common.value_parsing import sequence_strings
from ...models import FailureHandoff, SubAgentTask


def normalize_failure_handoff(value: object) -> FailureHandoff:
    if isinstance(value, FailureHandoff):
        return value
    if not isinstance(value, dict):
        return FailureHandoff()
    payload = {key: value[key] for key in _field_names(FailureHandoff) if key in value}
    for key in ["artifact_refs", "evidence_refs", "avoid_next_time"]:
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    payload["reserved"] = _dict_value(payload.get("reserved"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return FailureHandoff(**payload)


def write_failure_handoff(task: SubAgentTask) -> None:
    if not task.failure_handoff_json:
        return
    path = Path(task.failure_handoff_json)
    if not task.failure_handoff.run_id:
        path.unlink(missing_ok=True)
        return
    path.write_text(
        json.dumps(asdict(task.failure_handoff), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
