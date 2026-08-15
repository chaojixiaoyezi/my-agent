"""tool_context microcompact 回溯回收的钉子测试。"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.tool_context.microcompact import microcompact_tool_context


def _entry(round_no: int, *, big: bool = True, anchor: bool = True) -> str:
    body = "X" * 3000 if big else "small"
    lines = [
        f"[tool-record round={round_no} index=1]",
        '{"tool":"read_file","path":"a.py"}',
        f"[tool-output-record round={round_no} index=1]",
        body,
    ]
    if anchor:
        lines += ["- output_scoped_call_id: run:1-1", '- read_artifact_hint: {"tool":"read_artifact"}']
    return "\n".join(lines)


def test_reclaims_only_out_of_window_entries() -> None:
    ctx = [_entry(r) for r in range(10)]
    out = microcompact_tool_context(ctx, keep_recent=8)
    reclaimed = [i for i, s in enumerate(out) if "microcompacted" in s]
    assert reclaimed == [0, 1]
    # 回收条保留调用信息 + 锚点，去掉正文
    assert "read_file" in out[0] and "read_artifact_hint" in out[0] and "XXXX" not in out[0]
    # 近窗口完整保留正文
    assert "XXXX" in out[9]


def test_below_window_unchanged() -> None:
    ctx = [_entry(r) for r in range(5)]
    assert microcompact_tool_context(ctx, keep_recent=8) == ctx


def test_unanchored_results_not_reclaimed() -> None:
    ctx = [_entry(r, anchor=False) for r in range(10)]
    out = microcompact_tool_context(ctx, keep_recent=8)
    assert not any("microcompacted" in s for s in out)


def test_small_results_not_reclaimed() -> None:
    ctx = [_entry(r, big=False) for r in range(10)]
    out = microcompact_tool_context(ctx, keep_recent=8)
    assert not any("microcompacted" in s for s in out)


def test_system_notes_untouched() -> None:
    ctx = []
    for r in range(10):
        ctx.append(_entry(r))
        ctx.append("[tool-system]\n系统提示")
    out = microcompact_tool_context(ctx, keep_recent=3)
    assert all("microcompacted" not in s for s in out if "tool-system" in s)


def test_disabled_with_zero_or_negative_keep() -> None:
    # 配置语义：0 表示关闭回收（与 yaml 注释一致），负数同样关闭
    ctx = [_entry(r) for r in range(10)]
    assert microcompact_tool_context(ctx, keep_recent=0) == ctx
    assert microcompact_tool_context(ctx, keep_recent=-1) == ctx


def test_builder_wires_config_values() -> None:
    # prompt 拼装层从 AgentConfig 读 microcompact 配置；keep_recent=0 时透传不回收
    from agent_py_agent.agent.prompting_parts.builder import _task_and_transcript_section

    class _Cfg:
        tool_context_microcompact_keep_recent = 2
        tool_context_microcompact_min_chars = 100

    ctx = [_entry(r) for r in range(5)]
    section = _task_and_transcript_section(_Cfg(), "任务", ctx)
    assert section.count("microcompacted") == 3  # 5 条结果保留最近 2 条完整

    class _CfgOff:
        tool_context_microcompact_keep_recent = 0
        tool_context_microcompact_min_chars = 100

    section_off = _task_and_transcript_section(_CfgOff(), "任务", ctx)
    assert "microcompacted" not in section_off
