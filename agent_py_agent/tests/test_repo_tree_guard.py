# LLM: 尺寸报告分支退役后，MagicMock污染的指纹、违规判定、精确清理和并行诊断回归保持原样。
# 模块用途: 验证测试树仍能发现并清理错误的MagicMock路径写入。
"""仓库树防线自检：MagicMock/ 的指纹、违规判定、清理与报错文字。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.tests._repo_tree_guard import (
    MAGICMOCK_DIR_NAME,
    magicmock_failure_message,
    magicmock_fingerprint,
    magicmock_violation,
    remove_magicmock_dir,
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
    # 防线分不出是哪个进程写的，两种文字都要提示并行分片的可能和单独重跑。
    assert all("并行分片" in text and "单独重跑" in text for text in (created, kept))


# LLM: 直接驱动共享fixture，确保移除尺寸分支后仍在teardown报错，且只删除本测试新建的MagicMock目录。
# 函数用途: 验证自动防线清理新污染、保留旧残留的真实夹具行为。
@pytest.mark.parametrize("preexisting", [False, True])
def test_repo_tree_fixture_keeps_magicmock_detection_and_cleanup(tmp_path, preexisting):
    from types import SimpleNamespace

    from agent_py_agent.tests.conftest import _repo_tree_guard

    target = tmp_path / MAGICMOCK_DIR_NAME
    if preexisting:
        target.mkdir()
    request = SimpleNamespace(config=SimpleNamespace(invocation_params=SimpleNamespace(dir=tmp_path)),
                              node=SimpleNamespace(nodeid="fixture-self-check"))
    guard = _repo_tree_guard.__wrapped__(request)
    next(guard)
    (target / "pollution").mkdir(parents=True)
    with pytest.raises(pytest.fail.Exception, match="fixture-self-check"):
        next(guard)
    assert target.exists() is preexisting
