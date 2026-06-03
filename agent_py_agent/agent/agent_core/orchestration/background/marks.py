from __future__ import annotations

import time

from ....runtime_errors import runtime_error_report


def mark_background_start(request: object, *, status: str, error: str = "") -> list[dict[str, object]]:
    now = time.time()
    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    for run_id in list(getattr(request, "run_ids", []) or []):
        try:
            task = manager.load(run_id)
        except Exception as exc:
            mark_errors.append(_background_mark_error(str(run_id), exc, "background_dispatch.mark_start.load"))
            continue
        if getattr(task, "id", "") != run_id:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = {
            "launch_id": str(getattr(request, "launch_id", "") or ""),
            "status": status,
            "updated_at": now,
            "error": error,
        }
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception as exc:
            mark_errors.append(_background_mark_error(str(run_id), exc, "background_dispatch.mark_start.save"))
    return mark_errors


def attach_mark_errors(payload: dict[str, object], errors: list[dict[str, object]]) -> None:
    if errors:
        payload["background_mark_errors"] = errors


def _background_mark_error(run_id: str, exc: BaseException, context: str) -> dict[str, object]:
    return {"run_id": run_id, **runtime_error_report(exc, context=context)}
