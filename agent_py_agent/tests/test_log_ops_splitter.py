
from __future__ import annotations

"""可配置切割器单测 —— line/json/multiline/delimiter 切割正确性 + consumed 字节 + 增量续接不丢。

重点:consumed 字节决定采集 offset 推进多少,错了就丢/重。每种切割器都验"未闭合记录留到下次、
静默 flush 补采、跨边界续接不重不丢"——复用 folder bug 教训(脏数据 / 截断边界)。
"""

from agent_py_agent.agent.tooling.log_ops.splitter import (
    DelimiterSplitter,
    LineSplitter,
    MultilineStartSplitter,
    build_splitter,
)


def _u8(text: str) -> int:
    return len(text.encode("utf-8"))


# ----------------------- LineSplitter -----------------------


def test_line_basic() -> None:
    recs, consumed = LineSplitter().split("a\nb\nc\n", flush_trailing=False)
    assert recs == ["a", "b", "c"]
    assert consumed == _u8("a\nb\nc\n")


def test_line_partial_protected_when_active() -> None:
    recs, consumed = LineSplitter().split("a\nb\npartial", flush_trailing=False)
    assert recs == ["a", "b"]
    assert consumed == _u8("a\nb\n")  # partial 残行留到下次


def test_line_partial_flushed_when_idle() -> None:
    recs, consumed = LineSplitter().split("a\nb\nlast-no-nl", flush_trailing=True)
    assert recs == ["a", "b", "last-no-nl"]
    assert consumed == _u8("a\nb\nlast-no-nl")


def test_line_empty() -> None:
    assert LineSplitter().split("", flush_trailing=True) == ([], 0)


# ----------------------- MultilineStartSplitter -----------------------


def test_multiline_merges_stack() -> None:
    s = MultilineStartSplitter(r"^\d{4}-")
    text = "2026-01-01 ERROR boom\n  at foo\n  at bar\n2026-01-02 OK\n"
    recs, consumed = s.split(text, flush_trailing=False)
    # 第一条三行合并并闭合;第二条(2026-01-02)后面没有新 start,未闭合,留到下次。
    assert recs == ["2026-01-01 ERROR boom\n  at foo\n  at bar"]
    assert consumed == _u8("2026-01-01 ERROR boom\n  at foo\n  at bar\n")


def test_multiline_flush_last_record() -> None:
    s = MultilineStartSplitter(r"^\d{4}-")
    text = "2026-01-01 ERROR boom\n  at foo\n2026-01-02 OK done"
    recs, consumed = s.split(text, flush_trailing=True)
    assert recs == ["2026-01-01 ERROR boom\n  at foo", "2026-01-02 OK done"]
    assert consumed == _u8(text)


def test_multiline_incremental_resume_no_dup_no_loss() -> None:
    """两拍续采:第一拍闭合前两条 + offset 推进,第二拍从 offset 续读补最后一条,不重不丢。"""
    s = MultilineStartSplitter(r"^\d{4}-")
    full = "2026-01-01 A\n  s1\n2026-01-02 B\n  s2\n2026-01-03 C\n"
    recs1, c1 = s.split(full, flush_trailing=False)
    assert recs1 == ["2026-01-01 A\n  s1", "2026-01-02 B\n  s2"]  # C 未闭合
    rest = full.encode("utf-8")[c1:].decode("utf-8")  # 模拟从 offset 续读
    recs2, _ = s.split(rest, flush_trailing=True)
    assert recs2 == ["2026-01-03 C"]
    assert recs1 + recs2 == ["2026-01-01 A\n  s1", "2026-01-02 B\n  s2", "2026-01-03 C"]


def test_multiline_empty() -> None:
    assert MultilineStartSplitter(r"^X").split("", flush_trailing=True) == ([], 0)


# ----------------------- DelimiterSplitter -----------------------


def test_delimiter_basic() -> None:
    s = DelimiterSplitter("\n\n")
    recs, _ = s.split("rec1 a\nrec1 b\n\nrec2\n\n", flush_trailing=False)
    assert recs == ["rec1 a\nrec1 b", "rec2"]


def test_delimiter_unclosed_held_then_flushed() -> None:
    s = DelimiterSplitter("\n\n")
    text = "rec1\n\nrec2-still-writing"
    recs, consumed = s.split(text, flush_trailing=False)
    assert recs == ["rec1"]  # rec2 未遇到下一个分隔符,未闭合,留下
    assert consumed == _u8("rec1\n\n")
    recs2, _ = s.split(text, flush_trailing=True)
    assert recs2 == ["rec1", "rec2-still-writing"]


def test_delimiter_single_segment_no_delim() -> None:
    s = DelimiterSplitter("\n\n")
    assert s.split("no delim yet", flush_trailing=False) == ([], 0)  # 未闭合
    recs, _ = s.split("no delim yet", flush_trailing=True)
    assert recs == ["no delim yet"]


# ----------------------- build_splitter -----------------------


def test_build_default_line() -> None:
    assert isinstance(build_splitter(None), LineSplitter)
    assert isinstance(build_splitter({}), LineSplitter)


def test_build_json_lines_is_line() -> None:
    # json_lines 切割行为同 line(字段提取在 triage 层)。
    assert isinstance(build_splitter({"type": "json_lines"}), LineSplitter)


def test_build_unknown_falls_back_line() -> None:
    assert isinstance(build_splitter({"type": "weird"}), LineSplitter)


def test_build_multiline() -> None:
    s = build_splitter({"type": "multiline_start", "params": {"start_pattern": "^X"}})
    assert isinstance(s, MultilineStartSplitter)


def test_build_bad_regex_falls_back_no_crash() -> None:
    # LLM 给的坏正则不能崩 daemon,降级按行。
    s = build_splitter({"type": "multiline_start", "params": {"start_pattern": "["}})
    assert isinstance(s, LineSplitter)


def test_build_multiline_missing_pattern_falls_back() -> None:
    assert isinstance(build_splitter({"type": "multiline_start", "params": {}}), LineSplitter)


def test_build_delimiter() -> None:
    s = build_splitter({"type": "delimiter", "params": {"separator": "---"}})
    assert isinstance(s, DelimiterSplitter)
