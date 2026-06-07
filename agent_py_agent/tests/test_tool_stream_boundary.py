"""流式工具调用边界测试。"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.tool_stream import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    complete_machine_block_text,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
    malformed_tool_protocol_abort_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    STREAMING_INLINE_WRITE_ABORT_CHARS,
)


def test_tool_boundary_aborts_unclosed_large_write_content_stream() -> None:
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12)
    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"site/index.html","content":"')

    try:
        boundary("A" * 13)
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "write_file"
        assert exc.path == "site/index.html"
        assert exc.limit == 12
    else:  # pragma: no cover
        raise AssertionError("expected large write stream abort")


def test_tool_boundary_allows_default_write_file_past_inline_recommendation() -> None:
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=MAX_INLINE_WRITE_CONTENT_CHARS)

    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"site/index.html","content":"')
    boundary("A" * (MAX_INLINE_WRITE_CONTENT_CHARS + 1))


def test_tool_boundary_aborts_write_file_at_expanded_streaming_threshold() -> None:
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12_000)
    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"scripts/collect.py","content":"')

    try:
        boundary("A" * (STREAMING_INLINE_WRITE_ABORT_CHARS + 1))
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "write_file"
        assert exc.path == "scripts/collect.py"
        assert exc.limit == STREAMING_INLINE_WRITE_ABORT_CHARS
    else:  # pragma: no cover
        raise AssertionError("expected streaming threshold abort")


def test_long_write_abort_response_returns_parse_error_not_hidden_writer() -> None:
    response = long_write_abort_response(
        LongToolContentStreamAbort(
            LongToolContentAbortPayload(
                tool="write_file",
                path="outputs/site/index.html",
                chars=5000,
                limit=4000,
                content_prefix="<!doctype html>\n<html>",
            )
        ),
        backend="test-backend",
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert payload["tool"] == "__parse_error__"
    assert payload["source_tool"] == "write_file"
    assert payload["path"] == "outputs/site/index.html"
    assert payload["content_field_present"] is True
    assert payload["streaming_content_chars"] == 5000
    assert payload["streaming_content_limit"] == 4000
    assert "raw" not in payload


def test_tool_boundary_preserves_later_raw_write_block_when_trimming_prose() -> None:
    response = ModelResponse(
        text=(
            "先建目录\n"
            "[TOOL_CALL]\n"
            '{"tool":"run_command","command":"mkdir -p out"}\n'
            "[/TOOL_CALL]\n"
            "然后写完整文件\n"
            '[WRITE_FILE_RAW path="out/index.html"]\n'
            "<!doctype html>\n"
            "<html></html>\n"
            "[/WRITE_FILE_RAW]\n"
            "这些普通解释不应进入控制流"
        ),
        backend="test-backend",
    )

    cut, changed = cut_response_after_first_complete_tool_call(response)

    assert changed is True
    assert "先建目录" not in cut.text
    assert "然后写完整文件" not in cut.text
    assert "[TOOL_CALL]" in cut.text
    assert "[WRITE_FILE_RAW" in cut.text


def test_tool_boundary_stops_stream_inspection_after_complete_tool_call() -> None:
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append)

    boundary('[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]')
    boundary("\nTOOL_PROTOCOL_LINE\n" * 20)

    assert "".join(forwarded) == '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'


def test_tool_boundary_collects_complete_machine_blocks_without_abort() -> None:
    boundary = ToolBoundaryChunkFilter(None)
    boundary(
        '[TOOL_CALL]\n{"tool":"read_file","path":"a.md"}\n[/TOOL_CALL]\n'
        '[TOOL_CALL]\n{"tool":"read_file","path":"b.md"}\n[/TOOL_CALL]\n'
        "这段普通解释不应进入工具执行"
    )

    machine_text = complete_machine_block_text(boundary._text)

    assert '"path":"a.md"' in machine_text
    assert '"path":"b.md"' in machine_text
    assert "普通解释" not in machine_text


def test_tool_boundary_aborts_repeated_unclosed_tool_markers() -> None:
    boundary = ToolBoundaryChunkFilter(None)

    try:
        boundary("[TOOL_CALL]\n" * 9)
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "[TOOL_CALL]"
        assert exc.marker_count == 9
        assert exc.limit == 1
    else:  # pragma: no cover
        raise AssertionError("expected malformed tool protocol stream abort")


def test_tool_boundary_aborts_second_unclosed_tool_start_marker() -> None:
    boundary = ToolBoundaryChunkFilter(None)

    try:
        boundary('[TOOL_CALL]\n{"tool":"write_file","content":"partial"\n[TOOL_CALL]\n')
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "[TOOL_CALL]"
        assert exc.marker_count == 2
        assert exc.limit == 1
    else:  # pragma: no cover
        raise AssertionError("expected second unclosed tool marker abort")


def test_malformed_tool_protocol_abort_response_uses_parse_error_tool() -> None:
    response = malformed_tool_protocol_abort_response(
        MalformedToolProtocolStreamAbort(
            start_marker="[TOOL_CALL]",
            marker_count=9,
            limit=8,
        ),
        backend="test-backend",
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert payload["tool"] == "__parse_error__"
    assert "TOOL_CALL" in payload["error"]
