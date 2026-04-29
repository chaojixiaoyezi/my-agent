from __future__ import annotations

"""本地记忆模块。

记忆现在采用“双轨落盘”：
- JSONL 仍然是原始记忆流水，每行一条记录，方便直接打开查看。
- 可选 LocalStore 会把同一条记忆索引到 SQLite + FTS5，方便更快检索。

这样做的好处是：简单文件还在，后续要做本地检索、同步、迁移或审计时，
也已经有结构化账本可以接。
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .file_io import append_jsonl

if TYPE_CHECKING:
    from .local_store import LocalSearchResult, LocalStore


@dataclass
class MemoryRecord:
    """一条记忆记录。"""

    role: str
    content: str
    kind: str = "dialogue"
    tags: list[str] | None = None
    created_at: float = 0.0

    def to_json(self) -> str:
        """把当前记忆转成一行 JSON 文本，方便写入 JSONL 文件。"""

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(asdict(self), ensure_ascii=False)


class JsonlMemory:
    """基于 JSONL 文件的轻量记忆存储。

    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。
    """

    def __init__(self, path: str | Path, local_store: "LocalStore | None" = None):
        self.path = Path(path)
        self.local_store = local_store
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """追加一条记忆到磁盘。"""

        record = MemoryRecord(
            role=role,
            content=content,
            kind=kind,
            tags=tags or [],
            created_at=time.time(),
        )
        append_jsonl(self.path, asdict(record))
        self._try_index_record(record)
        return record

    def all(self) -> list[MemoryRecord]:
        """读取全部记忆记录。"""

        if not self.path.exists():
            return []
        records: list[MemoryRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            records.append(MemoryRecord(**obj))
        return records

    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """搜索记忆。

        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，退回 JSONL 关键词检索。
        """

        indexed = self._search_local_store(query, top_k)
        if indexed:
            return indexed
        return self._search_jsonl(query, top_k)

    def index_all(self) -> int:
        """把现有 JSONL 记忆补建到 LocalStore。

        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        """

        if not self.local_store:
            return 0
        count = 0
        for record in self.all():
            self._index_record(record)
            count += 1
        return count

    def _search_jsonl(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
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
        if not self.local_store:
            return []
        try:
            hits = self.local_store.search(query, limit=top_k, source_type="memory")
        except Exception:
            return []
        return [self._memory_from_hit(hit) for hit in hits]

    def _try_index_record(self, record: MemoryRecord) -> None:
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception:
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断。
            return

    def _index_record(self, record: MemoryRecord) -> None:
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
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

    def _memory_from_hit(self, hit: "LocalSearchResult") -> MemoryRecord:
        metadata = hit.metadata
        tags = metadata.get("tags")
        return MemoryRecord(
            role=str(metadata.get("role") or "unknown"),
            content=hit.content,
            kind=str(metadata.get("kind") or "dialogue"),
            tags=tags if isinstance(tags, list) else [],
            created_at=float(metadata.get("created_at") or hit.created_at),
        )
