"""生产 watch 长守监督与连续证据，不生成模拟事件、不读取专项 harness answer-key。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_py_agent.agent.ingestion.harvester import ensure_harvester
from agent_py_agent.agent.ingestion.source_http import SourceHttpRequest
from agent_py_agent.agent.ingestion.watch_state import list_states, load_state
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool

from ..common.json_io import jsonl_lines


@dataclass(frozen=True)
class ContinuousProofPolicy:
    minimum_seconds: int = 86_400
    minimum_sources: int = 3
    minimum_signatures: int = 2
    maximum_sample_gap_seconds: int = 120
    maximum_source_staleness_seconds: int = 900


@dataclass(frozen=True)
class ProofMetrics:
    duration: int = 0
    healthy_sources: int = 0
    signatures: int = 0
    maximum_gap: float = 0.0


def discover_owner_homes(my_agent_home: Path) -> list[Path]:
    """只走规范 owner 层级，禁止 ``**`` 递归进 task/artifact 大树。"""
    root = Path(my_agent_home)
    patterns = (
        "owners/local/main",
        "owners/providers/*/users/*",
        "owners/providers/*/groups/*",
    )
    candidates = chain.from_iterable(root.glob(pattern) for pattern in patterns)
    return sorted({owner_home for owner_home in candidates if _owner_has_watch(owner_home)})


def _owner_has_watch(owner_home: Path) -> bool:
    return any((owner_home / "watch_state").glob("ws-*.json"))


def recover_active_audit_harvesters(agent: object) -> int:
    """Recover collectors only for durably active named Audit sources.

    Ordinary ``watch_stream`` calls are turn-scoped and restart on the next
    explicit pull.  They have no parent lifecycle that authorizes a host
    restart to resume network traffic.  Named Audit sources do: their exact
    conversation task link is checked before a collector is reacquired.  An
    unavailable or inactive parent therefore fails closed.
    """
    owner_home_text = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home_text:
        return 0
    owner_home = Path(owner_home_text)
    started = 0
    tool = WatchStreamTool(object())
    for row in list_states(owner_home):
        if row.get("closed") or not bool(row.get("audit_guarantee")):
            continue
        state = load_state(owner_home, str(row.get("watch_id") or ""))
        if state is None:
            continue
        from agent_py_agent.agent.ingestion.source_worker import (
            audit_parent_reconcile_state,
        )

        parent_active, _parent_state = audit_parent_reconcile_state(
            agent,
            state.audit_root_task_id,
        )
        if not parent_active:
            continue

        def fetch(request: SourceHttpRequest, *, home: Path = owner_home):
            return tool._fetch_json_pinned(
                request,
                (),
                None,
            )

        from agent_py_agent.agent.ingestion.source_worker import (
            settle_audit_source_worker,
            wake_audit_source_worker,
        )

        if ensure_harvester(
            state,
            fetch,
            on_records_ready=lambda ready_state: wake_audit_source_worker(
                agent,
                ready_state,
            ),
            on_window_finalized=lambda finalized_state: settle_audit_source_worker(
                agent,
                finalized_state,
            ),
        ) is not None:
            started += 1
    return started


def build_snapshot(owner_homes: list[Path], *, observed_at: float | None = None) -> dict[str, Any]:
    now = float(observed_at or time.time())
    rows: list[dict[str, Any]] = []
    for owner_home in owner_homes:
        rows.extend(_owner_snapshot_rows(owner_home))
    return {"schema_version": "continuous-watch-evidence.v1", "observed_at": now, "watches": rows}


def _owner_snapshot_rows(owner_home: Path) -> list[dict[str, Any]]:
    states = [load_state(owner_home, str(row.get("watch_id") or "")) for row in list_states(owner_home)]
    return [_state_snapshot(owner_home, state) for state in states if state is not None and not state.closed]


def _state_snapshot(owner_home: Path, state: Any) -> dict[str, Any]:
    return {
        "owner_ref": hashlib.sha256(str(owner_home).encode()).hexdigest()[:16],
        "watch_id": state.watch_id,
        "source": _source_fact(state.source_url),
        "source_mode": state.source_mode or "cursor",
        "envelope_keys": sorted(str(key) for key in state.source_envelope)[:32],
        "audit_guarantee": state.audit_guarantee,
        "opened_at": state.opened_at,
        "last_pull_at": state.last_pull_at,
        "last_source_at": max(float(state.last_pull_at or 0.0), float(state.last_poll_at or 0.0)),
        "cursor": state.cursor,
        "totals": dict(state.totals),
        "last_error_code": state.last_error.split(":", 1)[0][:80] if state.last_error else "",
    }


def _source_fact(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}"
    return {
        "scheme": parts.scheme.lower(),
        "origin_hash": hashlib.sha256(origin.encode()).hexdigest()[:16],
        "path_shape": hashlib.sha256(parts.path.encode()).hexdigest()[:12],
    }


def append_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")


def read_snapshots(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        lines = jsonl_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def evaluate_continuous_proof(
    snapshots: list[dict[str, Any]],
    policy: ContinuousProofPolicy | None = None,
) -> dict[str, Any]:
    cfg = policy or ContinuousProofPolicy()
    ordered = sorted(snapshots, key=lambda row: float(row.get("observed_at") or 0))
    if not ordered:
        return _proof("no_evidence", ProofMetrics())
    metrics = _proof_metrics(ordered, cfg)
    reason = _proof_reason(metrics, cfg)
    return _proof(reason, metrics)


def _proof_metrics(ordered: list[dict[str, Any]], policy: ContinuousProofPolicy) -> ProofMetrics:
    segment_start: float | None = None
    previous_at: float | None = None
    segment_maximum_gap = 0.0
    latest_healthy = 0
    latest_signatures = 0
    for snapshot in ordered:
        observed_at = float(snapshot.get("observed_at") or 0.0)
        healthy, signatures = _snapshot_health(snapshot, policy)
        latest_healthy = healthy
        latest_signatures = signatures
        gap = observed_at - previous_at if previous_at is not None else 0.0
        qualifies = healthy >= policy.minimum_sources and signatures >= policy.minimum_signatures
        if not qualifies or gap < 0 or gap > policy.maximum_sample_gap_seconds:
            segment_start = observed_at if qualifies else None
            segment_maximum_gap = 0.0
        elif segment_start is None:
            segment_start = observed_at
        else:
            segment_maximum_gap = max(segment_maximum_gap, gap)
        previous_at = observed_at
    latest_at = float(ordered[-1].get("observed_at") or 0.0)
    duration = max(0, int(latest_at - segment_start)) if segment_start is not None else 0
    return ProofMetrics(duration, latest_healthy, latest_signatures, segment_maximum_gap)


def _snapshot_health(snapshot: dict[str, Any], policy: ContinuousProofPolicy) -> tuple[int, int]:
    """Return healthy guarantee sources and signatures for one evidence instant.

    A transient pull error is observable but does not immediately break the
    guarantee while the last successful source fact remains fresh.  Once that
    fact exceeds the configured staleness window, this snapshot stops
    qualifying and the continuous segment resets.
    """

    observed_at = float(snapshot.get("observed_at") or 0.0)
    watches = [row for row in snapshot.get("watches", []) if isinstance(row, dict)]
    healthy = [
        row
        for row in watches
        if row.get("audit_guarantee")
        and int((row.get("totals") or {}).get("pulls") or 0) > 0
        and float(row.get("last_source_at") or 0.0) > 0
        and 0
        <= observed_at - float(row.get("last_source_at") or 0.0)
        <= policy.maximum_source_staleness_seconds
    ]
    signatures = {
        (
            str((row.get("source") or {}).get("scheme") or ""),
            str((row.get("source") or {}).get("origin_hash") or ""),
            str(row.get("source_mode") or ""),
            tuple(row.get("envelope_keys") or ()),
        )
        for row in healthy
    }
    return len(healthy), len(signatures)


def _proof_reason(metrics: ProofMetrics, policy: ContinuousProofPolicy) -> str:
    if metrics.healthy_sources < policy.minimum_sources:
        return "insufficient_healthy_guarantee_sources"
    if metrics.signatures < policy.minimum_signatures:
        return "sources_not_heterogeneous"
    if metrics.duration < policy.minimum_seconds:
        return "duration_too_short"
    if metrics.maximum_gap > policy.maximum_sample_gap_seconds:
        return "evidence_gap"
    return "ok"


def _proof(reason: str, metrics: ProofMetrics) -> dict[str, Any]:
    return {
        "proven": reason == "ok",
        "reason": reason,
        "continuous_seconds": metrics.duration,
        "healthy_guarantee_sources": metrics.healthy_sources,
        "heterogeneous_signatures": metrics.signatures,
    }


__all__ = [
    "ContinuousProofPolicy",
    "append_snapshot",
    "build_snapshot",
    "discover_owner_homes",
    "evaluate_continuous_proof",
    "read_snapshots",
    "recover_active_audit_harvesters",
]
