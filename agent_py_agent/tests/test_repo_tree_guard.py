"""仓库树防线自检：MagicMock/ 的指纹、违规判定、清理与报错文字。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.tests._repo_tree_guard import (
    MAGICMOCK_DIR_NAME,
    REPO_ROOT,
    TRACKED_REPORT_NAME,
    magicmock_failure_message,
    magicmock_fingerprint,
    magicmock_violation,
    remove_magicmock_dir,
    tracked_report_failure_message,
    tracked_report_fingerprint,
)


def test_absent_directory_has_no_fingerprint_and_is_not_a_violation(tmp_path):
    assert magicmock_fingerprint(str(tmp_path)) is None
    assert not magicmock_violation(None, None)


# 真实形态：MagicMock 当路径时 os.fspath 给出相对路径 "MagicMock/<名字>/<id>"，落在当前目录下。
def test_magicmock_used_as_a_path_is_detected_and_cleaned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = str(tmp_path)
    before = magicmock_fingerprint(root)
    agent = MagicMock()
    (Path(agent.home_paths.owner_home_dir) / "memory_archive").mkdir(parents=True)
    after = magicmock_fingerprint(root)
    assert before is None and after is not None
    assert magicmock_violation(before, after)
    remove_magicmock_dir(root)
    assert not (tmp_path / MAGICMOCK_DIR_NAME).exists()


def test_untouched_leftover_is_not_a_violation(tmp_path):
    (tmp_path / MAGICMOCK_DIR_NAME / "mock.home_paths.owner_home_dir" / "1").mkdir(parents=True)
    before = magicmock_fingerprint(str(tmp_path))
    assert not magicmock_violation(before, magicmock_fingerprint(str(tmp_path)))


def test_new_entry_under_an_existing_leftover_is_a_violation(tmp_path):
    child = tmp_path / MAGICMOCK_DIR_NAME / "mock.home_paths.owner_home_dir"
    (child / "1").mkdir(parents=True)
    # 把子目录的 mtime 拨回很早，保证新建条目后一定不同，不依赖文件系统的时间精度。
    os.utime(child, ns=(1, 1))
    before = magicmock_fingerprint(str(tmp_path))
    (child / "2").mkdir()
    assert magicmock_violation(before, magicmock_fingerprint(str(tmp_path)))


def test_failure_message_names_the_test_the_place_and_the_fix(tmp_path):
    created = magicmock_failure_message("tests/test_x.py::test_y", str(tmp_path), created=True)
    kept = magicmock_failure_message("tests/test_x.py::test_y", str(tmp_path), created=False)
    assert "tests/test_x.py::test_y" in created and str(tmp_path / MAGICMOCK_DIR_NAME) in created
    assert "monkeypatch.chdir(tmp_path)" in created and "已删除" in created
    assert "不删除" in kept


def test_tracked_report_rewrite_changes_the_fingerprint(tmp_path):
    assert tracked_report_fingerprint(str(tmp_path)) is None
    report = tmp_path / TRACKED_REPORT_NAME
    report.write_text("old\n", encoding="utf-8")
    os.utime(report, ns=(1, 1))
    before = tracked_report_fingerprint(str(tmp_path))
    assert tracked_report_fingerprint(str(tmp_path)) == before
    report.write_text("old\n", encoding="utf-8")
    assert tracked_report_fingerprint(str(tmp_path)) != before


def test_repo_root_matches_the_code_size_script_root():
    assert (Path(REPO_ROOT) / "scripts" / "check_code_size.py").is_file()
    message = tracked_report_failure_message("tests/test_x.py::test_y", REPO_ROOT)
    assert "--report" in message and "tests/test_x.py::test_y" in message
