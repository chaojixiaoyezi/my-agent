# LLM: C固定formal锁出生与实际native先传播，Root只本地末叶；原普通索引降级/删除partial保留，默认旧写调用不变。

"""Indexing and search helpers mixed into JsonlMemory."""

# LLM: LocalStore/FTS and vector data are rebuildable projections; every hit must be revalidated against active JSONL.
# 模块用途: 为正式长期记忆提供索引写入、检索投影和删除后派生正文清理。

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
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
from ..runtime_errors import runtime_error_report

# 原scope末核与实际embedding运输callee会抛的类；只作异常传播，不是码名/权限分类表。
_NATIVE_MEMORY_ERRORS = (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
    LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable,
    RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
    ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError)

if TYPE_CHECKING:
    from ..local_storage import LocalSearchResult
    from .jsonl import MemoryRecord


# LLM: 已提交删除不能因派生清理失败被误报完整成功；调用方保留墓碑及候选以便重试。
# 类用途: 区分正式删除之后仍欠隐私清理的失败。
class RemovalCleanupError(RuntimeError):
    # LLM: 只有本轮正式删除提交后的必要清理可标True；历史恢复不得据已有墓碑冒充本轮写入。
    # 函数用途: 将真实删除提交事实和未完成清理责任分开传递给工具回执。
    def __init__(self, message: str, *, formal_memory_changed: bool = False):
        super().__init__(message)
        self.formal_memory_changed = formal_memory_changed


# LLM: 仅在正式删除提交之后包住必需清理；本轮提交事实由调用点给定，历史恢复固定为False。
# 函数用途: 候选、日志或派生层清理失败都保留准确的待清理回执，非删除异常不变。
# LLM: 实际canonical source/physical native先bare raise，不能包成可恢复清理结果；无实例共享入口只新增该负向分支，普通错误/partial原样。
# 函数用途: 保留删除后真实未结和原普通清理回执，拒绝不冒充成功。
@contextmanager
def removal_cleanup_phase(*, formal_memory_changed: bool, enabled: bool = True):
    try:
        yield
    except Exception as exc:
        if isinstance(exc, _NATIVE_MEMORY_ERRORS):
            raise
        if not enabled:
            raise
        raise RemovalCleanupError('REMOVE_CLEANUP_PENDING', formal_memory_changed=formal_memory_changed) from exc


# LLM: 此 mixin 只管理可重建检索索引，不能拥有 active memory 状态或写入裁决。
# 类用途: 集中 JsonlMemory 的搜索索引和删除后派生层清理。
# LLM: C只改原client可达末口与native传播；索引仍派生，正式锁/扫描/SQL参数准备留Root外。
# 类用途: 同步原搜索投影及删除清理，不产生另一记忆权威。
class JsonlMemoryIndexMixin:
    """Private LocalStore/search helpers for JsonlMemory."""

    # LLM: 权威 JSONL 搜索只读当前 materialized active records，不读取 ops、daily 或候选。
    # 函数用途: 在正式长期记忆当前态中执行关键词搜索。
    def _search_jsonl(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """Search the authority with the same normalization used by mirror fallback."""
        from .jsonl import _search_memory_records

        return _search_memory_records(self.all(), query, top_k)

    # LLM: 索引异常只能降级到正式 JSONL，不能让索引结果成为独立事实。
    # 函数用途: 尝试 LocalStore 检索并丢弃诊断的便捷入口。
    def _search_local_store(self, query: str, top_k: int) -> list[MemoryRecord]:
        """Try LocalStore search and return an empty list on index errors."""
        records, _load_errors = self._search_local_store_report(query, top_k)
        return records

    # LLM: 每个 hit 都按 memory path、active entry ID 和当前正文复核，阻止旧索引复活删除/替换内容。
    # 函数用途: 查询 LocalStore 并同时返回结构化读取错误。
    # LLM: C连接出生拒绝不可降级到关键词后付费；真实native同对象抛，普通索引错误仍原报告。
    # 函数用途: 读原LocalStore并按active JSONL核验召回。
    def _search_local_store_report(self, query: str, top_k: int) -> tuple[list[MemoryRecord], list[dict]]:
        """Try LocalStore search while preserving index read/search diagnostics."""
        if not self.local_store:
            return [], []
        try:
            hits = self.local_store.search(
                query,
                limit=max(top_k * 4, top_k + 8),
                source_type="memory",
            )
        except Exception as exc:
            if getattr(self, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            return [], [runtime_error_report(exc, context="memory_store.local_store.search")]
        scoped_hits = [hit for hit in hits if self._hit_matches_memory_path(hit)]
        active = {record.entry_id: record for record in self.all()}
        records: list[MemoryRecord] = []
        for hit in scoped_hits:
            indexed = self._memory_from_hit(hit)
            current = active.get(indexed.entry_id)
            if current is None or current.content != indexed.content:
                continue
            records.append(current)
            if len(records) >= top_k:
                break
        return records, []

    # LLM: 派生索引写失败不回滚已提交 JSONL，但必须留下可观察 debug 诊断。
    # 函数用途: 尽力为一条已提交正式记忆建立文本索引。
    # LLM: C真实native必须在_index_vector下一付费前抛，普通索引失败仍原debug，不包装/换来源。
    # 函数用途: 为已提交正式事实建立原文本索引并保partial。
    def _try_index_record(self, record: MemoryRecord) -> None:
        """Best-effort index write; JSONL remains the authoritative fact stream."""
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception as exc:
            if getattr(self, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断;
            # 但失败必须可观测,否则索引静默腐化、检索悄悄变差(体检实锤)。
            logging.getLogger(__name__).debug("memory index write failed", exc_info=True)
            return

    # LLM: 只有权威锁内确认仍是active的精确版本才写派生正文；与正式删除共享锁，避免延迟写回复活。
    # 函数用途: 核对当前事实后将一条正式记忆写入LocalStore/FTS。
    # LLM: C仅原formal锁FD出生进门，原版本检查/领域锁等待在外，原LocalStore负责其准确末writer。
    # 函数用途: 在原删除互斥锁内写同一LocalStore投影。
    def _index_record(self, record: MemoryRecord) -> None:
        from ..common.json_io import locked_json_path

        if not self.local_store:
            return
        with locked_json_path(self.path, **({} if not getattr(self, "coordinator_io", False) else {"birth_commit": _coordinator_index_write})):
            if self._projection_is_current(record):
                self._write_record_projection(record)

    # LLM: 调用方必须持权威锁至派生写入完成；只核对完整正式状态，坏读拒绝，不修改事实或候选。
    # 函数用途: 拒绝已删除、被替换或scope变化的旧索引快照。
    def _projection_is_current(self, record: MemoryRecord) -> bool:
        return any(row.entry_id == record.entry_id and row.version == record.version
                   and row.content == record.content and row.attributes == record.attributes
                   for row in self._maintenance_records())

    # LLM: 仅由_index_record在权威锁内调用，保留entry/version/path供召回二次核验。
    # 函数用途: 持有删除互斥边界时写入可重建的LocalStore索引。
    def _write_record_projection(self, record: MemoryRecord) -> None:
        self.local_store.upsert_record(
            source_type="memory",
            source_id=self._source_id(record),
            title=f"{record.kind}:{record.role}",
            content=record.content,
            metadata={
                "role": record.role,
                "kind": record.kind,
                "tags": record.tags or [],
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "expires_at": record.expires_at,
                "entry_id": record.entry_id,
                "version": record.version,
                "source": record.source,
                "attributes": record.attributes or {},
                "memory_path": self._memory_path_key(),
            },
        )

    # LLM: 新记录用 entry_id；legacy 无 ID 时只用完整记录 hash 生成稳定 projection key。
    # 函数用途: 计算 LocalStore 中一条记忆的稳定 source_id。
    def _source_id(self, record: MemoryRecord) -> str:
        """Generate the stable LocalStore source_id for one memory record."""
        if record.entry_id:
            return record.entry_id
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

    # LLM: 同一 LocalStore 可能含多个 owner/path projection，命中必须精确属于当前权威文件。
    # 函数用途: 判断检索命中是否属于当前 JsonlMemory 路径。
    def _hit_matches_memory_path(self, hit: LocalSearchResult) -> bool:
        return str(hit.metadata.get("memory_path") or "") == self._memory_path_key()

    # LLM: 路径键只用于 projection 隔离；解析失败时保留原路径字符串而不扩大范围。
    # 函数用途: 返回当前正式记忆文件的索引作用域键。
    def _memory_path_key(self) -> str:
        try:
            return str(self.path.resolve())
        except Exception:
            return str(self.path)

    # LLM: 从索引恢复的对象仍必须由调用方与 active authority 比对，不能直接用于 Prompt。
    # 函数用途: 把一个 LocalStore 命中转换成待复核 MemoryRecord。
    def _memory_from_hit(self, hit: LocalSearchResult) -> MemoryRecord:
        """Rebuild a MemoryRecord from a LocalStore search hit."""
        from .jsonl import MemoryRecord

        metadata = hit.metadata
        tags = metadata.get("tags")
        return MemoryRecord(
            role=str(metadata.get("role") or "unknown"),
            content=hit.content,
            kind=str(metadata.get("kind") or "dialogue"),
            tags=tags if isinstance(tags, list) else [],
            created_at=float(metadata.get("created_at") or hit.created_at),
            entry_id=str(metadata.get("entry_id") or ""),
            version=int(metadata.get("version") or 1),
            source=str(metadata.get("source") or ""),
            attributes=(
                dict(metadata.get("attributes"))
                if isinstance(metadata.get("attributes"), dict)
                else None
            ),
            updated_at=float(metadata.get("updated_at") or hit.updated_at or 0.0),
            expires_at=float(metadata.get("expires_at") or 0.0),
        )

    # LLM: 权威提交后只能同步派生层；remove 必须同时清向量、FTS 和外置正文。
    # LLM: 删除ID合并清理，孤儿文本向量全库扫描每批至多一次；失败保正式已提交回执。
    # 函数用途: 批量同步投影并一次清理删除后的孤儿向量。
    def _after_commit_indexes(self, records: list[MemoryRecord]) -> None:
        removed = {record.entry_id for record in records if record.action == "remove"}
        if removed:
            try:
                _purge_removed_indexes(self, removed)
            except RemovalCleanupError as exc:
                raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True) from exc
        for record in records:
            if record.action == "remove":
                continue
            self._try_index_record(record)
            self._index_vector(record)

    # LLM: 仅限本轮正式提交后的同步链；历史恢复不经过这里，不虚增本轮变更事实。
    # 函数用途: 保留删除已提交但隐私清理未完成的双层回执。
    def _purge_committed_indexes(self, entry_id: str) -> None:
        try:
            self.purge_removed_indexes(entry_id)
        except RemovalCleanupError as exc:
            raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True) from exc

    # LLM: 正式提交和崩溃恢复均调用；禁用embedding也不免除旧向量正文清理，任何失败必须可重试。
    # 函数用途: 严格清理当前owner精确条目的向量、SQLite/FTS和外置正文。
    def purge_removed_indexes(self, entry_id: str) -> None:
        _purge_removed_indexes(self, entry_id)

    # LLM: LocalStore 只是派生索引，删除失败不能让它重新成为权威；正文文件先清空再移除。
    # 函数用途: 尽力清除某条长期记忆的 SQLite/FTS 和外置内容文件。
    def _remove_local_index(self, entry_id: str) -> None:
        if not self.local_store or not entry_id:
            return
        try:
            self.local_store.delete_record(self.local_store.make_record_id("memory", entry_id))
        except Exception:
            pass

    # LLM: 记忆正文删除前同步擦除 remember 的结构化账本载荷；方法失败时权威文件保持未改，可安全重试。
    # 函数用途: 精确清除当前 owner 工具审计/幂等记录里的被删正文，而不删除操作身份。
    def _redact_local_tool_ledgers(self, contents: set[str]) -> None:
        if not self.local_store or not contents:
            return
        redact = getattr(self.local_store, "redact_tool_ledger_content", None)
        if callable(redact):
            redact(tool="remember", contents=contents)


# LLM: 删除与恢复复用现有无embedder回收；持正式库锁计算全部活跃index_text，覆盖所有模型指纹且不误删活跃同文键。
# 函数用途: 清除孤儿文本向量，真实读写错误转换为可重试删除清理失败。
# LLM: C原formal锁出生复用当次scope，keep扫描外置，native由原callee抛；普通error仍原CleanupPending。
# 函数用途: 回收原孤儿文本缓存，不能把未写盘当零成功。
def _purge_text_vector_orphans(memory) -> int:
    from ..common.json_io import locked_json_path

    with locked_json_path(memory.path, **({} if not getattr(memory, "coordinator_io", False) else {"birth_commit": _coordinator_index_write})):
        dropped, error = memory.reclaim_text_cache_orphans()
        if error:
            raise RemovalCleanupError("REMOVE_CLEANUP_PENDING")
        return dropped


# LLM: 同一删除清理流程供正式提交和恢复复用；禁用embedder仍清磁盘派生层，异常一律保留待重试事实。
# 函数用途: 按记忆身份清除向量、孤儿文本缓存、SQLite/FTS及其外置正文。
# LLM: C无embedder回退也复用原向量缓存字段/73策略；真实native优先，原vector→cache→SQLite正文删除顺序和普通pending保持。
# LLM: 单ID恢复与批量正式删除共用；先清每个ID，再只扫描一次孤儿缓存。
# 函数用途: 清除精确记录集合的派生正文，不重复全库扫描。
def _purge_removed_indexes(memory, entry_id: str | set[str]) -> None:
    from ..retrieval.vector_store import VectorStore

    try:
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


# LLM: 只消费当前原global scope，准确formal锁FD出生入门，等待/扫描及派生算法均在Root外。
# 函数用途: 为C索引桥复用原同步末门，不保存或制造来源。
def _coordinator_index_write(operation):
    return execute_global_write(operation, require_scope=True)
