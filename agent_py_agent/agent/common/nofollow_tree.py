# LLM: 递归删除仅用于宿主固定相对目录，父目录沿原 no-follow 打开；不能替代调用方资源退出和权限核验。
# 模块用途: 安全删除一个受管目录树，允许树内正常符号链接，但不会沿链接删除树外内容。

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .nofollow_fs import NoFollowPathError, _validate_relative_parts, open_directory_beneath


# LLM: 只支持标准库具备 dir_fd 防链接攻击的实现；缺失幂等，顶层链接拒绝，删除及父目录刷盘失败原样上抛。
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
            shutil.rmtree(relative_parts[-1], dir_fd=parent)
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
