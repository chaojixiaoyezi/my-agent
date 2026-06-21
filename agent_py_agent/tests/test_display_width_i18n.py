"""审计 #22(low/i18n)真测:显示宽度感知的表格对齐 + 字形簇边界截断。

终端里 CJK/全角占 2 列、组合/零宽占 0 列,但 len()/str:<N 按码点算 → 中日韩内容表格列错位;
text[:N] 按码点切会把 emoji ZWJ 家庭/国旗/组合变音截碎。真造 CJK 表格断言列对齐到同一显示列、
真造家庭 emoji 断言边界截断绝不切碎。emoji ZWJ 序列用显式 \\u 转义构造。学 终端交互 stringWidth+grapheme。
"""

from __future__ import annotations

from agent_py_agent.agent.common.display_width import (
    display_width,
    fit_column,
    pad_display,
    truncate_display,
)

# 男+ZWJ+女+ZWJ+女孩 = 一个家庭 emoji(显示为单字形,宽 2)
_FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
_FLAG_CN = "\U0001f1e8\U0001f1f3"  # 区域指示符对 = 中国国旗 emoji


# ---------- display_width 核心 ----------

def test_display_width_cjk_and_combining() -> None:
    assert display_width("abc") == 3  # ASCII 各 1 列
    assert display_width("中文") == 4  # CJK 各 2 列
    assert display_width("ｆｕｌｌ") == 8  # 全角拉丁各 2 列
    assert display_width("é") == 1  # e + 组合重音 = 1 列(组合标记 0 宽)
    assert display_width("") == 0


def test_display_width_zwj_sequence_counted_once() -> None:
    assert display_width(_FAMILY) == 2  # 整个家庭 emoji 算一个宽字形,不是 3 个相加
    assert display_width(_FLAG_CN) == 2  # 国旗(区域指示符对)算一个宽字形


def test_truncate_never_splits_family_emoji() -> None:
    base = "a" + _FAMILY
    assert truncate_display(base, 0) == ""
    assert truncate_display(base, 1) == "a"  # emoji 需 2 列,放不下 → 不切碎
    assert truncate_display(base, 2) == "a"  # 同上(1+2=3>2)
    assert truncate_display(base, 3) == base  # 装得下 → 完整保留


def test_truncate_by_display_columns() -> None:
    assert truncate_display("中文字符", 3) == "中"  # 第二个字会到 4 列 > 3,停在 1 字
    assert truncate_display("中文字符", 4) == "中文"  # 正好 4 列
    assert truncate_display("abcdef", 3) == "abc"


def test_pad_and_fit_column_by_display_width() -> None:
    assert pad_display("中", 4) == "中  "  # 2 列 + 2 空格 = 4 显示列
    assert pad_display("abc", 5) == "abc  "
    assert display_width(fit_column("中文长内容", 6)) == 6  # 截到 6 列并补满
    assert display_width(fit_column("hi", 6)) == 6  # 短内容补满到 6 列


# ---------- 表格对齐 / 看板预览 集成 ----------

def test_archive_table_columns_align_with_cjk() -> None:
    from agent_py_agent.agent.memory_archive.query.rendering_adapter import format_archive_records_table

    records = [
        {"created_at": "2026-06-21T10:00:00", "kind": "对话", "speaker": "用户",
         "action": "写入", "status": "完成", "content_preview": "中文预览内容"},
        {"created_at": "2026-06-21T10:00:01", "kind": "note", "speaker": "bot",
         "action": "read", "status": "ok", "content_preview": "ascii preview"},
    ]
    out = format_archive_records_table(records)
    data_lines = [line for line in out.splitlines() if line.startswith("2026")]
    assert len(data_lines) == 2
    for line, preview in zip(data_lines, ["中文预览内容", "ascii preview"]):
        idx = line.rfind(preview)
        # preview 列前的固定区段:20+1+14+1+12+1+10+1+8+1 = 69 显示列,两行(CJK/ASCII)都对齐到此
        assert display_width(line[:idx]) == 69


def test_board_goal_preview_display_budget_and_grapheme_safe() -> None:
    from agent_py_agent.cli._board import _BOARD_GOAL_PREVIEW_CHARS, _goal_preview

    long_cjk = "中" * 200  # 400 显示列,远超预算
    out = _goal_preview(long_cjk)
    assert out.endswith("...")
    assert display_width(out[:-3]) <= _BOARD_GOAL_PREVIEW_CHARS  # 按显示列宽预算截断
    # 家庭 emoji 卡在预算边界:绝不出现半个家庭
    text = "x" * (_BOARD_GOAL_PREVIEW_CHARS - 1) + _FAMILY
    out2 = _goal_preview(text)
    assert "\u200d" not in out2.rstrip(".")  # 截断结果不以悬空 ZWJ/半个 emoji 结尾
