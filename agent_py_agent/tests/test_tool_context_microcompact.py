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


def test_builder_uses_the_named_constant_for_keep_recent() -> None:
    # 拼装层已改用具名常量 DEFAULT_MICROCOMPACT_KEEP_RECENT（2026-09-28 参数减量）：
    # 这里钉住"常量值生效"（5 条结果保留最近 8 条 → 全保留，不回收），
    # 以及"门槛仍是代码常量"这一事实（正文 3000 > 1500，若 keep_recent 更小就会回收）。
    from agent_py_agent.agent.agent_core.tool_context.microcompact import (
        DEFAULT_MICROCOMPACT_KEEP_RECENT,
    )
    from agent_py_agent.agent.prompting_parts.builder import _task_and_transcript_section

    class _Cfg:
        """配置里已没有 keep_recent；拼装层不再读它。"""

    assert DEFAULT_MICROCOMPACT_KEEP_RECENT >= 5, "常量值变了：本用例假设它 >=5 才会全部保留，请同步调整"
    ctx = [_entry(r) for r in range(5)]
    section = _task_and_transcript_section(_Cfg(), "任务", ctx)
    # 保留条数 ≥ 总数 → 一条都不回收
    assert "microcompacted" not in section
    # 拼装层是从定义模块里取的常量（函数内 import），所以改定义模块才会影响它：
    # 改小常量后必须开始回收 —— 证明拼装层读的确实是这个常量。
    import agent_py_agent.agent.agent_core.tool_context.microcompact as microcompact_mod
    original = microcompact_mod.DEFAULT_MICROCOMPACT_KEEP_RECENT
    try:
        microcompact_mod.DEFAULT_MICROCOMPACT_KEEP_RECENT = 2
        section2 = _task_and_transcript_section(_Cfg(), "任务", ctx)
        assert section2.count("microcompacted") == 3, "改成 2 后应回收 3 条"
    finally:
        microcompact_mod.DEFAULT_MICROCOMPACT_KEEP_RECENT = original
