
from __future__ import annotations

"""Owner 正式长期记忆 JSONL 权威仓库及其可重建检索投影。"""

# LLM: memory.jsonl is the only formal long-term fact authority; indexes and ops never become recall authorities.
# 模块用途: 提供长期事实的稳定 ID 新增/替换/删除、原子批处理、hard delete 与 active 召回。

import json
import logging
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
    write_private_text_file_atomic_unlocked,
)
from ..common.text_norm import fold_key, nfc
from ..retrieval.embedding_usage import EMBEDDING_USAGE, counted_as
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
        semantic_status: dict[str, str] | None = None,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        vector_identity: dict[str, object] | None = None,
    ):
        """初始化 JSONL 记忆文件位置，并确保父目录存在。

        新手说明:
        创建 JsonlMemory 时只准备文件路径和可选索引对象，不会读取全部记忆。

        path: JSONL 文件路径。
        local_store: 可选 LocalStore，用于索引和搜索；为空时仍可正常写 JSONL。
        vector_identity: 可选向量空间身份（档案编号/线路协议/端点摘要/模型名，见 embedding_identity）；
            P14 用它裁决向量库快照，不一致、缺失或维度不符时语义召回退回关键词，不静默混用。
            None 是不管理身份的通用模式，只给测试和通用组件用；生产接线身份建不起来会直接关掉语义通道。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。"""
        self.path = Path(path)
        self.local_store = local_store
        self.ops_path = Path(ops_path) if ops_path is not None else None
        self.candidate_service = candidate_service
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._embedder = embedder  # 配了 → 记忆召回加一路语义向量(检索拓宽 #1);None → 纯关键词
        self._semantic_status = dict(semantic_status or {"state": "configured" if embedder is not None else "disabled"})
        self._semantic_errors: dict[str, str] = {}
        self._vector_store_cache = None
        self._text_vector_cache_obj = None
        self._vector_identity = dict(vector_identity) if vector_identity else None
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
                # 被本次提交取代的旧正文：replace 会用新正文覆盖同 entry_id 的旧版本，
                # 旧正文的缓存键必须在本次一并清掉，否则它只要事实还活着就永远留在缓存里（P2）。
                superseded_versions = [
                    prior
                    for record in committed
                    if record.action == "replace" and (prior := latest_events.get(record.entry_id)) is not None
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
                write_private_text_file_atomic_unlocked(self.path, next_text)
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
        # 派生缓存不留已删除或被替换正文的向量：按与写入相同的键口径清掉，避免缓存只增不减。
        # 这里必须用记录级索引文本（含 keywords_en），裸正文算出的键和写入键对不上（复审探针 P1）。
        # 已删事实取历史全部版本（含 final_removed_ids 命中的当前版本与 tombstone）；
        # replace 只取被覆盖的旧版本，新正文的键留给下次检索重建（P2）。
        self._forget_cached_vectors(
            [
                (record.content, record.attributes)
                for record in [*current_events, *committed]
                if record.entry_id in final_removed_ids and record.content
            ]
            + [(record.content, record.attributes) for record in superseded_versions if record.content]
        )
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
                write_private_text_file_atomic_unlocked(self.path, next_text)
        append_memory_purge_event(
            self.ops_path,
            entry_ids=set(expected),
            content_hashes={content_hash for _version, content_hash in expected.values()},
        )
        append_memory_operation_events(self.ops_path, tombstones)
        self._after_commit_indexes(tombstones)
        return tombstones

# LLM: 检索投影与生命周期治理分离:Search 只消费 active JSONL 并返回不含陈旧索引正文的召回。
# 类用途: 提供关键词/语义融合与 scope 内确定性召回。
class _JsonlMemorySearchMixin:
    # LLM: 每个向量命中必须按 active entry ID 和正文逐字复核，旧向量不能复活历史内容；
    #   身份不一致、没有身份、维度不符或库读不了时，把已有向量当作不存在（退回关键词），不静默混用两个向量空间。
    #   前置 identity_status 只为省一次付费嵌入；真正的裁决在 search 里对同一份快照再做一次（P14 第 3 条）。
    #   检索成功会清掉 identity/search 两类旧错误（别的进程重建后本进程自动恢复）。
    #   嵌入请求按“召回”计入进程用量（S7，embedding_usage）。第二个返回值是这一次语义臂的降级原因（空串表示语义检索做成了，
    #   零命中也算做成），给 _fuse_semantic 记召回方式用，口径同 HybridRetriever：嵌入失败 embedding_failed，身份不符用身份原因码。
    # 函数用途: 返回经过正式 JSONL 二次核验的语义检索结果和降级原因，失败保留可观察降级状态。
    @counted_as("memory_recall")
    def _semantic_records(self, query: str, top_k: int) -> tuple[list[MemoryRecord], str]:
        from ..retrieval.vector_store import VectorIdentityError

        try:
            store = self._vector_store()
            if store is None:
                return [], "embedder_unavailable"
            ok, reason = store.identity_status()
            if not ok:
                raise VectorIdentityError(reason)
            hits = store.search(self._embedder.embed([query])[0], top_k=top_k)
        except VectorIdentityError as exc:
            self._record_semantic_health("identity", exc)
            return [], str(getattr(exc, "reason", "") or "vector_identity_mismatch")
        except Exception as exc:
            self._record_semantic_health("search", exc)
            return [], "embedding_failed"
        self._record_semantic_health("identity")
        self._record_semantic_health("search")
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
        return records, ""

    # LLM: 搜索结果最终必须回到 active JSONL，并按确定性规则去重排序。不带作用域的检索（Gateway 与 IM 的 /memory、agent.recall）
    #   也按“每次检索记一次”计入召回方式（S7）：没有嵌入端记 keyword 和不可用原因，有嵌入端由 _fuse_semantic 记。
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
            EMBEDDING_USAGE.record_retrieval("keyword", _semantic_unavailable_reason(self._semantic_status))
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
        return self._search_scoped(query, top_k, predicate, record_access=True)[0]

    # LLM: 候选检索复用正式 allowlist 与混合排序，但候选尚未注入上下文，不能写访问信号。
    # 函数用途: 为可选的补充召回寻找候选；只有最终采用的记录可交给 confirm_scoped_access。
    def search_scoped_candidates(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
    ) -> list[MemoryRecord]:
        return self._search_scoped(query, top_k, predicate, record_access=False)[0]

    # LLM: 与 search_scoped_candidates 同一检索（同 allowlist、同排序、不写访问信号），另返回本次检索的结构化事实：
    #   mode（semantic/keyword/none）、fallback_reason、scoped_entries 与存储层 semantic_recall 状态。只读工具据此
    #   如实告诉模型这次走的是语义还是关键词；不得用它改变自动召回或访问信号。
    # 函数用途: 给只读记忆检索工具返回候选记录和“这次怎么检索的”事实，不登记访问。
    def search_scoped_candidates_report(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
    ) -> tuple[list[MemoryRecord], dict[str, object]]:
        return self._search_scoped(query, top_k, predicate, record_access=False)

    # LLM: 访问确认重读正式源并核对原 scope、ID、版本及正文；撤销或替换的候选不得留下 touch。
    # 函数用途: 在最终注入记忆后，给仍然有效的候选记录登记访问信号并返回已确认记录。
    def confirm_scoped_access(
        self,
        records: Iterable[MemoryRecord],
        predicate: Callable[[MemoryRecord], bool],
    ) -> list[MemoryRecord]:
        current = {record.entry_id: record for record in self.all() if predicate(record)}
        confirmed: list[MemoryRecord] = []
        seen: set[str] = set()
        for record in records:
            active = current.get(record.entry_id)
            if (active is None or active.entry_id in seen or active.version != record.version
                    or active.content != record.content or active.attributes != record.attributes
                    or active.source != record.source or active.kind != record.kind
                    or active.role != record.role or active.expires_at != record.expires_at):
                continue
            seen.add(active.entry_id)
            confirmed.append(active)
            self._note_access(active.entry_id)
        return confirmed

    # LLM: 正式与候选检索只能在访问确认时分叉；索引、scope 与排序必须完全相同。检索事实（第二个返回值）只是观察，
    #   取自 HybridRetriever 的 last_retrieval_mode/last_fallback_reason，不参与排序。
    #   这里发出的嵌入请求（查询与缺向量的条目）按“召回”计入进程用量（S7，embedding_usage）。
    # 函数用途: 执行原 scoped 检索；正式召回记录访问，候选检索只返回记录；同时返回本次检索方式。
    @counted_as("memory_recall")
    def _search_scoped(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
        *,
        record_access: bool,
    ) -> tuple[list[MemoryRecord], dict[str, object]]:
        if top_k <= 0:
            return [], _scoped_retrieval_facts(None, 0, self._semantic_status)
        active = [record for record in self.all() if predicate(record)]
        retriever = None
        candidate_limit = max(top_k * 4, top_k + 8)
        # 混合检索:BM25 词面 + 向量语义 RRF 融合(治"换词就召不回"),替代原纯子串+自管 RRF;
        # embedder 缺失或端点抖动时自动降级纯 BM25(仍强于纯子串)。active JSONL 已建 allowlist,
        # 索引命中不能复活删除版本;embedder 语义只在本列表内打分,不再走全库 _semantic_records。
        if active:
            from ..retrieval.hybrid import HybridRetriever
            from ..retrieval.text_vector_cache import index_text

            # BM25 臂拼入写路径生成的关键词(keywords_en),让英文查询词面命中中文记忆;
            # embedding 臂本就跨语言(mean centering 实证),两臂互补。
            # 索引文本必须与缓存键用同一口径（index_text），否则写入键与清理键不一致。
            docs = [(record.entry_id, index_text(record.content, record.attributes)) for record in active]
            cached = self._cached_vectors_for(docs)
            retriever = HybridRetriever(self._embedder)
            ranked = retriever.rank(
                query,
                docs,
                top_k=candidate_limit,
                # 注入侧阈值化:向量臂只留 cosine≥0.30 的真相关,弱相关不凑数带进 prompt。
                # 词面臂(BM25)不受限——词面重合本身是相关信号;无 embedder 自动降级纯 BM25。
                vector_min_score=0.30,
                cached_vectors=cached,
            )
            self._remember_cached_vectors(docs, retriever, cached)
            by_id = {record.entry_id: record for record in active}
            keyword = [by_id[entry_id] for entry_id, _score in ranked if entry_id in by_id]
        else:
            keyword = []
        selected = _rerank_memory_records(keyword, query, top_k)
        if record_access:
            for record in selected:
                self._note_access(record.entry_id)
        return selected, _scoped_retrieval_facts(retriever, len(active), self._semantic_status)

    # LLM: 检索侧缓存只是派生索引，读取失败必须静默退化为"没有缓存"（现嵌），结果不变。
    #   先用权威向量库 memory_vectors.json 里已有的向量（_stored_vectors_for：身份对上、文本完全一致），剩下的才查正文哈希缓存；
    #   第一次召回因此不再把写入/重建时嵌过的记忆再嵌一遍（S3，用户 10-02 拍板）。两段查找放在模块级，免得撑大检索 mixin。
    #   竞态（取缓存之后事实才被删/被替换）统一在回写点用 active 身份复核收口，
    #   见 `_remember_cached_vectors`，此处不做第二套判定（重复判据会变成不可达的冗余防御）。
    # 函数用途: 取出本轮检索可复用的文档向量（先向量库、后正文哈希缓存），避免重复嵌入。
    def _cached_vectors_for(
        self,
        docs: list[tuple[str, str]],
    ) -> dict[str, list[float]]:
        reused = _stored_vectors_for(self, docs)
        rest = [(doc_id, text) for doc_id, text in docs if doc_id not in reused]
        return {**reused, **_text_cache_vectors_for(self, rest)}

    # LLM: 缓存写失败不能影响检索结果；只把本轮真正现嵌过的向量按正文哈希写回。
    #   竞态收口：写回前用 active 身份复核每个 id，索引文本变了或记录已不在 active，
    #   一律不回写（P5：否则已删事实的向量会被检索回写复活）。
    # 函数用途: 把本次检索中现嵌得到的向量补进正文哈希缓存。
    def _remember_cached_vectors(
        self,
        docs: list[tuple[str, str]],
        retriever,
        cached: dict[str, list[float]],
    ) -> None:
        pending = getattr(retriever, "last_fresh_doc_vectors", None)
        if not isinstance(pending, dict) or not pending:
            return
        try:
            cache = self._text_vector_cache()
            if cache is None:
                return
            from ..retrieval.text_vector_cache import embedder_fingerprint, index_text

            fingerprint = embedder_fingerprint(self._embedder)
            texts = dict(docs)
            rows: dict[str, list[float]] = {}
            for doc_id, vector in pending.items():
                if doc_id not in texts or doc_id in cached or not isinstance(vector, list):
                    continue
                rows[self._cache_key(fingerprint, texts[doc_id])] = vector
            if not rows:
                return
            # 有效性强弱只由**这一处**判据决定：在缓存锁内按最新 active 重算有效键集合。
            # 锁外再放一套等价复核会变成永远测不到的冗余防御（变异验证会存活），故不做。
            # 它同时收口两件事：记录已删除/被替换（P5），以及复核之后、写入之前发生的删除（P5b）。
            cache.put(rows, keep=lambda: self._live_cache_keys(fingerprint))
            self._record_semantic_health("cache_write")
            reported = getattr(cache, "last_write_error", None)
            if reported:
                self._record_semantic_health("cache_write", reported)
        except Exception as exc:
            self._record_semantic_health("cache_write", exc)

    # LLM: 锁内复核判据必须是最新 active 状态；只返回当前仍有效的缓存键集合。
    # 函数用途: 给 cache.put 提供"写入时刻仍然有效"的键集合。
    def _live_cache_keys(self, fingerprint: str) -> set[str]:
        return {self._cache_key(fingerprint, text) for text in self._cache_texts(self.all())}

    # LLM: 清理必须用与写入完全相同的键口径（index_text + 指纹），否则带 keywords_en 的事实删不掉；
    #   replace 时旧版本正文也一并清，缓存才有上限（复审探针 P1/P2）。
    # 函数用途: 删除或替换事实时同步清掉对应的正文哈希缓存项。
    def _forget_cached_vectors(self, contents: Iterable[tuple[str, object]]) -> None:
        try:
            # 这个调用点最容易踩坑：它在 apply_batch **已经提交之后**执行，
            # 懒建缓存若在 try 外读盘失败，会把已落盘的写入报成失败，调用方会重试。
            cache = self._text_vector_cache()
            if cache is None:
                return
            from ..retrieval.text_vector_cache import embedder_fingerprint, index_text

            fingerprint = embedder_fingerprint(self._embedder)
            keys = [
                self._cache_key(fingerprint, index_text(content, attributes))
                for content, attributes in contents
                if content
            ]
            if keys:
                cache.remove(keys)
            self._record_semantic_health("cache_purge")
        except Exception as exc:
            self._record_semantic_health("cache_purge", exc)

    # LLM: 指纹与正文哈希的组合只有一个实现，读/写/清理三处共用，避免键口径再次分叉。
    # 函数用途: 按当前 embedder 指纹拼出一条索引文本的缓存键。
    def _cache_key(self, fingerprint: str, text: str) -> str:
        from ..retrieval.text_vector_cache import text_cache_key

        return text_cache_key(fingerprint, text)

    # LLM: 缓存键要跟"写入时用的索引文本"对齐，因此清理时也必须用 index_text 重算，而不是裸正文。
    # 函数用途: 给出一批记录参与缓存键的索引文本。
    def _cache_texts(self, records: Iterable[MemoryRecord]) -> list[str]:
        from ..retrieval.text_vector_cache import index_text

        return [index_text(record.content, record.attributes) for record in records if record.content]

    # LLM: RRF 只融合两个可重建候选列表，返回前仍使用稳定 entry ID 对齐。副作用：按语义臂这一次的结果记一次召回方式
    #   （S7：做成了记 semantic，降级记 keyword 和原因码），只被不带作用域的 search 调用。
    # 函数用途: 合并关键词与语义召回顺序，并记下这次召回走的方式。
    def _fuse_semantic(self, query: str, keyword_records: list[MemoryRecord], top_k: int) -> list[MemoryRecord]:
        """关键词召回 + 语义召回 RRF 融合(检索拓宽 #1)。语义为空则退回纯关键词(不崩不退化)。"""
        semantic, fallback_reason = self._semantic_records(query, top_k)
        EMBEDDING_USAGE.record_retrieval("keyword" if fallback_reason else "semantic", fallback_reason)
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


# LLM: 生命周期治理与检索投影分离:Lifecycle 只做访问信号、总量闸与健康快照,不直接召回。
# 类用途: 提供访问记录、flush、condense、过期视图与运行快照。
class _JsonlMemoryLifecycleMixin:
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
            write_private_text_file_atomic_unlocked(self.path, next_text)
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
            write_private_text_file_atomic_unlocked(archive_path, archived_text)
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
            "health": "degraded" if load_error_count or self._semantic_status.get("state") == "degraded" else "healthy",
            "active_total": len(active),
            "active_by_kind": by_kind,
            "event_total": len(events),
            "load_error_count": load_error_count,
            "load_error_codes": sorted(set(error_codes)),
            "text_index": "configured" if self.local_store is not None else "unconfigured",
            "semantic_index": "configured" if self._embedder is not None else "unconfigured",
            "semantic_recall": dict(self._semantic_status),
        }


# LLM: 投影：没有检索器（top_k<=0 或范围内没有条目）时 mode=none；semantic_recall 是存储层状态副本，
#   含档案不可用等原因码，不含正文和凭据。放在模块级是为了不撑大检索 mixin。
#   没有嵌入端时，fallback_reason 按存储层结构化诊断写明为什么没有（见 _semantic_unavailable_reason）。
#   每次 scoped 检索恰好调用一次，所以是 scoped 检索的召回方式进程计数点（S7，EMBEDDING_USAGE.record_retrieval）；不带作用域的
#   search 另在自己的路径上记一次（search / _fuse_semantic），两条路径互不重复。
# 函数用途: 把一次 scoped 检索的方式、降级原因和范围条目数整理成结构化事实，并记一次召回方式计数。
def _scoped_retrieval_facts(
    retriever: object | None, scoped_entries: int, semantic_status: dict[str, str]
) -> dict[str, object]:
    reason = str(getattr(retriever, "last_fallback_reason", "") or "")
    if reason == "embedder_unavailable":
        reason = _semantic_unavailable_reason(semantic_status)
    facts = {
        "mode": str(getattr(retriever, "last_retrieval_mode", "none") or "none"),
        "fallback_reason": reason,
        "scoped_entries": scoped_entries,
        "semantic_recall": dict(semantic_status),
    }
    EMBEDDING_USAGE.record_retrieval(str(facts["mode"]), reason)
    return facts


# LLM: 只读权威向量库：库不可用、身份对不上或读失败都返回空（等于没有可复用的），不报错、不写盘；只认条目 text 与本轮
#   索引文本逐字相同的向量（写入与重建都按 index_text 嵌入并存这段文本，旧条目只要文本不同就不复用）。维度由检索器再核。
#   memory 是检索 mixin 所在的 JsonlMemory；放在模块级是为了不撑大检索 mixin。
# 函数用途: 从 memory_vectors.json 取出能直接复用的文档向量。
def _stored_vectors_for(memory: object, docs: list[tuple[str, str]]) -> dict[str, list[float]]:
    if not docs:
        return {}
    try:
        store = memory._vector_store()
        stored = store.stored_vectors(doc_id for doc_id, _text in docs) if store is not None else {}
    except Exception:
        return {}
    texts = dict(docs)
    return {doc_id: vector for doc_id, (vector, text) in stored.items() if text == texts.get(doc_id)}


# LLM: 正文哈希缓存（memory_text_vectors.json）只补向量库没有的那部分；读失败等于没有缓存，并记入语义健康状态。
#   懒建缓存必须放进 try：构造缓存时会读盘，读错误不能冒出检索路径（模块合同：读失败等价于没有缓存，检索结果不变）。
#   放在模块级是为了不撑大检索 mixin。
# 函数用途: 按正文哈希取出已缓存的向量。
def _text_cache_vectors_for(memory: object, docs: list[tuple[str, str]]) -> dict[str, list[float]]:
    try:
        cache = memory._text_vector_cache()
        if cache is None or not docs:
            return {}
        from ..retrieval.text_vector_cache import embedder_fingerprint

        fingerprint = embedder_fingerprint(memory._embedder)
        keys = {doc_id: memory._cache_key(fingerprint, text) for doc_id, text in docs}
        found = cache.get(list(keys.values()))
        memory._record_semantic_health("cache_read")
        return {doc_id: found[key] for doc_id, key in keys.items() if isinstance(found.get(key), list)}
    except Exception as exc:
        memory._record_semantic_health("cache_read", exc)
        return {}


# LLM: 只读 composition root 写下的结构化诊断（state/error_code），不读日志文案：没开语义召回是 semantic_recall_disabled；
#   通道降级时从诊断码本身推导（去掉 MEMORY_ 前缀、转小写，如 MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE →
#   embedding_identity_unavailable），新增诊断码不用改这里；没有诊断码时保持 embedder_unavailable。
# 函数用途: 说明这次检索为什么没有嵌入端可用。
def _semantic_unavailable_reason(semantic_status: dict[str, str]) -> str:
    if str(semantic_status.get("state") or "") == "disabled":
        return "semantic_recall_disabled"
    code = str(semantic_status.get("error_code") or "").strip().lower()
    return code.removeprefix("memory_") or "embedder_unavailable"


# LLM: This mixin owns reads and rebuildable search projections; it never becomes the formal memory authority.
# 类用途: 从 active JSONL 读取并执行关键词、FTS 与可选向量召回。
class _JsonlMemoryRecallMixin(_JsonlMemorySearchMixin, _JsonlMemoryLifecycleMixin, JsonlMemoryIndexMixin):
    # LLM: 按操作独立记录派生索引错误；只有该操作成功才清除，检索成功不能掩盖索引写失败。
    #   带 reason 属性的异常（如 VectorIdentityError）以 reason 码作为错误码，保证结构一致。
    # 函数用途: 更新健康投影并给出不含密钥/正文的警告，正式 JSONL 状态不受影响。
    def _record_semantic_health(self, operation: str, error: Exception | None = None) -> None:
        if error is None:
            self._semantic_errors.pop(operation, None)
        else:
            code = getattr(error, "reason", None) or type(error).__name__
            if self._semantic_errors.get(operation) != code:
                logging.getLogger(__name__).warning("语义记忆 %s 降级（%s），正式记忆保留", operation, code)
            self._semantic_errors[operation] = code
        self._semantic_status = {
            "state": "degraded" if self._semantic_errors else "configured",
            **({"errors": dict(self._semantic_errors)} if self._semantic_errors else {}),
        }

    # LLM: 向量库位于当前 owner memory 根且仅是 lazy projection；无 embedder 时必须完全关闭。
    # 函数用途: 获取当前 owner 的可重建向量索引实例。
    def _vector_store(self):
        """本地 per-owner 向量库(memory_vectors.json,在 owner home 内)。无 embedder 返回 None。

        只用本地文件,绝不碰共享向量库(零外部依赖、按 owner 天然隔离)。懒建缓存。
        P14: 带上当前空间身份，向量库按同一份快照里的身份与实际维度裁决读写，不一致时视为不存在。
        """
        if self._embedder is None:
            return None
        if self._vector_store_cache is None:
            from ..retrieval.vector_store import VectorStore

            self._vector_store_cache = VectorStore(self._vector_store_path(), identity=self._vector_identity)
        return self._vector_store_cache

    # LLM: 向量库固定在正式记忆 JSONL 同目录的 memory_vectors.json；状态预览和语义通道必须指向同一文件。
    # 函数用途: 返回当前 owner 向量库快照文件路径。
    def _vector_store_path(self) -> Path:
        return self.path.parent / "memory_vectors.json"

    # LLM: 正文哈希缓存独立成文件（memory_text_vectors.json），检索路径永不重写含明文向量库；
    #   无 embedder 时必须完全关闭。它只是派生数据，丢了按"没有缓存"重算。
    # 函数用途: 获取当前 owner 的正文哈希向量缓存实例。
    def _text_vector_cache(self):
        """本地 per-owner 正文哈希缓存(memory_text_vectors.json)。无 embedder 返回 None。"""
        if self._embedder is None:
            return None
        if self._text_vector_cache_obj is None:
            from ..retrieval.text_vector_cache import TextVectorCache

            self._text_vector_cache_obj = TextVectorCache(self.path.parent / "memory_text_vectors.json")
        return self._text_vector_cache_obj

    # LLM: 向量写失败不改变权威提交；metadata 仍需携带可与 active JSONL 复核的完整身份。
    #   身份不一致时拒绝写入（不把新模型的向量混进旧库），降级状态可见，等重建修正。
    #   嵌入请求按“记忆写入”计入进程用量（S7，embedding_usage）。
    # 函数用途: 尽力把一条正式记忆写入向量索引，失败进入健康诊断，不回滚正式记忆。
    @counted_as("memory_write")
    def _index_vector(self, record: MemoryRecord) -> None:
        try:
            store = self._vector_store()
            if store is None:
                return
            # 写侧裁决统一交给 VectorStore.upsert（锁内重读最新快照）：空库由当前身份接管；有向量却没身份、身份不一致、
            # 维度不符或库读不了都拒绝混写，VectorIdentityError 在下方按 reason 记录降级。
            # 嵌入与召回同一段索引文本（正文 + keywords_en），条目 text 存的就是它：召回据此判断能否直接复用（S3）。
            from ..retrieval.text_vector_cache import index_text

            text = index_text(record.content, record.attributes)
            vector = self._embedder.embed([text])[0]
            store.upsert(_record_vec_id(record), vector, text=text, metadata=_record_payload(record))
            self._record_semantic_health("index")
        except Exception as exc:
            self._record_semantic_health("index", exc)

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
            # 即便没配 LocalStore，正文哈希缓存仍可能积累孤儿键；回收不依赖 FTS 索引。
            self._retain_text_cache_keys()
            return 0
        count = 0
        for record in self.all():
            self._index_record(record)
            count += 1
        self._retain_text_cache_keys()
        return count

    # LLM: 缓存会随换模型/迁移/绕过写入路径的改动积累孤儿键；这里按当前 active 事实重算保留集合，
    #   把不属于任何 active 记录的键丢弃（含旧文件遗留与旧指纹的空 (id) 键），保持缓存有上限。
    #   保留集合必须用与写入相同的 index_text 口径，否则会把仍有效的键误删（下次只是重嵌，不会出错）。
    #   与写入路径不同，回收只需**当前** embedder 的指纹：其他指纹的键本就命中不了，一并清掉更省空间。
    # 函数用途: 回收正文哈希缓存里不属于当前 active 事实的键。
    def _retain_text_cache_keys(self) -> int:
        try:
            cache = self._text_vector_cache()
            if cache is None:
                return 0
            from ..retrieval.text_vector_cache import embedder_fingerprint

            fingerprint = embedder_fingerprint(self._embedder)
            keep = {self._cache_key(fingerprint, text) for text in self._cache_texts(self.all())}
            dropped = cache.retain(keep)
            self._record_semantic_health("cache_purge")
            return dropped
        except Exception as exc:
            self._record_semantic_health("cache_purge", exc)
            return 0

    # LLM: 供 owner 维护这类**没有嵌入模型**的调用方回收孤儿键。此处刻意不依赖 embedder：
    #   键的形状是 `<指纹>:<正文哈希>`，保留集合只按**正文哈希**判定——只要某个 active 记录的
    #   index_text 哈希与键的正文部分一致，该键就保留（无论指纹属于哪个模型/端点；换过模型的旧键
    #   也会因为正文仍 active 而被保留，下次命不中自然重嵌，不构成正确性问题）。
    #   这样"键仍属于某条 active 事实"这一判据与 embedder 无关，维护进程无需加载模型即可回收。
    #   **已知时序**：保留集合是在文件锁**之外**算的（只读 active 记录，不持缓存锁）。
    #   算完到 `retain_content_hashes` 真正拿锁之间新加的事实，其键可能还没进缓存就被当成孤儿删掉——
    #   最坏后果是下一次检索多嵌一次，不影响正确性。
    # 函数用途: 对外暴露孤儿键回收，不要求调用方持有 embedder 上下文。
    # LLM: 回收**本身失败**（缓存读不了、拿不到锁）必须与"确实没有孤儿"分得开，
    #   否则维护状态天天写 `reclaimed: 0`，看起来像"跑过、没孤儿"，实际是没跑成——
    #   这正是上一轮被复审抓到的"假的结构化事实"。失败时返回错误说明由调用方带进维护状态。
    # 函数用途: 回收孤儿键；返回 (回收条数, 错误说明)，无错误时说明为空串。
    def reclaim_text_cache_orphans(self) -> tuple[int, str]:
        cache = self._text_vector_cache_for_reclaim()
        if cache is None:
            return 0, "cache_unavailable"
        try:
            from ..retrieval.text_vector_cache import index_text, text_content_hash

            keep_hashes = {
                text_content_hash(index_text(record.content, record.attributes))
                for record in self.all()
                if record.content
            }
            dropped = cache.retain_content_hashes(keep_hashes)
        except Exception as exc:
            return 0, f"{type(exc).__name__}: {exc}"
        # 键被成功清点但没删掉任何东西，也可能是"读不出缓存、拿不到锁"——两种情况
        # retain_matching 都只会留下 last_write_error。把它带出来，免得又被当成"没孤儿"。
        error = getattr(cache, "last_write_error", None) or getattr(cache, "last_read_error", None)
        if error is not None:
            return dropped, f"{type(error).__name__}: {error}"
        return dropped, ""

    # LLM: 回收只需要缓存文件本身，不需要 embedder；这里给出一条不经过 _text_vector_cache() 的取用路径。
    # 函数用途: 取得用于孤儿回收的正文哈希缓存实例（无 embedder 也返回）。
    def _text_vector_cache_for_reclaim(self):
        try:
            from ..retrieval.text_vector_cache import TextVectorCache

            return TextVectorCache(self.path.parent / "memory_text_vectors.json")
        except Exception:
            return None

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


# LLM: 向量投影的管理动作（状态预览、全量重建）与日常召回分开：只读写可重建的 memory_vectors.json，不碰 memory.jsonl
#   权威；依赖 Recall mixin 的 _vector_store/_vector_store_path/_record_semantic_health。改动同步 cli/memory_admin_commands、
#   test_vector_identity 与 test_vector_snapshot_consistency。
# 类用途: 给管理员入口提供向量库状态预览和全有或全无的重建。
class _JsonlMemoryVectorAdminMixin:
    # LLM: 预览只读投影，不触发嵌入或清库；重建前先看数量与身份，避免管理员盲操作。没有嵌入客户端时也独立读文件
    #   给出能确认的数量：文件不存在是 0，读不了是 None（未知），不用 0 冒充；active 数始终来自正式 JSONL。
    # 函数用途: 返回向量库现状：条目数、快照头、身份原因码与可重建的 active 记忆数。
    def vector_index_status(self) -> dict[str, object]:
        from ..retrieval.vector_store import VectorStore

        active_count = len(list(self.all()))
        store = self._vector_store()
        if store is None:
            summary = VectorStore(self._vector_store_path()).summary()
            return {"ok": False, "reason": "no_embedder", "active_count": active_count, **summary}
        ok, reason = store.identity_status()
        return {"ok": ok, "reason": reason, "active_count": active_count, **store.summary()}

    # LLM: 重建是管理员显式动作，全有或全无（P14 第 4 条）：逐条嵌入全部 active 记忆、在内存里拼出完整新快照，
    #   任一条失败立即停（后面的不再发请求，省 token），返回 ok=False + VECTOR_REBUILD_INCOMPLETE 和准确计数，旧库原样保留；
    #   全部成功才经 VectorStore.replace_all 一次原子替换。不修改 memory.jsonl 权威事实；失败原因只记类型名或原因码。
    # 函数用途: 全量重嵌当前 owner 的记忆向量，返回尝试数、成功数、失败数、写入数与结构化原因。
    def rebuild_vectors(self) -> dict[str, object]:
        from ..retrieval.vector_store import VectorStoreError

        store = self._vector_store()
        if store is None:
            return {"ok": False, "reason": "no_embedder", "attempted": 0, "embedded": 0, "failed": 0,
                    "rebuilt": 0, "vector_count": None}
        records = list(self.all())
        rows, failure = self._embed_rebuild_rows(records)
        # failed 只数嵌入失败的记录（首个失败即停，所以是 0 或 1）；写盘失败看 failed_stage=write。
        failed = 0 if failure is None else 1
        counts = {"active_count": len(records), "attempted": len(rows) + failed, "embedded": len(rows), "failed": failed}
        if failure is None:
            try:
                store.replace_all(rows)
            except Exception as exc:
                failure = ("write", getattr(exc, "reason", None) or type(exc).__name__)
        if failure is not None:
            self._record_semantic_health("rebuild", VectorStoreError("VECTOR_REBUILD_INCOMPLETE"))
            return {"ok": False, "reason": "VECTOR_REBUILD_INCOMPLETE", "failed_stage": failure[0], "failure": failure[1],
                    "rebuilt": 0, **counts, "vector_count": store.summary()["vector_count"]}
        for operation in ("rebuild", "index", "identity"):
            self._record_semantic_health(operation)
        return {"ok": True, "reason": "", "rebuilt": len(rows), **counts, "vector_count": store.summary()["vector_count"]}

    # LLM: 重建的嵌入阶段：一次只嵌一条，便于准确计数；首个失败即停并返回 ("embed", 原因码或异常类型名)，不吞成功。
    #   与写入、召回同一段索引文本（index_text），条目 text 存这段文本，召回才能直接复用重建好的向量（S3）。
    #   嵌入请求按“重建”计入进程用量（S7，embedding_usage）。
    # 函数用途: 为全部 active 记忆生成重建行，返回 (已成功的行, 失败信息或 None)。
    @counted_as("memory_rebuild")
    def _embed_rebuild_rows(self, records: list[MemoryRecord]) -> tuple[list[tuple], tuple[str, str] | None]:
        from ..retrieval.text_vector_cache import index_text

        rows: list[tuple] = []
        for record in records:
            text = index_text(record.content, record.attributes)
            try:
                vector = self._embedder.embed([text])[0]
            except Exception as exc:
                return rows, ("embed", getattr(exc, "reason", None) or type(exc).__name__)
            rows.append((_record_vec_id(record), vector, text, _record_payload(record)))
        return rows, None


# LLM: One public repository composes the mutation authority and recall projection without duplicate storage paths.
# 类用途: 作为 owner 唯一正式长期记忆仓库，对外保持既有 JsonlMemory 接口。
class JsonlMemory(
    _JsonlMemoryIdentityMixin,
    _JsonlMemoryMutationMixin,
    _JsonlMemoryRecallMixin,
    _JsonlMemoryVectorAdminMixin,
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


# LLM: v2 正式事实必须把显式 subject/scope 纳入精确幂等；legacy 无结构化身份时保持旧键兼容。
# 函数用途: 判断两条 role/kind/正文是否属于同一结构化范围内的同一条长期记忆。
def _memory_identity_key(record: MemoryRecord) -> tuple[str, ...]:
    base = (
        fold_key(record.role),
        fold_key(record.kind),
        normalized_memory_content(record.content),
    )
    subject_identity = _memory_subject_identity(record)
    if not subject_identity[0]:
        return base
    return (*base, *subject_identity)


# LLM: subject_key 只能从结构化 attributes 读取，不能从正文关键词推断。
# 函数用途: 取得调用方显式给出的稳定主题键。
def _memory_subject_key(record: MemoryRecord) -> str:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    return fold_key(str(attributes.get("subject_key") or ""))


# LLM: 同一主题在 personal/company/project 等不同 scope 可以并存；空 scope 仍作为 legacy 单一范围。
# 存储级身份比较与写侧共用同一 canonical 合同：旧账本 task:<id> 与新写入 project:<id>
# 是同一正式身份（project 单一权威），否则读时统一、写时双权威会生成第二条正式事实。
# 非法/未知 scope 值保持原样隔离（fail-closed，不扩大合并范围）。
# 函数用途: 构造长期事实冲突与召回去重共用的 subject+scope 身份。
def _memory_subject_identity(record: MemoryRecord) -> tuple[str, str, str]:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    scope_type = str(attributes.get("scope_type") or "legacy")
    scope_key = str(attributes.get("scope_key") or "legacy")
    scope_type, scope_key = _canonical_scope_pair(scope_type, scope_key)
    return (
        _memory_subject_key(record),
        fold_key(scope_type),
        fold_key(scope_key),
    )


# LLM: canonical 只在可识别 scope 类型上做，未知类型/坏值/空占位退回原字符串（fail-closed 隔离，
# 空 scope 的 "legacy" 占位不得被 canonical 成 "project:legacy" 等真实键）。task:<id> 旧账本
# 归一到 project:<id>，与写侧合同一致（一个概念一个权威位置）。
# 函数用途: 把 scope 身份归一到共享合同键，坏值不动。
def _canonical_scope_pair(scope_type: str, scope_key: str) -> tuple[str, str]:
    if not scope_key or scope_key == "legacy":
        return scope_type, scope_key
    try:
        from .scope_contract import canonical_scope_key

        canonical = canonical_scope_key(scope_type, scope_key)
    except (ValueError, TypeError):
        return scope_type, scope_key
    if canonical == scope_key:
        return scope_type, scope_key
    return scope_type, canonical


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
