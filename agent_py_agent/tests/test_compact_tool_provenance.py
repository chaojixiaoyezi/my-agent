"""工具输出按原调用身份落盘及跨进程 Compact 恢复的定向回归。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_call_archive_record as archive_module
from agent_py_agent.agent.agent_core.runtime_mixin import _merged_archive_tool_calls
from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
    carried_tool_call_records,
    tool_call_source_refs,
    tool_output_artifact_refs,
    tool_output_source_refs,
)
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


@pytest.mark.parametrize("externalized", [False, True])
def test_original_index_round_trips_exact_call_identity(tmp_path: Path, externalized: bool) -> None:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="shell",
            call_id="call-1",
            output="a" * 80,
            ok=True,
            run_id="run-1",
            attempt_id="attempt-2",
            turn_id="turn-3",
            task_id="task-1",
            min_chars=1 if externalized else 1_000,
            preview_chars=1_000,
            parameters={"command": "pwd"},
        )
    )
    index_path = tmp_path / "blobs" / "tool_outputs" / "index.jsonl"
    row = json.loads(index_path.read_text(encoding="utf-8").splitlines()[0])
    assert (row["run_id"], row["attempt_id"], row["turn_id"], row["call_id"]) == (
        "run-1", "attempt-2", "turn-3", "call-1",
    )
    assert row["scoped_call_id"] == "run-1:call-1"
    assert (record["attempt_id"], record["turn_id"]) == ("attempt-2", "turn-3")

    if externalized:
        artifact = json.loads(Path(record["artifact_ref"]).read_text(encoding="utf-8"))
        assert (artifact["attempt_id"], artifact["turn_id"]) == ("attempt-2", "turn-3")
        source = tool_output_source_refs(tmp_path, {"run_id": "run-1"})[0]
        restored = tool_output_artifact_refs({"source_refs": {"tool_outputs": [source]}})[0]
        assert (restored["attempt_id"], restored["turn_id"]) == ("attempt-2", "turn-3")
    else:
        source = tool_call_source_refs(tmp_path, {"run_id": "run-1"})[0]
    carried = carried_tool_call_records(tmp_path, {"run_id": "run-1"})[0]
    for item in (source, carried):
        assert (item["attempt_id"], item["turn_id"]) == ("attempt-2", "turn-3")


@pytest.mark.parametrize("externalized", [False, True])
def test_legacy_index_missing_identity_remains_unknown(tmp_path: Path, externalized: bool) -> None:
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="shell",
            call_id="old-call",
            output="old output",
            ok=True,
            run_id="old-run",
            min_chars=1 if externalized else 1_000,
            preview_chars=1_000,
        )
    )
    index_path = tmp_path / "blobs" / "tool_outputs" / "index.jsonl"
    old_row = json.loads(index_path.read_text(encoding="utf-8").splitlines()[0])
    old_row.pop("attempt_id")
    old_row.pop("turn_id")
    next_old_row = dict(old_row, created_at="distinct-legacy-event")
    index_path.write_text(
        json.dumps(old_row, ensure_ascii=False) + "\n"
        + json.dumps(next_old_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    source_refs = tool_output_source_refs if externalized else tool_call_source_refs
    source = source_refs(tmp_path, {"run_id": "old-run"})[0]
    carried_records = carried_tool_call_records(tmp_path, {"run_id": "old-run"})
    assert len(carried_records) == 2
    carried = carried_records[0]
    for item in (source, carried):
        assert item["attempt_id"] == ""
        assert item["turn_id"] == ""


def test_same_call_id_in_different_attempts_and_turns_survives_reload(tmp_path: Path) -> None:
    for attempt_id, turn_id in (("attempt-1", "turn-1"), ("attempt-2", "turn-2")):
        externalize_tool_output_record(
            ExternalizeToolOutputRequest(
                root=tmp_path,
                tool="shell",
                call_id="reused-call",
                output="ok",
                ok=True,
                run_id="run-1",
                attempt_id=attempt_id,
                turn_id=turn_id,
                min_chars=1_000,
                preview_chars=1_000,
            )
        )
    records = carried_tool_call_records(tmp_path, {"run_id": "run-1"})
    assert [(row["attempt_id"], row["turn_id"], row["call_id"]) for row in records] == [
        ("attempt-1", "turn-1", "reused-call"),
        ("attempt-2", "turn-2", "reused-call"),
    ]


def test_equal_large_outputs_keep_distinct_artifact_identity(tmp_path: Path) -> None:
    artifacts = []
    for attempt_id, turn_id in (("attempt-1", "turn-1"), ("attempt-2", "turn-2")):
        record = externalize_tool_output_record(
            ExternalizeToolOutputRequest(
                root=tmp_path,
                tool="shell",
                call_id="reused-call",
                output="same body" * 100,
                ok=True,
                run_id="run-1",
                attempt_id=attempt_id,
                turn_id=turn_id,
                min_chars=1,
            )
        )
        artifacts.append(Path(record["artifact_ref"]))
    assert artifacts[0] != artifacts[1]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    assert [(item["attempt_id"], item["turn_id"]) for item in payloads] == [
        ("attempt-1", "turn-1"),
        ("attempt-2", "turn-2"),
    ]
    source_refs = tool_output_source_refs(tmp_path, {"run_id": "run-1"})
    assert [ref["path"] for ref in source_refs] == [str(path) for path in artifacts]
    assert [(ref["attempt_id"], ref["turn_id"]) for ref in source_refs] == [
        ("attempt-1", "turn-1"),
        ("attempt-2", "turn-2"),
    ]


def test_archive_merge_keeps_cross_attempt_and_unknown_rows() -> None:
    first = {
        "run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "turn-1",
        "call_id": "call-1", "scoped_call_id": "run-1:call-1", "tool": "shell",
        "parameters": {"command": "pwd"}, "output_hash": "same-body",
    }
    later = dict(first, attempt_id="attempt-2", turn_id="turn-2")
    unknown = {key: value for key, value in first.items() if key not in {"attempt_id", "turn_id"}}
    merged = _merged_archive_tool_calls([first, unknown], [dict(first), later, dict(unknown)])
    assert merged == [first, unknown, later, unknown]


@pytest.mark.parametrize("projection", [False, True])
def test_archive_request_uses_executed_call_identity(tmp_path: Path, monkeypatch, projection: bool) -> None:
    class RequestCaptured(Exception):
        pass

    captured: list[ExternalizeToolOutputRequest] = []

    def capture(request: ExternalizeToolOutputRequest) -> dict[str, object]:
        captured.append(request)
        raise RequestCaptured

    monkeypatch.setattr(archive_module, "externalize_tool_output_record", capture)
    monkeypatch.setattr(archive_module, "_tool_output_archive_root", lambda *_: tmp_path)
    call = ToolCall(
        call_id="call-1",
        tool_name="shell",
        arguments={"command": "pwd"},
        source_protocol="native",
        schema_hash="sha256:1",
        run_id="actual-run",
        attempt_id="actual-attempt",
        turn_id="actual-turn",
    )
    params = SimpleNamespace(
        request_id="current-request",
        run_id="wrong-current-run",
        attempt_id="wrong-current-attempt",
        turn_id="wrong-current-turn",
        task_id="task-1",
        tool_runtime_snapshot=None,
    )
    agent = SimpleNamespace(config=SimpleNamespace())
    with pytest.raises(RequestCaptured):
        if projection:
            archive_module.archive_tool_output_projection(
                agent, params, call, ToolHandlerOutcome(tool="shell", ok=True, output="ok"),
            )
        else:
            result = SimpleNamespace(
                metadata={}, tool_name="shell", output="ok", ok=True,
                error_code="", reported_error_code="",
            )
            record = SimpleNamespace(call=call, params=params, result=result, model_visible_call=call)
            archive_module.archive_tool_call_record(agent, record)
    assert len(captured) == 1
    assert (captured[0].run_id, captured[0].attempt_id, captured[0].turn_id) == (
        "actual-run", "actual-attempt", "actual-turn",
    )
