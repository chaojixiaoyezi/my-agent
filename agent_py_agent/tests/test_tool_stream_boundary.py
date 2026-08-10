"""流式工具调用边界测试。"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.tool_stream import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    long_write_abort_response,
    malformed_tool_protocol_abort_response,
)
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


def test_long_write_abort_response_returns_host_violation_not_fake_tool() -> None:
    response = long_write_abort_response(
        LongToolContentStreamAbort(
            LongToolContentAbortPayload(
                tool="write_file",
                path="outputs/site/index.html",
                chars=5000,
                limit=4000,
            )
        ),
        backend="test-backend",
    )

    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert violation["code"] == "TOOL_INLINE_CONTENT_STREAM_ABORTED"
    assert evidence["source_tool"] == "write_file"
    assert evidence["streaming_content_chars"] == 5000
    assert evidence["streaming_content_limit"] == 4000
    assert evidence["previous_write_committed"] is False
    assert "outputs/site/index.html" not in str(response.tool_protocol_violations)
    assert len(evidence["path_sha256"]) == 64


def test_tool_boundary_stops_stream_inspection_after_complete_tool_call() -> None:
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append)

    boundary('[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]')
    boundary("\nTOOL_PROTOCOL_LINE\n" * 20)

    # J-4：块体原文不进 UI——第一个完整块 open 前无 prose，UI 看到空；
    # 完整块之后的任何内容（伪协议行）同样不再进入。
    assert "".join(forwarded) == ""


def test_tool_boundary_accepts_inline_closing_marker_after_complete_json() -> None:
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append)

    text = '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}[/TOOL_CALL]'
    boundary(text)
    boundary("\n普通解释不应继续进入机器块")

    assert boundary.complete_tool_text() == text
    # J-4：块体不进 UI（open 前无 prose）
    assert "".join(forwarded) == ""


def test_tool_boundary_forwards_only_prose_before_first_complete_block() -> None:
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append)

    boundary("先看一下项目结构。\n")
    boundary('[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]')
    boundary("\n块后的解释不进 UI")

    assert "".join(forwarded) == "先看一下项目结构。\n"
    assert boundary.complete_tool_text() == (
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )


def test_tool_boundary_holds_back_incomplete_marker_prefix_across_chunks() -> None:
    # chunk 切分停在 marker 中间时，半截 marker 不得提前进 UI：可见文本只由
    # 完整文本决定，与切分方式无关（J-4）。
    text = (
        "先看一下。\n"
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]\n收尾。'
    )
    one_shot: list[str] = []
    split: list[str] = []

    a = ToolBoundaryChunkFilter(one_shot.append)
    a(text)
    a.finish()

    b = ToolBoundaryChunkFilter(split.append)
    for chunk in (
        "先看一下。\n[TOOL_",
        "CALL]\n{",
        '"tool":"read_file","path":"README.md"}',
        "\n[/TOOL_CALL]\n收尾。",
    ):
        b(chunk)
    b.finish()

    assert "".join(one_shot) == "先看一下。\n"
    assert "".join(split) == "".join(one_shot)
    assert "TOOL_CALL" not in "".join(split)
    assert "[/TOOL" not in "".join(split)


def test_tool_boundary_bypass_forwards_all_text_for_native() -> None:
    # native 直通：正文提及 marker 全量转发、绝不误杀（裁决归 _native_prose_violation）。
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append, bypass=True)

    boundary("正文提到 [TOOL_CALL] 不应误杀\n")
    boundary("继续输出。")
    boundary.finish()

    assert "".join(forwarded) == "正文提到 [TOOL_CALL] 不应误杀\n继续输出。"
    assert boundary.complete_tool_text() == ""


def test_tool_boundary_ignores_inline_closing_marker_inside_json_string() -> None:
    payload = {
        "tool": "write_file",
        "path": "report.md",
        "content": "正文里提到 [/TOOL_CALL] 只是文本",
    }
    text = "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"

    boundary = ToolBoundaryChunkFilter(None)
    boundary(text)

    assert boundary.complete_tool_text() == text


def test_tool_boundary_ignores_near_tool_protocol_lines_without_exact_marker() -> None:
    boundary = ToolBoundaryChunkFilter(None)

    boundary("\nTOOL_PROTOCOL_LINE\n" * 20)

    assert boundary.complete_tool_text() == ""


def test_tool_boundary_detects_first_complete_block_without_rewriting_response() -> None:
    boundary = ToolBoundaryChunkFilter(None)
    text = (
        '[TOOL_CALL]\n{"tool":"read_file","path":"a.md"}\n[/TOOL_CALL]\n'
        '[TOOL_CALL]\n{"tool":"read_file","path":"b.md"}\n[/TOOL_CALL]\n'
        "这段普通解释不应进入工具执行"
    )
    boundary(text)

    assert boundary._text == text
    assert boundary.complete_tool_text() == (
        '[TOOL_CALL]\n{"tool":"read_file","path":"a.md"}\n[/TOOL_CALL]'
    )


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


def test_malformed_tool_protocol_abort_response_uses_host_violation() -> None:
    response = malformed_tool_protocol_abort_response(
        MalformedToolProtocolStreamAbort(
            start_marker="[TOOL_CALL]",
            marker_count=9,
            limit=8,
        ),
        backend="test-backend",
    )

    assert response.text == ""
    violation = response.tool_protocol_violations[0]
    evidence = json.loads(violation["evidence_preview"])
    assert violation["code"] == "TOOL_CALL_UNCLOSED"
    assert evidence == {"limit": 8, "marker_count": 9, "start_marker": "[TOOL_CALL]"}
