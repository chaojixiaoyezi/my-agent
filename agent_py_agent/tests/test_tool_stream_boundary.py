"""LLM: tests for streaming tool-call boundary behavior.

给人看的解释：
这里测试模型流式输出工具调用时的公共边界。
真实模型如果把一整个网页或脚本塞进一次 write_file 参数，系统应该尽早打断并引导它分块写入。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.tool_stream_boundary import (
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
    malformed_tool_protocol_abort_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.content_transport_policy import (
    STREAMING_INLINE_WRITE_ABORT_CHARS,
)
from agent_py_agent.agent.tooling.file_write_session_models import (
    DEFAULT_MAX_SESSION_CHUNK_CHARS,
)


# LLM: streaming large write_file content must stop before provider timeout hides the partial tool call.
# 函数用途: 验证未闭合 write_file content 超过 inline 上限时，流式边界会抛出可恢复中断。
def test_tool_boundary_aborts_unclosed_large_write_content_stream():
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12)

    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"site/index.html","content":"')

    try:
        boundary("A" * 13)
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "write_file"
        assert exc.path == "site/index.html"
        assert exc.limit == 12
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected large write stream abort")


# LLM: real models sometimes wrap filesystem parameters under a nested object before normalization runs.
# 函数用途: 验证流式边界也能识别 {"tool":"write_file","filesystem":{"path":...,"content":...}} 这种真实输出。
def test_tool_boundary_aborts_nested_filesystem_large_write_content_stream():
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12)

    boundary(
        '[TOOL_CALL]\n'
        '{"tool":"write_file","filesystem":{"path":"shop/index.html","content":"'
    )

    try:
        boundary("A" * 13)
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "write_file"
        assert exc.path == "shop/index.html"
        assert exc.limit == 12
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected nested filesystem large write stream abort")


# LLM: unclosed write streams use a stricter transport threshold than fully parsed write_file calls.
# 函数用途: 验证 12K 完整写入仍可配置，但未闭合流式工具调用会在较小内部阈值早停，避免真实模型输出阶段拖死。
def test_tool_boundary_uses_streaming_abort_threshold_even_when_inline_limit_is_larger():
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12_000)

    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"scripts/collect.py","content":"')

    try:
        boundary("A" * (STREAMING_INLINE_WRITE_ABORT_CHARS + 1))
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "write_file"
        assert exc.path == "scripts/collect.py"
        assert exc.limit == STREAMING_INLINE_WRITE_ABORT_CHARS
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected streaming threshold abort")


# LLM: file_write_session is the large-body channel, so it must not inherit write_file's tiny stream cutoff.
# 函数用途: 验证 file_write_session append 不会按普通 write_file 早停阈值截断，避免大 HTML 被拆坏。
def test_tool_boundary_allows_file_write_session_until_session_chunk_limit():
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12_000)

    boundary(
        '[TOOL_CALL]\n'
        '{"tool":"file_write_session","action":"append","session_id":"s1","chunk_index":0,"content":"'
    )

    boundary("A" * (STREAMING_INLINE_WRITE_ABORT_CHARS + 1))


# LLM: file_write_session still needs a high ceiling so truly runaway streams remain recoverable.
# 函数用途: 验证 file_write_session 超过 session chunk 上限后才早停，并给出可恢复的 append 前缀。
def test_tool_boundary_aborts_file_write_session_after_session_chunk_limit():
    boundary = ToolBoundaryChunkFilter(None, max_inline_content_chars=12_000)

    boundary(
        '[TOOL_CALL]\n'
        '{"tool":"file_write_session","action":"append","session_id":"s1","chunk_index":0,"content":"'
    )

    try:
        boundary("A" * (DEFAULT_MAX_SESSION_CHUNK_CHARS + 1))
    except LongToolContentStreamAbort as exc:
        assert exc.tool == "file_write_session"
        assert exc.path == "session_id=s1"
        assert exc.limit == DEFAULT_MAX_SESSION_CHUNK_CHARS
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected file_write_session stream abort")


# LLM: streamed file_write_session content is machine payload and should be salvaged into a real append call.
# 函数用途: 验证未闭合 file_write_session append 的内容前缀不会丢失，而是转成可执行工具调用。
def test_long_file_write_session_abort_response_salvages_append_prefix():
    response = long_write_abort_response(
        LongToolContentStreamAbort(
            tool="file_write_session",
            path="session_id=s1",
            chars=DEFAULT_MAX_SESSION_CHUNK_CHARS + 1,
            limit=DEFAULT_MAX_SESSION_CHUNK_CHARS,
            action="append",
            session_id="s1",
            chunk_index=3,
            content_prefix="hello\nworldhello\nworld",
        ),
        backend="test-backend",
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert payload == {
        "tool": "file_write_session",
        "action": "append",
        "session_id": "s1",
        "chunk_index": 3,
        "content": "hello\nworldhello\nworld",
    }


# LLM: oversized write_file streams should become a recoverable file_write_session chunk.
# 函数用途: 验证模型输出超长 write_file 被中断时，已生成正文前缀不会丢失为 parse error。
def test_long_write_file_abort_response_salvages_prefix_to_file_write_session():
    response = long_write_abort_response(
        LongToolContentStreamAbort(
            tool="write_file",
            path="outputs/site/index.html",
            chars=5000,
            limit=4000,
            content_prefix="<!doctype html>\n<html>",
        ),
        backend="test-backend",
    )

    payload = json.loads(response.text.split("\n", 2)[1])
    assert payload == {
        "tool": "file_write_session",
        "action": "append",
        "session_id": "stream_write_ec6a9f3c2770",
        "target_path": "outputs/site/index.html",
        "chunk_index": 0,
        "content": "<!doctype html>\n<html>",
    }


# LLM: completed machine blocks after a first tool call must survive prose trimming.
# 函数用途: 验证同一轮模型输出多个结构化工具块时，系统保留机器块、丢掉普通自然语言。
def test_tool_boundary_preserves_later_raw_write_block_when_trimming_prose():
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


# LLM: repeated unclosed tool markers should become a structured protocol abort instead of burning the run timeout.
# 函数用途: 验证模型连续输出多个未闭合 TOOL_CALL/SUBAGENT_CALL 标记时，流式边界会提前中断并进入恢复路径。
def test_tool_boundary_aborts_repeated_unclosed_tool_markers():
    boundary = ToolBoundaryChunkFilter(None)

    try:
        boundary("[TOOL_CALL]\n" * 9)
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "[TOOL_CALL]"
        assert exc.marker_count == 9
        assert exc.limit == 1
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected malformed tool protocol stream abort")


# LLM: a second tool opener before closing the first one is an invalid machine protocol transition.
# 函数用途: 验证模型不能在一个 TOOL_CALL 未闭合时再开另一个 TOOL_CALL；这会被立即中断进入恢复路径。
def test_tool_boundary_aborts_second_unclosed_tool_start_marker():
    boundary = ToolBoundaryChunkFilter(None)

    try:
        boundary('[TOOL_CALL]\n{"tool":"file_write_session","content":"partial"\n[TOOL_CALL]\n')
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "[TOOL_CALL]"
        assert exc.marker_count == 2
        assert exc.limit == 1
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected second unclosed tool marker abort")


# LLM: a later end marker cannot retroactively close an earlier tool after a nested opener.
# 函数用途: 验证第一个工具调用未闭合前出现第二个 TOOL_CALL 时，即使后面有结束标记也不能误判为合法闭合。
def test_tool_boundary_aborts_nested_tool_start_before_first_end_marker():
    boundary = ToolBoundaryChunkFilter(None)

    try:
        boundary(
            '[TOOL_CALL]\n{"tool":"file_write_session","content":"partial"\n'
            '[TOOL_CALL]\n{"tool":"read_file","path":"x"}\n[/TOOL_CALL]'
        )
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "[TOOL_CALL]"
        assert exc.marker_count == 2
        assert exc.limit == 1
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected nested tool marker abort")


# LLM: malformed protocol detection must cover near-tool marker storms seen in real model output.
# 函数用途: 验证 [TOOL read_file / <TOOL ... 这类坏协议会提前中断，而不是拖到真实任务超时。
def test_tool_boundary_aborts_repeated_near_tool_marker_lines():
    boundary = ToolBoundaryChunkFilter(None)
    text = "\n".join(
        [
            "[TOOL",
            "```",
            "[TOOL read_file",
            "<TOOL read_file",
            "TOOL write_file",
            "[TOOL",
            "[TOOL read_file",
            "[TOOL",
            "TOOL read_file",
        ]
    )

    try:
        boundary(text)
    except MalformedToolProtocolStreamAbort as exc:
        assert exc.start_marker == "TOOL_PROTOCOL_LINE"
        assert exc.marker_count == 8
        assert exc.limit == 7
    else:  # pragma: no cover - keeps assertion message clear.
        raise AssertionError("expected malformed near-tool protocol stream abort")


# LLM: malformed protocol aborts must reuse parser recovery instead of becoming user-visible transcript text.
# 函数用途: 验证连续工具标记异常会被封装成 __parse_error__ 工具调用，后续工具循环可以按结构化错误恢复。
def test_malformed_tool_protocol_abort_response_uses_parse_error_tool():
    response = malformed_tool_protocol_abort_response(
        MalformedToolProtocolStreamAbort(
            start_marker="[TOOL_CALL]",
            marker_count=9,
            limit=8,
        ),
        backend="test-backend",
    )

    assert '"tool": "__parse_error__"' in response.text
    payload = json.loads(response.text.split("\n", 2)[1])
    raw = json.loads(payload["raw"])
    assert raw["marker_count"] == 9
    assert response.text.startswith("[TOOL_CALL]")
    assert response.text.rstrip().endswith("[/TOOL_CALL]")
