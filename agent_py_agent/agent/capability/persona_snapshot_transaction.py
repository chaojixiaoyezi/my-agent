# LLM: 版本快照必须排他发布且绑定原身份；失败只撤回本次创建件，不删除外部冲突文件。
# 模块用途: 准备人格版本快照并提供条件回滚材料。
"""Exclusive version snapshots and identity-owned rollback; never unlink public conflicts."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .persona_storage_io import (
    make_private_directory,
    open_file,
    parent_directory,
    remove_private,
    temporary_directory,
    transfer,
)
from .persona_version_ledger import _ledger_image

_ACTIVE_SNAPSHOTS = ContextVar('persona_snapshot_transaction', default=None)


# LLM: 新建与复用快照都钉住FD，只有created对象有撤回责任。
# 类用途: 把快照验证依据和创建归属分开记录。
@dataclass
class _PinnedSnapshot:
    path: Path
    descriptor: int
    body: bytes
    created: bool
    retain_on_failure: bool = False


# LLM: ContextVar限定当前同步事务，不跨线程共享；调用者决定提交或回滚，本层只持创建FD。
# 函数用途: 保存本轮快照创建凭据，并保证结束后关闭文件描述符。
@contextmanager
def snapshot_transaction():
    created = []
    token = _ACTIVE_SNAPSHOTS.set(created)
    try:
        yield created
    finally:
        _ACTIVE_SNAPSHOTS.reset(token)
        for snapshot in created:
            os.close(snapshot.descriptor)


# LLM: 旧快照按身份和完整字节复用；新快照排他link；真实前像可声明失败时原位保留。
# 函数用途: 创建不可覆盖的版本或恢复快照，并保留FD核验依据。
def write_version_snapshot(path: Path, text: str, *, retain_on_failure=False) -> Path:
    body = text.encode('utf-8')
    with parent_directory(path, create=True):
        pass
    before = _ledger_image(path)
    if before is not None:
        if before[1] != body:
            raise OSError('PERSONA_SNAPSHOT_CONFLICT')
        _pin_existing(path, body, before)
        return path
    with temporary_directory(prefix='.persona-snapshot-', dir=path.parent) as directory:
        _publish_snapshot(path, Path(directory) / 'staged', body, retain_on_failure=retain_on_failure)
    return path


# LLM: link成功立即登记创建和保留责任；之后的unlink/fsync失败不能遗忘恢复材料。
# 函数用途: 发布已刷盘快照，刷目录并保留原始打开文件身份。
def _publish_snapshot(path, stage, body, *, retain_on_failure=False):
    descriptor = open_file(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL)
    retained = False
    try:
        with os.fdopen(os.dup(descriptor), 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        transfer(stage, path)
        created = _ACTIVE_SNAPSHOTS.get()
        if created is not None:
            created.append(_PinnedSnapshot(path, descriptor, body, True, retain_on_failure))
            retained = True
        remove_private(stage)
        with parent_directory(path) as parent:
            os.fsync(parent)
    finally:
        if not retained:
            os.close(descriptor)


# LLM: 每件独立回滚；真实前像必须原位保留，任一冲突不能阻止其它本轮件收尾。
# 函数用途: 撤回当前事务的临时版本快照，但不撤回唯一的恢复前像。
def rollback_snapshots(created):
    errors = []
    for snapshot in reversed(created):
        if not snapshot.created or snapshot.retain_on_failure:
            continue
        try:
            _withdraw_snapshot(snapshot.path, snapshot.descriptor, snapshot.body)
        except OSError as error:
            errors.append(error)
    if errors:
        raise OSError('PERSONA_SNAPSHOT_ROLLBACK_CONFLICT') from errors[0]


# LLM: 先核归属再rename捕获，并再次核验实际捕获物；即便正确捕获也保留文件以保护已打开的外部FD。
# 函数用途: 将自己的失败快照移到恢复区，不直接unlink可能被换身的公开名字。
def _withdraw_snapshot(path, descriptor, body):
    current = _ledger_image(path)
    if current is None:
        return
    _require_owned(current, descriptor, body)
    directory = make_private_directory(path.parent, '.persona-recovery-')
    captured = directory / 'captured'
    transfer(path, captured, move=True)
    try:
        _require_owned(_ledger_image(captured), descriptor, body)
    except OSError:
        # 原名已被抢占时link拒绝覆盖，冲突物继续保留在恢复区。
        transfer(captured, path)
        remove_private(captured)
        remove_private(directory, directory=True)
        raise


# LLM: 字节匹配不能替代inode身份；创建FD一直打开，防止inode号码被回收重用。
# 函数用途: 判断一个快照是否仍是本事务未改写的创建件。
def _require_owned(image, descriptor, body):
    owned = os.fstat(descriptor)
    if image is None or image[0][:2] != (owned.st_dev, owned.st_ino) or image[1] != body:
        raise OSError('PERSONA_SNAPSHOT_CHANGED')


# LLM: 打开不跟随描述符后再核对完整前像；只保留读凭据，不能认领既有快照的删除权。
# 函数用途: 防止复用快照在提交前被换身或改写而无人核验。
def _pin_existing(path, body, before):
    snapshots = _ACTIVE_SNAPSHOTS.get()
    if snapshots is None:
        return
    descriptor = open_file(path, os.O_RDONLY)
    try:
        image = _ledger_image(path)
        _require_owned(image, descriptor, body)
        if image != before:
            raise OSError('PERSONA_SNAPSHOT_CHANGED')
    except BaseException:
        os.close(descriptor)
        raise
    snapshots.append(_PinnedSnapshot(path, descriptor, body, False))


# LLM: 与创建时的仍打开FD和预期正文逐件比较；不从当前路径重新采样并冒充原身份。
# 函数用途: 发布账本前核对全部相关快照，包括首次基线与复用快照。
def verify_snapshots(snapshots):
    for snapshot in snapshots:
        _require_owned(_ledger_image(snapshot.path), snapshot.descriptor, snapshot.body)
