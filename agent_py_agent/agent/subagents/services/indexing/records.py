
from __future__ import annotations

"""record-specific LocalStore indexing helpers for SubAgentIndexingService."""

import json
from dataclasses import asdict
from typing import Any

from .params import DataclassRecordIndexParams, LocalRecordParams


def index_dataclass_record_via(
    service: Any,
    params: DataclassRecordIndexParams,
) -> None:
    """Index one dataclass-like record through the owning indexing service."""
    try:
        payload = asdict(params.record)
    except TypeError:
        payload = {"str": str(params.record), "repr": repr(params.record)}
    metadata: dict[str, object] = {
        key: value
        for key, value in payload.items()
        if key in {
            "id", "run_id", "decision", "status", "ok",
            "applied", "dry_run", "created_at", "cycle", "backend", "tool_rounds",
        }
    }
    service.log_local_record(
        params=LocalRecordParams(
            source_type=params.source_type,
            source_id=params.source_id,
            title=params.title,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata=metadata,
            event_type=params.event_type,
        ),
    )


def index_action_apply_via(service: Any, record: object) -> None:
    index_dataclass_record_via(
        service,
        DataclassRecordIndexParams(
            "subagent_action_apply", record.id,
            f"Action apply {record.action} {record.run_id or 'global'}",
            record, "subagent_action_apply_logged",
        ),
    )


def index_capability_route_via(service: Any, record: object) -> None:
    index_dataclass_record_via(
        service,
        DataclassRecordIndexParams(
            "subagent_capability_route", record.id,
            f"Capability route {record.request_id} {record.status}",
            record, "subagent_capability_route_logged",
        ),
    )


def index_patch_review_via(service: Any, record: object) -> None:
    index_dataclass_record_via(
        service,
        DataclassRecordIndexParams(
            "subagent_patch_review", record.id,
            f"Patch review {record.decision} {record.run_id}",
            record, "subagent_patch_review_logged",
        ),
    )


def index_channel_probe_via(service: Any, result: object) -> None:
    index_dataclass_record_via(
        service,
        DataclassRecordIndexParams(
            "subagent_channel_probe",
            f"{result.run_id}:{result.created_at:.6f}",
            f"Channel probe {result.run_id} {result.channel_status}",
            result, "subagent_channel_probe_logged",
        ),
    )


def index_runner_result_via(service: Any, result: object, output_payload: dict[str, object]) -> None:
    content = "\n".join([
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        "",
        "## Output",
        json.dumps(output_payload, ensure_ascii=False, indent=2),
    ])
    service.log_local_record(
        params=LocalRecordParams(
            source_type="subagent_runner_result",
            source_id=f"{result.run_id}:{result.created_at:.6f}",
            title=f"Runner {result.run_id} {result.status}",
            content=content,
            metadata={
                "run_id": result.run_id,
                "dry_run": result.dry_run,
                "ok": result.ok,
                "status": result.status,
                "verification_status": result.verification_status,
                "backend": result.backend,
                "tool_rounds": result.tool_rounds,
                "result_json": result.result_json,
                "created_at": result.created_at,
            },
            event_type="subagent_runner_result_logged",
        ),
    )
