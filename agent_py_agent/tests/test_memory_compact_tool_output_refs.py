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


# LLM: This focused fixture creates one compactable run and one large tool-output artifact.
# 函数用途: 为 compact apply/resume 验证 tool output artifact refs，避免测试依赖完整模型工具循环。
def _write_run_with_large_tool_output(root: Path) -> str:
    _append_raw_tool_ref_event(root)
    _append_tool_ref_snapshot(root)
    _append_tool_ref_token_usage(root)
    return _write_large_tool_output_artifact(root)


# LLM: _append_raw_tool_ref_event writes the user-side archive fact for the compact scope.
# 函数用途: 写入与大工具输出同 scope 的 raw 事件，供 compact plan 找到本次任务。
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


# LLM: _append_tool_ref_snapshot writes the recovery snapshot side of the compact fixture.
# 函数用途: 写入包含目标和下一步的压缩快照，让 work_state 能回填恢复状态。
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


# LLM: _append_tool_ref_token_usage gives compact plan a token ledger for the same session.
# 函数用途: 写入最小 token 账本，验证 compact apply 仍保留 token 恢复引用。
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


# LLM: _write_large_tool_output_artifact creates the scoped tool-output artifact under the real index path.
# 函数用途: 调用正式 externalizer 写完整大输出和 index.jsonl，返回 artifact 引用路径。
def _write_large_tool_output_artifact(root: Path) -> str:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="1-1",
            output="very long log\n" + ("x" * 1400),
            ok=True,
            request_id="request-tool-ref",
            run_id="run-tool-ref",
            task_id="task-tool-ref",
        )
    )
    return str(record["artifact_ref"])


# LLM: compact apply should carry tool-output artifact refs as first-class restore facts.
# 函数用途: 验证大工具输出 artifact 会进入 restore_refs、work_state 和 resume 推荐路径。
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
    assert tool_refs[0]["tool"] == "read_file"
    assert tool_refs[0]["scoped_call_id"] == "run-tool-ref:1-1"
    assert apply_result["work_state_snapshot"]["artifact_refs"][0]["path"] == artifact_ref
    assert apply_result["work_state_snapshot"]["artifact_refs"][0]["kind"] == "tool_output"
    assert artifact_ref in resume["recommended_read_paths"]
    assert resume["artifact_read_hints"][0]["tool"] == "read_artifact"
    assert resume["artifact_read_hints"][0]["artifact_ref"] == "run-tool-ref:1-1"
    assert resume["artifact_read_hints"][0]["fallback_path"] == artifact_ref
    assert resume["continue_packet"]["artifact_read_hints"][0]["artifact_ref"] == "run-tool-ref:1-1"
    assert "Artifact Read Hints" in resume["context_block"]
    assert '"tool": "read_artifact"' in resume["context_block"]
    metadata = json.loads(Path(apply_result["refs"]["metadata"]).read_text(encoding="utf-8"))
    assert metadata["restore_refs"]["source_refs"]["tool_outputs"][0]["path"] == artifact_ref
