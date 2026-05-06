from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    TurnTokenUsage,
    append_raw_event,
    append_session_token_usage,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _write_compact_fixture(root: Path) -> None:
    append_raw_event(root, _compact_raw_event())
    snapshot = _compact_snapshot()
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    _append_compact_token_usage(root)


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
        content_preview="需要自动 compact dry-run 计划",
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
        user_intents=["需要自动 compact dry-run 计划"],
        assistant_actions=["准备扫描归档、snapshot 和 token ledger。"],
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
        next_actions=["先看 dry-run，再决定是否启用 apply。"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )


def _append_compact_token_usage(root: Path) -> None:
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


def test_build_memory_compact_plan_is_read_only_summary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    plan = build_memory_compact_plan(
        root,
        MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact", level=2),
    )

    assert plan["mode"] == "dry-run"
    assert plan["dry_run"] is True
    assert plan["archive"]["record_count"] == 2
    assert plan["archive"]["by_layer"] == {"hook": 1, "raw": 1}
    assert plan["snapshots"]["file_count"] == 1
    assert plan["tokens"]["ledger_count"] == 1
    assert plan["tokens"]["turn_count"] == 1
    assert plan["tokens"]["cumulative_tokens"] == 125
    assert plan["risks"] == []
    assert plan["estimated_compactable_bytes"] == plan["archive"]["total_bytes"] + plan["tokens"]["total_bytes"]


def test_memory_compact_cli_outputs_json_plan(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    _write_compact_fixture(_workspace(config_path))
    parser = build_parser()

    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-compact",
            "--session-id",
            "session-compact",
            "--request-id",
            "request-compact",
            "--json",
        ]
    )
    code = args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["scope"]["session_id"] == "session-compact"
    assert payload["archive"]["record_count"] == 2
    assert payload["snapshots"]["file_count"] == 1


def test_memory_compact_plan_reports_invalid_manifests(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    (root / "memory_archive" / "snapshots" / "bad.json").write_text("{bad", encoding="utf-8")
    (root / "memory_archive" / "tokens" / "bad.json").write_text("{bad", encoding="utf-8")

    plan = build_memory_compact_plan(root, MemoryCompactPlanOptions())

    assert plan["snapshots"]["invalid_count"] == 1
    assert plan["tokens"]["invalid_count"] == 1
    assert "compression snapshot directory contains invalid JSON files" in plan["risks"]
    assert "token ledger directory contains invalid JSON files" in plan["risks"]


def test_memory_compact_apply_is_explicitly_blocked(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    parser = build_parser()

    args = parser.parse_args(["--config", str(config_path), "memory-compact", "--apply"])
    code = args.func(args)
    captured = capsys.readouterr()

    assert code == 2
    assert "--apply 尚未实现" in captured.out
