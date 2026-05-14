# LLM: Stored acceptance record loader converts task-local JSON back into report dataclasses.
# 模块用途: follow-up apply 后读取单 run `acceptance_review.json`，不让 dispatch 主模块膨胀。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..subagents.reports import AcceptanceReviewRecord


# LLM: stored_acceptance_record reloads the authoritative record written by the follow-up apply gate.
# 函数用途: follow-up apply 后从 task-local acceptance_review.json 取回 after_status/applied 等最终字段。
def stored_acceptance_record(agent: Any, run_id: str) -> AcceptanceReviewRecord | None:
    try:
        task = agent.subagents.load(run_id)
    except FileNotFoundError:
        return None
    path = Path(task.reports_dir) / "acceptance_review.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return AcceptanceReviewRecord(
        id=str(payload.get("id") or ""),
        run_id=str(payload.get("run_id") or run_id),
        dry_run=bool(payload.get("dry_run", True)),
        applied=bool(payload.get("applied", False)),
        ok=bool(payload.get("ok", False)),
        decision=str(payload.get("decision") or ""),
        message=str(payload.get("message") or ""),
        before_status=str(payload.get("before_status") or ""),
        after_status=str(payload.get("after_status") or ""),
        before_verification_status=str(payload.get("before_verification_status") or ""),
        after_verification_status=str(payload.get("after_verification_status") or ""),
        reviewer=str(payload.get("reviewer") or ""),
        note=str(payload.get("note") or ""),
        evidence_count=int(payload.get("evidence_count") or 0),
        test_count=int(payload.get("test_count") or 0),
        artifact_count=int(payload.get("artifact_count") or 0),
        worker_claims=_string_list(payload.get("worker_claims")),
        evidence_facts=_string_list(payload.get("evidence_facts")),
        parent_conclusions=_string_list(payload.get("parent_conclusions")),
        evidence_paths=_string_list(payload.get("evidence_paths")),
        created_at=float(payload.get("created_at") or 0.0),
    )


# LLM: _string_list normalizes optional JSON arrays from stored audit files.
# 函数用途: 把 acceptance_review.json 中可能缺失或非字符串的数组字段安全转回字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]
