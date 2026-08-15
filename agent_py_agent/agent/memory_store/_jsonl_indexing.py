
"""Indexing and search helpers mixed into JsonlMemory."""

# LLM: LocalStore/FTS and vector data are rebuildable projections; every hit must be revalidated against active JSONL.
# 模块用途: 为正式长期记忆提供索引写入、检索投影和删除后派生正文清理。

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ..local_storage import LocalSearchResult
    from .jsonl import MemoryRecord


# LLM: 此 mixin 只管理可重建检索索引，不能拥有 active memory 状态或写入裁决。
# 类用途: 集中 JsonlMemory 的搜索索引和删除后派生层清理。
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
    def _try_index_record(self, record: MemoryRecord) -> None:
        """Best-effort index write; JSONL remains the authoritative fact stream."""
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception:
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断;
            # 但失败必须可观测,否则索引静默腐化、检索悄悄变差(体检实锤)。
            logging.getLogger(__name__).debug("memory index write failed", exc_info=True)
            return

    # LLM: 这里只写可重建 projection，metadata 必须保留 entry/version/path 供召回二次核验。
    # 函数用途: 将一条正式记忆 upsert 到 LocalStore/FTS。
    def _index_record(self, record: MemoryRecord) -> None:
        """Write one MemoryRecord into LocalStore when an index is configured."""
        if not self.local_store:
            return
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
    # 函数用途: 把一批已提交记忆同步到搜索索引，或清除被删除条目。
    def _after_commit_indexes(self, records: list[MemoryRecord]) -> None:
        for record in records:
            if record.action == "remove":
                self._remove_vector(record.entry_id)
                self._remove_local_index(record.entry_id)
                continue
            self._try_index_record(record)
            self._index_vector(record)

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
