"""统一增量文本工具解析器测试（R0 #89）。

核心不变量：
1. 同一段文本，任意 chunk 切分与 one-shot 的解析结果完全一致
   （abort 判定 / 可见文本 / 第一个完整块）。
2. 流式与全文对"未闭合 open 数 > 1 → 整轮拒绝"对齐（好块也不执行）。
3. UI（filter 转发）永不出现块体原文与半截 marker（J-4）。
4. J-6 常量接线：未闭合块体上限 / 响应总量 / 块数上限。
5. G5（2026-08-10 用户裁决）：任何协议错误 → 整轮零执行——未闭合、截断/
   JSON 损坏、fence 包裹、超限，任一出现即「不完整响应」，本响应所有 text
   calls 都没有执行权（J.5 terminal 语义扩展到所有协议错误；不再有安全
   前缀，坏块之前的完整好块也不执行）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_stream import (
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
)
from agent_py_agent.agent.backends.text_protocol_parser import (
    MAX_BLOCK_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_TEXT_CALLS,
    MAX_UNCLOSED_OPEN_MARKERS,
    scan_text_blocks,
)
from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    TextToolProtocolAdapter,
)
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    ToolAvailability,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)


def _block(payload: dict) -> str:
    return "[TOOL_CALL]" + json.dumps(payload, ensure_ascii=False) + "[/TOOL_CALL]"


READ_A = {"tool": "read_file", "path": "a.txt"}
READ_B = {"tool": "read_file", "path": "b.txt"}
WRITE_MARKER_TEXT = {
    "tool": "write_file",
    "path": "r.md",
    "content": "正文 [/TOOL_CALL] 只是文本",
}

_INVARIANT_CASES = [
    "纯 prose 回复。",
    "先看一下。\n" + _block(READ_A) + "\n收尾。",
    "第一步：\n" + _block(READ_A) + "\n第二步：\n" + _block(READ_B) + "\n以上完成。",
    "prose 前缀\n" + _block(READ_A) + "\n中间 prose\n" + _block(READ_B) + "\n尾部 prose",
    _block(READ_A) + " 未闭合块继续写..." + _block(READ_B),
    _block(READ_A) + "[TOOL_CALL]" + json.dumps(READ_B, ensure_ascii=False),
    _block(WRITE_MARKER_TEXT),
    "```\n" + _block(READ_A) + "\n```",
    _block(READ_A) + " 好块后坏 JSON 块：[TOOL_CALL]{bad[/TOOL_CALL]\n" + _block(READ_B),
    "两个未闭合（好块也不执行）：" + _block(READ_A) + "[TOOL_CALL]" + json.dumps(READ_B, ensure_ascii=False) + "[TOOL_CALL]" + json.dumps(READ_A, ensure_ascii=False),
    _block(READ_A) + "\nTOOL_PROTOCOL_LINE\n" * 5,
]


def _runtime_snapshot() -> ToolRuntimeSnapshot:
    model_spec = ToolModelSpec(
        name="run_command",
        description="运行一个命令",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    return ToolRuntimeSnapshot(
        run_id="run-1",
        runtimes=(
            ToolRuntime(
                model_spec=model_spec,
                runtime_policy=ToolRuntimePolicy(EffectResolverPolicy("mutating")),
                handler=object(),
                availability=ToolAvailability.ready(),
            ),
        ),
        available_tool_names=frozenset({"run_command"}),
        unavailable_tools=(),
        allowed_tools=None,
    )


def _request(response: object) -> ProviderToolCallRequest:
    return ProviderToolCallRequest(
        response=response,
        protocol=ToolProtocolSnapshot(
            run_id="run-1",
            source_protocol="text",
            capability=ProviderToolCapability(
                provider="test",
                endpoint="local://test",
                model="fake",
                stream=False,
                native_supported=False,
                evidence="test_contract",
            ),
        ),
        runtime_snapshot=_runtime_snapshot(),
        turn_id="turn-1",
        attempt_id="attempt-1",
    )


def _run_filter(chunks: list[str]):
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(forwarded.append)
    aborted = False
    try:
        for chunk in chunks:
            boundary(chunk)
        boundary.finish()
    except (MalformedToolProtocolStreamAbort, LongToolContentStreamAbort):
        aborted = True
    return aborted, "".join(forwarded), boundary.complete_tool_text()


def test_scan_text_blocks_good_and_bad_blocks_coexist() -> None:
    text = (
        _block(READ_A)
        + "\n"
        + "```\n"
        + _block(READ_B)
        + "\n```\n"
        + _block(WRITE_MARKER_TEXT)
    )
    scan = scan_text_blocks(text)

    assert [b.payload["path"] for b in scan.blocks if b.payload is not None] == [
        "a.txt", "r.md",
    ]
    assert scan.unclosed_count == 0
    assert any(
        "Markdown fences" in (b.error or "") for b in scan.blocks
    ), [b.error for b in scan.blocks]


def test_scan_text_blocks_counts_unclosed_opens() -> None:
    text = (
        '[TOOL_CALL]{"tool":"read_file","path":"a.txt"}'
        '[TOOL_CALL]{"tool":"read_file","path":"b.txt"}'
    )
    scan = scan_text_blocks(text)

    assert scan.unclosed_count == 2
    assert scan.payloads == []
    assert [b.error for b in scan.blocks] == ["text tool block is not closed"] * 2


def test_scan_text_blocks_accepts_close_marker_inside_closed_json_string() -> None:
    # 字符串值里的 [/TOOL_CALL] 是合法内容（写文件内容高频），块仍完整闭合。
    text = _block(WRITE_MARKER_TEXT)
    scan = scan_text_blocks(text)

    assert len(scan.payloads) == 1
    assert scan.payloads[0]["content"] == "正文 [/TOOL_CALL] 只是文本"
    assert scan.unclosed_count == 0


def test_scan_text_blocks_plain_prose_is_not_violation() -> None:
    scan = scan_text_blocks("目录里暂时没有文件。")

    assert scan.payloads == []
    assert scan.errors == []
    assert scan.unclosed_count == 0


def test_scan_text_blocks_empty_body_is_unclosed() -> None:
    # 空 body 的 [/TOOL_CALL] 前不是完整 JSON → 不算闭合 → 坏块不执行。
    scan = scan_text_blocks("[TOOL_CALL][/TOOL_CALL]")

    assert scan.payloads == []
    assert scan.errors == ["text tool block is not closed"]


def test_adapter_more_than_one_unclosed_rejects_good_blocks_too() -> None:
    # 统一规则（J-6）：未闭合 open 数 > 1 → 整轮拒绝，同一响应里的好块也不执行
    # ——与流式 MalformedToolProtocolStreamAbort 对齐（块序混乱即协议损坏）。
    text = (
        '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]'
        '[TOOL_CALL]{"tool":"run_command","command":"ls"}'
        '[TOOL_CALL]{"tool":"run_command","command":"pwd"}'
    )
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    assert sum("not closed" in v.detail for v in result.violations) == 2


def test_adapter_any_unclosed_block_rejects_whole_response() -> None:
    # G5（用户裁决）：任何协议错误 → 整轮零执行——完整好块 + 末尾未闭合块，
    # 前面的完整好块也不执行（不完整响应不能获得执行权）。
    text = (
        '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]'
        '[TOOL_CALL]{"tool":"run_command","command":"ls"}'
    )
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    assert any("not closed" in v.detail for v in result.violations)


def test_adapter_any_broken_block_rejects_whole_response() -> None:
    # G5（用户裁决）：任何协议错误 → 整轮零执行——fence 包裹的坏块存在时，
    # 它之前的完整好块和之后的完好块都不执行（坏块位置之后的文本可能被坏块
    # 吞掉/溢出，块序损坏即协议不可信；坏块之前的完整好块同样不执行，
    # 因为响应整体「不完整」）。
    text = (
        '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]\n'
        "```\n"
        '[TOOL_CALL]{"tool":"run_command","command":"ls"}[/TOOL_CALL]\n'
        "```\n"
        '[TOOL_CALL]{"tool":"run_command","command":"pwd"}[/TOOL_CALL]'
    )
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    details = [v.detail for v in result.violations]
    assert any("Markdown fences" in detail for detail in details)


def test_adapter_enforces_text_call_limit() -> None:
    # J-6 + G5：块数上限——超限即「不完整响应」，整轮拒绝，不执行任何块。
    text = "".join(
        '[TOOL_CALL]{"tool":"run_command","command":"echo %d"}[/TOOL_CALL]' % i
        for i in range(MAX_TEXT_CALLS + 1)
    )
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    assert any(
        f"exceeds {MAX_TEXT_CALLS} tool calls" in v.detail
        and "whole response was not executed" in v.detail
        for v in result.violations
    )


def test_adapter_enforces_unclosed_block_chars() -> None:
    # J-6：未闭合块体长度上限（finalize 级，仅未闭合块）。
    text = '[TOOL_CALL]\n{"tool":"write_file","content":"' + "A" * (MAX_BLOCK_CHARS + 1)
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    assert any(
        f"exceeds {MAX_BLOCK_CHARS} chars" in v.detail for v in result.violations
    )


def test_adapter_enforces_response_chars() -> None:
    # J-6：响应总量 terminal 上限——超限直接整轮拒绝。
    text = "x" * (MAX_RESPONSE_CHARS + 1)
    result = TextToolProtocolAdapter().tool_calls(_request(SimpleNamespace(text=text)))

    assert result.calls == ()
    assert [v.detail for v in result.violations] == [
        f"text response exceeds {MAX_RESPONSE_CHARS} chars"
    ]


def test_filter_visible_and_cut_are_chunk_split_invariant() -> None:
    # 核心等价性：任意 chunk 切分与 one-shot 的 aborted / 可见文本 /
    # 第一个完整块完全相同。
    for text in _INVARIANT_CASES:
        expected = _run_filter([text])
        assert _run_filter(list(text)) == expected, text
        assert _run_filter([text[i : i + 3] for i in range(0, len(text), 3)]) == expected, text
        assert _run_filter([text[i : i + 7] for i in range(0, len(text), 7)]) == expected, text


def test_filter_visible_text_never_contains_protocol_markers() -> None:
    # J-4：UI 转发永不出现块体原文与半截 marker。
    for text in _INVARIANT_CASES:
        _aborted, visible, _cut = _run_filter([text])
        assert "[TOOL_CALL]" not in visible, text
        assert "[/TOOL_CALL]" not in visible, text
        assert "TOOL_CALL" not in visible, text


def test_filter_cut_matches_scan_first_complete_block() -> None:
    # filter 的完整块（执行契约）与统一 parser 的第一个好块严格一致。
    text = "先看一下。\n" + _block(READ_A) + "\n" + _block(READ_B)
    boundary = ToolBoundaryChunkFilter(None)
    boundary(text)
    boundary.finish()

    scan = scan_text_blocks(text)
    first = scan.blocks[0]
    expected = text[first.open_index : first.close_index + len("[/TOOL_CALL]")].strip()
    assert boundary.complete_tool_text() == expected


def test_bypass_forwards_all_and_never_aborts() -> None:
    # native 直通：正文全量转发，长写内容/伪 marker 都不触发 text 协议中止
    # （native 裁决归 _native_prose_violation）。
    forwarded: list[str] = []
    boundary = ToolBoundaryChunkFilter(
        forwarded.append, max_inline_content_chars=12, bypass=True
    )
    boundary('[TOOL_CALL]\n{"tool":"write_file","path":"x.txt","content":"')
    boundary("A" * 1000)
    boundary.finish()

    assert "".join(forwarded) == boundary._text
    assert boundary._text.endswith("A")
    assert boundary.complete_tool_text() == ""
