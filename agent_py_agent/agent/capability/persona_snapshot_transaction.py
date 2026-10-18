# LLM: C仅固定stage/link/登记/清理或capture恢复叶入门；原_ACTIVE_SNAPSHOTS身份和关闭规则保持，native阻止后继补偿。
# 模块用途: 保存不可覆盖的人格版本快照，C失源时保材料和原异常。
"""Exclusive version snapshots and identity-owned rollback; never unlink public conflicts."""
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .persona_storage_io import (
    _PERSONA_NATIVE_ERRORS,
    make_private_directory,
    open_file,
    parent_directory,
    persona_io,
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


# LLM: 原ContextVar/token/创建责任不变，C只在native退出时保primary并逐件关闭原FD，不新增scope或状态。
# 函数用途: 管理原快照FD生命周期，失源仍关闭且不继续改盘。
@contextmanager
def snapshot_transaction(*, coordinator_io=False):
    created = []
    token = _ACTIVE_SNAPSHOTS.set(created)
    try:
        yield created
    finally:
        _ACTIVE_SNAPSHOTS.reset(token)
        if coordinator_io:
            _close_transaction_snapshots(created, sys.exception())
        else:
            for snapshot in created:
                os.close(snapshot.descriptor)


# LLM: C仅固定stage/link/登记/清理或capture恢复叶入门；原_ACTIVE_SNAPSHOTS身份和关闭规则保持，native阻止后继补偿。
# 函数用途: 保存不可覆盖的人格版本快照，C失源时保材料和原异常。
def write_version_snapshot(path: Path, text: str, *, retain_on_failure=False, coordinator_io=False) -> Path:
    body = text.encode('utf-8')
    with parent_directory(path, create=True, **({} if not coordinator_io else {'coordinator_io': True})):
        pass
    before = _ledger_image(path)
    if before is not None:
        if before[1] != body:
            raise OSError('PERSONA_SNAPSHOT_CONFLICT')
        _pin_existing(path, body, before)
        return path
    with temporary_directory(prefix='.persona-snapshot-', dir=path.parent, **({} if not coordinator_io else {'coordinator_io': True})) as directory:
        _publish_snapshot(path, Path(directory) / 'staged', body, retain_on_failure=retain_on_failure, **({} if not coordinator_io else {'coordinator_io': True}))
    return path


# LLM: C仅固定stage/link/登记/清理或capture恢复叶入门；原_ACTIVE_SNAPSHOTS身份和关闭规则保持，native阻止后继补偿。
# 函数用途: 保存不可覆盖的人格版本快照，C失源时保材料和原异常。
def _publish_snapshot(path, stage, body, *, retain_on_failure=False, coordinator_io=False):
    if coordinator_io:
        return persona_io(lambda: _publish_snapshot(path, stage, body, retain_on_failure=retain_on_failure), coordinator_io=True)
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


# LLM: C仅固定stage/link/登记/清理或capture恢复叶入门；原_ACTIVE_SNAPSHOTS身份和关闭规则保持，native阻止后继补偿。
# 函数用途: 保存不可覆盖的人格版本快照，C失源时保材料和原异常。
def rollback_snapshots(created, *, coordinator_io=False):
    errors = []
    for snapshot in reversed(created):
        if not snapshot.created or snapshot.retain_on_failure:
            continue
        try:
            _withdraw_snapshot(snapshot.path, snapshot.descriptor, snapshot.body, **({} if not coordinator_io else {'coordinator_io': True}))
        except OSError as error:
            if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
                raise
            errors.append(error)
    if errors:
        raise OSError('PERSONA_SNAPSHOT_ROLLBACK_CONFLICT') from errors[0]


# LLM: C仅固定stage/link/登记/清理或capture恢复叶入门；原_ACTIVE_SNAPSHOTS身份和关闭规则保持，native阻止后继补偿。
# 函数用途: 保存不可覆盖的人格版本快照，C失源时保材料和原异常。
def _withdraw_snapshot(path, descriptor, body, *, coordinator_io=False):
    if coordinator_io:
        return persona_io(lambda: _withdraw_snapshot(path, descriptor, body), coordinator_io=True)
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


# LLM: 只关闭原_ACTIVE_SNAPSHOTS本事务已持FD，原token先reset；native primary不因secondary close改类，仍逐件尝试close。
# 函数用途: 原快照事务失源退出时归还准确FD，不改盘或发行清理权。
def _close_transaction_snapshots(created, primary):
    for snapshot in created:
        try:
            os.close(snapshot.descriptor)
        except OSError:
            if not isinstance(primary, _PERSONA_NATIVE_ERRORS):
                raise
