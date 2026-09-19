from __future__ import annotations

"""LLM: regression tests for externalized runtime tool outputs.

给人看的解释：
这组测试确认大工具输出会进入 artifact 文件，归档记录只留下摘要、hash 和路径；
下一轮 live prompt 只读取摘要和恢复锚点，避免把黑盒大输出直接塞回上下文。
"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolCallRecordParams, ToolLoopService
from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_output_projection
from agent_py_agent.agent.agent_core.tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
    render_tool_payload_for_live_prompt,
)
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
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
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolFailureFacts,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
    runtime_snapshot_for_tools,
)


def _canonical_record(
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
    idx: int,
    tool_name: str,
    arguments: dict[str, object],
    output: str,
    ok: bool = True,
    error_code: str = "TOOL_EXECUTION_FAILED",
    handler_details: dict[str, object] | None = None,
    handler_executed: bool = True,
) -> ToolCallRecordParams:
    """Build an archive record from the sole canonical call/result contracts."""

    run_id = params.run_id or params.request_id or "archive-test-run"
    attempt_id = params.request_id or "archive-test-attempt"
    call = canonical_history_call(
        tool_name,
        arguments,
        call_id=f"{tool_rounds}-{idx}",
        source_protocol="native",
        run_id=run_id,
        turn_id=f"{run_id}:round-{tool_rounds}",
        attempt_id=attempt_id,
    )
    metadata = {"handler_details": dict(handler_details or {})}
    result = (
        ToolResult.succeeded(
            call,
            output,
            facts=ToolSuccessFacts(
                metadata=metadata,
                handler_executed=handler_executed,
            ),
        )
        if ok
        else ToolResult.failed(
            call,
            output,
            error_code=error_code,
            failure_stage="execution" if handler_executed else "protocol",
            facts=ToolFailureFacts(
                handler_executed=handler_executed,
                metadata=metadata,
            ),
        )
    )
    return ToolCallRecordParams(
        params=params,
        tool_rounds=tool_rounds,
        idx=idx,
        call=call,
        result=result,
    )


def test_tool_loop_externalizes_large_tool_output_for_archive(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "line\n" + ("x" * 250_000)

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=1,
            idx=1,
            tool_name="shell",
            arguments={"command": "cat large.log"},
            output=large_output,
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
    assert '"run_id": "run-tool"' not in params.tool_context[-1]
    assert "完整工具输出已外置" in params.tool_context[-1]
    assert record["fail_safe_checkpoint_path"] in params.tool_context[-1]
    native_messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    native_result = native_messages[-1]["content"][0]["content"]
    assert native_result == params.tool_context[-1].split(
        "[tool-output-record round=1 index=1]\n",
        1,
    )[1]
    assert str(artifact_path) not in native_result
    assert '"read_artifact", "artifact_ref": "run-tool:1-1"' in native_result


def test_tool_call_index_preserves_nested_plan_and_dispatch_parameters(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
        carried_tool_call_records,
    )

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="create_subagents",
            call_id="plan-dispatch-1",
            output="created",
            ok=True,
            run_id="root-run",
            task_id="root-run",
            min_chars=10_000,
            parameters={
                "goal": "完成现有 Todo",
                "items": [
                    {
                        "goal": "实现命令层",
                        "covers": ["commands"],
                        "attributes": {"phase": "implementation"},
                        "api_key": "must-not-survive",
                    }
                ],
            },
        )
    )
    index_path = tmp_path / "blobs" / "tool_outputs" / "index.jsonl"
    index = [
        json.loads(line)
        for line in index_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    params = index[-1]["parameters"]
    assert record["output_externalized"] is False
    assert params["items"][0]["goal"] == "实现命令层"
    assert params["items"][0]["covers"] == ["commands"]
    assert params["items"][0]["attributes"] == {"phase": "implementation"}
    assert params["items"][0]["api_key"] == "<redacted>"
    carried = carried_tool_call_records(
        tmp_path,
        {"run_id": "root-run", "task_id": "root-run"},
    )
    assert carried[0]["parameters"] == params


def test_explicit_preview_truncation_forces_recovery_artifact_below_global_threshold(
    tmp_path: Path,
) -> None:
    """工具主动缩短模型预览时，完整正文必须仍可通过统一 artifact 合同恢复。"""

    output = "header\n" + ("full-body-" * 100)
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="web_fetch",
            call_id="web-small-preview",
            output=output,
            ok=True,
            run_id="web-run",
            task_id="web-task",
            min_chars=100_000,
            preview_chars=100_000,
            result_envelope={
                "tool_output_policy": {
                    "live_prompt_output": "header\nfull-body-... 已截断",
                    "requires_recovery_artifact": True,
                }
            },
        )
    )

    assert record["output_externalized"] is True
    artifact = json.loads(Path(record["output_path"]).read_text(encoding="utf-8"))
    assert artifact["content"] == output


def test_production_archiver_preserves_tool_preview_policy_when_adding_trust(
    tmp_path: Path,
) -> None:
    """宿主补 trust/redaction 时不能覆盖工具声明的强制恢复归档事实。"""

    params = _tool_loop_params(request_id="req-web", run_id="run-web", task_id="task-web")
    call = canonical_history_call(
        "web_fetch",
        {"url": "https://example.com"},
        call_id="web-call",
        source_protocol="native",
        run_id="run-web",
        turn_id="run-web:round-1",
        attempt_id="req-web",
    )
    outcome = ToolHandlerOutcome(
        "web_fetch",
        True,
        "complete response below every global threshold",
        result_envelope={
            "tool_output_policy": {
                "live_prompt_output": "bounded preview... 已截断",
                "requires_recovery_artifact": True,
            }
        },
    )

    projection = archive_tool_output_projection(
        SimpleNamespace(root=tmp_path, config=AgentConfig()),
        params,
        call,
        outcome,
    )
    record = projection.metadata["archive_output_record"]

    assert record["output_externalized"] is True
    assert Path(record["output_path"]).is_file()


@pytest.mark.parametrize("archive_threshold", [1, 200_000])
@pytest.mark.parametrize("page_chars", [12_000, 5_000])
def test_production_file_read_keeps_bounded_body_and_continuation(
    tmp_path: Path, archive_threshold: int, page_chars: int,
) -> None:
    """读取器的有界结果经过真实执行/归档链后，不能被日志预览再裁一次。"""
    (tmp_path / "source.txt").write_text("row-data\n" * 800 + "LAST-ROW\n", encoding="utf-8")
    tool = ReadFileTool(tmp_path, max_chars=page_chars)
    params = _tool_loop_params(request_id="req-read", run_id="run-read", task_id="task-read")
    params = replace(params, tool_runtime_snapshot=runtime_snapshot_for_tools({"read_file": tool}, run_id="run-read"))
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(
        tool_output_externalize_min_chars=archive_threshold, tool_output_preview_chars=4_000,
    ))
    outcome = tool.execute({"path": "source.txt"})
    execution = execute_canonical_test_call(
        tmp_path, tools={"read_file": tool}, tool_name="read_file",
        arguments={"path": "source.txt"}, run_id="run-read",
        output_archiver=lambda call, result: archive_tool_output_projection(agent, params, call, result),
    )
    result = execution.result
    archive = result.metadata["archive_output_record"]
    rendered = render_tool_result_for_live_prompt(result, archive)
    assert result.ok
    assert len(outcome.output) > 4_000
    assert outcome.output in rendered
    assert "... [truncated" not in rendered
    assert result.metadata["projection_truncated"] is False
    assert len(archive["output_preview"]) < len(outcome.output)
    ToolLoopService(agent)._record_tool_call(ToolCallRecordParams(
        params=params, tool_rounds=1, idx=1, call=execution.call, result=result,
    ))
    native = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    assert outcome.output in native[-1]["content"][0]["content"]


def test_production_artifact_page_keeps_valid_json_and_next_cursor(tmp_path: Path) -> None:
    """归档读取页本身不可再次变成预览，否则正文和下一页参数均无法使用。"""
    output = json.dumps({"reads_artifact_body": True, "artifact_ref": "source-run:source-call",
        "content": "page-data\n" * 700, "next_read": {"offset": 7_000, "max_chars": 7_000}})
    params = _tool_loop_params(request_id="req-page", run_id="run-page", task_id="task-page")
    call = canonical_history_call("read_artifact", {}, call_id="page-call", run_id="run-page")
    projection = archive_tool_output_projection(
        SimpleNamespace(root=tmp_path, config=AgentConfig()), params, call,
        ToolHandlerOutcome("read_artifact", True, output),
    )
    body = "\n".join(block.text for block in projection.content_blocks if block.type == "text")
    assert json.loads(body)["next_read"] == {"offset": 7_000, "max_chars": 7_000}
    assert body == output


def test_production_explicit_preserve_policy_keeps_bounded_body(tmp_path: Path) -> None:
    """宿主允许工具保留的有界正文，不能在下游 reducer 使用前已丢失。"""
    output = "bounded item\n" * 500
    params = _tool_loop_params(request_id="req-keep", run_id="run-keep", task_id="task-keep")
    call = canonical_history_call("watch_stream", {}, call_id="keep-call", run_id="run-keep")
    projection = archive_tool_output_projection(
        SimpleNamespace(root=tmp_path, config=AgentConfig()), params, call,
        ToolHandlerOutcome("watch_stream", True, output, result_envelope={
            "tool_output_policy": {"preserve_prompt_output": True},
        }),
    )
    assert projection.metadata["archive_output_record"]["output_externalized"] is True
    assert projection.metadata["projection_truncated"] is False
    assert output == "\n".join(block.text for block in projection.content_blocks if block.type == "text")


def test_production_inline_body_still_uses_redaction(tmp_path: Path) -> None:
    """改为保留内联正文不代表可以绕过最终脱敏，尾部非敏感信息仍须到达模型。"""
    output = json.dumps({"payload": "bounded-data " * 500,
                         "api_key": "fixture-secret-not-a-real-key", "tail": "LAST-ITEM"})
    params = _tool_loop_params(request_id="req-safe", run_id="run-safe", task_id="task-safe")
    projection = archive_tool_output_projection(
        SimpleNamespace(root=tmp_path, config=AgentConfig()), params,
        canonical_history_call("read_file", {}, call_id="safe-call", run_id="run-safe"),
        ToolHandlerOutcome("read_file", True, output),
    )
    body = "\n".join(block.text for block in projection.content_blocks if block.type == "text")
    assert "fixture-secret-not-a-real-key" not in body
    assert json.loads(body)["tail"] == "LAST-ITEM"
    assert projection.metadata["projection_truncated"] is False


def test_compact_carried_create_subagents_keeps_structured_child_recovery_facts(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        _reconstructed_tool_context_entry,
    )

    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(
        request_id="req-tree",
        run_id="run-main",
        task_id="task-main",
    )
    output = json.dumps(
        {
            "scope": "task_workspace",
            "root_id": "run-main",
            "status_buckets": {
                "running": [],
                "blocked": ["child-retry"],
                "completed": ["child-done"],
                "failed": [],
                "takeover_candidates": ["child-retry"],
            },
            "child_result_index": [
                {
                    "run_id": "child-retry",
                    "status": "BLOCKED",
                    "verification_status": "INCOMPLETE",
                    "expected_outputs": ["output/report.md"],
                    "primary_artifact_refs": [],
                    "read_order": [],
                }
            ],
            "padding": "x" * 6_000,
        },
        ensure_ascii=False,
    )

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=3,
            idx=1,
            tool_name="create_subagents",
            arguments={"goal": "继续既有计划"},
            output=output,
        )
    )

    record = params.archive_tool_calls[-1]
    carried = _reconstructed_tool_context_entry(record)

    assert record["output_externalized"] is True
    assert "status_buckets" in record["model_summary"]
    assert "child-retry" in record["model_summary"]
    assert "takeover_candidates" in record["model_summary"]
    assert "child-retry" in carried
    assert "padding" not in carried


def test_compact_carried_large_write_keeps_path_but_omits_full_content() -> None:
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        _reconstructed_tool_context_entry,
    )

    content = "UNIQUE-LARGE-WRITE-BODY\n" * 1_500
    carried = _reconstructed_tool_context_entry(
        {
            "tool": "write_file",
            "ok": True,
            "parameters": {
                "tool": "write_file",
                "path": "reports/final.md",
                "mode": "overwrite",
                "content": content,
            },
            "output_hash": "sha256:typed-output",
            "handler_executed": True,
        }
    )

    assert "tool_call_1: tool=write_file" in carried
    assert "reports/final.md" in carried
    assert "mode: overwrite" in carried
    assert "<large text omitted" in carried
    assert "sha256=" in carried
    assert content not in carried
    assert len(carried) < 2_000


def test_tool_output_index_preserves_failed_tool_status(tmp_path: Path) -> None:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-2",
            output="CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行。",
            ok=False,
            error_code="CONTEXT_COMPACT_DEFERRED",
            reported_error_code="PROVIDER_CONTEXT_LIMIT",
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
    assert record["reported_error_code"] == "PROVIDER_CONTEXT_LIMIT"
    assert artifact["ok"] is False
    assert artifact["status"] == "error"
    assert artifact["error_code"] == "CONTEXT_COMPACT_DEFERRED"
    assert artifact["reported_error_code"] == "PROVIDER_CONTEXT_LIMIT"
    assert index[-1]["ok"] is False
    assert index[-1]["status"] == "error"
    assert index[-1]["error_code"] == "CONTEXT_COMPACT_DEFERRED"
    assert index[-1]["reported_error_code"] == "PROVIDER_CONTEXT_LIMIT"


def test_tool_call_index_preserves_short_failed_tool_status(tmp_path: Path) -> None:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-3",
            output="CONTEXT_COMPACT_DEFERRED: 未执行。",
            ok=False,
            error_code="CONTEXT_COMPACT_DEFERRED",
            reported_error_code="PROVIDER_CONTEXT_LIMIT",
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=1000,
            parameters={"path": "data/big.txt", "offset": 100, "max_chars": 100},
        )
    )
    index_path = tmp_path / "blobs" / "tool_outputs" / "index.jsonl"
    index = [
        json.loads(line)
        for line in index_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["output_externalized"] is False
    assert "artifact_ref" not in record
    assert index[-1]["kind"] == "tool_call"
    assert index[-1]["ok"] is False
    assert index[-1]["status"] == "error"
    assert index[-1]["error_code"] == "CONTEXT_COMPACT_DEFERRED"
    assert index[-1]["reported_error_code"] == "PROVIDER_CONTEXT_LIMIT"


def test_externalizer_keeps_recovery_artifact_when_preview_truncates(
    tmp_path: Path,
) -> None:
    output = "0123456789abcdef"

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="inspect_agent_tree",
            call_id="1-preview",
            output=output,
            ok=True,
            run_id="run-tree",
            min_chars=100,
            preview_chars=6,
        )
    )

    assert record["output_preview"].startswith("012345\n")
    assert "truncated 10 chars" in record["output_preview"]
    assert record["output_externalized"] is True
    artifact = json.loads(Path(str(record["artifact_ref"])).read_text(encoding="utf-8"))
    assert artifact["content"] == output


def test_tool_output_index_preserves_host_execution_diagnostics(tmp_path: Path) -> None:
    execution = {
        "handler_executed": False,
        "failure_stage": "validation",
        "duration_ms": 7,
    }
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="read_file",
            call_id="1-diagnostic",
            output="缺少 path",
            ok=False,
            error_code="TOOL_PARAMETER_REQUIRED",
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=0,
            parameters={},
            result_envelope={
                "tool_execution": {
                    **execution,
                    "private": "must-not-persist",
                }
            },
        )
    )
    artifact_path = Path(str(record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = json.loads(
        (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )

    assert record["tool_execution"] == execution
    assert artifact["tool_execution"] == execution
    assert index["tool_execution"] == execution
    assert "must-not-persist" not in json.dumps(
        [record, artifact, index],
        ensure_ascii=False,
    )


def test_tool_output_index_preserves_operation_terminal_for_background_carry(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
        carried_tool_call_records,
    )

    operation = {
        "schema_version": "tool_operation.v1",
        "operation_id": "operation-create-1",
        "result_ref": "tool-operation://root-run/operation-create-1",
        "status": "succeeded",
        "action": "execute",
        "replayed": False,
        "idempotency_scope": "turn",
        "diagnostic": "must-not-persist",
        "private": {"token": "must-not-persist"},
    }
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="create_subagents",
            call_id="create-1",
            output="created\n" + ("x" * 2_000),
            ok=True,
            request_id="request-root",
            conversation_request_id="foreground-turn-1",
            run_id="root-run",
            task_id="root-run",
            min_chars=0,
            parameters={"items": [{"goal": "实现模块"}]},
            result_envelope={
                "tool_execution": {
                    "handler_executed": True,
                    "duration_ms": 31,
                },
                "tool_operation": operation,
            },
        )
    )
    artifact_path = Path(str(record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = json.loads(
        (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    carried = carried_tool_call_records(
        tmp_path,
        {"conversation_request_id": ("foreground-turn-1",)},
    )

    expected_operation = {
        key: value
        for key, value in operation.items()
        if key not in {"diagnostic", "private"}
    }
    assert record["tool_operation"] == expected_operation
    assert record["conversation_request_id"] == "foreground-turn-1"
    assert artifact["tool_operation"] == expected_operation
    assert artifact["conversation_request_id"] == "foreground-turn-1"
    assert index["tool_operation"] == expected_operation
    assert index["conversation_request_id"] == "foreground-turn-1"
    assert carried[0]["handler_executed"] is True
    assert carried[0]["operation_id"] == "operation-create-1"
    assert carried[0]["tool_operation_status"] == "succeeded"
    assert carried[0]["tool_operation_action"] == "execute"
    assert carried[0]["tool_operation_idempotency_scope"] == "turn"
    assert carried[0]["tool_operation_replayed"] is False
    assert carried[0]["conversation_request_id"] == "foreground-turn-1"
    assert "must-not-persist" not in json.dumps(
        [record["tool_operation"], artifact["tool_operation"], index["tool_operation"]],
        ensure_ascii=False,
    )


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


def test_tool_output_index_preserves_page_window_metadata(tmp_path: Path) -> None:
    page_window = {
        "kind": "offset_page",
        "tool": "search_text",
        "source_path": ".",
        "offset": 20,
        "limit": 20,
        "returned": 20,
        "next_offset": 40,
        "complete": False,
        "output_mode": "content",
    }
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="search_text",
            call_id="1-5",
            output="page body",
            ok=True,
            run_id="run-tool",
            task_id="task-tool",
            request_id="req-tool",
            min_chars=0,
            parameters={"query": "needle", "path": ".", "offset": 20, "limit": 20},
            result_envelope={"page_window": page_window},
        )
    )

    artifact_path = Path(str(record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["page_window"] == page_window
    assert artifact["page_window"] == page_window
    assert index[-1]["page_window"] == page_window


def test_tool_output_index_persists_value_free_input_sources(tmp_path: Path) -> None:
    expected = [
        {
            "path": "$.command",
            "source": "model_proposed",
            "source_ref": "tool_call:call-source#/input/command",
        },
        {
            "path": "$.working_dir",
            "source": "trusted_context",
            "source_ref": "write_boundary.task_root",
        },
        {
            "path": "$.timeout",
            "source": "safe_default",
            "source_ref": "tool_spec:run_command#/safe_parameter_defaults/timeout",
        },
    ]
    envelope_sources = [
        {**item, "value": "must-not-be-persisted", "private": "drop-me"} for item in expected
    ]

    short_record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path / "short",
            tool="run_command",
            call_id="call-source",
            output="ok",
            ok=True,
            run_id="run-source",
            task_id="task-source",
            request_id="req-source",
            min_chars=1000,
            parameters={"command": "pwd"},
            result_envelope={"input_sources": envelope_sources},
        )
    )
    short_index_path = tmp_path / "short" / "blobs" / "tool_outputs" / "index.jsonl"
    short_index = json.loads(short_index_path.read_text(encoding="utf-8").splitlines()[-1])

    long_record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path / "long",
            tool="run_command",
            call_id="call-source",
            output="externalized",
            ok=True,
            run_id="run-source",
            task_id="task-source",
            request_id="req-source",
            min_chars=0,
            parameters={"command": "pwd"},
            result_envelope={"input_sources": envelope_sources},
        )
    )
    artifact_path = Path(str(long_record["artifact_ref"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    long_index = json.loads(
        (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )

    assert short_record["input_sources"] == expected
    assert short_index["input_sources"] == expected
    assert long_record["input_sources"] == expected
    assert artifact["input_sources"] == expected
    assert long_index["input_sources"] == expected
    for payload in (short_record, short_index, long_record, artifact, long_index):
        serialized = json.dumps(payload["input_sources"], ensure_ascii=False)
        assert "must-not-be-persisted" not in serialized
        assert "drop-me" not in serialized


def test_tool_loop_keeps_moderate_tool_output_inline_for_model_context(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    output = "\n".join(f"章节 {idx:03d}: CP-{idx:03d}-{idx:03d}" for idx in range(1, 81))

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=1,
            idx=1,
            tool_name="run_command",
            arguments={"command": "extract chapters"},
            output=output,
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
        _canonical_record(
            params,
            tool_rounds=3,
            idx=2,
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            output="文件内容",
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
    large_output = "line\n" + ("x" * 250_000)

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=7,
            idx=1,
            tool_name="inspect_agent_tree",
            arguments={},
            output=large_output,
        )
    )
    record = params.archive_tool_calls[0]
    artifact = json.loads(Path(record["output_path"]).read_text(encoding="utf-8"))

    assert record["run_id"] == "runner-42"
    assert record["scoped_call_id"] == "runner-42:7-1"
    assert artifact["run_id"] == "runner-42"


def test_externalizer_preserves_internal_tool_outputs_without_path_sanitizer(
    tmp_path: Path,
) -> None:
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


def test_externalizer_redacts_preview_and_preserves_projection_metadata(tmp_path: Path) -> None:
    output = "remote payload api_key=opaque-secret-value"
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="web_fetch",
            call_id="1-external",
            output=output,
            ok=True,
            run_id="run-web",
            min_chars=10,
            result_envelope={
                "tool_output_policy": {
                    "trust": "external_data",
                    "redaction": "default",
                }
            },
        )
    )

    assert "opaque-secret-value" not in record["output_preview"]
    assert record["tool_output_trust"] == "external_data"
    assert record["tool_output_redaction"] == "default"
    artifact = json.loads(Path(record["artifact_ref"]).read_text(encoding="utf-8"))
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
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]

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
    output = "CPX-001-ABCDEF1234\n" + ("x" * 250_000)

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=2,
            idx=1,
            tool_name="read_file",
            arguments={"path": "fragment-001.txt"},
            output=output,
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
        _canonical_record(
            params,
            tool_rounds=1,
            idx=1,
            tool_name="write_file",
            arguments={"path": "shop/flow-a.html", "content": huge_html},
            output="路径不在 allowed_write_roots 内",
            ok=False,
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
        _canonical_record(
            params,
            tool_rounds=1,
            idx=1,
            tool_name="read_file",
            arguments={"path": "README.md"},
            output="ok",
            handler_details={"runtime_gate": {"status": "ALLOW", "allowed": True}},
        )
    )

    record = params.archive_tool_calls[0]
    assert record["runtime_gate"]["status"] == "ALLOW"
    assert record["runtime_gate"]["allowed"] is True



def test_render_tool_payload_keeps_small_payload_readable() -> None:
    payload = {"tool": "read_file", "path": "README.md"}
    rendered = render_tool_payload_for_live_prompt(payload)

    assert "tool_call_1: tool=read_file" in rendered
    assert "path: README.md" in rendered
    assert "{'tool'" not in rendered


def test_tool_loop_writes_fail_safe_checkpoint_before_externalizing_large_output(
    tmp_path: Path,
) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "danger\n" + ("x" * 250_000)

    service._record_tool_call(
        _canonical_record(
            params,
            tool_rounds=1,
            idx=1,
            tool_name="blackbox_tool",
            arguments={},
            output=large_output,
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
    assert snapshots[-1]["next_actions"] == [
        "先读取工具输出 artifact 摘要和 fail-safe checkpoint，再决定是否把内容切片读回 prompt。"
    ]


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
                tool_calls=[
                    {
                        "tool": "read_file",
                        "id": "1-1",
                        "ok": True,
                        "output_preview": "preview",
                        "output_hash": "hash",
                        "output_path": str(output_path),
                        "output_externalized": True,
                        "parameters": {"path": "large.log"},
                    }
                ],
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
    snapshot_run_id = run_id or request_id or "archive-test-run"
    write_file_spec = make_test_model_spec(
        "write_file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
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
        write_boundary=None,
        task_attributes=None,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id=snapshot_run_id,
            source_protocol="native",
        ),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(
            (write_file_spec,),
            run_id=snapshot_run_id,
        ),
    )
