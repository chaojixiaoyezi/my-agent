from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    TurnTokenUsage,
    append_raw_event,
    append_session_token_usage,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)


def _write_compact_fixture(root: Path) -> None:
    append_raw_event(root, _compact_raw_event())
    snapshot = _compact_snapshot()
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-compact",
            turn_id="turn-1",
            input_tokens=100,
            output_tokens=20,
            tool_tokens=5,
            created_at="2026-05-06T08:02:00+00:00",
        ),
    )


def _compact_raw_event() -> RawMemoryEvent:
    return RawMemoryEvent(
        event_id="raw-compact-1",
        session_id="session-compact",
        request_id="request-compact",
        run_id="run-compact",
        task_id="run-compact",
        speaker="user",
        target="assistant",
        action="message",
        status="ok",
        content_preview="need compact dry-run plan",
        source="run",
        archive_level=2,
        created_at="2026-05-06T08:00:00+00:00",
    )


def _compact_snapshot() -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snapshot-compact-1",
        session_id="session-compact",
        compression_id="compression-compact",
        turn_range={
            "start": 1,
            "end": 1,
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
        },
        user_intents=["need compact dry-run plan"],
        assistant_actions=["scan archive, snapshot, and token ledger"],
        dispatch_events=[
            {
                "source": "run",
                "request_id": "request-compact",
                "run_id": "run-compact",
                "task_id": "run-compact",
                "status": "ok",
            }
        ],
        task_refs=["run-compact"],
        next_actions=["review dry-run before apply"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )


def _write_tool_output_fail_safe_checkpoint(root: Path) -> Path:
    path = root / "memory" / "hooks" / "2026-05-06.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"snapshot_id": "ordinary", "session_id": "session-compact", "created_at": "2026-05-06T08:02:00+00:00"},
        {
            "snapshot_id": "failsafe-tool-1",
            "session_id": "session-compact",
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "source": "tool_output_externalizer",
            "status": "checkpoint_before_externalize",
            "created_at": "2026-05-06T08:03:00+00:00",
            "tool_calls": [
                {
                    "tool": "blackbox_tool",
                    "id": "call-1",
                    "ok": True,
                    "output_hash": "large-output-hash",
                    "output_size_bytes": 4096,
                    "output_externalized": "pending",
                }
            ],
            "next_actions": ["read checkpoint before explicitly reading artifact body"],
            "archive_level": 3,
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def _append_corrupt_fail_safe_checkpoint_row(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{bad-json\n")


def test_memory_resume_from_compact_prioritizes_fail_safe_tool_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    checkpoint_path = _write_tool_output_fail_safe_checkpoint(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))
    checkpoints = resume["fail_safe_checkpoints"]

    assert checkpoints[0]["path"] == str(checkpoint_path)
    assert checkpoints[0]["line_no"] == 2
    assert checkpoints[0]["source"] == "tool_output_externalizer"
    assert checkpoints[0]["tool_calls"][0]["output_hash"] == "large-output-hash"
    assert checkpoints[0]["tool_calls"][0]["output_size_bytes"] == 4096
    assert checkpoints[0]["reads_artifact_bodies"] is False
    assert str(checkpoint_path) in resume["recommended_read_paths"]
    assert "## Fail Safe Checkpoints" in resume["context_block"]
    assert "large-output-hash" in resume["context_block"]


def test_memory_resume_reports_corrupt_fail_safe_checkpoint_rows(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    checkpoint_path = _write_tool_output_fail_safe_checkpoint(root)
    _append_corrupt_fail_safe_checkpoint_row(checkpoint_path)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    assert resume["fail_safe_checkpoints"][0]["snapshot_id"] == "failsafe-tool-1"
    errors = resume["fail_safe_checkpoint_load_errors"]
    assert errors[0]["context"] == "compact_resume.fail_safe_checkpoint"
    assert errors[0]["path"] == str(checkpoint_path)
    assert errors[0]["line_no"] == 3
    assert "Fail Safe Checkpoint Load Errors" in resume["context_block"]


def test_memory_resume_reports_corrupt_apply_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    work_state_ref = Path(str(apply_result["refs"]["work_state_snapshot"]))
    work_state_ref.write_text("{bad-json", encoding="utf-8")

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    assert resume["ok"] is False
    errors = resume["artifact_load_errors"]
    assert errors[0]["artifact"] == "work_state"
    assert errors[0]["context"] == "compact_resume.artifact.work_state"
    assert errors[0]["path"] == str(work_state_ref)
    assert "Compact Artifact Load Errors" in resume["context_block"]


def test_memory_resume_reports_corrupt_metadata_separately_from_missing_metadata(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    metadata_ref = Path(str(apply_result["refs"]["metadata"]))
    metadata_ref.write_text("{bad-json", encoding="utf-8")

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    assert resume["ok"] is False
    assert resume["status"] == "blocked_compact_metadata_load_error"
    assert resume["metadata_load_error"]["context"] == "compact_resume.metadata"
    assert resume["metadata_load_error"]["path"] == str(metadata_ref)
    assert resume["consistency_report"]["metadata_load_error"]["path"] == str(metadata_ref)


def test_memory_compact_work_state_reports_corrupt_archive_sources(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    for snapshot_ref in (root / "memory_archive" / "snapshots").glob("*.json"):
        snapshot_ref.unlink()
    archive_ref = root / "audit" / "2026-05-06.jsonl"
    with archive_ref.open("a", encoding="utf-8") as handle:
        handle.write("{bad-json\n")

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = apply_result["work_state_snapshot"]
    assert work_state["goal"] == "need compact dry-run plan"
    errors = work_state["source_load_errors"]
    assert errors[0]["context"] == "compact_work_state.archive"
    assert errors[0]["path"] == str(archive_ref)
    assert errors[0]["line_no"] == 2

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))
    assert "Source Load Errors" in resume["context_block"]
    assert str(archive_ref) in resume["context_block"]
