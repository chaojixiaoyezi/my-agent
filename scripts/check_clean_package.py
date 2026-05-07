#!/usr/bin/env python3
# LLM: Repository maintenance script; keep command-line behavior and generated artifacts stable.
# 模块用途: 提供仓库维护、打包、验收或自动化辅助能力。

from __future__ import annotations

"""check that a directory or tar.gz archive contains no dirty artifacts.

给人看的解释：
这个脚本检查目录或 tar.gz 交付包是否包含 macOS 元数据、Python 缓存等脏文件。
发现任何脏文件则退出码非 0，交付包视为不通过。
"""

import argparse
import subprocess
import sys
import tarfile
from pathlib import Path

DIRTY_PREFIXES = ("._",)
DIRTY_NAMES = {
    ".DS_Store",
    ".AppleDouble",
    ".LSOverride",
}
DIRTY_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "htmlcov",
}
DIRTY_SUFFIXES = {".pyc", ".pyo"}


# LLM: _is_dirty 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def _is_dirty(name: str) -> bool:
    """Check if a path component or filename is dirty."""
    parts = name.replace("\\", "/").split("/")
    for part in parts:
        if part in DIRTY_NAMES:
            return True
        if part in DIRTY_PARTS:
            return True
        if any(part.startswith(prefix) for prefix in DIRTY_PREFIXES):
            return True
        if any(part.endswith(suffix) for suffix in DIRTY_SUFFIXES):
            return True
    return False


# LLM: _git_tracked_files 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _git_tracked_files(root: Path) -> set[str] | None:
    """Get set of git-tracked files, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        )
        return set(result.stdout.splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# LLM: check_directory 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def check_directory(root: Path) -> list[str]:
    """Check a directory for dirty files.

    In a git repo, only checks tracked files (untracked dirty files
    are acceptable since they're in .gitignore).
    In non-git environments, checks all files."""
    tracked = _git_tracked_files(root)
    offenders: list[str] = []
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        # In git mode: only check tracked files
        if tracked is not None and rel not in tracked:
            continue
        if _is_dirty(path.name):
            offenders.append(rel)
    return offenders


# LLM: check_tarball 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def check_tarball(tar_path: Path) -> list[str]:
    """Check a tar.gz archive for dirty files."""
    with tarfile.open(tar_path, "r:gz") as tf:
        return _dirty_tar_members(tf.getmembers())


# LLM: _dirty_tar_members 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _dirty_tar_members(members: list[tarfile.TarInfo]) -> list[str]:
    return [member.name for member in members if _is_dirty(member.name)]


# LLM: main 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 脚本入口，解析参数、运行主流程，并用退出码表达成功或失败。
def main() -> int:
    parser = argparse.ArgumentParser(description="Check directory or tar.gz for dirty artifacts.")
    parser.add_argument("target", help="Directory path or .tar.gz file to check")
    args = parser.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"ERROR: target does not exist: {target}", file=sys.stderr)
        return 1

    if target.is_dir():
        offenders = check_directory(target)
        label = f"directory {target}"
    elif target.is_file() and (target.suffix == ".gz" or target.name.endswith(".tar.gz")):
        offenders = check_tarball(target)
        label = f"archive {target}"
    else:
        print(f"ERROR: unsupported target type: {target}", file=sys.stderr)
        return 1

    if offenders:
        print(f"FAILED: {label} contains {len(offenders)} dirty file(s):")
        for path in sorted(offenders):
            print(f"  {path}")
        return 1

    print(f"OK: {label} is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
