"""生产 watch 长守监督与连续证据，不生成模拟事件、不读取专项 harness answer-key。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_py_agent.agent.ingestion.harvester import ensure_harvester
from agent_py_agent.agent.ingestion.watch_state import list_states, load_state
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool
from agent_py_agent.agent.user_space.network_grants import active_private_hosts


@dataclass(frozen=True)
class ContinuousProofPolicy:
    minimum_seconds: int = 86_400
    minimum_sources: int = 3
    minimum_signatures: int = 2
    maximum_sample_gap_seconds: int = 120


@dataclass(frozen=True)
class ProofMetrics:
    duration: int = 0
    healthy_sources: int = 0
    signatures: int = 0
    maximum_gap: float = 0.0


def discover_owner_homes(my_agent_home: Path) -> list[Path]:
    homes = {path.parent.parent for path in Path(my_agent_home).glob("owners/**/watch_state/ws-*.json")}
    return sorted(homes)


def ensure_owner_harvesters(owner_home: Path) -> int:
    """从盘上恢复所有未关闭真实 watch；授权每次 fetch 新鲜读，吊销后下一拍立即失效。"""
    started = 0
    tool = WatchStreamTool(object())
    for row in list_states(owner_home):
        if row.get("closed"):
            continue
        state = load_state(owner_home, str(row.get("watch_id") or ""))
        if state is None:
            continue

        def fetch(url: str, *, home: Path = owner_home):
            return tool._fetch_json_pinned(url, active_private_hosts(home), None)

        if ensure_harvester(state, fetch) is not None:
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
        lines = path.read_text(encoding="utf-8").splitlines()
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
    metrics = _proof_metrics(ordered)
    reason = _proof_reason(metrics, cfg)
    return _proof(reason, metrics)


def _proof_metrics(ordered: list[dict[str, Any]]) -> ProofMetrics:
    duration = max(0, int(float(ordered[-1]["observed_at"]) - float(ordered[0]["observed_at"])))
    gaps = [
        float(right["observed_at"]) - float(left["observed_at"])
        for left, right in zip(ordered, ordered[1:])
    ]
    latest = [row for row in ordered[-1].get("watches", []) if isinstance(row, dict)]
    signatures = {
        (
            str((row.get("source") or {}).get("scheme") or ""),
            str((row.get("source") or {}).get("origin_hash") or ""),
            str(row.get("source_mode") or ""),
            tuple(row.get("envelope_keys") or ()),
        )
        for row in latest
    }
    healthy = [
        row
        for row in latest
        if row.get("audit_guarantee")
        and int((row.get("totals") or {}).get("pulls") or 0) > 0
        and not row.get("last_error_code")
    ]
    return ProofMetrics(duration, len(healthy), len(signatures), max(gaps, default=0.0))


def _proof_reason(metrics: ProofMetrics, policy: ContinuousProofPolicy) -> str:
    if metrics.duration < policy.minimum_seconds:
        return "duration_too_short"
    if metrics.maximum_gap > policy.maximum_sample_gap_seconds:
        return "evidence_gap"
    if metrics.healthy_sources < policy.minimum_sources:
        return "insufficient_healthy_guarantee_sources"
    if metrics.signatures < policy.minimum_signatures:
        return "sources_not_heterogeneous"
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
    "ensure_owner_harvesters",
    "evaluate_continuous_proof",
    "read_snapshots",
]
