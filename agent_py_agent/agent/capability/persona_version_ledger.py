# LLM: 版本账本整批提交，失败仅回收私有暂存；不能截断已发布账本或覆盖并发外部写入。
# 模块用途: 原子发布人格版本记录并保留可恢复的事务边界。
"""Persona版本账本的整批发布；失败只丢弃私有暂存，不截断正式账本。"""
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .persona_storage_io import (
    make_private_directory,
    open_file,
    parent_directory,
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


# LLM: 所有合作写者沿用versions路径锁；发布后退出异常保留提交事实，正式账本从不回滚截断。
# 函数用途: 把一批版本先写完整，再核对原账本身份与字节后一次发布。
def append_persona_versions(path: Path, records: list[dict], append_records, *, validate) -> None:
    state = _PublicationState()
    try:
        with versions_lock(path), temporary_directory(
            prefix='.persona-ledger-', dir=path.parent, ignore_cleanup_errors=True,
        ) as directory:
            before = _ledger_image(path)
            stage = Path(directory) / 'versions.jsonl'
            _stage_versions(stage, before, records, append_records)
            validate()
            _require_ledger_image(path, before)
            _publish_ledger(stage, path, before, state)
    except Exception as error:
        if state.published:
            raise PersonaLedgerPublishedError('PERSONA_LEDGER_COMMITTED_CLEANUP_PENDING') from error
        raise


# LLM: 临时目录私有且名称不复用；调用旧追加器以保留格式，所有可能失败的写入在正式发布之前。
# 函数用途: 完成并刷盘整批账本，任何半写都只留在临时目录。
def _stage_versions(stage, before, records, append_records) -> None:
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


# LLM: 发布仅排他link，旧公开账本先捕获而不销毁；提交后任一错误必须保留正文和版本引用。
# 函数用途: 避免检查到replace窗口覆盖外部记录，并区分提交与清理阶段。
def _publish_ledger(stage, path, before, state):
    captured = _capture_ledger(path, before)
    try:
        transfer(stage, path)
        state.published = True
    except OSError:
        _restore_ledger(path, captured)
        raise
    try:
        remove_private(stage)
        if captured is not None and not _same_captured(_ledger_image(captured), before):
            raise OSError(f'PERSONA_LEDGER_EXTERNAL_CHANGE: preserved at {captured}')
        with parent_directory(path) as descriptor:
            os.fsync(descriptor)
    except Exception as error:
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


# LLM: 与发布共用锁读取实际旧账本；配额增量须同时覆盖新记录和保留在恢复区的旧文件。
# 函数用途: 计算一次账本替换额外保留的字节，不把恢复文件当成免费空间。
def retained_ledger_bytes(path: Path) -> int:
    with versions_lock(path):
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
