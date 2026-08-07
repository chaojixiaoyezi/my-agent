
from __future__ import annotations

"""Owner 正式长期记忆 JSONL 权威仓库及其可重建检索投影。"""

# LLM: memory.jsonl is the only formal long-term fact authority; indexes and ops never become recall authorities.
# 模块用途: 提供长期事实的稳定 ID 新增/替换/删除、原子批处理、hard delete 与 active 召回。

import json
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..common.text_norm import fold_key, nfc
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange


# LLM: 未知 legacy 字段可忽略，但无法构造的行只能返回 None 供上层计入损坏诊断。
# 函数用途: 将一个 JSONL 对象恢复为 MemoryRecord。
def _memory_record_from_obj(obj: dict) -> MemoryRecord | None:
    """dict → MemoryRecord:过滤未知顶层字段(旧版本读新字段记录不崩),构造失败返回 None(跳过坏记录)。"""
    known = set(MemoryRecord.__dataclass_fields__)
    try:
        return MemoryRecord(**{k: v for k, v in obj.items() if k in known})
    except (TypeError, ValueError):
        return None

from ._jsonl_indexing import JsonlMemoryIndexMixin
from .operations import (
    append_memory_operation_events,
    append_memory_purge_event,
    memory_content_hash,
    normalized_memory_content,
)

if TYPE_CHECKING:
    from ..local_storage import LocalSearchResult, LocalStore
    from ..user_space.owner_quota import OwnerQuotaEnforcer
    from .candidates import CandidateService


# LLM: quota admission 必须先于正式记忆文件锁获取，未配置时只提供透明空上下文。
# 函数用途: 进入当前 owner 的长期记忆容量临界区。
@contextmanager
def _quota_admission(
    enforcer: OwnerQuotaEnforcer | None,
) -> Iterator[OwnerQuotaAdmission | None]:
    if enforcer is None:
        yield None
        return
    with enforcer.admission() as admission:
        yield admission


# LLM: 配额按整个下一版权威文件字节数检查，不按单条 delta 猜最终占用。
# 函数用途: 在原子替换前校验长期记忆文件的最终容量。
def _check_memory_quota(
    admission: OwnerQuotaAdmission | None,
    memory: JsonlMemory,
    records: list[MemoryRecord],
    next_text: str,
) -> None:
    if admission is None:
        return
    changes = [OwnerQuotaChange(memory.path, len(next_text.encode("utf-8")))]
    admission.check(changes)


# LLM: MemoryRecord is a versioned formal fact event; ordinary transcript turns must not be auto-created here.
# 类用途: 表示正式长期记忆的一次 add/replace/remove 版本事件。
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
    # 访问信号:search_scoped/search 命中时由 touch 事件写入;总量闸浓缩按它淘汰冷条目。
    # 旧 JSONL 行无此字段,读取时按 0 兼容(浓缩时退化为按 updated_at)。
    last_accessed_at: float = 0.0

    # LLM: 序列化只补缺失 created_at，不写文件或更新任何派生索引。
    # 函数用途: 把当前长期记忆事件转换为单行 JSON 文本。
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


# LLM: subject conflict 是稳定 ID replace 的结构化修复信号，不能自动选择旧条目或语义合并。
# 类用途: 告诉 remember 调用方某个主题已经有不同事实，应先列出再精确修改。
class MemorySubjectConflict(ValueError):
    """A structured subject already exists and must be replaced by stable id."""

    # LLM: 异常只携带主题键和精确旧 entry ID，调用方必须显式 replace 或进入冲突审核。
    # 函数用途: 构造一条长期事实主题冲突。
    def __init__(self, subject_key: str, entry_id: str):
        self.subject_key = subject_key
        self.entry_id = entry_id
        super().__init__(
            f"memory subject {subject_key!r} already exists as {entry_id}; "
            "list and replace the stable entry_id"
        )


# LLM: This mixin owns authoritative JSONL mutations; recall/index behavior is composed separately below.
# 类用途: 初始化长期记忆并实现新增、替换、删除、批量提交和 legacy 清除。
class _JsonlMemoryIdentityMixin:
    """JSONL-backed memory store with optional LocalStore indexing.

    新手说明:
    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 可选 LocalStore；有它时 add/index_all/search 可以同步索引和优先搜索索引。"""

    # LLM: 构造器只绑定唯一 authority/ops/Candidate/index/quota；不得增加 legacy 双读路径。
    # 函数用途: 初始化当前 owner 正式长期记忆仓库及可选派生服务。
    def __init__(
        self,
        path: str | Path,
        local_store: LocalStore | None = None,
        *,
        ops_path: str | Path | None = None,
        candidate_service: CandidateService | None = None,
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
        self.ops_path = Path(ops_path) if ops_path is not None else None
        self.candidate_service = candidate_service
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder  # 配了 → 记忆召回加一路语义向量(检索拓宽 #1);None → 纯关键词
        self._vector_store_cache = None
        self.quota_enforcer = quota_enforcer
        # 访问信号缓冲:命中先累积内存,随下一次写原子落盘,避免每次召回都写一次文件。
        self._pending_access: dict[str, float] = {}

    # LLM: add 仍走 add_record/apply_batch 唯一权威提交链；attributes 只承载结构化来源等元数据。
    # 函数用途: 便捷新增一条长期记忆，并返回实际写入或已存在的稳定记录。
    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
        source: str = "",
        expires_at: float = 0.0,
        attributes: dict | None = None,
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
                attributes=dict(attributes or {}) or None,
            )
        )

    # LLM: 记忆写入的底层唯一落盘口(add 是它的便捷封装)。接受完整 MemoryRecord,
    #   attributes 等扩展字段(P5-2 trigger_conditions)由调用方在 record 上携带。
    #   副作用:JSONL 原子提交 + LocalStore/向量派生索引更新(索引失败不改变权威事实)。
    # 函数用途: 想写带结构化扩展字段的记忆时,构造好 MemoryRecord 从这里进。
    def add_record(self, record: MemoryRecord) -> MemoryRecord:
        record = _normalized_new_record(record)
        committed = self.apply_batch(
            [
                {
                    "action": "add",
                    "entry_id": record.entry_id,
                    "role": record.role,
                    "content": record.content,
                    "kind": record.kind,
                    "tags": record.tags,
                    "created_at": record.created_at,
                    "attributes": record.attributes,
                    "source": record.source,
                    "expires_at": record.expires_at,
                }
            ]
        )
        if committed:
            return committed[0]
        identity = _memory_identity_key(record)
        for current in self.all():
            if _memory_identity_key(current) == identity:
                return current
        return record

    # LLM: replace 必须按精确 entry_id/version 进入 apply_batch，新时间不能自动选择替换目标。
    # 函数用途: 创建一条正式长期记忆替换版本。
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

    # LLM: remove 必须按精确 entry_id/version 进入 hard-delete 主链并保留无正文 tombstone。
    # 函数用途: 删除一条正式长期记忆的所有可召回正文。
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

# LLM: This mixin performs locked, quota-admitted mutations after public inputs are normalized by the identity mixin.
# 类用途: 原子提交长期记忆批次，并为迁移精确清除已有 legacy 记录。
class _JsonlMemoryMutationMixin:
    # LLM: batch 锁内全量验证并原子替换权威 JSONL；remove 还要清派生层明文且可精确重放。
    # 函数用途: 一次提交一批新增、替换或删除，任何权威校验失败都不写半批。
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
                latest_events = {record.entry_id: record for record in current_events}
                committed = _prepare_memory_operations(
                    operations,
                    active,
                    latest_events=latest_events,
                )
                if not committed:
                    return []
                final_removed_ids = {
                    record.entry_id
                    for record in committed
                    if record.action == "remove" and record.entry_id not in active
                }
                removed_content_hashes = {
                    memory_content_hash(record.content)
                    for record in [*current_events, *committed]
                    if record.entry_id in final_removed_ids and record.content
                }
                removed_contents = {
                    record.content
                    for record in [*current_events, *committed]
                    if record.entry_id in final_removed_ids and record.content
                }
                persisted = [
                    record
                    for record in committed
                    if record.entry_id not in final_removed_ids or record.action == "remove"
                ]
                retained_events = [
                    record for record in current_events if record.entry_id not in final_removed_ids
                ]
                # 访问信号:本次写事务顺带落盘累积的 touch 事件(原子,不额外开锁)。
                touch_events = _touch_events(self._pending_access)
                next_events = [*retained_events, *persisted, *touch_events]
                next_text = "".join(
                    json.dumps(_record_payload(record), ensure_ascii=False) + "\n"
                    for record in next_events
                )
                self._pending_access.clear()
                _check_memory_quota(admission, self, persisted, next_text)
                self._redact_local_tool_ledgers(removed_contents)
                write_text_file_atomic_unlocked(self.path, next_text)
        if removed_content_hashes and self.candidate_service is not None:
            self.candidate_service.redact_content(
                content_hashes=removed_content_hashes,
                exclude_candidate_ids={
                    source.removeprefix("candidate:")
                    for record in persisted
                    if record.action == "remove"
                    and (source := str(record.source or "")).startswith("candidate:")
                },
            )
        if final_removed_ids:
            append_memory_purge_event(
                self.ops_path,
                entry_ids=final_removed_ids,
                content_hashes=removed_content_hashes,
            )
        append_memory_operation_events(self.ops_path, persisted)
        self._after_commit_indexes(persisted)
        return committed

    # LLM: 此入口仅供显式 Memory schema migration 在已持久备份后清除 legacy active 正文；
    #   它留下 hash tombstone 并清派生索引，但刻意不清 Candidate/工具账本，因为旧正文已迁入新事实链。
    # 函数用途: 按精确 entry_id、version 和 content hash 原子移除一批旧长期记忆，供可回滚迁移使用。
    def remove_migrated_legacy(
        self,
        records: Iterable[MemoryRecord],
        *,
        source: str,
        backup_ref: str,
    ) -> list[MemoryRecord]:
        targets = list(records)
        migration_source = str(source or "").strip()
        if not migration_source.startswith("memory-migration-"):
            raise ValueError("legacy migration source must use memory-migration-* namespace")
        if not str(backup_ref or "").strip():
            raise ValueError("legacy migration requires a durable backup_ref")
        if not targets:
            return []
        expected: dict[str, tuple[int, str]] = {}
        for record in targets:
            entry_id = str(record.entry_id or "").strip()
            if not entry_id or entry_id in expected:
                raise ValueError("legacy migration records require unique entry_id values")
            expected[entry_id] = (
                int(record.version or 1),
                memory_content_hash(record.content),
            )
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                current_events = [
                    _with_legacy_identity(record)
                    for record in self._read_memory_events(self.path)
                ]
                active = {
                    record.entry_id: record
                    for record in _materialize_memory_events(
                        current_events,
                        include_expired=True,
                    )
                }
                for entry_id, (version, content_hash) in expected.items():
                    current = active.get(entry_id)
                    if current is None:
                        raise KeyError(entry_id)
                    if int(current.version or 1) != version:
                        raise RuntimeError(
                            f"legacy migration stale version for {entry_id}: "
                            f"expected {version}, current {current.version}"
                        )
                    if memory_content_hash(current.content) != content_hash:
                        raise RuntimeError(
                            f"legacy migration content hash changed for {entry_id}"
                        )
                now = time.time()
                tombstones = [
                    _removed_memory_record(
                        {"source": migration_source},
                        active[entry_id],
                        entry_id,
                        int(active[entry_id].version or 1) + 1,
                        now,
                    )
                    for entry_id in sorted(expected)
                ]
                retained = [
                    record
                    for record in current_events
                    if record.entry_id not in expected
                ]
                next_text = "".join(
                    json.dumps(_record_payload(record), ensure_ascii=False) + "\n"
                    for record in [*retained, *tombstones]
                )
                _check_memory_quota(admission, self, tombstones, next_text)
                write_text_file_atomic_unlocked(self.path, next_text)
        append_memory_purge_event(
            self.ops_path,
            entry_ids=set(expected),
            content_hashes={content_hash for _version, content_hash in expected.values()},
        )
        append_memory_operation_events(self.ops_path, tombstones)
        self._after_commit_indexes(tombstones)
        return tombstones

# LLM: This mixin owns reads and rebuildable search projections; it never becomes the formal memory authority.
# 类用途: 从 active JSONL 读取并执行关键词、FTS 与可选向量召回。
class _JsonlMemoryRecallMixin(JsonlMemoryIndexMixin):
    # LLM: 向量库位于当前 owner memory 根且仅是 lazy projection；无 embedder 时必须完全关闭。
    # 函数用途: 获取当前 owner 的可重建向量索引实例。
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

    # LLM: 向量写失败不改变权威提交；metadata 仍需携带可与 active JSONL 复核的完整身份。
    # 函数用途: 尽力把一条正式记忆写入向量索引。
    def _index_vector(self, record: MemoryRecord) -> None:
        store = self._vector_store()
        if store is None:
            return
        try:
            vector = self._embedder.embed([record.content])[0]
            store.upsert(_record_vec_id(record), vector, text=record.content, metadata=_record_payload(record))
        except Exception:
            pass  # 向量索引失败不打断记忆写入(JSONL 才是事实源)

    # LLM: 删除只针对精确 entry ID 的派生向量，失败不能使索引获得事实权威。
    # 函数用途: 尽力清除一条已删除记忆的向量项。
    def _remove_vector(self, entry_id: str) -> None:
        store = self._vector_store()
        if store is None or not entry_id:
            return
        try:
            store.remove(entry_id)
        except Exception:
            pass

    # LLM: 每个向量命中必须按 active entry ID 和正文逐字复核，旧向量不能复活历史内容。
    # 函数用途: 返回经过正式 JSONL 二次核验的语义检索结果。
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

    # LLM: all 只 materialize 当前 active、未过期的正式记忆，绝不合并 Daily/Candidate/ops。
    # 函数用途: 读取当前 owner 可召回的全部长期事实。
    def all(self) -> list[MemoryRecord]:
        """从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        这个方法没有输入参数。

        返回说明:
        返回当前 owner 唯一正式长期记忆文件的 active MemoryRecord 列表。

        异常说明:
        如果某一行不是合法 JSON，目前会由 json.loads 抛错；后续如需容错可加 read audit。"""

        return self._read_memory_file(self.path)

    # LLM: 访问信号只在召回命中时产生;不命中不 touch,命中才证明"这条被用过"。
    # 函数用途: 记录一次对某条记忆的访问(内存累积,随下次写原子落盘)。
    def _note_access(self, entry_id: str) -> None:
        if not entry_id:
            return
        self._pending_access[entry_id] = time.time()

    # LLM: 落盘仍是写路径职责(锁内原子);公开入口供无写场景时手动冲刷。
    # 函数用途: 手动把累积的访问信号作为 touch 事件写入文件。
    def flush_access_events(self) -> int:
        if not self._pending_access:
            return 0
        with locked_json_path(self.path):
            if not self._pending_access:
                return 0
            pending = dict(self._pending_access)
            current_events = self._read_memory_events(self.path)
            next_text = "".join(
                json.dumps(_record_payload(record), ensure_ascii=False) + "\n"
                for record in [*current_events, *_touch_events(pending)]
            )
            self._pending_access.clear()
            write_text_file_atomic_unlocked(self.path, next_text)
        return len(pending)

    # LLM: 总量闸只淘汰"非用户明确要求"的冷条目;user_explicit 永不因用量淘汰(用户显式记忆
    # 是硬事实),且单次最多移除 25%——冷启动后仍有渐进收缩空间,不一次性清空。
    # 函数用途: 超总量上限时,把最久未被访问的非 user_explicit 条目移入 archive 并移除。
    def condense(
        self,
        *,
        max_records: int = 2000,
        target_records: int = 1200,
        max_ratio: float = 0.25,
    ) -> list[MemoryRecord]:
        active = self.all()
        if len(active) <= max_records:
            return []
        removable = [
            record
            for record in active
            if str((record.attributes or {}).get("origin") or "") != "user_explicit"
        ]
        if not removable:
            return []
        excess = len(active) - target_records
        cap = max(1, int(len(active) * max_ratio))
        to_remove = sorted(
            removable,
            key=lambda item: (
                float(item.last_accessed_at or 0.0)
                or float(item.updated_at or 0.0)
                or float(item.created_at or 0.0),
                item.entry_id,
            ),
        )[: min(excess, cap)]
        if not to_remove:
            return []
        # 先入 archive 账本,再在主文件移除——archive 多写一次无害,主文件移除失败可下次重试。
        archive_path = self.path.with_name(self.path.name + ".archive.jsonl")
        with locked_json_path(archive_path):
            archived_text = "".join(
                json.dumps(_record_payload(record), ensure_ascii=False) + "\n"
                for record in to_remove
            )
            if archive_path.exists():
                archived_text = archive_path.read_text(encoding="utf-8") + archived_text
            write_text_file_atomic_unlocked(archive_path, archived_text)
        self.apply_batch(
            [
                {
                    "action": "remove",
                    "entry_id": record.entry_id,
                    "source": "condense:quota",
                    "expected_version": record.version,
                }
                for record in to_remove
            ]
        )
        return to_remove

    # LLM: 过期 active 记录仍可能含需迁移/清除的正文；此入口只供 retention、migration 和 doctor，召回不得使用。
    # 函数用途: 返回包括已过期但尚未删除的全部 active 长期记忆。
    def all_including_expired(self) -> list[MemoryRecord]:
        return _materialize_memory_events(
            self._read_memory_events(self.path),
            include_expired=True,
        )

    # LLM: 健康快照只返回计数/错误码/索引状态，不能泄露正文或本地路径。
    # 函数用途: 为 capabilities/doctor 投影长期记忆运行状态。
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

    # LLM: 搜索结果最终必须回到 active JSONL，并按确定性规则去重排序。
    # 函数用途: 返回不含陈旧索引正文的长期记忆召回结果。
    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """搜索记忆，优先使用 LocalStore 索引，再读取 JSONL 正式源。

        新手说明:
        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，继续从正式 JSONL 检索。

        query: 搜索文本。
        top_k: 最多返回多少条结果。

        返回说明:
        返回 MemoryRecord 列表。LocalStore 是检索索引，JSONL 是正式记忆源。"""

        candidate_limit = max(top_k * 4, top_k + 8)
        records, _load_errors = self.search_report(query, top_k=candidate_limit)
        if self._embedder is None:
            selected = _rerank_memory_records(records, query, top_k)
        else:
            fused = self._fuse_semantic(query, records, candidate_limit)
            selected = _select_diverse_memory_records(fused, top_k)
        for record in selected:
            self._note_access(record.entry_id)
        return selected

    # LLM: Runtime recall 先以 active JSONL 建 allowlist，再读取派生索引；陈旧 FTS/vector 命中不能复活删除版本。
    # 函数用途: 在当前正式记录和显式 scope predicate 内做确定性召回。
    def search_scoped(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
    ) -> list[MemoryRecord]:
        if top_k <= 0:
            return []
        active = [record for record in self.all() if predicate(record)]
        candidate_limit = max(top_k * 4, top_k + 8)
        # 混合检索:BM25 词面 + 向量语义 RRF 融合(治"换词就召不回"),替代原纯子串+自管 RRF;
        # embedder 缺失或端点抖动时自动降级纯 BM25(仍强于纯子串)。active JSONL 已建 allowlist,
        # 索引命中不能复活删除版本;embedder 语义只在本列表内打分,不再走全库 _semantic_records。
        if active:
            from ..retrieval.hybrid import HybridRetriever

            # BM25 臂拼入写路径生成的关键词(keywords_en),让英文查询词面命中中文记忆;
            # embedding 臂本就跨语言(mean centering 实证),两臂互补。
            def _index_text(record: MemoryRecord) -> str:
                attrs = record.attributes if isinstance(record.attributes, dict) else {}
                keywords = attrs.get("keywords_en") or []
                if isinstance(keywords, list):
                    suffix = " ".join(str(item) for item in keywords)
                else:
                    suffix = str(keywords)
                return record.content + (" " + suffix if suffix else "")

            ranked = HybridRetriever(self._embedder).rank(
                query,
                [(record.entry_id, _index_text(record)) for record in active],
                top_k=candidate_limit,
                # 注入侧阈值化:向量臂只留 cosine≥0.30 的真相关,弱相关不凑数带进 prompt。
                # 词面臂(BM25)不受限——词面重合本身是相关信号;无 embedder 自动降级纯 BM25。
                vector_min_score=0.30,
            )
            by_id = {record.entry_id: record for record in active}
            keyword = [by_id[entry_id] for entry_id, _score in ranked if entry_id in by_id]
        else:
            keyword = []
        selected = _rerank_memory_records(keyword, query, top_k)
        for record in selected:
            self._note_access(record.entry_id)
        return selected

    # LLM: RRF 只融合两个可重建候选列表，返回前仍使用稳定 entry ID 对齐。
    # 函数用途: 合并关键词与语义召回顺序。
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

    # LLM: report 保留索引读取错误并从 authority fallback；错误不能伪装成“没有记忆”。
    # 函数用途: 搜索正式记忆并返回可恢复的派生索引诊断。
    def search_report(self, query: str, top_k: int = 5) -> tuple[list[MemoryRecord], list[dict]]:
        """搜索记忆并保留可恢复的索引读取错误。

        LocalStore 只是检索索引，不是记忆事实源。索引坏了时继续从当前 owner JSONL
        检索，但把错误报告给调用方，避免上层误判为“没有记忆”。
        """

        candidate_limit = max(top_k * 4, top_k + 8)
        indexed, load_errors = self._search_local_store_report(query, candidate_limit)
        active_records = self.all()
        active_by_id = {record.entry_id: record for record in active_records if record.entry_id}
        # Index rows are projections only. Return the current source object and reject any
        # missing/deleted ID so an old index cannot resurrect a removed or replaced body.
        indexed = [
            active_by_id[record.entry_id]
            for record in indexed
            if record.entry_id in active_by_id
        ]
        source_records = _search_memory_records(active_records, query, candidate_limit)
        merged = _merge_search_result_groups(
            (indexed, source_records),
            top_k=candidate_limit,
        )
        return _rerank_memory_records(merged, query, top_k), load_errors

    # LLM: 全量建索引只读 active authority，不修改 memory.jsonl 或生成第二份长期事实。
    # 函数用途: 从正式长期记忆重建 LocalStore/FTS 索引。
    def index_all(self) -> int:
        """把现有 JSONL 记忆补写到 LocalStore 索引。

        新手说明:
        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        如果没有 local_store，就什么都不做并返回 0。

        这个方法没有输入参数。

        返回说明:
        返回成功尝试索引的 owner 长期记忆记录数量。

        副作用说明:
        会调用 LocalStore.upsert_record；不会改写 JSONL。"""

        if not self.local_store:
            return 0
        count = 0
        for record in self.all():
            self._index_record(record)
            count += 1
        return count

    # LLM: 文件读取先 materialize 版本事件并过滤删除/过期，供正常召回使用。
    # 函数用途: 读取一个长期记忆文件的当前 active 状态。
    def _read_memory_file(self, path: Path) -> list[MemoryRecord]:
        return _materialize_memory_events(self._read_memory_events(path))

    # LLM: 原始事件读取保留 version/tombstone 语义；坏行由上层 report 暴露而非变成事实。
    # 函数用途: 读取一个长期记忆 JSONL 的全部可解析版本事件。
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


# LLM: One public repository composes the mutation authority and recall projection without duplicate storage paths.
# 类用途: 作为 owner 唯一正式长期记忆仓库，对外保持既有 JsonlMemory 接口。
class JsonlMemory(
    _JsonlMemoryIdentityMixin,
    _JsonlMemoryMutationMixin,
    _JsonlMemoryRecallMixin,
):
    pass

# LLM: 权威序列化省略空可选字段但保留 entry/action/version，使旧行可显式 materialize。
# 函数用途: 将 MemoryRecord 转为 JSONL 行字典；空扩展字段不落盘。
def _record_payload(record: MemoryRecord) -> dict:
    payload = asdict(record)
    if not payload.get("attributes"):
        payload.pop("attributes", None)
    for key in ("entry_id", "source"):
        if not payload.get(key):
            payload.pop(key, None)
    for key in ("updated_at", "expires_at", "last_accessed_at"):
        if not float(payload.get(key) or 0.0):
            payload.pop(key, None)
    return payload


# LLM: 正式 entry_id 优先；legacy fallback 只用于派生索引对齐，不能写回改变权威身份。
# 函数用途: 生成关键词与向量检索共用的稳定记录 ID。
def _record_vec_id(record: MemoryRecord) -> str:
    """记忆的稳定向量 id(按 role+kind+nfc(content) 哈希,跨关键词/语义两路对齐做 RRF 融合)。"""
    import hashlib

    if record.entry_id:
        return record.entry_id
    key = f"{record.role}\x00{record.kind}\x00{nfc(record.content)}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


# LLM: 精确去重键只做 Unicode 规范化，不用相似度合并不同事实。
# 函数用途: 生成 legacy 召回结果的稳定去重键。
def _memory_record_key(record: MemoryRecord) -> tuple[str, str, str, float]:
    # content 走 nfc 规范化(审计 #20):NFD/NFC 等价的同一条记忆(macOS 文件名/不同输入法)归一后
    # 同键去重,不再因码点形式差异重复堆积。用 nfc 而非 fold_key——内容去重保大小写/全角语义,只统一编码形式。
    return (record.role, record.kind, nfc(record.content), float(record.created_at or 0.0))


# LLM: legacy ID 来自完整结构化事件 hash，必须跨重启稳定且不依赖文件行号。
# 函数用途: 为没有 entry_id 的旧正式记忆派生只读稳定身份。
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


# LLM: legacy 归一只补缺失机器字段，不推断 scope/subject/origin 或改正文。
# 函数用途: 将旧记录补成可参与当前 materialization 的版本事件。
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


# LLM: touch 是访问信号事件,不携带正文/不改版本;last_accessed_at 是唯一有效载荷。
# 函数用途: 把累积访问缓冲转成 touch 事件列表。
def _touch_events(pending: dict[str, float]) -> list[MemoryRecord]:
    return [
        MemoryRecord(
            role="system",
            content="",
            kind="touch",
            action="touch",
            entry_id=entry_id,
            created_at=timestamp,
            updated_at=timestamp,
            last_accessed_at=timestamp,
        )
        for entry_id, timestamp in sorted(pending.items())
    ]


# LLM: 新增记录只补唯一 ID、时间、版本与 tags，证据/scope 仍必须由上游结构化提供。
# 函数用途: 规范化一条准备新增的长期记忆事件。
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


# LLM: materialization 只按 entry/version/action/expiry 计算当前态，新时间不会替代不同 entry 的事实。
# 函数用途: 从长期记忆版本事件构造当前 active 记录集合。
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
        if action == "touch":
            # 访问信号:只更新 last_accessed_at,不改内容、不 bump version。
            prior = active.get(record.entry_id)
            if prior is not None:
                prior.last_accessed_at = max(
                    float(record.last_accessed_at or 0.0),
                    float(prior.last_accessed_at or 0.0),
                )
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


# LLM: 整批操作先在内存副本全量校验，任一错误发生时权威文件保持不变。
# 函数用途: 将结构化 add/replace/remove 请求转换为待提交版本事件。
def _prepare_memory_operations(
    operations: list[dict[str, object]],
    active: dict[str, MemoryRecord],
    *,
    latest_events: dict[str, MemoryRecord] | None = None,
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
                latest_events=latest_events or {},
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
    role = str(raw.get("role") or "user").strip() or "user"
    kind = str(raw.get("kind") or "fact").strip() or "fact"
    attributes = _operation_attributes(raw.get("attributes"))
    candidate = MemoryRecord(
        role=role,
        content=content,
        kind=kind,
        attributes=attributes,
    )
    identity = _memory_identity_key(candidate)
    for existing in active.values():
        if _memory_identity_key(existing) == identity:
            return None
    _raise_subject_conflict(candidate, active.values())
    entry_id = str(raw.get("entry_id") or "").strip() or "memory-" + uuid.uuid4().hex
    current = active.get(entry_id)
    if current is not None:
        if _memory_identity_key(current) == identity:
            return None
        raise ValueError(f"memory operation {index} entry_id already exists")
    created_at = _operation_timestamp(raw.get("created_at"), fallback=now)
    record = MemoryRecord(
        role=role,
        content=content,
        kind=kind,
        tags=_operation_tags(raw.get("tags")),
        created_at=created_at,
        attributes=attributes,
        entry_id=entry_id,
        action="add",
        version=1,
        source=str(raw.get("source") or "").strip(),
        updated_at=created_at,
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
    latest_events: dict[str, MemoryRecord],
    now: float,
) -> MemoryRecord:
    entry_id = str(raw.get("entry_id") or "").strip()
    if not entry_id:
        raise ValueError(f"memory operation {index} entry_id is required")
    prior = active.get(entry_id)
    if prior is None:
        latest = latest_events.get(entry_id)
        if action == "remove" and latest is not None and latest.action == "remove":
            return latest
        raise KeyError(entry_id)
    expected = raw.get("expected_version")
    if expected not in (None, "") and int(expected) != int(prior.version or 1):
        raise RuntimeError(
            f"memory operation {index} stale version: expected {expected}, current {prior.version}"
        )
    version = int(prior.version or 1) + 1
    if action == "replace":
        record = _replacement_memory_record(raw, prior, entry_id, version, now, index)
        _raise_subject_conflict(
            record,
            (candidate for candidate_id, candidate in active.items() if candidate_id != entry_id),
        )
        active[entry_id] = record
        return record
    if action == "remove":
        record = _removed_memory_record(raw, prior, entry_id, version, now)
        del active[entry_id]
        return record
    raise ValueError(f"memory operation {index} has unknown action {action!r}")


# LLM: replace 保留旧身份/创建时间并显式增加 version；只合并结构化 attributes。
# 函数用途: 构造一条替换后的长期记忆版本事件。
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
    prior_attributes = dict(prior.attributes or {})
    incoming_attributes = _operation_attributes(raw.get("attributes"))
    if incoming_attributes:
        prior_attributes.update(incoming_attributes)
    return MemoryRecord(
        role=prior.role,
        content=content,
        kind=(str(kind_value).strip() if kind_value not in (None, "") else prior.kind),
        tags=(_operation_tags(tags_value) if tags_value is not None else list(prior.tags or [])),
        created_at=prior.created_at,
        attributes=prior_attributes or None,
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


# LLM: remove 事件正文和 attributes 必须为空，只保留身份、version、hash 审计所需字段。
# 函数用途: 构造一条无正文长期记忆 tombstone。
def _removed_memory_record(
    raw: dict[str, object],
    prior: MemoryRecord,
    entry_id: str,
    version: int,
    now: float,
) -> MemoryRecord:
    return MemoryRecord(
        role=prior.role,
        content="",
        kind=prior.kind,
        tags=[],
        created_at=prior.created_at,
        attributes=None,
        entry_id=entry_id,
        action="remove",
        version=version,
        source=str(raw.get("source") or "").strip(),
        updated_at=now,
        expires_at=0.0,
    )


# LLM: tags 只接受列表并确定性去重，不能把任意自然语言对象解释成标签。
# 函数用途: 规范化正式记忆操作的标签列表。
def _operation_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


# LLM: attributes 只复制显式对象，非对象值不得成为隐式结构化事实。
# 函数用途: 规范化长期记忆的结构化扩展字段。
def _operation_attributes(value: object) -> dict | None:
    return dict(value) if isinstance(value, dict) and value else None


# LLM: expiry 是非负 epoch 机器字段，不能从正文中的时间描述推断。
# 函数用途: 校验并规范化正式记忆失效时间。
def _operation_expiry(value: object) -> float:
    if value in (None, ""):
        return 0.0
    expiry = float(value)
    if expiry < 0:
        raise ValueError("expires_at must be a non-negative Unix timestamp")
    return expiry


# LLM: 仅可信内部 add_record 会携带 created_at；无效或非正值必须回退当前提交时间。
# 函数用途: 规范化内部记忆事件时间戳。
def _operation_timestamp(value: object, *, fallback: float) -> float:
    if value in (None, ""):
        return fallback
    timestamp = float(value)
    return timestamp if timestamp > 0 else fallback


# LLM: identity 只做精确规范化幂等，不得扩展成 embedding/模糊语义合并。
# 函数用途: 判断两条 role/kind/正文是否其实是同一条长期记忆。
def _memory_identity_key(record: MemoryRecord) -> tuple[str, str, str]:
    return (
        fold_key(record.role),
        fold_key(record.kind),
        normalized_memory_content(record.content),
    )


# LLM: subject_key 只能从结构化 attributes 读取，不能从正文关键词推断。
# 函数用途: 取得调用方显式给出的稳定主题键。
def _memory_subject_key(record: MemoryRecord) -> str:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    return fold_key(str(attributes.get("subject_key") or ""))


# LLM: 同一主题在 personal/company/project 等不同 scope 可以并存；空 scope 仍作为 legacy 单一范围。
# 函数用途: 构造长期事实冲突与召回去重共用的 subject+scope 身份。
def _memory_subject_identity(record: MemoryRecord) -> tuple[str, str, str]:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    return (
        _memory_subject_key(record),
        fold_key(str(attributes.get("scope_type") or "legacy")),
        fold_key(str(attributes.get("scope_key") or "legacy")),
    )


# LLM: 同一 subject 的不同正文必须返回现有 entry_id，禁止静默覆盖或新增冲突副本。
# 函数用途: 在新增或替换前检查主题唯一性。
def _raise_subject_conflict(
    record: MemoryRecord,
    existing_records: Iterable[MemoryRecord],
) -> None:
    subject_key = _memory_subject_key(record)
    if not subject_key:
        return
    identity = _memory_subject_identity(record)
    for existing in existing_records:
        if (
            _memory_subject_identity(existing) == identity
            and _memory_identity_key(existing) != _memory_identity_key(record)
        ):
            raise MemorySubjectConflict(subject_key, existing.entry_id)


# LLM: 关键词候选只按统一规范化文本评分，不改变来源权威或 scope 决策。
# 函数用途: 从一组已验证 active 记录中选择有界文本命中。
def _search_memory_records(records: list[MemoryRecord], query: str, top_k: int) -> list[MemoryRecord]:
    query_terms = {term for term in fold_key(query).split() if term}  # 统一规范化(审计 #20)
    scored: list[tuple[int, float, MemoryRecord]] = []
    for record in records:
        score = _memory_search_score(record, query, query_terms)
        if score > 0 or not query_terms:
            scored.append((score, record.created_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:top_k]]


# LLM: 分数只用于排序，不能替代 evidence、status 或 scope 过滤。
# 函数用途: 计算一条正式记忆与查询的确定性关键词分数。
def _memory_search_score(record: MemoryRecord, query: str, query_terms: set[str]) -> int:
    text = fold_key(record.content)  # 与 query_terms 同走统一规范化(审计 #20):全角/NFD/大小写不漏命中
    score = sum(1 for term in query_terms if term in text)
    folded_query = fold_key(query)
    if folded_query and folded_query in text:
        score += 3
    # 中文召回增强(真机缺口 2026-08-06):中文无空格,split 后整句匹配失败(如 query
    # "萧战认为最对的事是什么" vs 记忆"萧战认为最对的事是没让..."——整句不命中)。
    # 用 query 的滑动窗口子串在 content 中命中计分(中文短问也能召回)。纯结构化、确定性。
    if len(folded_query) >= 4:
        text_len = len(text)
        for window in (4, 6, 8, 12):
            hits = 0
            step = max(1, window // 2)
            for start in range(0, max(1, len(folded_query) - window + 1), step):
                sub = folded_query[start : start + window]
                if sub and sub in text:
                    hits += 1
            if hits:
                score += hits
                break
    return score


# LLM: rerank 只能读取文本匹配、结构化来源和时间，不调用模型、不解析业务状态。
# 函数用途: 在有界召回候选中确定性排序更相关、更可信且更新的记忆。
def _rerank_memory_records(
    records: list[MemoryRecord],
    query: str,
    top_k: int,
) -> list[MemoryRecord]:
    """Deterministically rank recalled facts without another model call."""

    if top_k <= 0:
        return []
    query_terms = {term for term in fold_key(query).split() if term}
    origin_weight = {
        "user_explicit": 3,
        "reviewed": 3,
        "tool_verified": 2,
        "legacy": 1,
    }
    ranked: list[tuple[int, int, float, int, MemoryRecord]] = []
    for index, record in enumerate(records):
        attributes = record.attributes if isinstance(record.attributes, dict) else {}
        origin = str(attributes.get("origin") or "legacy")
        ranked.append(
            (
                _memory_search_score(record, query, query_terms),
                origin_weight.get(origin, 0),
                float(record.updated_at or record.created_at or 0.0),
                -index,
                record,
            )
        )
    ranked.sort(key=lambda item: item[:4], reverse=True)
    return _select_diverse_memory_records(
        [record for _score, _trust, _updated, _rank, record in ranked],
        top_k,
    )


# LLM: diversity 仅按显式 subject/entry 去重，不能按自然语言相似度丢记录。
# 函数用途: 保留召回顺序，同时避免同一主题或条目重复占满上下文。
def _select_diverse_memory_records(
    records: list[MemoryRecord],
    top_k: int,
) -> list[MemoryRecord]:
    selected: list[MemoryRecord] = []
    seen_subjects: set[str] = set()
    seen_entries: set[str] = set()
    for record in records:
        if record.entry_id and record.entry_id in seen_entries:
            continue
        attributes = record.attributes if isinstance(record.attributes, dict) else {}
        subject_identity = _memory_subject_identity(record)
        subject_key = subject_identity[0]
        subject_marker = "\x1f".join(subject_identity)
        if subject_key and subject_marker in seen_subjects:
            continue
        selected.append(record)
        if record.entry_id:
            seen_entries.add(record.entry_id)
        if subject_key:
            seen_subjects.add(subject_marker)
        if len(selected) >= top_k:
            break
    return selected


# LLM: 分组按调用方优先级合并并使用精确记录键去重，不做语义覆盖。
# 函数用途: 合并多个检索投影并保持确定性先后顺序。
def _merge_search_result_groups(groups: tuple[list[MemoryRecord], ...], *, top_k: int) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for group in groups:
        _append_unique_records(records, seen, group, top_k=top_k)
        if len(records) >= top_k:
            return records
    return records


# LLM: helper 只追加尚未出现的精确键，并严格遵守 top_k 上限。
# 函数用途: 向检索结果追加一组不重复记录。
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


# LLM: 去重只按精确规范化键，不能把相似但 scope/事实不同的内容合并。
# 函数用途: 对 legacy 检索记录做稳定去重。
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
