"""Indexing and search helpers mixed into JsonlMemory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_store import LocalSearchResult
    from .jsonl import MemoryRecord


class JsonlMemoryIndexMixin:
    """Private LocalStore/search helpers for the JsonlMemory facade."""

    def _search_jsonl(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """Search the JSONL fact stream with simple keyword scoring."""
        query_terms = {term.lower() for term in query.split() if term.strip()}
        scored: list[tuple[int, float, MemoryRecord]] = []
        for rec in self.all():
            text = rec.content.lower()
            score = sum(1 for term in query_terms if term in text)
            if query and query.lower() in text:
                score += 3
            if score > 0 or not query_terms:
                scored.append((score, rec.created_at, rec))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in scored[:top_k]]

    def _search_local_store(self, query: str, top_k: int) -> list[MemoryRecord]:
        """Try LocalStore search and return an empty list on index errors."""
        if not self.local_store:
            return []
        try:
            hits = self.local_store.search(query, limit=top_k, source_type="memory")
        except Exception:
            return []
        return [self._memory_from_hit(hit) for hit in hits]

    def _try_index_record(self, record: MemoryRecord) -> None:
        """Best-effort index write; JSONL remains the authoritative fact stream."""
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception:
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断。
            return

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
            },
        )

    def _source_id(self, record: MemoryRecord) -> str:
        """Generate the stable LocalStore source_id for one memory record."""
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

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
        )
