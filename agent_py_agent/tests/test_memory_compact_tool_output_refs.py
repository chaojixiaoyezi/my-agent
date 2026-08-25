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
from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
    carried_tool_call_records,
)
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)


def _write_run_with_large_tool_output(root: Path) -> str:
    _append_raw_tool_ref_event(root)
    _append_tool_ref_snapshot(root)
    _append_tool_ref_token_usage(root)
    return _write_large_tool_output_artifact(root)


def _append_raw_tool_ref_event(root: Path) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-tool-ref-1",
            session_id="session-tool-ref",
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="需要恢复大工具输出线索",
            source="run",
            archive_level=2,
            created_at="2026-05-06T08:00:00+00:00",
        ),
    )


def _append_tool_ref_snapshot(root: Path) -> None:
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-tool-ref-1",
        session_id="session-tool-ref",
        compression_id="compression-tool-ref",
        turn_range={"start": 1, "end": 1, "request_id": "request-tool-ref", "run_id": "run-tool-ref"},
        user_intents=["需要恢复大工具输出线索"],
        assistant_actions=["已经读取长日志，完整输出外置为 artifact。"],
        task_refs=["task-tool-ref"],
        next_actions=["恢复时先读 tool output artifact ref，而不是重新扫长日志。"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)


def _append_tool_ref_token_usage(root: Path) -> None:
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-tool-ref",
            turn_id="turn-1",
            input_tokens=100,
            output_tokens=20,
            tool_tokens=500,
            created_at="2026-05-06T08:02:00+00:00",
        ),
    )


def _write_large_tool_output_artifact(root: Path) -> str:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="shell",
            call_id="1-1",
            output="very long log\n" + ("x" * 1400),
            ok=True,
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
            min_chars=1,
            parameters={"command": "python scripts/collect-alpha.py"},
        )
    )
    return str(record["artifact_ref"])


def _write_task_progress_tool_output_artifact(root: Path) -> str:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="task_progress",
            call_id="8-1",
            output=json.dumps({"summary": "旧进度 4/40", "next_action": "继续 fragment-005"}, ensure_ascii=False),
            ok=True,
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
            min_chars=0,
            parameters={"action": "update", "summary": "旧进度 4/40"},
        )
    )
    return str(record["output_path"])


def _write_read_file_cursor_fixture_with_failed_retry(root: Path) -> None:
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="read-1",
            output="[char-window offset=0 chars=100 total_chars=500]\nPARTIAL view only",
            ok=True,
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
            min_chars=0,
            parameters={"path": "data/big.txt", "offset": 0, "max_chars": 100},
        )
    )
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="read-2",
            output="CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行。",
            ok=False,
            error_code="CONTEXT_COMPACT_DEFERRED",
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
            min_chars=1000,
            parameters={"path": "data/big.txt", "offset": 100, "max_chars": 100},
        )
    )


def test_carried_tool_call_records_restore_only_exact_root_scope(tmp_path: Path) -> None:
    root = tmp_path / "work"
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="task_progress",
            call_id="plan-1",
            output="已登记 Rust 复刻计划",
            ok=True,
            request_id="request-root",
            run_id="root-run",
            task_id="root-run",
            min_chars=1000,
            parameters={"action": "create", "summary": "使用 Rust 完整复刻"},
            result_envelope={
                "tool_execution": {
                    "handler_executed": False,
                    "duration_ms": 4,
                    "failure_stage": "",
                }
            },
        )
    )
    artifact = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="create_subagents",
            call_id="child-1",
            output="x" * 1400,
            ok=True,
            request_id="request-root",
            run_id="root-run",
            task_id="root-run",
            min_chars=1,
            parameters={"goal": "用 Rust 实现命令层"},
            result_envelope={
                "tool_execution": {
                    "handler_executed": True,
                    "duration_ms": 19,
                },
                "tool_operation": {
                    "schema_version": "tool_operation.v1",
                    "operation_id": "operation-child-1",
                    "status": "succeeded",
                    "action": "execute",
                    "replayed": False,
                    "idempotency_scope": "turn",
                },
            },
        )
    )
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="write_file",
            call_id="private-1",
            output="child output",
            ok=True,
            request_id="request-child",
            run_id="child-run",
            task_id="child-run",
            min_chars=1000,
            parameters={"path": "child-only.txt"},
        )
    )

    records = carried_tool_call_records(
        root,
        {"run_id": "root-run", "task_id": "root-run"},
    )

    assert [item["call_id"] for item in records] == ["plan-1", "child-1"]
    assert records[0]["parameters"]["summary"] == "使用 Rust 完整复刻"
    assert records[0]["handler_executed"] is False
    assert records[0]["duration_ms"] == 4
    assert records[1]["artifact_ref"] == artifact["artifact_ref"]
    assert records[1]["handler_executed"] is True
    assert records[1]["duration_ms"] == 19
    assert records[1]["operation_id"] == "operation-child-1"
    assert records[1]["tool_operation_status"] == "succeeded"
    assert records[1]["tool_operation_action"] == "execute"


def test_carried_tool_call_records_restore_exact_conversation_turn_across_durable_task(
    tmp_path: Path,
) -> None:
    root = tmp_path / "work"
    for request_id, call_id in (
        ("foreground-turn-1", "child-1"),
        ("foreground-turn-2", "child-2"),
    ):
        externalize_tool_output_record(
            ExternalizeToolOutputRequest(
                root=root,
                tool="create_subagents",
                call_id=call_id,
                output=f"created {call_id}",
                ok=True,
                request_id=request_id,
                conversation_request_id=request_id,
                run_id=request_id,
                task_id=request_id,
                min_chars=1000,
                parameters={"items": [{"goal": call_id}]},
            )
        )

    records = carried_tool_call_records(
        root,
        {"conversation_request_id": ("foreground-turn-2",)},
    )

    assert [item["call_id"] for item in records] == ["child-2"]
    assert records[0]["conversation_request_id"] == "foreground-turn-2"

    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="create_subagents",
            call_id="legacy-child",
            output="created legacy child",
            ok=True,
            request_id="legacy-foreground-turn",
            run_id="legacy-foreground-turn",
            task_id="legacy-foreground-turn",
            min_chars=1000,
            parameters={"items": [{"goal": "legacy child"}]},
        )
    )

    legacy_records = carried_tool_call_records(
        root,
        {"conversation_request_id": ("legacy-foreground-turn",)},
    )

    assert [item["call_id"] for item in legacy_records] == ["legacy-child"]


def test_carried_tool_call_records_dedupe_same_scoped_call_id(tmp_path: Path) -> None:
    root = tmp_path / "work"
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True)
    row = {
        "kind": "tool_call",
        "tool": "create_subagents",
        "call_id": "same-call",
        "scoped_call_id": "root-run:same-call",
        "request_id": "request-root",
        "run_id": "root-run",
        "task_id": "root-run",
        "ok": True,
        "status": "ok",
        "parameters": {"goal": "一次性派工"},
    }
    index.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for _ in range(2)) + "\n",
        encoding="utf-8",
    )

    records = carried_tool_call_records(
        root,
        {"run_id": "root-run", "task_id": "root-run"},
    )

    assert len(records) == 1
    assert records[0]["scoped_call_id"] == "root-run:same-call"


def test_compact_apply_and_resume_include_scoped_tool_output_artifact_refs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    artifact_ref = _write_run_with_large_tool_output(root)

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-tool-ref",
                request_id="request-tool-ref",
                run_id="run-tool-ref",
                task_id="task-tool-ref",
            ),
        ),
    )
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    tool_refs = apply_result["restore_refs"]["source_refs"]["tool_outputs"]
    assert tool_refs[0]["path"] == artifact_ref
    assert tool_refs[0]["tool"] == "shell"
    assert tool_refs[0]["scoped_call_id"] == "run-tool-ref:1-1"
    assert tool_refs[0]["source_path"] == "python scripts/collect-alpha.py"
    assert apply_result["work_state_snapshot"]["artifact_refs"][0]["path"] == artifact_ref
    assert apply_result["work_state_snapshot"]["artifact_refs"][0]["source_path"] == "python scripts/collect-alpha.py"
    assert apply_result["work_state_snapshot"]["artifact_refs"][0]["kind"] == "tool_output"
    assert artifact_ref in resume["recommended_read_paths"]
    assert resume["artifact_read_hints"][0]["tool"] == "read_artifact"
    assert resume["artifact_read_hints"][0]["artifact_ref"] == "run-tool-ref:1-1"
    assert resume["artifact_read_hints"][0]["source_path"] == "python scripts/collect-alpha.py"
    assert resume["artifact_read_hints"][0]["artifact_path"] == artifact_ref
    assert resume["continue_packet"]["artifact_read_hints"][0]["artifact_ref"] == "run-tool-ref:1-1"
    assert "Artifact Read Hints" in resume["context_block"]
    assert '"tool": "read_artifact"' in resume["context_block"]
    metadata = json.loads(Path(apply_result["refs"]["metadata"]).read_text(encoding="utf-8"))
    assert metadata["restore_refs"]["source_refs"]["tool_outputs"][0]["path"] == artifact_ref


def test_compact_apply_finds_task_work_tool_output_index_from_owner_root(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    task_work = root / "tasks" / "2026-06-07" / "demo" / "work"
    _append_raw_tool_ref_event(root)
    _append_tool_ref_snapshot(root)
    _append_tool_ref_token_usage(root)
    artifact_ref = _write_large_tool_output_artifact(task_work)

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-tool-ref",
                request_id="request-tool-ref",
                run_id="run-tool-ref",
                task_id="task-tool-ref",
            ),
        ),
    )

    tool_refs = apply_result["restore_refs"]["source_refs"]["tool_outputs"]
    assert tool_refs[0]["path"] == artifact_ref
    assert Path(artifact_ref).is_relative_to(task_work)
    assert not (root / "blobs" / "tool_outputs" / "index.jsonl").exists()


def test_compact_apply_does_not_advance_read_cursor_for_failed_tool_call(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _append_raw_tool_ref_event(root)
    _append_tool_ref_snapshot(root)
    _append_tool_ref_token_usage(root)
    _write_read_file_cursor_fixture_with_failed_retry(root)

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-tool-ref",
                request_id="request-tool-ref",
                run_id="run-tool-ref",
                task_id="task-tool-ref",
            ),
        ),
    )

    progress = [
        item
        for item in apply_result["work_state_snapshot"]["tool_progress"]
        if item.get("tool") == "read_file" and item.get("source_path") == "data/big.txt"
    ]

    assert len(progress) == 1
    assert progress[0]["offset"] == 0
    assert progress[0]["next_offset"] == 100
    assert all(item.get("error_code") != "CONTEXT_COMPACT_DEFERRED" for item in progress)


def test_compact_apply_does_not_expose_task_progress_tool_output_blobs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    shell_ref = _write_run_with_large_tool_output(root)
    progress_ref = _write_task_progress_tool_output_artifact(root)

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-tool-ref",
                request_id="request-tool-ref",
                run_id="run-tool-ref",
                task_id="task-tool-ref",
            ),
        ),
    )

    tool_refs = apply_result["restore_refs"]["source_refs"]["tool_outputs"]
    artifact_refs = apply_result["work_state_snapshot"]["artifact_refs"]
    metadata = json.loads(Path(apply_result["refs"]["metadata"]).read_text(encoding="utf-8"))

    assert shell_ref in [item["path"] for item in tool_refs]
    assert progress_ref not in [item["path"] for item in tool_refs]
    assert progress_ref not in [item["path"] for item in artifact_refs]
    assert progress_ref not in [
        item["path"] for item in metadata["restore_refs"]["source_refs"]["tool_outputs"]
    ]
