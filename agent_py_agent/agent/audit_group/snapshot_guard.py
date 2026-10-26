# LLM: 只打开已有原锁，不创建锁/目录或做恢复；锁出生、消失或换inode都会拒绝本次快照。
# 模块用途: 让跨owner冻结读取与原资产/Scheduler写者协调而不触动原件。
from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager

from ..common.json_io import _flock_descriptor, _funlock_descriptor
from ..common.nofollow_fs import open_readonly_file_beneath
from .access_policy import AuditGroupError


# LLM: 仅no-follow打开已有单链接普通锁，缺失返回None；权限/链接等错误继续向外失败关闭。
# 函数用途: 在不创建锁文件的前提下观察原锁身份。
def existing_lock(reader, parts):
    try:
        return open_readonly_file_beneath(reader.root, (*reader.parts, *parts))
    except FileNotFoundError:
        return None


# LLM: 对比持有原FD与当前命名叶的dev/inode，不能把锁缺失当成无竞争证明。
# 函数用途: 拒绝读取窗口里发生锁出生、删除或替换的快照。
def check_lock(reader, parts, original):
    current = existing_lock(reader, parts)
    try:
        if (current is None) != (original is None):
            raise AuditGroupError("REVISION_CONFLICT", status_code=409)
        if original is not None:
            before, after = os.fstat(original), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise AuditGroupError("REVISION_CONFLICT", status_code=409)
    finally:
        if current is not None:
            os.close(current)


# LLM: 按原root→collection→Scheduler顺序持短读窗口，绝不持源锁等待脚本/模型；只操作原锁FD无磁盘写。
# 函数用途: 保护一次完整依赖快照的读取，所有出口释放已有文件锁和描述符。
@contextmanager
def source_snapshot_guard(reader):
    parts_list = ((".asset-batch.lock",), ("scripts", ".assets.lock"),
                  ("workflows", ".assets.lock"), ("data", "scheduler", "store.json.lock"))
    with ExitStack() as stack:
        observed = []
        for parts in parts_list:
            descriptor = existing_lock(reader, parts)
            if descriptor is not None:
                stack.callback(os.close, descriptor)
                _flock_descriptor(descriptor)
                stack.callback(_funlock_descriptor, descriptor)
            observed.append((parts, descriptor))
            check_lock(reader, parts, descriptor)
        try:
            yield
        finally:
            for parts, descriptor in observed:
                check_lock(reader, parts, descriptor)
