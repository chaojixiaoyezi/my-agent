# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 模块用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
"""Persona版本账本的整批发布；失败只丢弃私有暂存，不截断正式账本。"""
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .persona_storage_io import (
    _PERSONA_NATIVE_ERRORS,
    append_encoded_version_records,
    encode_version_records,
    make_private_directory,
    open_file,
    parent_directory,
    persona_io,
    remove_private,
    temporary_directory,
    transfer,
    versions_lock,
)


# LLM: 正式账本已提交但退出失败；调用者必须保留正文与快照，不能按未提交补偿。
# 类用途: 区分提交后的收尾失败与可安全回滚的准备失败。
class PersonaLedgerPublishedError(OSError):
    pass


# LLM: 在link成功返回后立即登记，后续异常覆盖不得抹掉这一进程内提交事实。
# 类用途: 向全部退出路径传递账本是否已经公开。
@dataclass
class _PublicationState:
    published: bool = False


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def append_persona_versions(path: Path, records: list[dict], append_records, *, validate, coordinator_io=False) -> None:
    state = _PublicationState()
    try:
        with versions_lock(path, **({} if not coordinator_io else {'coordinator_io': True})), temporary_directory(
            prefix='.persona-ledger-', dir=path.parent, ignore_cleanup_errors=True,
            **({} if not coordinator_io else {'coordinator_io': True}),
        ) as directory:
            before = _ledger_image(path)
            stage = Path(directory) / 'versions.jsonl'
            _stage_versions(stage, before, records, append_records, **({} if not coordinator_io else {'coordinator_io': True}))
            validate()
            _require_ledger_image(path, before)
            _publish_ledger(stage, path, before, state, **({} if not coordinator_io else {'coordinator_io': True}))
    except Exception as error:
        if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
            raise
        if state.published:
            raise PersonaLedgerPublishedError('PERSONA_LEDGER_COMMITTED_CLEANUP_PENDING') from error
        raise


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def _stage_versions(stage, before, records, append_records, *, coordinator_io=False) -> None:
    if coordinator_io:
        body = encode_version_records(records)
        persona_io(lambda: _stage_preimage(stage, before), coordinator_io=True)
        persona_io(lambda: append_encoded_version_records(stage, body), coordinator_io=True)
        return persona_io(lambda: _sync_stage(stage), coordinator_io=True)
    fd = open_file(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(before[1] if before is not None else b'')
    append_records(stage, records)
    with os.fdopen(open_file(stage, os.O_RDONLY), 'rb') as handle:
        os.fsync(handle.fileno())


# LLM: 拒绝符号链接、多硬链接及非普通文件；描述符读前后和路径都须保持同一身份，不跟随替换目标。
# 函数用途: 取得用于发布前核验的账本身份和原始字节，缺失不创建文件。
def _ledger_image(path):
    try:
        with parent_directory(path) as parent:
            return _read_ledger_image(parent, path.name)
    except FileNotFoundError:
        return None


# LLM: parent是已核验的父目录FD；读取前后均校验同一普通单链接文件，不放宽严格读取。
# 函数用途: 固定账本描述符并读取与身份一致的完整前像。
def _read_ledger_image(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError('PERSONA_LEDGER_UNSAFE')
        content = handle.read()
        identity = _identity(before)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(os.fstat(handle.fileno())) != identity or _identity(current) != identity:
            raise OSError('PERSONA_LEDGER_CHANGED')
        return identity, content


# LLM: 身份包含mode/link和修改标记，不只比较长度或正文；仅供ledger前像校验。
# 函数用途: 汇总用于判断账本换身或修改的文件属性。
def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def _publish_ledger(stage, path, before, state, *, coordinator_io=False):
    if coordinator_io:
        return persona_io(lambda: _publish_ledger_current(stage, path, before, state, coordinator_io=True), coordinator_io=True)
    return _publish_ledger_current(stage, path, before, state)


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def _publish_ledger_current(stage, path, before, state, *, coordinator_io=False):
    captured = _capture_ledger(path, before)
    try:
        transfer(stage, path)
        state.published = True
    except OSError as error:
        if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
            raise
        _restore_ledger(path, captured)
        raise
    try:
        remove_private(stage)
        if captured is not None and not _same_captured(_ledger_image(captured), before):
            raise OSError(f'PERSONA_LEDGER_EXTERNAL_CHANGE: preserved at {captured}')
        with parent_directory(path) as descriptor:
            os.fsync(descriptor)
    except Exception as error:
        if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
            raise
        raise PersonaLedgerPublishedError('PERSONA_LEDGER_COMMITTED_CLEANUP_PENDING') from error


# LLM: rename目的位于私有唯一目录，捕获物永不无条件删除；捕获到外部换身/改写时排他恢复。
# 函数用途: 保住正式账本原文件，再核对实际捕获的身份和字节。
def _capture_ledger(path, before):
    if before is None:
        return None
    directory = make_private_directory(path.parent, '.persona-ledger-recovery-')
    captured = directory / 'captured'
    transfer(path, captured, move=True)
    try:
        if not _same_captured(_ledger_image(captured), before):
            raise OSError('PERSONA_LEDGER_CHANGED_DURING_CLAIM')
    except OSError:
        _restore_ledger(path, captured)
        raise
    return captured


# LLM: rename会改变ctime，比较其余身份字段及完整字节；旧文件保留避免外部已打开FD后续写入被销毁。
# 函数用途: 核验隔离前后账本仅发生预期的目录迁移。
def _same_captured(actual, before):
    return actual is not None and actual[0][:-1] == before[0][:-1] and actual[1] == before[1]


# LLM: 原公开名字若被再次抢占不覆盖，捕获材料留在随机恢复目录并将冲突抛给调用方。
# 函数用途: 在发布失败时无覆盖地归还捕获的外部账本。
def _restore_ledger(path, captured):
    if captured is not None:
        transfer(captured, path)
        remove_private(captured)
        remove_private(captured.parent, directory=True)


# LLM: 正式捕获前最后一次前像复核；真正发布仍用排他link而非依赖该检查消除竞态。
# 函数用途: 及早拒绝已被外部修改的账本。
def _require_ledger_image(path, before):
    if _ledger_image(path) != before:
        raise OSError('PERSONA_LEDGER_CHANGED: refusing to replace changed ledger')


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def retained_ledger_bytes(path: Path, *, coordinator_io=False) -> int:
    with versions_lock(path, **({} if not coordinator_io else {"coordinator_io": True})):
        before = _ledger_image(path)
        return len(before[1]) if before is not None else 0


# LLM: 读取物理文件只在_ledger_image中完成；本对象只让既有JSONL解析器消费不可变内存前像。
# 类用途: 复用原诊断与LF边界，不把安全读取又降级为普通路径open。
@dataclass(frozen=True)
class _VersionReadImage:
    path: Path
    data: bytes | None

    # LLM: 缺失只取已冻结的读取结果，不重复观察路径。
    # 函数用途: 为JSONL解析器提供存在状态。
    def exists(self):
        return self.data is not None

    # LLM: decode错误保留给既有诊断器，不静默裁剪或清洗。
    # 函数用途: 返回已安全读出的原文。
    def read_text(self, *, encoding):
        return self.data.decode(encoding)

    # LLM: 路径只用于既有错误诊断，不参与后续I/O。
    # 函数用途: 保持诊断中的文件标识。
    def __str__(self):
        return str(self.path)


# LLM: 调用方已持versions锁；先安全取得完整前像，再按旧JSONL容错/诊断方式解析。
# 函数用途: 给Persona历史提供父链不跟随的记录读取。
def read_version_records(path):
    from ..common.json_io import read_jsonl_objects_report
    image = _ledger_image(path)
    frozen = _VersionReadImage(path, None if image is None else image[1])
    return read_jsonl_objects_report(frozen, context='persona_repository.history').records


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def _stage_preimage(stage, before):
    fd = open_file(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(before[1] if before is not None else b'')


# LLM: C编码在Root外，固定stage叶与完整capture/link事务入原门，发布后native同对象穿出；普通published错误合同保留，O(N)本地读取不称恒短。
# 函数用途: 按原完整前像排他发布版本账本，保留提交事实和恢复材料。
def _sync_stage(stage):
    with os.fdopen(open_file(stage, os.O_RDONLY), 'rb') as handle:
        os.fsync(handle.fileno())
