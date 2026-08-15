from __future__ import annotations

from collections.abc import Iterable


def remember_orchestration_run_ids(agent: object, run_ids: Iterable[object]) -> None:
    seen = getattr(agent, "_orchestration_run_ids_seen", None)
    if not isinstance(seen, set):
        seen = set()
        agent._orchestration_run_ids_seen = seen
    for raw in run_ids:
        run_id = str(raw or "").strip()
        if run_id:
            seen.add(run_id)


def remembered_orchestration_run_ids(agent: object) -> set[str]:
    seen = getattr(agent, "_orchestration_run_ids_seen", None)
    if not isinstance(seen, set):
        return set()
    return {str(item) for item in seen if str(item or "").strip()}


def remember_dispatched_orchestration_run_ids(agent: object, run_ids: Iterable[object]) -> None:
    seen = getattr(agent, "_orchestration_dispatched_run_ids_seen", None)
    if not isinstance(seen, set):
        seen = set()
        agent._orchestration_dispatched_run_ids_seen = seen
    for raw in run_ids:
        run_id = str(raw or "").strip()
        if run_id:
            seen.add(run_id)


def remembered_dispatched_orchestration_run_ids(agent: object) -> set[str]:
    seen = getattr(agent, "_orchestration_dispatched_run_ids_seen", None)
    if not isinstance(seen, set):
        return set()
    return {str(item) for item in seen if str(item or "").strip()}
