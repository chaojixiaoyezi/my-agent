"""AttemptView（3.txt E.1/E.2）：attempt 的一致读写视图。

- Git 项目 → 独立 worktree（`git worktree add --detach`），共享仓库提交
  历史共享但工作文件独立。
- 非 Git → 完整快照：逐文件 reflink（Linux FICLONE / macOS cp -c clone）
  优先，fallback 普通复制。
- 绝不使用 hardlink 让 staging 与共享 workspace 指向同一 inode（E.2，
  §5 测试 8：staging 与共享文件 inode 必须不同）。

R2 只建立机制（构建/清理），不接执行链路（接线归 R4 cutover）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# FICLONE ioctl（Linux reflink）：_IOW(0x94, 9, int)
_FICLONE = 0x40049409


@dataclass(frozen=True)
class AttemptView:
    """一个 attempt 的独立读写视图。"""

    view_path: Path            # 可写视图根（worktree 或快照目录）
    shared_workspace: Path     # 共享 workspace（只读，视图为其工作副本）
    kind: str                  # "git_worktree" | "snapshot"
    is_git: bool               # 共享项目是否为 Git 仓库

    def discard(self) -> None:
        """回收视图（R4/R5 接线方在 attempt 结束后调用）。

        worktree 用 `git worktree remove --force` + prune 清注册；快照直接删。
        失败不阻断（回收尽力而为，残留由后续 attempt 复用/清理兜底）。
        """
        if self.kind == "git_worktree":
            try:
                removed = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(self.view_path)],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if removed.returncode == 0:
                    self._prune_worktrees()
                    return
                # worktree 移除失败（脏文件等）→ 落到快照式 rmtree 兜底，
                # 不提前 return 留下残留目录；注册仍要 prune。
            except (OSError, subprocess.TimeoutExpired):
                pass
            shutil.rmtree(self.view_path, ignore_errors=True)
            self._prune_worktrees()
            return
        shutil.rmtree(self.view_path, ignore_errors=True)

    def _prune_worktrees(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.shared_workspace), "worktree", "prune"],
            capture_output=True,
            timeout=30,
            check=False,
        )


def build_attempt_view(
    *,
    shared_workspace: str | Path,
    views_root: str | Path,
    view_name: str,
) -> AttemptView:
    """构建 attempt 视图（E.1）：Git 项目 worktree；非 Git 快照。

    - views_root：attempt 视图的根目录（通常 staging 区下按 attempt 分目录）。
    - view_name：目录名（框架按 attempt_id 生成，禁止用户输入直拼）。
    - worktree 不可用（无 git / 命令失败）→ 快照 fallback；快照也失败 → 抛错
      （fail-closed：视图建不出来时 attempt 不得开始执行）。
    """
    shared = Path(shared_workspace).resolve()
    views = Path(views_root).resolve()
    view_path = views / view_name
    is_git = _is_git_repository(shared)
    if is_git:
        try:
            _git_worktree_add(shared, view_path)
            return AttemptView(view_path, shared, "git_worktree", True)
        except (OSError, subprocess.TimeoutExpired, _GitWorktreeError):
            # 允许视图已存在（崩溃残留）时先清理再重试一次。
            try:
                shutil.rmtree(view_path)
            except OSError:
                pass
            try:
                _git_worktree_add(shared, view_path)
                return AttemptView(view_path, shared, "git_worktree", True)
            except (OSError, subprocess.TimeoutExpired, _GitWorktreeError):
                pass  # fallthrough：worktree 不可用 → 快照
    views.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        shared,
        view_path,
        copy_function=_copy_file_reflink_or_copy,
        ignore_dangling_symlinks=True,
        dirs_exist_ok=True,
    )
    return AttemptView(view_path, shared, "snapshot", is_git)


class _GitWorktreeError(RuntimeError):
    pass


def _is_git_repository(path: Path) -> bool:
    if (path / ".git").exists():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and str(result.stdout or "").strip() == "true"


def _git_worktree_add(shared: Path, view_path: Path) -> None:
    if not shutil.which("git"):
        raise _GitWorktreeError("git 不可用")
    result = subprocess.run(
        ["git", "-C", str(shared), "worktree", "add", "--detach", str(view_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise _GitWorktreeError(
            f"git worktree add 失败(exit={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[-500:]}"
        )


def _copy_file_reflink_or_copy(src: str, dst: str) -> None:
    """单文件复制：reflink（COW，不同 inode）优先，fallback 普通复制。

    E.2：绝不 hardlink——reflink/copy 都保证 inode 不同。
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if _reflink_one(src_path, dst_path):
        return
    shutil.copy2(src_path, dst_path)


def _reflink_one(src: Path, dst: Path) -> bool:
    """尝试 reflink 单文件。Linux FICLONE；macOS cp -c（APFS clone）。失败 False。"""
    if os.name != "posix":
        return False
    try:
        import fcntl
    except ImportError:
        return False
    # Linux FICLONE（COW reflink，目标 inode 与源不同）。
    try:
        with open(src, "rb") as in_fd, open(dst, "wb") as out_fd:
            fcntl.ioctl(out_fd.fileno(), _FICLONE, in_fd.fileno())
        return True
    except OSError:
        pass
    # macOS：cp -c 调 clonefile(2)（APFS 专属，与 FICLONE 不同 syscall）。
    if not shutil.which("cp"):
        return False
    try:
        result = subprocess.run(
            ["cp", "-c", str(src), str(dst)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
