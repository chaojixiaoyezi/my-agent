# LLM: 首次来源创建回滚先隔离认领，绝不销毁捕获物；私有路径不代表排斥已打开的外部FD。
# 模块用途: 消除校验到unlink之间的公开路径替换竞态，冲突时保留外部数据。
import os
import secrets

from ..common.nofollow_fs import _openat_directory, _openat_file
from ._agents_contract_fields import AgentsContractError
from .agents_source_files import AGENTS_SOURCE_MAX_BYTES, read_descriptor


# LLM: 调用方持来源锁和父FD；私有隔离目录只容纳一次失败事务，无法无覆盖恢复时保留材料并报冲突。
# 函数用途: 撤销自己创建的叶子，但不删除恰好抢占同名路径的外部文件。
def withdraw_created_source(parent: int, name: str, owned: int, expected: bytes) -> None:
    control = _openat_directory(parent, "persona")
    temporary = "agents-rollback-" + secrets.token_hex(12)
    quarantine = None
    try:
        os.mkdir(temporary, mode=0o700, dir_fd=control)
        quarantine = _openat_directory(control, temporary)
        os.rename(name, "captured", src_dir_fd=parent, dst_dir_fd=quarantine)
        os.fsync(parent)
        os.fsync(quarantine)
        try:
            _verify_captured_source(quarantine, owned, expected)
        except Exception:
            _restore_captured_source(parent, name, quarantine)
            raise
        # 已打开的外部FD可能继续写；匹配的捕获物也保留在隔离目录。
    finally:
        if quarantine is not None:
            os.close(quarantine)
        try:
            _remove_empty_quarantine(control, temporary)
        finally:
            os.close(control)


# LLM: 只接受单链接普通文件且设备/inode与仍打开的原创建FD一致，字节比较不是身份比较的替代。
# 函数用途: 确认隔离文件确实是本次创建且未被外部改写。
def _verify_captured_source(quarantine, owned, expected):
    descriptor = _openat_file(quarantine, "captured", os.O_RDONLY, 0o600)
    try:
        before, actual = os.fstat(owned), os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
        if read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES) != expected:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
    finally:
        os.close(descriptor)


# LLM: link发布天然排他；公开名称被第三方重新占据时不覆盖也不删除隔离材料，交由人工恢复。
# 函数用途: 恢复误认领的外部文件，保留符号链接本体而不跟随。
def _restore_captured_source(parent, name, quarantine):
    os.link("captured", name, src_dir_fd=quarantine, dst_dir_fd=parent, follow_symlinks=False)
    os.fsync(parent)
    os.unlink("captured", dir_fd=quarantine)
    os.fsync(quarantine)


# LLM: 只移除空隔离目录；ENOTEMPTY表示仍有恢复材料，不能为清理隐藏原始错误而删数据。
# 函数用途: 清理已处理完的空目录，留下未处理材料。
def _remove_empty_quarantine(control, temporary):
    import errno
    try:
        os.rmdir(temporary, dir_fd=control)
    except OSError as error:
        if error.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
            raise
