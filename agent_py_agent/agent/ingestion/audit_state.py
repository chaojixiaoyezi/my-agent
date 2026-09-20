"""Typed runtime facts for persisted Audit sources.

Audit source workers are durable, lease-backed jobs.  This module exposes their
persisted collection and verdict state without scheduling model turns.  Host
reconciliation keeps workers alive; findings and lifecycle changes wake the
main agent through their canonical event routes.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .watch_state import list_states, load_state


def task_has_incomplete_watch(
    agent: Any,
    task_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Return whether one exact Audit task still owns unfinished source input."""
    owner_home = _owner_home(agent)
    selected_task_id = str(task_id or "").strip()
    if owner_home is None or not selected_task_id:
        return False
    try:
        moment = now if now is not None else time.time()
        run_epoch = _current_audit_run_epoch(agent, selected_task_id)
        return any(
            str(row.get("audit_root_task_id") or "").strip() == selected_task_id
            and bool(row.get("audit_guarantee"))
            and _row_matches_run_epoch(row, run_epoch)
            and _watch_row_incomplete(owner_home, row, moment)
            for row in list_states(owner_home)
        )
    except Exception:
        # Completion is irreversible.  Unreadable persisted state therefore
        # remains incomplete until a later reconciliation can inspect it.
        return True


def audit_task_source_facts(
    agent: Any,
    task_id: str,
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Project only one exact Audit task's persisted sources into model context."""
    owner_home = _owner_home(agent)
    selected_task_id = str(task_id or "").strip()
    if owner_home is None or not selected_task_id:
        return []
    moment = time.time() if now is None else float(now)
    run_epoch = _current_audit_run_epoch(agent, selected_task_id)
    facts: list[dict[str, object]] = []
    for row in list_states(owner_home):
        if (
            not bool(row.get("audit_guarantee"))
            or str(row.get("audit_root_task_id") or "").strip()
            != selected_task_id
            or not _row_matches_run_epoch(row, run_epoch)
        ):
            continue
        watch_id = str(row.get("watch_id") or "").strip()
        window = max(0, int(row.get("watch_window_seconds") or 0))
        opened_at = float(row.get("opened_at") or 0.0)
        elapsed = max(0.0, moment - opened_at) if opened_at > 0 else 0.0
        window_complete = bool(window and opened_at and elapsed >= window)
        source: dict[str, object] = {
            "watch_id": watch_id,
            "source_id": str(row.get("source_id") or "").strip(),
            "run_epoch": max(0, int(row.get("audit_run_epoch") or 0)),
            "source_url": str(row.get("source_url") or ""),
            "cursor": int(row.get("cursor") or 0),
            "closed": bool(row.get("closed")),
            "window_complete": window_complete,
            "collection_active": bool(
                not row.get("closed") and (window <= 0 or not window_complete)
            ),
            "watch_window_seconds": window,
            "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": (
                max(0, int(round(window - elapsed))) if window > 0 else None
            ),
        }
        state = load_state(owner_home, watch_id)
        if state is None:
            source["state_available"] = False
        else:
            from .harvester import audit_capacity_facts, audit_receipt_facts
            from .source_worker import source_worker_facts

            source["state_available"] = True
            source["audit_receipt"] = audit_receipt_facts(state)
            source["capacity"] = audit_capacity_facts(state, now=moment)
            source["source_worker"] = source_worker_facts(agent, state)
            source["last_error_code"] = state.last_error_code or None
        facts.append(source)
    return sorted(facts, key=lambda item: str(item.get("watch_id") or ""))


def audit_task_activation_facts(
    agent: Any,
    link: object,
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Compare one run's published source cardinality with current watch facts.

    This is a mechanical coverage check only. It does not inspect prompts,
    source records, verdict prose or business meaning. A prior run cannot
    satisfy it because ``audit_task_source_facts`` selects the current epoch.
    """

    task_id = str(getattr(link, "task_id", "") or "").strip()
    expected_rows = [
        row
        for row in (getattr(link, "effective_source_bindings", ()) or ())
        if isinstance(row, dict)
    ]
    expected_ids = [str(row.get("source_id") or "").strip() for row in expected_rows]
    expected_ids = [item for item in expected_ids if item]
    try:
        sources = audit_task_source_facts(agent, task_id, now=now)
        state_available = True
    except Exception:
        sources = []
        state_available = False
    observed_ids = [str(row.get("source_id") or "").strip() for row in sources]
    counts = {source_id: observed_ids.count(source_id) for source_id in set(observed_ids)}
    missing = [source_id for source_id in expected_ids if counts.get(source_id, 0) == 0]
    duplicates = sorted(
        source_id for source_id, count in counts.items() if source_id and count > 1
    )
    expected_set = set(expected_ids)
    unexpected = sorted(
        source_id for source_id in counts if not source_id or source_id not in expected_set
    )
    unavailable = sorted(
        str(row.get("source_id") or "").strip()
        for row in sources
        if row.get("state_available") is not True
    )
    ready = bool(
        state_available
        and expected_ids
        and len(expected_ids) == len(expected_rows)
        and len(set(expected_ids)) == len(expected_ids)
        and len(sources) == len(expected_ids)
        and not missing
        and not duplicates
        and not unexpected
        and not unavailable
    )
    return {
        "schema_version": "audit-run-activation.v1",
        "task_id": task_id,
        "run_epoch": max(0, int(getattr(link, "run_epoch", 0) or 0)),
        "state_available": state_available,
        "ready": ready,
        "expected_source_count": len(expected_ids),
        "observed_source_count": len(sources),
        "missing_source_ids": missing,
        "duplicate_source_ids": duplicates,
        "unexpected_source_ids": unexpected,
        "unavailable_source_ids": unavailable,
        "reason": (
            "ready"
            if ready
            else "state_unavailable"
            if not state_available
            else "no_published_sources"
            if not expected_ids
            else "source_coverage_incomplete"
        ),
    }


def audit_task_summary_facts(
    agent: Any,
    task_id: str,
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Aggregate exact source receipts without interpreting business meaning."""
    sources = audit_task_source_facts(agent, task_id, now=now)
    totals = {
        "enqueued": 0,
        "judged": 0,
        "pending": 0,
        "dropped": 0,
        "findings_submitted": 0,
        "findings_projected": 0,
    }
    verdicts = {"hit": 0, "clear": 0, "unsure": 0}
    available = 0
    for source in sources:
        receipt = source.get("audit_receipt")
        if not isinstance(receipt, dict):
            continue
        available += 1
        for key in totals:
            totals[key] += int(receipt.get(key) or 0)
        row_verdicts = receipt.get("verdicts")
        if isinstance(row_verdicts, dict):
            for key in verdicts:
                verdicts[key] += int(row_verdicts.get(key) or 0)
    return {
        "schema_version": "audit-task-summary.v1",
        "task_id": str(task_id or "").strip(),
        "source_count": len(sources),
        "source_state_available_count": available,
        "source_urls": [str(row.get("source_url") or "") for row in sources],
        "collection_active": any(
            bool(row.get("collection_active")) for row in sources
        ),
        "all_source_windows_complete": bool(sources)
        and all(
            bool(row.get("window_complete")) or bool(row.get("closed"))
            for row in sources
        ),
        "all_receipts_settled": bool(sources)
        and available == len(sources)
        and totals["pending"] == 0,
        "coverage_has_no_drops": bool(sources)
        and available == len(sources)
        and totals["dropped"] == 0,
        "receipt_totals": totals,
        "verdict_totals": verdicts,
    }


def owner_home_has_incomplete_watch(
    owner_home: Path,
    *,
    now: float | None = None,
) -> bool:
    """Disk-only owner discovery signal; never selects an execution route."""
    try:
        moment = now if now is not None else time.time()
        return any(
            _watch_row_incomplete(owner_home, row, moment)
            for row in list_states(owner_home)
        )
    except Exception:
        return False


def _watch_row_incomplete(
    owner_home: Path,
    row: dict[str, Any],
    now: float,
) -> bool:
    # ``closed`` ends collection only.  A guaranteed Audit remains unfinished
    # until every already-persisted record has a verdict acknowledgement.
    if bool(row.get("closed")):
        return lane_unjudged_backlog(owner_home, row) > 0
    try:
        window = int(row.get("watch_window_seconds") or 0)
        opened_at = float(row.get("opened_at") or 0.0)
    except (TypeError, ValueError):
        window = 0
        opened_at = 0.0
    if window > 0 and opened_at > 0 and now - opened_at < window:
        return True
    return lane_unjudged_backlog(owner_home, row) > 0


def lane_unjudged_backlog(
    owner_home: Path,
    lane: dict[str, Any],
) -> int:
    """Number of persisted candidate records not yet acknowledged by a verdict."""
    written = int((lane.get("totals") or {}).get("spool_candidates") or 0)
    if written <= 0:
        return 0
    return max(
        0,
        written
        - _acknowledged_candidates(
            owner_home,
            str(lane.get("watch_id") or ""),
        ),
    )


def _acknowledged_candidates(owner_home: Path, watch_id: str) -> int:
    if not watch_id:
        return 0
    from .harvester import consumed_and_acked_on_disk

    _consumed, acknowledged = consumed_and_acked_on_disk(owner_home, watch_id)
    return acknowledged


def _owner_home(agent: Any) -> Path | None:
    raw = str(
        getattr(
            getattr(agent, "home_paths", None),
            "owner_home_dir",
            "",
        )
        or ""
    ).strip()
    return Path(raw) if raw else None


def _current_audit_run_epoch(agent: Any, task_id: str) -> int | None:
    """Read current epoch from the durable link; None keeps standalone compatibility."""

    store = getattr(agent, "conversation_store", None)
    loader = getattr(getattr(store, 'tasks', None), 'load', None)
    if not callable(loader):
        return None
    try:
        link = loader(task_id)
    except Exception:
        return -1
    if link is None or str(getattr(link, "task_id", "") or "").strip() != task_id:
        return -1
    return max(0, int(getattr(link, "run_epoch", 0) or 0))


def _row_matches_run_epoch(row: dict[str, Any], run_epoch: int | None) -> bool:
    if run_epoch is None:
        return True
    if run_epoch < 0:
        return False
    try:
        observed = max(0, int(row.get("audit_run_epoch") or 0))
    except (TypeError, ValueError):
        return False
    return observed == run_epoch


__all__ = [
    "audit_task_source_facts",
    "audit_task_activation_facts",
    "audit_task_summary_facts",
    "lane_unjudged_backlog",
    "owner_home_has_incomplete_watch",
    "task_has_incomplete_watch",
]
