"""AttemptView 测试（3.txt E.1/E.2，§5 测试 8 的 inode 部分）。

- Git 项目 → 独立 worktree；非 Git → 快照（reflink 优先）。
- staging（视图）与共享 workspace 文件 inode 必须不同（E.2：禁 hardlink）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.view import AttemptView, build_attempt_view


@pytest.fixture
def layout(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "file.txt").write_text("hello", encoding="utf-8")
    (shared / "sub").mkdir()
    (shared / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    views_root = tmp_path / "views"
    return shared, views_root


def _inode(path: Path) -> int:
    return path.stat().st_ino


def test_snapshot_view_content_and_inode(layout):
    shared, views = layout
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    assert view.kind == "snapshot"
    assert (view.view_path / "file.txt").read_text(encoding="utf-8") == "hello"
    assert (view.view_path / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested"
    # E.2：staging 与共享 inode 不同（非 hardlink）。
    assert _inode(view.view_path / "file.txt") != _inode(shared / "file.txt")
    assert _inode(view.view_path / "sub" / "nested.txt") != _inode(shared / "sub" / "nested.txt")
    # 视图可写不影响共享。
    (view.view_path / "file.txt").write_text("view-changed", encoding="utf-8")
    assert (shared / "file.txt").read_text(encoding="utf-8") == "hello"


def test_snapshot_isolated_dirs(layout):
    shared, views = layout
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    (view.view_path / "new-file.txt").write_text("new", encoding="utf-8")
    assert not (shared / "new-file.txt").exists()


def test_view_discard_snapshot(layout):
    shared, views = layout
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    path = view.view_path
    view.discard()
    assert not path.exists()


def _git_available() -> bool:
    return shutil.which("git") is not None


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    return env


@pytest.mark.skipif(not _git_available(), reason="git 不可用")
def test_git_worktree_view(layout):
    shared, views = layout
    env = _git_env()
    subprocess.run(
        ["git", "-C", str(shared), "init", "-q"],
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(shared), "add", "-A"],
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(shared), "commit", "-q", "-m", "init"],
        capture_output=True,
        check=True,
        env=env,
    )
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    assert view.kind == "git_worktree"
    assert view.is_git
    assert (view.view_path / "file.txt").read_text(encoding="utf-8") == "hello"
    # worktree 独立工作文件：视图改动不影响共享工作树。
    (view.view_path / "file.txt").write_text("worktree-changed", encoding="utf-8")
    assert (shared / "file.txt").read_text(encoding="utf-8") == "hello"
    # E.2：inode 不同。
    assert _inode(view.view_path / "file.txt") != _inode(shared / "file.txt")


@pytest.mark.skipif(not _git_available(), reason="git 不可用")
def test_git_worktree_discard(layout):
    shared, views = layout
    env = _git_env()
    subprocess.run(["git", "-C", str(shared), "init", "-q"], capture_output=True, check=True, env=env)
    subprocess.run(["git", "-C", str(shared), "add", "-A"], capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "-C", str(shared), "commit", "-q", "-m", "init"],
        capture_output=True,
        check=True,
        env=env,
    )
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    path = view.view_path
    view.discard()
    assert not path.exists()
    # worktree 注册被 prune。
    listing = subprocess.run(
        ["git", "-C", str(shared), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(path) not in listing.stdout


def test_view_name_isolated_from_shared(layout):
    shared, views = layout
    view = build_attempt_view(shared_workspace=shared, views_root=views, view_name="att-1")
    assert view.shared_workspace == shared
