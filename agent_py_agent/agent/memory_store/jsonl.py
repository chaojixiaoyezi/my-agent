
from __future__ import annotations

"""本模块提供 JSONL 记忆事实流水，并可选同步索引到 LocalStore 方便搜索。

新手说明:
记忆现在采用'双轨落盘'：
- JSONL 仍然是原始记忆流水，每行一条记录，方便直接打开查看。
- 可选 LocalStore 会把同一条记忆索引到 SQLite + FTS5，方便更快检索。

这样做的好处是：简单文件还在，后续要做本地检索、同步、迁移或审计时，
也已经有结构化账本可以接。
"""

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..common.text_norm import fold_key, nfc
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange


def _memory_record_from_obj(obj: dict) -> MemoryRecord | None:
    """dict → MemoryRecord:过滤未知顶层字段(旧版本读新字段记录不崩),构造失败返回 None(跳过坏记录)。"""
    known = set(MemoryRecord.__dataclass_fields__)
    try:
        return MemoryRecord(**{k: v for k, v in obj.items() if k in known})
    except (TypeError, ValueError):
        return None

from ..io import append_jsonl
from ._jsonl_indexing import JsonlMemoryIndexMixin

if TYPE_CHECKING:
    from ..local_storage import LocalSearchResult, LocalStore
    from ..user_space.owner_quota import OwnerQuotaEnforcer


@contextmanager
def _quota_admission(
    enforcer: OwnerQuotaEnforcer | None,
) -> Iterator[OwnerQuotaAdmission | None]:
    if enforcer is None:
        yield None
        return
    with enforcer.admission() as admission:
        yield admission


def _check_memory_quota(
    admission: OwnerQuotaAdmission | None,
    memory: JsonlMemory,
    records: list[MemoryRecord],
    next_text: str,
) -> None:
    if admission is None:
        return
    changes = [OwnerQuotaChange(memory.path, len(next_text.encode("utf-8")))]
    for record in records:
        payload_size = len(
            (json.dumps(_record_payload(record), ensure_ascii=False) + "\n").encode("utf-8")
        )
        for daily_dir in memory.daily_mirror_dirs:
            path = daily_dir / f"{date.fromtimestamp(record.created_at).isoformat()}.jsonl"
            changes.append(OwnerQuotaChange(path, payload_size, append=True))
    admission.check(changes)


@dataclass
class MemoryRecord:
    """表示一条已经准备写入 JSONL 的记忆事实。

    新手说明:
    MemoryRecord 是最小记忆单位。它记录'谁说的、说了什么、属于哪类、有哪些标签、什么时候创建'。

    字段说明:
    role: 记忆来源角色，例如 user、assistant、tool、system。
    content: 记忆正文。
    kind: 记忆类型，默认 dialogue；也可以是 rule、summary、note 等上层定义。
    tags: 标签列表，用于粗分类和后续检索。
    created_at: Unix 时间戳；为 0 时写 JSON 前会自动补当前时间。"""

    role: str
    content: str
    kind: str = "dialogue"
    tags: list[str] | None = None
    created_at: float = 0.0
    # 开放结构化扩展位(P5-2):上层语义字段挂这里,例如教训记忆的
    # trigger_conditions(结构化触发条件,决策端按字段匹配提权,不解析正文)。
    # 旧 JSONL 行没有此键,读取时按空 dict 兼容;空 dict 不写入 JSON 行(省字节)。
    attributes: dict | None = None
    # Durable identity and operation metadata.  Old JSONL rows omit these
    # fields; the materializer derives a deterministic legacy id for them.
    entry_id: str = ""
    action: str = "add"
    version: int = 1
    source: str = ""
    updated_at: float = 0.0
    expires_at: float = 0.0

    def to_json(self) -> str:
        """把当前记忆转成一行 UTF-8 JSON 字符串。

        新手说明:
        JSONL 文件是一行一个 JSON。这个方法负责把 MemoryRecord 变成可以直接追加到文件的一行文本。

        这个方法没有输入参数，只读取当前 record 字段。

        返回说明:
        返回 JSON 字符串，不包含换行符。

        副作用说明:
        如果 created_at 还是 0，会把它改成当前时间；这个方法本身不写文件。"""

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(_record_payload(self), ensure_ascii=False)


class JsonlMemory(JsonlMemoryIndexMixin):
    """JSONL-backed memory store with optional LocalStore indexing.

    新手说明:
    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 可选 LocalStore；有它时 add/index_all/search 可以同步索引和优先搜索索引。"""

    def __init__(
        self,
        path: str | Path,
        local_store: LocalStore | None = None,
        daily_mirror_dir: str | Path | None = None,
        *,
        embedder: object | None = None,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ):
        """初始化 JSONL 记忆文件位置，并确保父目录存在。

        新手说明:
        创建 JsonlMemory 时只准备文件路径和可选索引对象，不会读取全部记忆。

        path: JSONL 文件路径。
        local_store: 可选 LocalStore，用于索引和搜索；为空时仍可正常写 JSONL。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。"""
        self.path = Path(path)
        self.local_store = local_store
        self.daily_mirror_dirs = _daily_mirror_dirs(daily_mirror_dir)
        self.daily_mirror_dir = self.daily_mirror_dirs[0] if self.daily_mirror_dirs else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder  # 配了 → 记忆召回加一路语义向量(检索拓宽 #1);None → 纯关键词
        self._vector_store_cache = None
        self.quota_enforcer = quota_enforcer

    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
        source: str = "",
        expires_at: float = 0.0,
    ) -> MemoryRecord:
        """追加一条记忆到 JSONL，并尽力同步索引到 LocalStore。

        新手说明:
        这是写入记忆的主入口。先构造 MemoryRecord，再写入 JSONL，最后尝试写索引。
        索引失败不会让记忆写入失败，因为 JSONL 才是主事实流水。
        需要带结构化扩展字段（attributes，如教训的 trigger_conditions）时，
        自行构造 MemoryRecord 走 add_record。

        role: 记忆来源角色，例如 user 或 assistant。
        content: 记忆正文。
        kind: 记忆类型，默认 dialogue。
        tags: 可选标签列表；None 会变成空列表。

        返回说明:
        返回刚写入的 MemoryRecord。

        副作用说明:
        会追加写入 JSONL 文件；如果 local_store 存在，会尝试 upsert 一条索引记录。"""

        return self.add_record(
            MemoryRecord(
                role=role,
                content=content,
                kind=kind,
                tags=tags or [],
                created_at=time.time(),
                source=source,
                expires_at=float(expires_at or 0.0),
            )
        )

    # LLM: 记忆写入的底层唯一落盘口(add 是它的便捷封装)。接受完整 MemoryRecord,
    #   attributes 等扩展字段(P5-2 trigger_conditions)由调用方在 record 上携带。
    #   副作用:JSONL 追加 + daily mirror + LocalStore 索引(索引失败不打断)。
    # 函数用途: 想写带结构化扩展字段的记忆时,构造好 MemoryRecord 从这里进。
    def add_record(self, record: MemoryRecord) -> MemoryRecord:
        record = _normalized_new_record(record)
        self._commit_events([record])
        return record

    def replace(
        self,
        entry_id: str,
        content: str,
        *,
        kind: str | None = None,
        tags: list[str] | None = None,
        source: str = "",
        expires_at: float | None = None,
        expected_version: int | None = None,
    ) -> MemoryRecord:
        """Replace one active entry by stable id and append a versioned event."""

        return self.apply_batch(
            [
                {
                    "action": "replace",
                    "entry_id": entry_id,
                    "content": content,
                    "kind": kind,
                    "tags": tags,
                    "source": source,
                    "expires_at": expires_at,
                    "expected_version": expected_version,
                }
            ]
        )[0]

    def remove(
        self,
        entry_id: str,
        *,
        source: str = "",
        expected_version: int | None = None,
    ) -> MemoryRecord:
        """Tombstone one active entry by stable id."""

        return self.apply_batch(
            [
                {
                    "action": "remove",
                    "entry_id": entry_id,
                    "source": source,
                    "expected_version": expected_version,
                }
            ]
        )[0]

    def apply_batch(self, operations: list[dict[str, object]]) -> list[MemoryRecord]:
        """Validate and append a memory mutation batch as one locked commit.

        The JSONL file remains the sole authority.  All operations are applied
        to an in-memory copy while the file lock is held; a bad operation
        aborts before any bytes are replaced.
        """

        if not operations:
            raise ValueError("memory operations list is empty")
        committed: list[MemoryRecord]
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                current_events = self._read_memory_events(self.path)
                working = _materialize_memory_events(current_events, include_expired=True)
                active = {record.entry_id: record for record in working}
                committed = _prepare_memory_operations(operations, active)
                payloads = [_record_payload(record) for record in committed]
                existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
                suffix = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in payloads)
                prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
                next_text = prefix + suffix
                _check_memory_quota(admission, self, committed, next_text)
                write_text_file_atomic_unlocked(self.path, next_text)
            self._append_daily_mirrors(committed)
        self._after_commit_indexes(committed)
        return committed

    def _commit_events(self, records: list[MemoryRecord]) -> None:
        payloads = [_record_payload(record) for record in records]
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
                suffix = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in payloads)
                prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
                next_text = prefix + suffix
                _check_memory_quota(admission, self, records, next_text)
                write_text_file_atomic_unlocked(self.path, next_text)
            self._append_daily_mirrors(records)
        self._after_commit_indexes(records)

    def _append_daily_mirrors(self, records: list[MemoryRecord]) -> None:
        for record in records:
            self._append_daily_mirror(record)

    def _after_commit_indexes(self, records: list[MemoryRecord]) -> None:
        for record in records:
            if record.action == "remove":
                self._remove_vector(record.entry_id)
                continue
            self._try_index_record(record)
            self._index_vector(record)

    def _vector_store(self):
        """本地 per-owner 向量库(memory_vectors.json,在 owner home 内)。无 embedder 返回 None。

        只用本地文件,绝不碰共享向量库(零外部依赖、按 owner 天然隔离)。懒建缓存。
        """
        if self._embedder is None:
            return None
        if self._vector_store_cache is None:
            from ..retrieval.vector_store import VectorStore

            self._vector_store_cache = VectorStore(self.path.parent / "memory_vectors.json")
        return self._vector_store_cache

    def _index_vector(self, record: MemoryRecord) -> None:
        store = self._vector_store()
        if store is None:
            return
        try:
            vector = self._embedder.embed([record.content])[0]
            store.upsert(_record_vec_id(record), vector, text=record.content, metadata=_record_payload(record))
        except Exception:
            pass  # 向量索引失败不打断记忆写入(JSONL 才是事实源)

    def _remove_vector(self, entry_id: str) -> None:
        store = self._vector_store()
        if store is None or not entry_id:
            return
        try:
            store.remove(entry_id)
        except Exception:
            pass

    def _semantic_records(self, query: str, top_k: int) -> list[MemoryRecord]:
        store = self._vector_store()
        if store is None:
            return []
        try:
            query_vec = self._embedder.embed([query])[0]
            hits = store.search(query_vec, top_k=top_k)
        except Exception:
            return []
        active = {record.entry_id: record for record in self.all()}
        records: list[MemoryRecord] = []
        for hit in hits:
            record = _memory_record_from_obj(hit.metadata)
            if record is None:
                continue
            record = _with_legacy_identity(record)
            current = active.get(record.entry_id)
            if current is not None and current.content == record.content:
                records.append(current)
        return records

    def _append_daily_mirror(self, record: MemoryRecord) -> None:
        if not self.daily_mirror_dirs:
            return
        for daily_dir in self.daily_mirror_dirs:
            path = daily_dir / f"{date.fromtimestamp(record.created_at).isoformat()}.jsonl"
            append_jsonl(path, _record_payload(record))

    def all(self) -> list[MemoryRecord]:
        """从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        这个方法没有输入参数。

        返回说明:
        返回当前 owner 长期记忆文件的 MemoryRecord 列表；home daily mirror 由 search() 补充检索或 CLI 直接读取。

        异常说明:
        如果某一行不是合法 JSON，目前会由 json.loads 抛错；后续如需容错可加 read audit。"""

        return self._read_memory_file(self.path)

    def runtime_snapshot(self) -> dict[str, object]:
        """Project owner-local Memory health without exposing paths or content."""

        try:
            report = read_jsonl_objects_report(
                self.path,
                context="memory_store.runtime_snapshot",
            )
            events: list[MemoryRecord] = []
            invalid_records = 0
            for payload in report.records:
                record = _memory_record_from_obj(payload)
                if record is None:
                    invalid_records += 1
                    continue
                events.append(_with_legacy_identity(record))
            active = _materialize_memory_events(events)
        except Exception as exc:
            return {
                "state": "unavailable",
                "health": "unavailable",
                "active_total": 0,
                "active_by_kind": {},
                "load_error_count": 1,
                "load_error_codes": [type(exc).__name__],
            }
        by_kind: dict[str, int] = {}
        for record in active:
            kind = str(record.kind or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        error_codes = sorted(
            {
                str(error.get("error_type") or "MEMORY_LOAD_ERROR")
                for error in report.load_errors
            }
        )
        if invalid_records:
            error_codes.append("MemoryRecordValidationError")
        load_error_count = len(report.load_errors) + invalid_records
        return {
            "state": "available",
            "health": "degraded" if load_error_count else "healthy",
            "active_total": len(active),
            "active_by_kind": by_kind,
            "event_total": len(events),
            "load_error_count": load_error_count,
            "load_error_codes": sorted(set(error_codes)),
            "text_index": "configured" if self.local_store is not None else "unconfigured",
            "semantic_index": "configured" if self._embedder is not None else "unconfigured",
        }

    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """搜索记忆，优先使用 LocalStore 索引，再读取 JSONL 正式源。

        新手说明:
        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，继续从 owner JSONL / daily mirror 检索。

        query: 搜索文本。
        top_k: 最多返回多少条结果。

        返回说明:
        返回 MemoryRecord 列表。LocalStore 是检索索引，JSONL 是正式记忆源。"""

        records, _load_errors = self.search_report(query, top_k=top_k)
        if self._embedder is None:
            return records  # 无 embedder → 纯关键词(现状不变)
        return self._fuse_semantic(query, records, top_k)

    def _fuse_semantic(self, query: str, keyword_records: list[MemoryRecord], top_k: int) -> list[MemoryRecord]:
        """关键词召回 + 语义召回 RRF 融合(检索拓宽 #1)。语义为空则退回纯关键词(不崩不退化)。"""
        semantic = self._semantic_records(query, top_k)
        if not semantic:
            return keyword_records[:top_k]
        from ..retrieval.lexical import reciprocal_rank_fusion

        by_id: dict[str, MemoryRecord] = {}
        for record in [*keyword_records, *semantic]:
            by_id.setdefault(_record_vec_id(record), record)
        fused = reciprocal_rank_fusion(
            [[_record_vec_id(r) for r in keyword_records], [_record_vec_id(r) for r in semantic]]
        )
        return [by_id[doc_id] for doc_id, _score in fused if doc_id in by_id][:top_k]

    def search_report(self, query: str, top_k: int = 5) -> tuple[list[MemoryRecord], list[dict]]:
        """搜索记忆并保留可恢复的索引读取错误。

        LocalStore 只是检索索引，不是记忆事实源。索引坏了时继续从当前 owner JSONL / daily
        mirror 检索，但把错误报告给调用方，避免上层误判为“没有记忆”。
        """

        indexed, load_errors = self._search_local_store_report(query, top_k)
        if len(indexed) >= top_k:
            return indexed[:top_k], load_errors
        source_records = _merge_search_result_groups(
            (
                self._search_jsonl(query, top_k),
                self._search_daily_mirror(query, top_k),
            ),
            top_k=top_k,
        )
        return _merge_search_result_groups((indexed, source_records), top_k=top_k), load_errors

    def index_all(self) -> int:
        """把现有 JSONL 记忆补写到 LocalStore 索引。

        新手说明:
        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        如果没有 local_store，就什么都不做并返回 0。

        这个方法没有输入参数。

        返回说明:
        返回成功尝试索引的 owner 长期记忆记录数量；home daily mirror 暂不混入本地索引重建。

        副作用说明:
        会调用 LocalStore.upsert_record；不会改写 JSONL。"""

        if not self.local_store:
            return 0
        count = 0
        for record in self.all():
            self._index_record(record)
            count += 1
        return count

    def _daily_mirror_files(self) -> list[Path]:
        files: list[Path] = []
        for daily_dir in self.daily_mirror_dirs:
            if daily_dir.exists():
                files.extend(path for path in daily_dir.glob("*.jsonl") if path.is_file())
        return sorted(set(files))

    def _read_memory_file(self, path: Path) -> list[MemoryRecord]:
        return _materialize_memory_events(self._read_memory_events(path))

    def _read_memory_events(self, path: Path) -> list[MemoryRecord]:
        if not path.exists():
            return []
        # 逐行容错:坏 JSON 行跳过并上报(复用 json_io 健壮读取器),未知顶层字段过滤——
        # 单坏行/旧版本写的新字段记录不再崩掉整条记忆召回(审计 #11,多版本滚动升级的数据可用性)。
        report = read_jsonl_objects_report(path, context="memory_store.read_memory_file")
        records: list[MemoryRecord] = []
        for obj in report.records:
            record = _memory_record_from_obj(obj)
            if record is not None:
                records.append(_with_legacy_identity(record))
        return records

    def _search_daily_mirror(self, query: str, top_k: int) -> list[MemoryRecord]:
        events: list[MemoryRecord] = []
        for path in self._daily_mirror_files():
            events.extend(self._read_memory_events(path))
        records = _materialize_memory_events(events)
        return _search_memory_records(records, query, top_k)

# 函数用途: MemoryRecord → JSONL 行字典;attributes 为空时不写该键,旧行格式不变。
def _record_payload(record: MemoryRecord) -> dict:
    payload = asdict(record)
    if not payload.get("attributes"):
        payload.pop("attributes", None)
    for key in ("entry_id", "source"):
        if not payload.get(key):
            payload.pop(key, None)
    for key in ("updated_at", "expires_at"):
        if not float(payload.get(key) or 0.0):
            payload.pop(key, None)
    return payload


def _record_vec_id(record: MemoryRecord) -> str:
    """记忆的稳定向量 id(按 role+kind+nfc(content) 哈希,跨关键词/语义两路对齐做 RRF 融合)。"""
    import hashlib

    if record.entry_id:
        return record.entry_id
    key = f"{record.role}\x00{record.kind}\x00{nfc(record.content)}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def _memory_record_key(record: MemoryRecord) -> tuple[str, str, str, float]:
    # content 走 nfc 规范化(审计 #20):NFD/NFC 等价的同一条记忆(macOS 文件名/不同输入法)归一后
    # 同键去重,不再因码点形式差异重复堆积。用 nfc 而非 fold_key——内容去重保大小写/全角语义,只统一编码形式。
    return (record.role, record.kind, nfc(record.content), float(record.created_at or 0.0))


def _legacy_entry_id(record: MemoryRecord) -> str:
    """Derive a stable identity for pre-versioned rows without rewriting them."""

    import hashlib

    payload = json.dumps(
        {
            "role": record.role,
            "content": nfc(record.content),
            "kind": record.kind,
            "tags": record.tags or [],
            "created_at": float(record.created_at or 0.0),
            "attributes": record.attributes or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "memory-legacy-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _with_legacy_identity(record: MemoryRecord) -> MemoryRecord:
    if not record.entry_id:
        record.entry_id = _legacy_entry_id(record)
    if not record.action:
        record.action = "add"
    if int(record.version or 0) < 1:
        record.version = 1
    if not record.updated_at:
        record.updated_at = float(record.created_at or 0.0)
    return record


def _normalized_new_record(record: MemoryRecord) -> MemoryRecord:
    now = time.time()
    if not record.created_at:
        record.created_at = now
    if not record.updated_at:
        record.updated_at = record.created_at
    if not record.entry_id:
        record.entry_id = "memory-" + uuid.uuid4().hex
    record.action = "add"
    record.version = max(1, int(record.version or 1))
    record.tags = list(record.tags or [])
    return record


def _materialize_memory_events(
    events: list[MemoryRecord],
    *,
    include_expired: bool = False,
) -> list[MemoryRecord]:
    active: dict[str, MemoryRecord] = {}
    for raw in events:
        record = _with_legacy_identity(raw)
        action = str(record.action or "add").strip().lower()
        if action == "remove":
            active.pop(record.entry_id, None)
            continue
        if action not in {"add", "replace"}:
            continue
        prior = active.get(record.entry_id)
        if prior is not None and int(record.version or 1) < int(prior.version or 1):
            continue
        active[record.entry_id] = record
    if include_expired:
        return list(active.values())
    now = time.time()
    return [
        record
        for record in active.values()
        if not float(record.expires_at or 0.0) or float(record.expires_at) > now
    ]


def _prepare_memory_operations(
    operations: list[dict[str, object]],
    active: dict[str, MemoryRecord],
) -> list[MemoryRecord]:
    committed: list[MemoryRecord] = []
    now = time.time()
    for index, raw in enumerate(operations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"memory operation {index} must be an object")
        action = str(raw.get("action") or "add").strip().lower()
        if action == "add":
            record = _prepare_add_memory_operation(raw, index=index, active=active, now=now)
        else:
            record = _prepare_existing_memory_operation(
                raw,
                index=index,
                action=action,
                active=active,
                now=now,
            )
        if record is not None:
            committed.append(record)
    return committed


# LLM: Add preparation mutates only the in-memory batch projection, never the JSONL authority.
# 函数用途: 校验新增记忆并放入本批 active 视图；完全相同的稳定 ID 内容视为幂等。
def _prepare_add_memory_operation(
    raw: dict[str, object],
    *,
    index: int,
    active: dict[str, MemoryRecord],
    now: float,
) -> MemoryRecord | None:
    content = str(raw.get("content") or "").strip()
    if not content:
        raise ValueError(f"memory operation {index} content is required")
    entry_id = str(raw.get("entry_id") or "").strip() or "memory-" + uuid.uuid4().hex
    current = active.get(entry_id)
    if current is not None:
        if current.content == content:
            return None
        raise ValueError(f"memory operation {index} entry_id already exists")
    record = MemoryRecord(
        role=str(raw.get("role") or "user").strip() or "user",
        content=content,
        kind=str(raw.get("kind") or "fact").strip() or "fact",
        tags=_operation_tags(raw.get("tags")),
        created_at=now,
        attributes=_operation_attributes(raw.get("attributes")),
        entry_id=entry_id,
        action="add",
        version=1,
        source=str(raw.get("source") or "").strip(),
        updated_at=now,
        expires_at=_operation_expiry(raw.get("expires_at")),
    )
    active[entry_id] = record
    return record


# LLM: Replace/remove share stable-id and optimistic-version validation for one batch projection.
# 函数用途: 根据当前 active 版本准备替换或删除事件，并同步本批内存视图。
def _prepare_existing_memory_operation(
    raw: dict[str, object],
    *,
    index: int,
    action: str,
    active: dict[str, MemoryRecord],
    now: float,
) -> MemoryRecord:
    entry_id = str(raw.get("entry_id") or "").strip()
    if not entry_id:
        raise ValueError(f"memory operation {index} entry_id is required")
    prior = active.get(entry_id)
    if prior is None:
        raise KeyError(entry_id)
    expected = raw.get("expected_version")
    if expected not in (None, "") and int(expected) != int(prior.version or 1):
        raise RuntimeError(
            f"memory operation {index} stale version: expected {expected}, current {prior.version}"
        )
    version = int(prior.version or 1) + 1
    if action == "replace":
        record = _replacement_memory_record(raw, prior, entry_id, version, now, index)
        active[entry_id] = record
        return record
    if action == "remove":
        record = _removed_memory_record(raw, prior, entry_id, version, now)
        del active[entry_id]
        return record
    raise ValueError(f"memory operation {index} has unknown action {action!r}")


def _replacement_memory_record(
    raw: dict[str, object],
    prior: MemoryRecord,
    entry_id: str,
    version: int,
    now: float,
    index: int,
) -> MemoryRecord:
    content = str(raw.get("content") or "").strip()
    if not content:
        raise ValueError(f"memory operation {index} content is required")
    kind_value = raw.get("kind")
    tags_value = raw.get("tags")
    expiry_value = raw.get("expires_at")
    return MemoryRecord(
        role=prior.role,
        content=content,
        kind=(str(kind_value).strip() if kind_value not in (None, "") else prior.kind),
        tags=(_operation_tags(tags_value) if tags_value is not None else list(prior.tags or [])),
        created_at=prior.created_at,
        attributes=(
            _operation_attributes(raw.get("attributes")) if "attributes" in raw else prior.attributes
        ),
        entry_id=entry_id,
        action="replace",
        version=version,
        source=str(raw.get("source") or "").strip(),
        updated_at=now,
        expires_at=(
            _operation_expiry(expiry_value)
            if expiry_value is not None
            else float(prior.expires_at or 0.0)
        ),
    )


def _removed_memory_record(
    raw: dict[str, object],
    prior: MemoryRecord,
    entry_id: str,
    version: int,
    now: float,
) -> MemoryRecord:
    return MemoryRecord(
        role=prior.role,
        content=prior.content,
        kind=prior.kind,
        tags=list(prior.tags or []),
        created_at=prior.created_at,
        attributes=prior.attributes,
        entry_id=entry_id,
        action="remove",
        version=version,
        source=str(raw.get("source") or "").strip(),
        updated_at=now,
        expires_at=float(prior.expires_at or 0.0),
    )


def _operation_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _operation_attributes(value: object) -> dict | None:
    return dict(value) if isinstance(value, dict) and value else None


def _operation_expiry(value: object) -> float:
    if value in (None, ""):
        return 0.0
    expiry = float(value)
    if expiry < 0:
        raise ValueError("expires_at must be a non-negative Unix timestamp")
    return expiry


def _search_memory_records(records: list[MemoryRecord], query: str, top_k: int) -> list[MemoryRecord]:
    query_terms = {term for term in fold_key(query).split() if term}  # 统一规范化(审计 #20)
    scored: list[tuple[int, float, MemoryRecord]] = []
    for record in records:
        score = _memory_search_score(record, query, query_terms)
        if score > 0 or not query_terms:
            scored.append((score, record.created_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:top_k]]


def _memory_search_score(record: MemoryRecord, query: str, query_terms: set[str]) -> int:
    text = fold_key(record.content)  # 与 query_terms 同走统一规范化(审计 #20):全角/NFD/大小写不漏命中
    score = sum(1 for term in query_terms if term in text)
    folded_query = fold_key(query)
    if folded_query and folded_query in text:
        score += 3
    return score


def _merge_search_result_groups(groups: tuple[list[MemoryRecord], ...], *, top_k: int) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for group in groups:
        _append_unique_records(records, seen, group, top_k=top_k)
        if len(records) >= top_k:
            return records
    return records


def _append_unique_records(
    records: list[MemoryRecord],
    seen: set[tuple[str, str, str, float]],
    group: list[MemoryRecord],
    *,
    top_k: int,
) -> None:
    for record in group:
        key = _memory_record_key(record)
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
        if len(records) >= top_k:
            return


def _dedupe_memory_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    deduped: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for record in records:
        key = _memory_record_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _daily_mirror_dirs(value: object) -> tuple[Path, ...]:
    if value is None:
        return ()
    raw_items = value if isinstance(value, (list, tuple, set)) else (value,)
    dirs: list[Path] = []
    for item in raw_items:
        path = Path(item)
        if path not in dirs:
            dirs.append(path)
    return tuple(dirs)
