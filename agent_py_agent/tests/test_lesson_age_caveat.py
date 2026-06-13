"""召回 lesson 的陈旧标注钉子测试。"""

from __future__ import annotations

import os
from pathlib import Path

from agent_py_agent.agent.prompting_parts.builder import _lesson_age_caveat


def test_fresh_lesson_has_no_caveat(tmp_path: Path) -> None:
    path = tmp_path / "lesson.md"
    path.write_text("内容", encoding="utf-8")
    os.utime(path, (1000.0, 1000.0))
    # now 仅比 mtime 晚 1 天 < 7 天阈值
    assert _lesson_age_caveat(path, now=1000.0 + 86400.0) == ""


def test_stale_lesson_gets_caveat(tmp_path: Path) -> None:
    path = tmp_path / "lesson.md"
    path.write_text("内容", encoding="utf-8")
    os.utime(path, (1000.0, 1000.0))
    caveat = _lesson_age_caveat(path, now=1000.0 + 10 * 86400.0)
    assert "memory-age-caveat" in caveat
    assert "10 天" in caveat


def test_missing_file_no_crash(tmp_path: Path) -> None:
    assert _lesson_age_caveat(tmp_path / "nope.md", now=1_000_000.0) == ""


def test_custom_stale_threshold(tmp_path: Path) -> None:
    path = tmp_path / "lesson.md"
    path.write_text("内容", encoding="utf-8")
    os.utime(path, (1000.0, 1000.0))
    # 3 天龄，阈值 2 天 → 标注；阈值 5 天 → 不标注
    assert _lesson_age_caveat(path, now=1000.0 + 3 * 86400.0, stale_days=2.0) != ""
    assert _lesson_age_caveat(path, now=1000.0 + 3 * 86400.0, stale_days=5.0) == ""


def test_zero_threshold_disables_caveat(tmp_path: Path) -> None:
    # 配置 home_lesson_stale_caveat_days=0 表示关闭提示，再老也不标注
    path = tmp_path / "lesson.md"
    path.write_text("内容", encoding="utf-8")
    os.utime(path, (1000.0, 1000.0))
    assert _lesson_age_caveat(path, now=1000.0 + 100 * 86400.0, stale_days=0.0) == ""
