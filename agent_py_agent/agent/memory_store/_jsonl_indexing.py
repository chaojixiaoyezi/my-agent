# LLM: 正式文本投影只归MemoryReadIndex；旧LocalStore仅用于精确删除/审计脱敏，绝不查询、新增或重建memory投影。
# 模块用途: 复核冷热索引候选、保留向量CAS并执行删除后的派生正文清理。
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable

_NATIVE_MEMORY_ERRORS = (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
    LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable,
    RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
    ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError)

if TYPE_CHECKING:
    from .jsonl import MemoryRecord


# LLM: 正式提交与派生清理分开报告，不得把待清理隐私副本当完整成功。
# 类用途: 表达删除已生效但仍有必要清理责任。
class RemovalCleanupError(RuntimeError):
    # LLM: formal_memory_changed只能来自本轮真实提交，历史恢复不得据已有墓碑置True。
    # 函数用途: 携带正式变更事实及清理失败信息。
    def __init__(self, message: str, *, formal_memory_changed: bool = False):
        super().__init__(message)
        self.formal_memory_changed = formal_memory_changed


# LLM: 原native异常必须同对象穿出，普通删除后清理故障保留准确partial回执。
# 函数用途: 在正式删除已提交后保护候选、日志及派生清理阶段。
@contextmanager
def removal_cleanup_phase(*, formal_memory_changed: bool, enabled: bool = True):
    try:
        yield
    except Exception as exc:
        if isinstance(exc, _NATIVE_MEMORY_ERRORS) or not enabled:
            raise
        raise RemovalCleanupError('REMOVE_CLEANUP_PENDING', formal_memory_changed=formal_memory_changed) from exc


# LLM: 此mixin没有正式正文权威或旧workspace投影读写口；向量与删除只消费同一逻辑库。
# 类用途: 集中冷热候选复核、向量版本核验及删除清理。
class JsonlMemoryIndexMixin:
    # LLM: 索引缺失/未追平返回None供权威降级，错误不能伪装完整零命中。
    # 函数用途: 查询本代冷热索引并回读当前分片，返回真实coverage。
    def _search_tier_index(self, query, top_k):
        from .read_index import MemoryReadIndex, MemoryReadIndexGenerationConflict

        index = MemoryReadIndex(self.path)
        try:
            candidates, coverage = index.query(query, limit=min(1000, max(1, top_k)))
            if coverage["status"] != "complete":
                return None, coverage
            return index.resolve(candidates, operation_id=coverage["operation_id"]), coverage
        except MemoryReadIndexGenerationConflict:
            return None, {"status": "stale", "reason_code": "MEMORY_READ_INDEX_GENERATION_CONFLICT"}
        except Exception as exc:
            if getattr(self, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            return None, {"status": "unavailable", "reason_code": type(exc).__name__}

    # LLM: 调用方持正式锁直到派生向量发布；版本/正文/scope任一变化就拒绝旧快照。
    # 函数用途: 核对向量快照仍属于当前冷热事实。
    def _projection_is_current(self, record: MemoryRecord) -> bool:
        return any(row.entry_id == record.entry_id and row.version == record.version
                   and row.content == record.content and row.attributes == record.attributes
                   for row in self._maintenance_records())

    # LLM: 路径键只给后台向量任务隔离owner，不作为旧LocalStore memory命中转换依据。
    # 函数用途: 返回本库向量补齐任务的稳定身份键。
    def _memory_path_key(self) -> str:
        try:
            return str(self.path.resolve())
        except Exception:
            return str(self.path)

    # LLM: 正式提交后只写向量；文本索引由显式有界维护追平，不再将正式正文双写workspace。
    # 函数用途: 同步向量并批量清理已删除事实的所有派生副本。
    def _after_commit_indexes(self, records: list[MemoryRecord]) -> None:
        removed = {record.entry_id for record in records if record.action == "remove"}
        if removed:
            try:
                _purge_removed_indexes(self, removed)
            except RemovalCleanupError as exc:
                raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True) from exc
        for record in records:
            if record.action != "remove":
                self._index_vector(record)

    # LLM: 仅本轮正式删除后的入口，历史恢复不得虚增formal_memory_changed。
    # 函数用途: 保留删除已提交但隐私清理未完成的回执。
    def _purge_committed_indexes(self, entry_id: str) -> None:
        try:
            self.purge_removed_indexes(entry_id)
        except RemovalCleanupError as exc:
            raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True) from exc

    # LLM: 删除与恢复共用，禁用embedding也必须清理磁盘旧副本，失败可重试。
    # 函数用途: 严格清理当前owner精确记忆身份的派生正文。
    def purge_removed_indexes(self, entry_id: str) -> None:
        _purge_removed_indexes(self, entry_id)

    # LLM: 旧workspace依赖仅保删除职责，不能借此新增或重建旧投影。
    # 函数用途: 尽力删除一个精确记忆ID的旧文本副本。
    def _remove_local_index(self, entry_id: str) -> None:
        if not self.local_store or not entry_id:
            return
        try:
            self.local_store.delete_record(self.local_store.make_record_id("memory", entry_id))
        except Exception:
            pass

    # LLM: 正文删除前沿原remember工具账本脱敏，保留操作身份，失败时不修改正式库。
    # 函数用途: 精确清除当前owner审计及幂等账本中的被删正文。
    def _redact_local_tool_ledgers(self, contents: set[str]) -> None:
        if not self.local_store or not contents:
            return
        redact = getattr(self.local_store, "redact_tool_ledger_content", None)
        if callable(redact):
            redact(tool="remember", contents=contents)


# LLM: 同一正式锁下计算完整冷热有效文本，保留所有模型指纹及其它仍引用的同文缓存键。
# 函数用途: 严格回收孤儿文本向量并保留native失败。
def _purge_text_vector_orphans(memory) -> int:
    from ..common.json_io import locked_json_path

    with locked_json_path(memory.path, **({} if not getattr(memory, "coordinator_io", False) else {"birth_commit": _coordinator_index_write})):
        dropped, error = memory.reclaim_text_cache_orphans()
        if error:
            raise RemovalCleanupError("REMOVE_CLEANUP_PENDING")
        return dropped


# LLM: 批量删除每个精确ID后只扫描一次孤儿缓存；旧LocalStore只执行删除，原native异常先透传。
# 函数用途: 清除冷热历史、SQLite、向量与外置正文，失败保待清理责任。
def _purge_removed_indexes(memory, entry_id: str | set[str]) -> None:
    from ..retrieval.vector_store import VectorStore

    try:
        from ._jsonl_maintenance import purge_tier_copies

        purge_tier_copies(memory)
        store = memory._vector_store()
        if store is None:
            if getattr(memory, "coordinator_io", False):
                store = memory._vector_store_cache
                if store is None:
                    store = VectorStore(memory._vector_store_path(), coordinator_io=True)
                    memory._vector_store_cache = store
            else:
                store = VectorStore(memory._vector_store_path())
        removed_ids = {entry_id} if isinstance(entry_id, str) else entry_id
        for removed_id in removed_ids:
            store.remove(removed_id)
            if memory.local_store is not None:
                memory.local_store.delete_record(memory.local_store.make_record_id("memory", removed_id))
        _purge_text_vector_orphans(memory)
    except Exception as exc:
        if getattr(memory, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
            raise
        raise RemovalCleanupError("REMOVE_CLEANUP_PENDING") from exc


# LLM: 只消费当前原global scope，不保存callback，不把扫描/锁等待搬入Root。
# 函数用途: 为现有C正式锁FD出生复用原同步末门。
def _coordinator_index_write(operation):
    return execute_global_write(operation, require_scope=True)


# LLM: 仅表示权威降级达到显式预算，不吞坏根、坏hash或nofollow权限错误。
# 类用途: 让预算耗尽返回真实partial而非全库扫描。
class _AuthorityScanLimit(Exception):
    pass


# LLM: stat只做提前预算与末尾漂移检测，实际读取仍走nofollow并设置字节硬上限。
# 函数用途: 记录一条权威路径的本次物理身份，不创建文件。
def _scan_stamp(path):
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


# LLM: 控制源、墓碑与分片累计计费；换代最多重读两次且不重置预算，真损坏拒绝，未核全屏障不暴露正文。
# 函数用途: 有界读取当前权威候选，并报告实际解析条数/字节与停止位置。
def bounded_authority_records(memory, *, max_records, max_bytes):
    import hashlib
    import json

    from ..common.nofollow_fs import NoFollowPathError, read_bytes_beneath
    from ._jsonl_maintenance import require_maintenance_event
    from .generation_manifest import MemoryGenerationStore
    from .jsonl import (
        _materialize_memory_events,
        _memory_record_from_obj,
        _tombstone_path,
        _with_legacy_identity,
        memory_content_hash,
    )

    store = MemoryGenerationStore(memory.path)
    controls = [store.manifest_path, store.tombstones.path, _tombstone_path(memory.path)]
    used, scanned = 0, 0

    # LLM: 预算先于正文；底层最多多读一个拒绝探测字节，只转换明确超限错误，不吞完整性错误。
    # 函数用途: 读取一个受预算约束的私有权威文件。
    def read(path):
        nonlocal used
        stamp = _scan_stamp(path)
        if stamp is not None and stamp[2] > max_bytes - used:
            raise _AuthorityScanLimit()
        try:
            value = read_bytes_beneath(path.parent, (path.name,), max_bytes=max_bytes - used, require_dir_fd=True)
        except ValueError as exc:
            if str(exc) != "managed file exceeds read limit":
                raise
            used = max_bytes
            raise _AuthorityScanLimit() from exc
        used += len(value or b"")
        return value

    # LLM: 新账、旧墓碑及正文共用同一条数硬限，重复代次重读也累计，不凭去重后的条数少计。
    # 函数用途: 在解码下一个权威记录前扣除预算。
    def charge_record():
        nonlocal scanned
        if scanned >= max_records:
            raise _AuthorityScanLimit()
        scanned += 1

    for attempt in range(3):
        stamps = {path: _scan_stamp(path) for path in controls}
        records, blocked, root, partial, cursor = [], None, None, False, None
        try:
            root = store.manifest(_read=read)
            blocked = store.tombstones.load(_read=read, on_record=charge_record)
            if root is None:
                stamps[memory.path] = _scan_stamp(memory.path)
                body = read(memory.path)
                for line in (body or b"").splitlines():
                    if not line.strip():
                        continue
                    charge_record()
                    record = _memory_record_from_obj(json.loads(line))
                    require_maintenance_event(record)
                    records.append(_with_legacy_identity(record))
            else:
                for number, segment in enumerate(root["segments"]):
                    cursor = [number, 0]
                    if scanned >= max_records:
                        raise _AuthorityScanLimit()
                    body = read(memory.path.parent / segment["path"])
                    if body is None or hashlib.sha256(body).hexdigest() != segment["sha256"]:
                        raise ValueError("MEMORY_GENERATION_UNREADABLE")
                    lines = [line for line in body.splitlines() if line]
                    if len(lines) != segment["count"]:
                        raise ValueError("MEMORY_GENERATION_UNREADABLE")
                    for offset, line in enumerate(lines):
                        cursor = [number, offset]
                        charge_record()
                        record = _memory_record_from_obj(json.loads(line))
                        require_maintenance_event(record)
                        records.append(record)
                cursor = None
        except _AuthorityScanLimit:
            partial = True
            if root is None or blocked is None:
                records = []
        except (OSError, ValueError) as exc:
            if isinstance(exc, (*_NATIVE_MEMORY_ERRORS, NoFollowPathError)):
                raise
            if not any(_scan_stamp(path) != stamp for path, stamp in stamps.items()):
                raise
            if attempt == 2:
                raise ValueError("MEMORY_GENERATION_CHANGED") from exc
            continue
        if any(_scan_stamp(path) != stamp for path, stamp in stamps.items()):
            if attempt < 2 and used < max_bytes and scanned < max_records:
                continue
            records, partial = [], True
        if blocked is not None:
            records = _materialize_memory_events([*records, *blocked.values()])
            records = [r for r in records if not store.tombstones.excludes(r.entry_id,
                memory_content_hash(r.content), records=blocked)]
        break
    return records, {"partial": partial, "scanned_records": scanned, "scanned_bytes": used,
        "max_records": max_records, "max_bytes": max_bytes,
        "continuation": {"operation_id": root["operation_id"], "cursor": cursor} if root and cursor else None}
