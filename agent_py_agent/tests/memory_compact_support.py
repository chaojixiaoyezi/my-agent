
from __future__ import annotations

import json
from pathlib import Path

import agent_py_agent.agent.memory_archive.compact_apply as compact_apply_module
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


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: ""\n'
        f'my_agent_home: "{(tmp_path / "home").as_posix()}"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def owner_home(config_path: Path) -> Path:
    return config_path.parent / "home" / "owners" / "local" / "main"


def write_compact_fixture(root: Path) -> None:
    append_raw_event(root, compact_raw_event())
    snapshot = compact_snapshot()
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    append_compact_token_usage(root)


def write_real_run_archive_fixture(root: Path) -> None:
    append_raw_event(root, compact_raw_event())
    append_snapshot(root, compact_snapshot())
    append_compact_token_usage(root)


def compact_raw_event() -> RawMemoryEvent:
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


def compact_snapshot() -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snapshot-compact-1",
        session_id="session-compact",
        compression_id="compression-compact",
        turn_range={"start": 1, "end": 1, "request_id": "request-compact", "run_id": "run-compact", "task_id": "run-compact"},
        user_intents=["需要自动 compact dry-run 计划"],
        assistant_actions=["准备扫描归档、snapshot 和 token ledger。"],
        dispatch_events=[{"source": "run", "request_id": "request-compact", "run_id": "run-compact", "task_id": "run-compact", "status": "ok"}],
        task_refs=["run-compact"],
        next_actions=["先看 dry-run，再决定是否启用 apply。"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )


def append_compact_token_usage(root: Path) -> None:
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


def assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"]["name"] == name
    assert record["schema"]["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert "reserved" not in record


def assert_apply_ids_match(result: dict[str, object], *records: dict[str, object]) -> None:
    for record in records:
        assert record["apply_id"] == result["apply_id"]
        assert record["plan_id"] == result["plan_id"]


def check_names(self_check: dict[str, object]) -> set[str]:
    return {str(item["name"]) for item in self_check["checks"]}


def failed_self_check(
    plan: dict[str, object], paths: dict[str, Path], now: str, work_state: dict[str, object]
) -> dict[str, object]:
    schema = compact_apply_module.COMPACT_SELF_CHECK_SCHEMA
    return {
        "version": RUNTIME_MEMORY_SCHEMA_VERSION,
        "schema": compact_apply_module.runtime_memory_schema_payload(schema),
        "ok": False,
        "apply_id": work_state["apply_id"],
        "plan_id": work_state["plan_id"],
        "event_type": "post_compact_self_check",
        "checks": [{"name": "forced_failure", "ok": False, "severity": "hard"}],
        "created_at": now,
    }


def load_apply_artifacts(result: dict[str, object]) -> dict[str, object]:
    refs = result["refs"]
    assert isinstance(refs, dict)
    ledger_lines = Path(str(refs["apply_ledger"])).read_text(encoding="utf-8").splitlines()
    return {
        "apply_bundle": json.loads(Path(str(refs["apply_bundle"])).read_text(encoding="utf-8")),
        "restore_refs": json.loads(Path(str(refs["restore_refs"])).read_text(encoding="utf-8")),
        "work_state": json.loads(Path(str(refs["work_state_snapshot"])).read_text(encoding="utf-8")),
        "self_check": json.loads(Path(str(refs["post_compact_self_check"])).read_text(encoding="utf-8")),
        "ledger_record": json.loads(ledger_lines[-1]),
    }


def assert_apply_preserved_sources(root: Path) -> None:
    assert (root / "audit" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()
