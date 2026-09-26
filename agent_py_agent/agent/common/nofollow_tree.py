# LLM: 递归删除仅用于宿主固定相对目录，父目录沿原 no-follow 打开；不能替代调用方资源退出和权限核验。
# 模块用途: 安全删除一个受管目录树，允许树内正常符号链接，但不会沿链接删除树外内容。

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .nofollow_fs import (
    NoFollowPathError,
    _openat_directory,
    _validate_relative_parts,
    open_directory_beneath,
)


# LLM: 只支持具备 dir_fd/scandir(fd) 的平台（与标准库防链接攻击 rmtree 同一能力判定）；缺失幂等，顶层链接拒绝，
#   删除及父目录刷盘失败原样上抛。
# 函数用途: 按受信根删除一个已确认不再使用的目录，不按扫描或模糊路径选择目标。
def remove_tree_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> None:
    _validate_relative_parts(relative_parts)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise NoFollowPathError("安全递归删除在当前平台不可用")
    try:
        parent = open_directory_beneath(root, relative_parts[:-1])
    except FileNotFoundError:
        return
    try:
        try:
            target = os.stat(relative_parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(target.st_mode):
            raise NoFollowPathError("受管删除目标不是原目录")
        try:
            _remove_directory_at(parent, relative_parts[-1])
        except FileNotFoundError:
            # 并发同代收尾可以先删完；其它错误不能按缺失吞掉。
            try:
                os.stat(relative_parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        os.fsync(parent)
    finally:
        os.close(parent)


# LLM: 子目录一律经 no-follow 打开并复核身份，删除全程只用目录描述符相对名，不回到路径字符串；
#   shutil.rmtree 的 dir_fd 参数到 3.11 才有，项目仍支持 3.10，所以这里自己按描述符逐层删除。
# 函数用途: 删除父目录描述符下的一个子目录及其全部内容，任何错误原样上抛。
def _remove_directory_at(parent_fd: int, name: str) -> None:
    descriptor = _openat_directory(parent_fd, name)
    try:
        _remove_entries(descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


# LLM: 目录项类型取自 no-follow 视角，符号链接只删链接本身；目录递归时再次经身份复核打开。
# 函数用途: 清空一个已打开目录里的所有文件、链接和子目录。
def _remove_entries(directory_fd: int) -> None:
    with os.scandir(directory_fd) as scanned:
        entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in scanned]
    for name, is_directory in entries:
        if is_directory:
            _remove_directory_at(directory_fd, name)
        else:
            os.unlink(name, dir_fd=directory_fd)
