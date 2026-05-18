"""LLM: tests for streaming tool-call boundary behavior.

给人看的解释：
这里测试模型流式输出工具调用时的公共边界。
真实模型如果把一整个网页或脚本塞进一次 write_file 参数，系统应该尽早打断并引导它分块写入。
"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.tool_stream_boundary import (
    LongToolContentStreamAbort,
    ToolBoundaryChunkFilter,
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
