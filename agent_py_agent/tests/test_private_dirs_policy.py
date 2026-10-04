"""pdp：私有写只动自己建的东西（3a 2026-10-03 裁定，与锁收私 ds8 同口径）回归。

钉三件事：
1. ensure_private_dir 独立方向：缺失目录按 0700 新建；已存在的 0755 目录（含符号链接）一位不动。
2. 可配置的宿主数据路径（audit_log_path 单文件形态、LocalStore 的 events 目录）指向已存在的 0755
   用户目录时，写入只新增 0600 的私有文件，目录与旁边的兄弟文件完全不受影响。
3. 本批改过的“先普通 mkdir 再私有写”建目录点：umask 0o022 下缺失目录新建出来是 0700。

全部用 pytest tmp_path，不碰真实 home，不调真实模型。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.audit.logger import AuditAction, AuditLogger, AuditStatus, LogParams
from agent_py_agent.agent.common.json_io import append_private_jsonl_capped, locked_json_path
from agent_py_agent.agent.common.nofollow_fs import (
    NoFollowPathError,
    ensure_private_dir,
    open_directory_beneath,
    resolve_existing_symlink_anchor,
    split_existing_anchor,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.subagents.task_trash import ensure_task_trash

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


# 函数用途: 固定 umask 0o022（生产常见值），验证私有权限不依赖 umask；用例结束恢复原值。
@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


# 函数用途: 读一个路径的权限位。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# 函数用途: 触发一条审计写入（所有审计便捷方法都汇聚到 AuditLogger.log）。
def _log_one(logger: AuditLogger, task_id: str):
    return logger.log(
        LogParams(
            action=AuditAction.CREATE_TASK,
            user_id="user1",
            channel="chat",
            target_type="task",
            target_id=task_id,
            status=AuditStatus.SUCCESS,
        )
    )


# ---- 1. ensure_private_dir 独立方向（X_M8 两个方向都必须被抓到）----


@_POSIX_ONLY
def test_ensure_private_dir_creates_missing_directories_with_0700(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "leaf"

    ensure_private_dir(target)

    assert _mode(target) == 0o700
    assert _mode(target.parent) == 0o700, "缺失的中间目录同样按 0700 建（no-follow 原语逐段创建）"


@_POSIX_ONLY
def test_ensure_private_dir_creates_every_missing_level_with_0700(tmp_path: Path) -> None:
    """pbfix 2026-10-04：多级新建时每一级都是 0700（mkdir(parents=True, mode=0o700) 只保最后一级）。"""
    target = tmp_path / "a" / "b" / "c"

    ensure_private_dir(target)

    assert (_mode(target), _mode(target.parent), _mode(target.parent.parent)) == (0o700, 0o700, 0o700)


@_POSIX_ONLY
def test_ensure_private_dir_keeps_existing_directory_mode(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    os.chmod(target, 0o755)

    ensure_private_dir(target)

    assert _mode(target) == 0o755, "已存在的目录不管权限多宽都一位不动"


@_POSIX_ONLY
def test_ensure_private_dir_does_not_touch_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o755)
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)

    ensure_private_dir(link)

    assert link.is_symlink(), "符号链接本身不许被替换"
    assert _mode(real) == 0o755, "链接目标权限一位不动"


@_POSIX_ONLY
def test_private_dir_beneath_symlinked_workspace_root_creates_target_children(tmp_path: Path) -> None:
    """pdp 2026-10-04：工作区根是指向别处的符号链接时，.background_jobs 在链接目标里按 0700 建好。"""
    real = tmp_path / "real-workspace"
    real.mkdir()
    os.chmod(real, 0o755)
    link = tmp_path / "link-workspace"
    os.symlink(real, link, target_is_directory=True)

    target = link / ".background_jobs" / "registry.jsonl"
    append_private_jsonl_capped(target, {"n": 1}, max_records=10)

    assert (real / ".background_jobs").is_dir()
    assert _mode(real / ".background_jobs") == 0o700, "缺失段在链接目标里按 0700 新建"
    assert _mode(real) == 0o755, "链接目标目录权限一位不动"
    assert link.is_symlink(), "链接本身不被替换"
    assert _mode(target) == 0o600


@_POSIX_ONLY
def test_private_dir_rejects_symlink_segment_with_unusable_target(tmp_path: Path) -> None:
    """pdp 2026-10-04：新建段上方被人放了符号链接（断链/指向文件）→ 仍然拒绝。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)

    (real / ".background_jobs").symlink_to(tmp_path / "missing-target", target_is_directory=True)
    with pytest.raises(OSError):
        ensure_private_dir(link / ".background_jobs" / "sub")

    (real / ".background_jobs").unlink()
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    (real / ".background_jobs").symlink_to(plain)
    with pytest.raises(OSError):
        ensure_private_dir(link / ".background_jobs" / "sub")

    # 原语层：从真实目录往下建缺失段时，段里已存在的符号链接一律拒绝（不跟随）。
    outside = tmp_path / "outside"
    outside.mkdir()
    (real / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        open_directory_beneath(real, ("sub", "leaf"), create=True)


@_POSIX_ONLY
def test_private_lock_beneath_symlinked_anchor_creates_lock_in_target(tmp_path: Path) -> None:
    """pdp 2026-10-04：私有锁同一口径——锚点是符号链接时跟随一次，锁文件在真实目录里按 0600 建。"""
    real = tmp_path / "real-state"
    real.mkdir()
    os.chmod(real, 0o755)
    link = tmp_path / "linked-state"
    os.symlink(real, link, target_is_directory=True)

    with locked_json_path(link / "state.json"):
        lock = real / "state.json.lock"
        assert lock.is_file()
        assert _mode(lock) == 0o600

    assert _mode(real) == 0o755, "链接目标目录权限一位不动"
    assert link.is_symlink()


# ---- 1b. split_existing_anchor 停在断链符号链接处（st1 2026-10-04，pbfixr 初审 M2 缺口）----


@_POSIX_ONLY
def test_split_existing_anchor_stops_at_broken_symlink_ancestor(tmp_path: Path) -> None:
    """断链符号链接祖先必须被当作"最近已存在祖先"停下，不能因为 exists() 为假就继续往上找。

    pbfix 把 6 处内联循环收进 split_existing_anchor 后没有专门用例；循环条件一旦丢掉
    `and not anchor.is_symlink()`，断链会被跳过、锚点跑到更上层，私有写就会在错误的位置建目录
    （断链本身也该按 fail-closed 拒绝，而不是被绕过）。
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    broken = outer / "broken-link"
    broken.symlink_to(outer / "missing-target", target_is_directory=True)

    anchor, parts = split_existing_anchor(broken / "leaf" / "deep")

    assert anchor == broken, "必须停在断链这一级（不是 outer，也不是更上层）"
    assert parts == ("leaf", "deep"), "断链以下才是缺失段"

    # 实际使用它的私有写入口必须拒绝，而不是在断链上方悄悄建目录。
    with pytest.raises(NoFollowPathError):
        ensure_private_dir(broken / "leaf" / "deep")
    assert not (outer / "leaf").exists(), "拒绝就不能在错误位置留下目录"

    # 同一断链交给锚点解析同样拒绝（ensure_private_dir 内部正是这么用的）。
    with pytest.raises(NoFollowPathError):
        resolve_existing_symlink_anchor(broken)


@_POSIX_ONLY
def test_split_existing_anchor_stops_at_file_symlink_ancestor(tmp_path: Path) -> None:
    """指向文件的符号链接祖先同样停下并拒绝，不继续向上找。"""
    outer = tmp_path / "outer2"
    outer.mkdir()
    target_file = tmp_path / "plain.txt"
    target_file.write_text("x", encoding="utf-8")
    link = outer / "file-link"
    link.symlink_to(target_file)

    anchor, parts = split_existing_anchor(link / "leaf")

    assert anchor == link and parts == ("leaf",)
    with pytest.raises(NoFollowPathError):
        resolve_existing_symlink_anchor(link)
    with pytest.raises(NoFollowPathError):
        ensure_private_dir(link / "leaf")


# ---- 2. 可配置路径指向已存在用户目录 ----


class _AuditConfig:
    # LLM: 只暴露 audit_log_path；AuditLogger 的其它开关按缺省走（与既有测试替身同一形态）。
    # 类用途: 给 AuditLogger 用的最小配置替身。
    def __init__(self, log_path: Path) -> None:
        self.audit_log_path = str(log_path)


@_POSIX_ONLY
def test_audit_file_in_existing_project_dir_keeps_dir_and_siblings(tmp_path: Path) -> None:
    """luna2 实测场景：audit_log_path 配成用户项目目录里的单个文件，目录不许被收紧。"""
    project = tmp_path / "user-project"
    project.mkdir()
    os.chmod(project, 0o755)
    sibling = project / "notes.txt"
    sibling.write_text("user data\n", encoding="utf-8")
    os.chmod(sibling, 0o644)

    logger = AuditLogger(_AuditConfig(project / "audit.jsonl"))
    _log_one(logger, "task-pdp")

    assert _mode(project) == 0o755, "已存在的用户目录一位都不许动"
    assert _mode(sibling) == 0o644 and sibling.read_text(encoding="utf-8") == "user data\n"
    assert _mode(project / "audit.jsonl") == 0o600, "审计文件本身仍按私有出生"


@_POSIX_ONLY
def test_audit_root_missing_is_created_with_0700(tmp_path: Path) -> None:
    logger = AuditLogger(_AuditConfig(tmp_path / "audit-root"))

    _log_one(logger, "task-pdp-2")

    assert _mode(tmp_path / "audit-root") == 0o700, "缺失的审计根按 0700 新建"
    assert _mode(tmp_path / "audit-root" / "audit.jsonl") == 0o600


@_POSIX_ONLY
def test_local_store_events_in_existing_wide_dir_keeps_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "user-data"
    data_dir.mkdir()
    os.chmod(data_dir, 0o755)

    store = LocalStore(data_dir / "local.db")
    store.record_event(event_type="demo", payload={"k": 1})

    assert _mode(data_dir) == 0o755, "已存在的数据目录一位都不许动"
    assert _mode(store.events_path) == 0o600
    assert _mode(store.files_dir) == 0o700, "缺失的子目录仍按 0700 新建"


# ---- 3. 建目录点：umask 0o022 下缺失目录按 0700 新建 ----


@_POSIX_ONLY
def test_memory_store_builders_create_private_dirs(tmp_path: Path) -> None:
    daily = tmp_path / "m1" / "daily"
    DailyMemoryStore(daily)
    assert _mode(daily) == 0o700

    candidates = tmp_path / "m2" / "candidates.jsonl"
    CandidateService(candidates)
    assert _mode(candidates.parent) == 0o700

    lessons = tmp_path / "m3" / "lessons"
    index = tmp_path / "m3" / "routing" / "INDEX.md"
    LessonRepository(lessons, index)
    assert _mode(lessons) == 0o700
    assert _mode(index.parent) == 0o700

    hot = tmp_path / "m4" / "memory-hot.md"
    HotRuleRepository(hot)
    assert _mode(hot.parent) == 0o700


@_POSIX_ONLY
def test_scheduler_repository_root_is_created_with_0700(tmp_path: Path) -> None:
    root = tmp_path / "scheduler"

    SchedulerRepository(root, owner_provider="local", owner_kind="main", owner_id="local/main")

    assert _mode(root) == 0o700


@_POSIX_ONLY
def test_task_trash_dir_is_created_with_0700(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()

    trash = ensure_task_trash(task)

    assert _mode(trash) == 0o700


@_POSIX_ONLY
def test_gateway_json_write_creates_private_dir(tmp_path: Path) -> None:
    target = tmp_path / "gateway" / "state.json"

    write_json_file(target, {"k": 1})

    assert _mode(target.parent) == 0o700, "Gateway 宿主运行数据目录按 0700 新建"
