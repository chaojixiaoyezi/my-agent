
"""Indexing and search helpers mixed into JsonlMemory."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.json_io import locked_json_path, write_text_file_atomic_unlocked
from ..io import append_jsonl
from ..runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ..local_storage import LocalSearchResult
    from .jsonl import MemoryRecord


# LLM: 此 mixin 只管理可重建索引和 daily mirror，不能拥有 active memory 状态或写入裁决。
# 类用途: 集中 JsonlMemory 的搜索索引、每日镜像同步和删除后派生层清理。
class JsonlMemoryIndexMixin:
    """Private LocalStore/search and derived mirror helpers for JsonlMemory."""

    def _search_jsonl(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """Search the authority with the same normalization used by mirror fallback."""
        from .jsonl import _search_memory_records

        return _search_memory_records(self.all(), query, top_k)

    def _search_local_store(self, query: str, top_k: int) -> list[MemoryRecord]:
        """Try LocalStore search and return an empty list on index errors."""
        records, _load_errors = self._search_local_store_report(query, top_k)
        return records

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

    def _source_id(self, record: MemoryRecord) -> str:
        """Generate the stable LocalStore source_id for one memory record."""
        if record.entry_id:
            return record.entry_id
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

    def _hit_matches_memory_path(self, hit: LocalSearchResult) -> bool:
        return str(hit.metadata.get("memory_path") or "") == self._memory_path_key()

    def _memory_path_key(self) -> str:
        try:
            return str(self.path.resolve())
        except Exception:
            return str(self.path)

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

    # LLM: daily 是派生镜像；hard delete 时必须移除该 entry 的全部历史正文后再写无正文 tombstone。
    # 函数用途: 删除每日镜像里属于指定长期记忆的所有旧版本。
    def _purge_daily_mirror_entries(self, entry_ids: set[str]) -> None:
        if not entry_ids:
            return
        from .jsonl import _record_payload

        for path in self._daily_mirror_files():
            with locked_json_path(path):
                retained = [
                    record
                    for record in self._read_memory_events(path)
                    if record.entry_id not in entry_ids
                ]
                text = "".join(
                    json.dumps(_record_payload(record), ensure_ascii=False) + "\n"
                    for record in retained
                )
                write_text_file_atomic_unlocked(path, text)

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

    # LLM: daily mirror 只复制权威事件，不参与 ID、版本或冲突裁决。
    # 函数用途: 将已提交的一批记忆事件追加到当前 owner 的每日镜像。
    def _append_daily_mirrors(self, records: list[MemoryRecord]) -> None:
        for record in records:
            self._append_daily_mirror(record)

    # LLM: 镜像日期沿用事件 created_at，remove 正文必须保持为空。
    # 函数用途: 将一条权威记忆事件写入所有配置的每日镜像目录。
    def _append_daily_mirror(self, record: MemoryRecord) -> None:
        if not self.daily_mirror_dirs:
            return
        from .jsonl import _record_payload

        for daily_dir in self.daily_mirror_dirs:
            path = daily_dir / f"{date.fromtimestamp(record.created_at).isoformat()}.jsonl"
            append_jsonl(path, _record_payload(record))

    # LLM: 只枚举当前 JsonlMemory 显式配置的 owner mirror dirs，禁止扩大到仓库或其他 owner。
    # 函数用途: 列出当前 owner 的每日记忆镜像文件。
    def _daily_mirror_files(self) -> list[Path]:
        files: list[Path] = []
        for daily_dir in self.daily_mirror_dirs:
            if daily_dir.exists():
                files.extend(path for path in daily_dir.glob("*.jsonl") if path.is_file())
        return sorted(set(files))

    # LLM: daily 召回必须复用 authority 的 materialize/search 语义，不能另写文本匹配规则。
    # 函数用途: 在每日镜像中按统一规则补充搜索结果。
    def _search_daily_mirror(self, query: str, top_k: int) -> list[MemoryRecord]:
        from .jsonl import _materialize_memory_events, _search_memory_records

        events: list[MemoryRecord] = []
        for path in self._daily_mirror_files():
            events.extend(self._read_memory_events(path))
        records = _materialize_memory_events(events)
        return _search_memory_records(records, query, top_k)
