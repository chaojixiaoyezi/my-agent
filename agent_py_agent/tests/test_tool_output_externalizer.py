from __future__ import annotations

"""LLM: regression tests for externalized runtime tool outputs.

给人看的解释：
这组测试确认大工具输出会进入 artifact 文件，归档记录只留下摘要、hash 和路径；
下一轮 live prompt 只读取摘要和恢复锚点，避免把黑盒大输出直接塞回上下文。
"""

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolCallRecordParams, ToolLoopService
from agent_py_agent.agent.agent_core.tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
    render_tool_payload_for_live_prompt,
)
from agent_py_agent.agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from agent_py_agent.agent.memory_archive.runtime.turn_archiver import (
    ArchiveRunTurnParams,
    ArchiveTurnContext,
    archive_run_turn,
)
from agent_py_agent.agent.memory_archive.schema import RUNTIME_MEMORY_SCHEMA_VERSION
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    RECOVERY_WRITE_CHUNK_CHARS,
)
from agent_py_agent.agent.tooling.models import ToolExecutionResult


def test_tool_loop_externalizes_large_tool_output_for_archive(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "line\n" + ("x" * 25_000)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "shell", "command": "cat large.log"},
            result=ToolExecutionResult("shell", True, large_output),
        )
    )
    record = params.archive_tool_calls[0]
    artifact_path = Path(record["output_path"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["output_externalized"] is True
    _assert_schema_v2(record, "tool_output_archive_record")
    _assert_schema_v2(artifact, "tool_output_artifact")
    _assert_schema_v2(index[-1], "tool_output_index")
    assert record["output_hash"] == artifact["sha256"]
    assert record["output_size_bytes"] == len(large_output.encode("utf-8"))
    assert "x" * 5000 not in json.dumps(record, ensure_ascii=False)
    assert artifact["content"] == large_output
    assert artifact["request_id"] == "req-tool"
    assert artifact["run_id"] == "run-tool"
    assert artifact["scoped_call_id"] == "run-tool:1-1"
    assert index[-1]["path"] == str(artifact_path)
    assert index[-1]["sha256"] == artifact["sha256"]
    assert index[-1]["run_id"] == "run-tool"
    assert index[-1]["scoped_call_id"] == "run-tool:1-1"
    assert large_output not in params.tool_context[-1]
    assert str(artifact_path) not in params.tool_context[-1]
    assert "output_artifact_ref:" not in params.tool_context[-1]
    assert "output_call_id: 1-1" in params.tool_context[-1]
    assert "output_scoped_call_id: run-tool:1-1" in params.tool_context[-1]
    assert '"read_artifact", "artifact_ref": "run-tool:1-1"' in params.tool_context[-1]
    assert '"run_id": "run-tool"' in params.tool_context[-1]
    assert "完整工具输出已外置" in params.tool_context[-1]
    assert record["fail_safe_checkpoint_path"] in params.tool_context[-1]


def test_tool_output_index_preserves_failed_tool_status(tmp_path: Path) -> None:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-2",
            output="CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行。",
            ok=False,
            error_code="CONTEXT_COMPACT_DEFERRED",
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=0,
            parameters={"path": "data/big.txt", "offset": 100, "max_chars": 100},
        )
    )

    artifact_path = Path(str(record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["ok"] is False
    assert record["status"] == "error"
    assert record["error_code"] == "CONTEXT_COMPACT_DEFERRED"
    assert artifact["ok"] is False
    assert artifact["status"] == "error"
    assert artifact["error_code"] == "CONTEXT_COMPACT_DEFERRED"
    assert index[-1]["ok"] is False
    assert index[-1]["status"] == "error"
    assert index[-1]["error_code"] == "CONTEXT_COMPACT_DEFERRED"


def test_tool_call_index_preserves_short_failed_tool_status(tmp_path: Path) -> None:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-3",
            output="CONTEXT_COMPACT_DEFERRED: 未执行。",
            ok=False,
            error_code="CONTEXT_COMPACT_DEFERRED",
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=1000,
            parameters={"path": "data/big.txt", "offset": 100, "max_chars": 100},
        )
    )
    index_path = tmp_path / "blobs" / "tool_outputs" / "index.jsonl"
    index = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert record["output_externalized"] is False
    assert "artifact_ref" not in record
    assert index[-1]["kind"] == "tool_call"
    assert index[-1]["ok"] is False
    assert index[-1]["status"] == "error"
    assert index[-1]["error_code"] == "CONTEXT_COMPACT_DEFERRED"


def test_tool_output_index_preserves_read_file_window_metadata(tmp_path: Path) -> None:
    read_window = {
        "kind": "char_window",
        "offset": 100,
        "chars": 50,
        "next_offset": 150,
        "total_chars": 500,
        "complete": False,
    }
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-4",
            output="window body",
            ok=True,
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=0,
            parameters={"path": "data/big.txt", "offset": 100, "max_chars": 50},
            result_envelope={"read_window": read_window},
        )
    )

    artifact_path = Path(str(record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["read_window"] == read_window
    assert artifact["read_window"] == read_window
    assert index[-1]["read_window"] == read_window


def test_tool_loop_keeps_moderate_tool_output_inline_for_model_context(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    output = "\n".join(f"章节 {idx:03d}: CP-{idx:03d}-{idx:03d}" for idx in range(1, 81))

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "run_command", "command": "extract chapters"},
            result=ToolExecutionResult("run_command", True, output),
        )
    )

    record = params.archive_tool_calls[0]
    assert len(output) > 1200
    assert record["output_externalized"] is False
    assert "章节 080: CP-080-080" in params.tool_context[-1]


def test_tool_loop_records_live_raw_archive_for_each_tool_result(tmp_path: Path) -> None:
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(), session_id="session-live")
    service = ToolLoopService(agent)
    params = _tool_loop_params(request_id="req-live", run_id="run-live", task_id="task-live")

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=3,
            idx=2,
            payload={"tool": "read_file", "path": "notes.txt"},
            result=ToolExecutionResult("read_file", True, "文件内容"),
        )
    )

    record = params.archive_tool_calls[0]
    raw_path = Path(record["raw_archive_path"])
    raw_records = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]

    assert record["raw_archive_event_id"]
    assert raw_records[-1]["action"] == "tool_call"
    assert raw_records[-1]["tool_name"] == "read_file"
    assert raw_records[-1]["tool_call_id"] == "3-2"
    assert raw_records[-1]["request_id"] == "req-live"


def test_tool_loop_externalizer_falls_back_to_current_subagent_run_id(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path, _current_subagent_run_id="runner-42"))
    params = _tool_loop_params(request_id="", run_id="", task_id="")
    large_output = "line\n" + ("x" * 25_000)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=7,
            idx=1,
            payload={"tool": "inspect_agent_tree"},
            result=ToolExecutionResult("inspect_agent_tree", True, large_output),
        )
    )
    record = params.archive_tool_calls[0]
    artifact = json.loads(Path(record["output_path"]).read_text(encoding="utf-8"))

    assert record["run_id"] == "runner-42"
    assert record["scoped_call_id"] == "runner-42:7-1"
    assert artifact["run_id"] == "runner-42"


def test_externalizer_preserves_internal_tool_outputs_without_path_sanitizer(tmp_path: Path) -> None:
    path = "/repo/current/tasks/run_1/work/agents/run_1/final_report.md"
    output = json.dumps({"workspace_refs": {"final_report": path}}, ensure_ascii=False)

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="inspect_agent_tree",
            call_id="8-1",
            output=output,
            ok=True,
            run_id="run-tree",
            min_chars=10,
        )
    )
    artifact = json.loads(Path(record["artifact_ref"]).read_text(encoding="utf-8"))

    assert path in record["output_preview"]
    assert path in artifact["content"]
    assert "[internal_legacy_subagent_path_hidden]" not in artifact["content"]


def test_externalizer_archives_read_file_output_but_keeps_live_inline(tmp_path: Path) -> None:
    output = "用户文档里提到 /repo/data/subagents/tasks/run_1 这个历史路径。"

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-1",
            output=output,
            ok=True,
            run_id="run-file",
            min_chars=10,
        )
    )

    assert record["output_preview"] == output
    assert record["output_externalized"] is False
    assert record["output_path"] == ""
    assert record["artifact_ref"] == record["source_artifact_ref"]
    assert record["source_output_archived"] is True
    assert Path(str(record["source_artifact_ref"])).exists()
    artifact = json.loads(Path(str(record["source_artifact_ref"])).read_text(encoding="utf-8"))
    assert artifact["content"] == output


def test_externalizer_archives_read_file_when_utf8_bytes_cross_threshold(tmp_path: Path) -> None:
    output = "现场记录：" + ("汉" * 60_000)

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-utf8",
            output=output,
            ok=True,
            run_id="run-file",
            min_chars=100_000,
            parameters={"path": "data/field_journal.txt", "start_line": 1, "max_chars": 100_000},
        )
    )

    artifact_path = Path(str(record["source_artifact_ref"]))
    index = [json.loads(line) for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()]

    assert len(output) < 100_000
    assert len(output.encode("utf-8")) >= 100_000
    assert record["output_externalized"] is False
    assert record["source_output_archived"] is True
    assert artifact_path.exists()
    assert index[-1]["kind"] == "tool_output"
    assert index[-1]["size_bytes"] == len(output.encode("utf-8"))


def test_tool_loop_read_file_archive_does_not_hide_live_result(tmp_path: Path) -> None:
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(), session_id="session-live")
    service = ToolLoopService(agent)
    params = _tool_loop_params(request_id="req-read", run_id="run-read", task_id="task-read")
    output = "CPX-001-ABCDEF1234\n" + ("x" * 25_000)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=2,
            idx=1,
            payload={"tool": "read_file", "path": "fragment-001.txt"},
            result=ToolExecutionResult("read_file", True, output),
        )
    )

    record = params.archive_tool_calls[0]
    assert record["output_externalized"] is False
    assert record["source_output_archived"] is True
    assert record["artifact_ref"] == record["source_artifact_ref"]
    assert Path(str(record["source_artifact_ref"])).exists()
    assert "CPX-001-ABCDEF1234" in params.tool_context[-1]
    assert "output_scoped_call_id:" in params.tool_context[-1]


def test_read_artifact_rejects_cross_run_absolute_tool_output_path(tmp_path: Path) -> None:
    old_record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="shell",
            call_id="1-1",
            output="OLD-RUN-CONTENT",
            ok=True,
            run_id="run-old",
            task_id="run-old",
            request_id="run-old",
            min_chars=1,
        )
    )

    blocked = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=tmp_path,
            artifact_ref=str(old_record["artifact_ref"]),
            run_id="run-new",
            task_id="run-new",
            request_id="run-new",
        )
    )
    allowed = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=tmp_path,
            artifact_ref="run-old:1-1",
            run_id="run-old",
            max_chars=40,
        )
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "artifact_not_registered"
    assert allowed["ok"] is True
    assert allowed["content"] == "OLD-RUN-CONTENT"


def test_read_artifact_output_is_not_re_externalized(tmp_path: Path) -> None:
    output = json.dumps(
        {
            "ok": True,
            "reads_artifact_body": True,
            "artifact_ref": str(tmp_path / "blobs/tool_outputs/read_file-1.json"),
            "content": "important recovery packet\n" + ("x" * 1600),
            "content_chars": 1626,
            "truncated": False,
        },
        ensure_ascii=False,
    )

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_artifact",
            call_id="2-1",
            output=output,
            ok=True,
            run_id="runner-1",
        )
    )

    assert record["output_externalized"] is False
    assert record["output_path"] == ""
    assert not (tmp_path / "blobs/tool_outputs").exists()


def test_tool_loop_summarizes_large_tool_call_payload_for_live_prompt() -> None:
    huge_html = "<html>" + ("x" * 9000) + "</html>"
    response = (
        "我要写条目页面。\n"
        "[TOOL_CALL]\n"
        + json.dumps({"tool": "write_file", "path": "shop/list.html", "content": huge_html})
        + "\n[/TOOL_CALL]"
    )

    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(
            response_text=response,
            tool_calls=[{"tool": "write_file", "path": "shop/list.html", "content": huge_html}],
        )
    )

    assert "assistant tool-call response summarized" in rendered
    assert "tool_call_1: tool=write_file" in rendered
    assert "path: shop/list.html" in rendered
    assert "large text omitted" in rendered
    assert huge_html not in rendered


def test_tool_call_record_summarizes_large_payload_for_live_prompt(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    huge_html = "<html>" + ("x" * 9000) + "</html>"

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "shop/flow-a.html", "content": huge_html},
            result=ToolExecutionResult("write_file", False, "路径不在 allowed_write_roots 内"),
        )
    )

    live_context = params.tool_context[-1]
    assert "tool_call_1: tool=write_file" in live_context
    assert "path: shop/flow-a.html" in live_context
    assert "large text omitted" in live_context
    assert huge_html not in live_context


def test_tool_call_archive_keeps_runtime_gate_for_replay(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "read_file", "path": "README.md"},
            result=ToolExecutionResult(
                "read_file",
                True,
                "ok",
                result_envelope={"runtime_gate": {"status": "ALLOW", "allowed": True}},
            ),
        )
    )

    record = params.archive_tool_calls[0]
    assert record["runtime_gate"]["status"] == "ALLOW"
    assert record["runtime_gate"]["allowed"] is True


def test_tool_loop_enters_long_content_recovery_after_truncated_write_parse_error(
    tmp_path: Path,
) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    payload = {
        "tool": "__parse_error__",
        "error_code": "TOOL_INLINE_CONTENT_STREAM_ABORTED",
        "error": "工具调用缺少结束标记 [/TOOL_CALL]",
        "source_tool": "write_file",
        "path": "site/app.js",
        "content_field_present": True,
    }
    output = (
        "工具调用缺少结束标记 [/TOOL_CALL]。如果上一轮是 write_file/apply_patch 且 "
        "content 太长，不要重复输出完整 content；必须先用 write_file 写短骨架，再用 "
        f"apply_patch 分块追加内容；content 降到不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符。"
    )

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=2,
            idx=1,
            payload=payload,
            result=ToolExecutionResult(
                "__parse_error__",
                False,
                output,
                error_code="TOOL_INLINE_CONTENT_STREAM_ABORTED",
            ),
        )
    )

    live_context = "\n".join(params.tool_context)
    assert "long_content_recovery_mode" in live_context
    assert "WRITE_FILE_RAW" in live_context
    assert "write_file.content" in live_context
    assert f"不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符" in live_context
    assert "site/app.js" in live_context


def test_tool_loop_does_not_enter_long_content_recovery_from_raw_parse_error_text(
    tmp_path: Path,
) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    payload = {
        "tool": "__parse_error__",
        "error_code": "TOOL_INLINE_CONTENT_STREAM_ABORTED",
        "error": "工具调用缺少结束标记 [/TOOL_CALL]",
        "raw": '{"tool":"write_file","path":"site/app.js","content":"const data = ',
    }

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=2,
            idx=1,
            payload=payload,
            result=ToolExecutionResult(
                "__parse_error__",
                False,
                "工具调用缺少结束标记 [/TOOL_CALL]",
                error_code="TOOL_INLINE_CONTENT_STREAM_ABORTED",
            ),
        )
    )

    live_context = "\n".join(params.tool_context)
    assert "long_content_recovery_mode" not in live_context


def test_tool_loop_enters_structured_json_recovery_after_truncated_parse_error(
    tmp_path: Path,
) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-json", run_id="run-json", task_id="task-json")
    payload = {
        "tool": "__parse_error__",
        "error_code": "TOOL_CALL_UNCLOSED",
        "error": "工具调用缺少结束标记 [/TOOL_CALL]",
        "raw": '{"tool":"write_file","path":"outputs/report/source_data.json","sheets":[{"rows":[',
    }
    output = "工具调用缺少结束标记 [/TOOL_CALL]。write_structured_json 参数太长。"

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=2,
            idx=1,
            payload=payload,
            result=ToolExecutionResult("__parse_error__", False, output),
        )
    )

    live_context = "\n".join(params.tool_context)
    assert "structured_json_recovery_mode" not in live_context
    assert "write_file" in live_context
    assert "write_structured_json 参数太长" in live_context
    assert "outputs/report/source_data.json" in live_context


def test_tool_loop_does_not_enter_long_content_recovery_from_inline_write_message_only(
    tmp_path: Path,
) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    rejected_chars = MAX_INLINE_WRITE_CONTENT_CHARS + 500
    output = (
        f"write_file.content inline content 超过推荐值：{rejected_chars} 字符，"
        f"推荐最多 {MAX_INLINE_WRITE_CONTENT_CHARS} 字符。 path=site/app.css\n"
        "请先用 write_file 写短骨架，再用 apply_patch 分块追加。"
    )

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=3,
            idx=1,
            payload={"tool": "write_file", "path": "site/app.css", "content": "A" * rejected_chars},
            result=ToolExecutionResult("write_file", False, output),
        )
    )

    live_context = "\n".join(params.tool_context)
    assert "long_content_recovery_mode" not in live_context


def test_render_tool_payload_keeps_small_payload_readable() -> None:
    payload = {"tool": "read_file", "path": "README.md"}
    rendered = render_tool_payload_for_live_prompt(payload)

    assert "tool_call_1: tool=read_file" in rendered
    assert "path: README.md" in rendered
    assert "{'tool'" not in rendered


def test_tool_loop_writes_fail_safe_checkpoint_before_externalizing_large_output(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "danger\n" + ("x" * 25_000)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "blackbox_tool"},
            result=ToolExecutionResult("blackbox_tool", True, large_output),
        )
    )

    record = params.archive_tool_calls[0]
    checkpoint_path = Path(record["fail_safe_checkpoint_path"])
    snapshots = [
        json.loads(line)
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["fail_safe_checkpoint_written"] is True
    assert checkpoint_path.exists()
    assert snapshots[-1]["turn_range"]["source"] == "tool_output_externalizer"
    assert snapshots[-1]["tool_calls"][0]["tool"] == "blackbox_tool"
    assert snapshots[-1]["tool_calls"][0]["output_hash"] == record["output_hash"]
    assert snapshots[-1]["next_actions"] == ["先读取工具输出 artifact 摘要和 fail-safe checkpoint，再决定是否把内容切片读回 prompt。"]


def test_archive_tool_event_keeps_externalized_output_path(tmp_path: Path) -> None:
    output_path = tmp_path / "blobs" / "tool_outputs" / "demo.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("{}", encoding="utf-8")

    result = archive_run_turn(
        ArchiveRunTurnParams(
            root=tmp_path,
            ctx=ArchiveTurnContext(
                session_id="session-tool",
                user_prompt="run tool",
                response_text="done",
                backend="echo",
                request_id="req-tool",
                run_id="run-tool",
                task_id="task-tool",
                tool_calls=[{
                    "tool": "read_file",
                    "id": "1-1",
                    "ok": True,
                    "output_preview": "preview",
                    "output_hash": "hash",
                    "output_path": str(output_path),
                    "output_externalized": True,
                    "parameters": {"path": "large.log"},
                }],
            ),
        )
    )
    tool_event = next(event for event in result.events if event.speaker == "tool")

    assert tool_event.content_path == str(output_path)
    assert tool_event.content_preview
    assert tool_event.tool_name == "read_file"


def _assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"]["name"] == name
    assert record["schema"]["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert "reserved" not in record


def _tool_loop_params(*, request_id: str, run_id: str, task_id: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
