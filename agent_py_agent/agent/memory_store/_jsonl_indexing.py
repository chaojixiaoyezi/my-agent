# LLM: Memory store module; keep JSONL storage and indexing formats stable.
# 模块用途: 提供底层记忆 JSONL 存储、索引和读取能力。

"""Indexing and search helpers mixed into JsonlMemory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_store import LocalSearchResult
    from .jsonl import MemoryRecord


# LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 JsonlMemoryIndexMixin 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 JsonlMemoryIndexMixin 的状态和协作方法，作为当前模块对外复用的领域对象。
class JsonlMemoryIndexMixin:
    """Private LocalStore/search helpers for the JsonlMemory facade."""

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _search_jsonl 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 search jsonl 的候选结果，并按参数完成筛选、排序或数量限制。
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

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _search_local_store 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 search local store 的候选结果，并按参数完成筛选、排序或数量限制。
    def _search_local_store(self, query: str, top_k: int) -> list[MemoryRecord]:
        """Try LocalStore search and return an empty list on index errors."""
        if not self.local_store:
            return []
        try:
            hits = self.local_store.search(query, limit=top_k, source_type="memory")
        except Exception:
            return []
        return [self._memory_from_hit(hit) for hit in hits]

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _try_index_record 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 try index record 在当前模块中的核心转换或协调步骤，衔接 memory store 以 JSONL 记录和本地索引作为事实来源。
    def _try_index_record(self, record: MemoryRecord) -> None:
        """Best-effort index write; JSONL remains the authoritative fact stream."""
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception:
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断。
            return

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _index_record 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 index record 相关记录，集中处理目标路径、格式化和状态更新。
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

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _source_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 source id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    def _source_id(self, record: MemoryRecord) -> str:
        """Generate the stable LocalStore source_id for one memory record."""
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 _memory_from_hit 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 memory from hit 在当前模块中的核心转换或协调步骤，衔接 memory store 以 JSONL 记录和本地索引作为事实来源。
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
