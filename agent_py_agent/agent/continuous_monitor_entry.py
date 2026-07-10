"""生产异构来源长守进程：恢复 harvester，并持续落可审计 proof snapshots。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_py_agent.agent.ingestion.continuous_monitor import (
    ContinuousProofPolicy,
    append_snapshot,
    build_snapshot,
    discover_owner_homes,
    ensure_owner_harvesters,
    evaluate_continuous_proof,
    read_snapshots,
)
from agent_py_agent.agent.llm_scale import redis_client_from_url
from agent_py_agent.agent.observability.otel import configure_otel_from_env
from agent_py_agent.agent.pg_rls import require_restricted_app_role
from agent_py_agent.agent.runtime_schema import require_runtime_schema_current
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend


def serve() -> None:  # pragma: no cover - 真长跑进程
    config = ScaleRuntimeConfig.from_env(ScaleRole.MONITOR, os.environ)
    if config.is_scale:
        backend = StorageBackend(config.database_url)
        require_restricted_app_role(backend, config.database_app_role)
        require_runtime_schema_current(backend, include_scale_data=True, app_role=config.database_app_role)
        redis_client_from_url(config.redis_url).ping()
    configure_otel_from_env(service_name="my-agent-continuous-monitor", required=config.is_scale)
    home = Path(config.agent_home).expanduser().resolve()
    evidence = Path(
        os.environ.get("CONTINUOUS_MONITOR_EVIDENCE")
        or home / "scale_evidence" / "continuous-watch.ndjson"
    )
    interval = max(5, int(os.environ.get("CONTINUOUS_MONITOR_SAMPLE_SECONDS", "30")))
    policy = ContinuousProofPolicy(
        minimum_seconds=max(3600, int(os.environ.get("CONTINUOUS_MONITOR_PROOF_SECONDS", "86400"))),
        minimum_sources=max(3, int(os.environ.get("CONTINUOUS_MONITOR_MIN_SOURCES", "3"))),
        minimum_signatures=max(2, int(os.environ.get("CONTINUOUS_MONITOR_MIN_SIGNATURES", "2"))),
        maximum_sample_gap_seconds=max(interval * 3, int(os.environ.get("CONTINUOUS_MONITOR_MAX_GAP_SECONDS", "120"))),
        maximum_source_staleness_seconds=max(
            interval * 3,
            int(os.environ.get("CONTINUOUS_MONITOR_MAX_SOURCE_STALE_SECONDS", "900")),
        ),
    )
    while True:
        owners = discover_owner_homes(home)
        for owner in owners:
            ensure_owner_harvesters(owner)
        snapshot = build_snapshot(owners)
        append_snapshot(evidence, snapshot)
        report = evaluate_continuous_proof(read_snapshots(evidence), policy)
        summary = evidence.with_suffix(".summary.json")
        summary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    serve()
