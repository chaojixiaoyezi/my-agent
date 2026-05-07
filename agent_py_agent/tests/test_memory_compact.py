from __future__ import annotations

import json
from pathlib import Path

import agent_py_agent.agent.memory_archive.compact_apply as compact_apply_module
from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.memory_archive import (
    RUNTIME_MEMORY_SCHEMA_VERSION,
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
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
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


def test_apply_memory_compact_writes_non_destructive_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    refs = result["refs"]
    apply_bundle = json.loads(Path(refs["apply_bundle"]).read_text(encoding="utf-8"))
    restore_refs = json.loads(Path(refs["restore_refs"]).read_text(encoding="utf-8"))
    self_check = json.loads(Path(refs["post_compact_self_check"]).read_text(encoding="utf-8"))
    ledger = Path(refs["apply_ledger"]).read_text(encoding="utf-8").splitlines()

    assert result["mode"] == "apply"
    _assert_schema_v2(result, "compact_apply")
    _assert_schema_v2(apply_bundle, "compact_apply_bundle")
    _assert_schema_v2(restore_refs, "compact_apply_restore_refs")
    _assert_schema_v2(self_check, "compact_apply_self_check")
    assert result["dry_run"] is False
    assert result["compact_status"] == "applied_non_destructive"
    assert result["content_preserved"] is True
    assert result["source_plan"]["archive_record_count"] == 2
    assert Path(refs["compact_context"]).exists()
    assert Path(refs["metadata"]).exists()
    assert apply_bundle["restore_refs_summary"] == {
        "archive_files": 2,
        "snapshot_files": 1,
        "token_ledgers": 1,
    }
    assert restore_refs["source_refs"]["archive_files"]
    assert restore_refs["source_refs"]["snapshot_files"]
    assert restore_refs["source_refs"]["token_ledgers"]
    assert self_check["ok"] is True
    ledger_record = json.loads(ledger[-1])
    _assert_schema_v2(ledger_record, "compact_apply_ledger")
    assert ledger_record["event_id"] == result["event_id"]
    assert (root / "memory" / "raw" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()


def test_apply_memory_compact_records_self_check_failure_without_rewriting_sources(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    monkeypatch.setattr(compact_apply_module, "_self_check_payload", _failed_self_check)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    refs = result["refs"]
    failure = json.loads(Path(refs["self_check_failure"]).read_text(encoding="utf-8"))
    ledger_record = json.loads(Path(refs["apply_ledger"]).read_text(encoding="utf-8").splitlines()[-1])

    assert result["ok"] is False
    assert result["compact_status"] == "blocked_self_check_failed"
    _assert_schema_v2(failure, "compact_apply_self_check_failure")
    assert failure["failed_checks"][0]["name"] == "forced_failure"
    assert ledger_record["compact_status"] == "blocked_self_check_failed"
    assert ledger_record["restore_ready"] is False
    assert (root / "memory" / "raw" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()


def test_memory_compact_cli_apply_outputs_json_result(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    _write_compact_fixture(_workspace(config_path))
    parser = build_parser()

    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-compact",
            "--apply",
            "--session-id",
            "session-compact",
            "--json",
        ]
    )
    code = args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["mode"] == "apply"
    assert payload["scope"]["session_id"] == "session-compact"
    assert Path(payload["refs"]["compact_context"]).exists()


# LLM: _assert_schema_v2 keeps compact apply metadata, ledger, and self-check on the shared v2 contract.
# 函数用途: 校验 compact apply 相关记录的 schema 名称、版本和 reserved 扩展槽。
def _assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"]["name"] == name
    assert record["reserved"]["schema_name"] == name
    assert record["reserved"]["schema_version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert set(record["reserved"]) >= {"extensions", "compat", "future"}


# LLM: _failed_self_check simulates a hard post-apply validation failure without touching production files.
# 函数用途: 为 self-check failure 测试返回一个 v2 自检失败 payload，验证 apply 会阻断而不是改写事实源。
def _failed_self_check(plan: dict[str, object], paths: dict[str, Path], now: str) -> dict[str, object]:
    schema = compact_apply_module.COMPACT_SELF_CHECK_SCHEMA
    return {
        "version": RUNTIME_MEMORY_SCHEMA_VERSION,
        "schema": compact_apply_module.runtime_memory_schema_payload(schema),
        "ok": False,
        "event_type": "post_compact_self_check",
        "checks": [{"name": "forced_failure", "ok": False, "severity": "hard"}],
        "created_at": now,
        "reserved": compact_apply_module.runtime_memory_reserved_fields(schema),
    }
