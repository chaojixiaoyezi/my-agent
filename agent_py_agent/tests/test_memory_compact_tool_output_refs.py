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
