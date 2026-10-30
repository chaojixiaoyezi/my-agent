# LLM: 共用原C末门、quota→policy→formal锁和费用计量；搜索前后短formal锁绑定权威指纹，网络不持锁，漂移有界刷新。
# 模块用途: 管理唯一冷热长期记忆库、永久遗忘与派生检索，失败保留真实提交和清理责任。
from __future__ import annotations

"""Owner 正式长期记忆 JSONL 权威仓库及其可重建检索投影。"""

# LLM: 初始JSONL与迁移后同代分片是同一逻辑权威；根选择已提交快照，索引/ops永不成为正文权威。
# 模块用途: 提供稳定ID事实新增/替换/删除、同代冷热提交、永久遗忘与授权召回。

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.global_write_commit import execute_global_write
from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..common.text_norm import fold_key, nfc
from ..retrieval.embedding_usage import EMBEDDING_USAGE, counted_as, embedding_input_type
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange


# LLM: 未知顶层字段单独保留，无损往返；内部_extra_fields不从磁盘授予canonical覆盖权。
# 函数用途: 恢复MemoryRecord并保留未来版本字段，构造失败仍交容错读跳过。
def _memory_record_from_obj(obj: dict) -> MemoryRecord | None:
    """dict → MemoryRecord:过滤未知顶层字段(旧版本读新字段记录不崩),构造失败返回 None(跳过坏记录)。"""
    known = set(MemoryRecord.__dataclass_fields__) - {"_extra_fields"}
    try:
        record = MemoryRecord(**{k: v for k, v in obj.items() if k in known})
        record._extra_fields = {k: deepcopy(v) for k, v in obj.items() if k not in known and k != "_extra_fields"}
        return record
    except (TypeError, ValueError):
        return None

from ._jsonl_indexing import _NATIVE_MEMORY_ERRORS, JsonlMemoryIndexMixin
from .operations import (
    append_memory_operation_events,
    append_memory_purge_event,
    memory_content_hash,
    normalized_memory_content,
)

if TYPE_CHECKING:
    from ..local_storage import LocalStore
    from ..user_space.owner_quota import OwnerQuotaEnforcer
    from .candidates import CandidateService


# LLM: 仅同进程同线程、原锁尚未退出的活对象允许重入；复制Context只能共享失效标记，不能延寿锁。
# 类用途: 记录当前受管policy锁的精确持有期，不保存策略或授予管理员权限。
@dataclass
class _ManagedPolicyHold:
    identity: tuple[str, str]
    pid: int
    thread_id: int
    active: bool = True


_MANAGED_POLICY_HELD: ContextVar[_ManagedPolicyHold | None] = ContextVar("memory_managed_policy_held", default=None)


# LLM: 只解析原登记owner；仅同owner/进程/线程且未失效的活锁可重入，finally先失效复制Context再释放原锁。
# 函数用途: 让晋升和JSONL共用quota之后、candidate/formal之前的治理临界区。
@contextmanager
def _managed_policy_scope(owner_home: Path):
    from ..user_space.managed_user_governance import _ADMIN_MEMORY_WRITE, _persona_governance

    context = _persona_governance(owner_home)
    if context is None:
        yield None
        return
    control, _identity, home = context
    identity = (str(home.root), home.owner_id)
    held = _MANAGED_POLICY_HELD.get()
    if (_ADMIN_MEMORY_WRITE.get() == identity or held is not None and held.active and held.identity == identity
            and held.pid == os.getpid() and held.thread_id == threading.get_ident()):
        yield context
        return
    with control.locked():
        held = _ManagedPolicyHold(identity, os.getpid(), threading.get_ident())
        token = _MANAGED_POLICY_HELD.set(held)
        try:
            yield context
        finally:
            held.active = False
            _MANAGED_POLICY_HELD.reset(token)


# LLM: context仅来自原宿主治理解析，严格bool控制自动学习；目标来自原目标解析器或固定JSONL目标。
# 函数用途: 返回受管自动学习的稳定阻断码，不创建政策、候选或正式记忆。
def _managed_learning_policy_block(context, target: str) -> str:
    if context is None:
        return ""
    policy = context[0].policy()
    flags = policy["memory_governance"]
    if policy.get("memory_curate_mode") == "admin_only":
        return "AUTO_GOVERNANCE_ADMIN_ONLY"
    if flags.get("allow_self_learning") is False:
        return "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    if ((target in {"long_term", "user"} and flags.get("require_admin_review_for_long_term") is True)
            or (target in {"lesson", "hot"} and flags.get("require_admin_review_for_lesson") is True)):
        return "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED"
    return ""


# LLM: 保规范路径、管理员活栈、locked ID及锁序；source字符串不授豁免，同owner晋升活锁只复用不重领。
# 函数用途: 在同一治理锁内核对长期写入的全部管理员学习开关。
@contextmanager
def _managed_learning_batch_guard(path: Path, operations):
    from ..plugin_host.errors import HostAPIError
    from ..user_space.managed_user_governance import (
        _ADMIN_MEMORY_WRITE,
        require_managed_memory_write,
    )

    path = path.absolute()
    if tuple(path.parts[-3:]) != ("memory", "long_term", "memory.jsonl"):
        yield
        return
    with _managed_policy_scope(path.parents[2]) as context:
        if context is not None:
            _control, _identity, home = context
            for operation in operations:
                require_managed_memory_write(home.root, home.home_dir, operation.get("entry_id"))
            if _ADMIN_MEMORY_WRITE.get() != (str(home.root), home.owner_id):
                code = _managed_learning_policy_block(context, "long_term")
                if code:
                    raise HostAPIError(code, 403)
        yield


# LLM: quota 必须先于 policy/正式记忆锁；晋升显式传已持准入，不能重复获取不可重入 flock。
# 函数用途: 进入当前 owner 的长期记忆容量临界区。
@contextmanager
def _quota_admission(
    enforcer: OwnerQuotaEnforcer | None,
    admitted: OwnerQuotaAdmission | None = None,
) -> Iterator[OwnerQuotaAdmission | None]:
    if admitted is not None:
        yield admitted
        return
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


# LLM: 仅保留旧调用方导入名；20k现在是默认热工作集预算，不再作为正式写入拒绝条件。
MEMORY_LIBRARY_WRITE_CAP_CHARS = 20000


# LLM: 历史调用方仍导入此异常；新正式写链永不抛出，避免跨席位导入断裂。
# 类用途: 仅兼容识别旧回执，不能再用于拒绝新的长期记忆。
class MemoryLibraryWriteCapExceeded(RuntimeError):
    code = "MEMORY_LIBRARY_WRITE_CAP_EXCEEDED"

    # LLM: 错误只携带稳定code和数值，不包含正文；调用方据此保留待确认候选而不报告正式提交。
    # 函数用途: 记录学习库写入前后数量与硬帽，供结构化失败回执使用。
    def __init__(self, *, before_chars: int, after_chars: int, cap_chars: int) -> None:
        super().__init__(
            f"{self.code}: active content {after_chars} > cap {cap_chars} (before {before_chars})"
        )
        self.before_chars = before_chars
        self.after_chars = after_chars
        self.cap_chars = cap_chars


# LLM: 乐观版本冲突错误：调用方（如 promotion 的既有条目提交）需要把"并发写者已推进版本"
# 与其他运行时错误区分开，转成结构化冲突结果，而不是让裸异常逃出。
# 类用途: 表示一次 replace/remove 的 expected_version 已落后于当前活动版本。
class MemoryVersionConflict(RuntimeError):
    code = "MEMORY_VERSION_CONFLICT"


# LLM: 正式记忆记录是带版本的事实事件；普通聊天不得自动转成该记录，替换/删除必须按原CAS。
# LLM: 未知字段仅供无损往返，remove必须剥离；访问时间不改变语义版本。
# 类用途: 表示正式记忆版本及前向兼容元数据。
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
    # 删除墓碑的持久清理责任只保存历史hash，不保存旧正文/quote；旧行按空集合兼容。
    removed_content_hashes: tuple[str, ...] = ()
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

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
# LLM: 唯一真实JSONL依赖DTO只聚合原七个引用/配置和IO bool；不签权、不持source/callback、不管理生命周期。
# 类用途: 让原constructor和C显式factory共用同一初始化输入，避免增加第九个展开参数。
@dataclass(frozen=True)
class JsonlMemoryDependencies:
    local_store: LocalStore | None = None
    ops_path: str | Path | None = None
    candidate_service: CandidateService | None = None
    embedder: object | None = None
    semantic_status: dict[str, str] | None = None
    quota_enforcer: OwnerQuotaEnforcer | None = None
    vector_identity: dict[str, object] | None = None
    coordinator_io: bool = False




# LLM: C仅保存IO bool，不保存source/callback；原正式事实、缓存与pending仍同实例，构造次序保持；本mixin负责权威JSONL写操作，检索与索引在下文组合。
# 类用途: 绑定唯一JSONL和原检索依赖，初始化长期记忆并实现新增、替换、删除、批量提交和legacy清除。
class _JsonlMemoryIdentityMixin:
    """JSONL-backed memory store with optional LocalStore indexing.

    新手说明:
    JSONL 与同代根构成正式事实库，MemoryReadIndex 是可重建文本投影。
    SQLite 或 FTS5 出问题不能使已提交事实丢失。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 仅保留旧副本精确删除和remember审计脱敏依赖，不供文本投影读写。"""

    # LLM: 构造器只绑定唯一 authority/ops/Candidate/index/quota；不得增加 legacy 双读路径。
    # 函数用途: 初始化当前 owner 正式长期记忆仓库及可选派生服务。
    # LLM: 原八参constructor只聚合默认False依赖DTO，初始化由同一叶完成；明确C另走from_dependencies，不后置改策略。
    # 函数用途: 准备原正式记忆对象及可选C本地IO策略。
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
        local_store: 可选旧副本删除/审计脱敏依赖；不参与正式索引新增或召回。
        vector_identity: 可选向量空间身份（档案编号/线路协议/端点摘要/模型名，见 embedding_identity）；
            P14 用它裁决向量库快照，不一致、缺失或维度不符时语义召回退回关键词，不静默混用。
            None 是不管理身份的通用模式，只给测试和通用组件用；生产接线身份建不起来会直接关掉语义通道。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。"""

        self._init_from_dependencies(path, JsonlMemoryDependencies(
            local_store=local_store,
            ops_path=ops_path,
            candidate_service=candidate_service,
            embedder=embedder,
            semantic_status=semantic_status,
            quota_enforcer=quota_enforcer,
            vector_identity=vector_identity,
        ))

    # LLM: 原constructor与明确C factory共用唯一字段/目录初始化叶；dependencies是纯真实引用，不保存source，不复制算法。
    # 函数用途: 按原顺序初始化正式记忆，IO策略在第一次目录出生前已确定。
    def _init_from_dependencies(self, path, dependencies):
        local_store = dependencies.local_store
        ops_path = dependencies.ops_path
        candidate_service = dependencies.candidate_service
        embedder = dependencies.embedder
        semantic_status = dependencies.semantic_status
        quota_enforcer = dependencies.quota_enforcer
        vector_identity = dependencies.vector_identity
        coordinator_io = dependencies.coordinator_io
        self.coordinator_io = coordinator_io
        self.path = Path(path)
        self.local_store = local_store
        self.ops_path = Path(ops_path) if ops_path is not None else None
        self.candidate_service = candidate_service
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        if self.coordinator_io:
            _coordinator_memory_write(lambda: ensure_private_dir(self.path.parent))
        else:
            ensure_private_dir(self.path.parent)
        self._embedder = embedder  # 配了 → 记忆召回加一路语义向量(检索拓宽 #1);None → 纯关键词
        self._semantic_status = dict(semantic_status or {"state": "configured" if embedder is not None else "disabled"})
        self._semantic_errors: dict[str, str] = {}
        self._vector_store_cache = None
        self._text_vector_cache_obj = None
        self._vector_identity = dict(vector_identity) if vector_identity else None
        self.quota_enforcer = quota_enforcer
        # 访问信号缓冲:命中先累积内存,随下一次写原子落盘,避免每次召回都写一次文件。
        self._pending_access: dict[str, float] = {}


    # LLM: 仅实际JsonlMemory的明确依赖factory；不借此绕自定义子类初始化，不制造Agent/source或后置改策略。
    # 函数用途: 分配同一个真实JSONL仓库后调用唯一初始化叶，失败不发布半构造对象。
    @classmethod
    def from_dependencies(cls, path, dependencies):
        if cls is not JsonlMemory or type(dependencies) is not JsonlMemoryDependencies:
            raise TypeError("explicit memory construction requires JsonlMemory dependencies")
        memory = cls.__new__(cls)
        memory._init_from_dependencies(path, dependencies)
        return memory

    # LLM: 只取原formal锁；调用方先quota→policy准入，锁内禁止反取上层锁。
    # 函数用途: 共用普通及协调写入的正式记忆锁。
    def _locked_memory(self):
        return locked_json_path(self.path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_memory_write}))

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
        """提交一条正式记忆，超过字符热预算时同代转冷。

        新手说明:
        这是写入主入口。先构造MemoryRecord，再原子提交完整逻辑事实，最后尝试向量投影。
        文本索引由显式有界维护追平，不再双写workspace。
        需要带结构化扩展字段（attributes，如教训的 trigger_conditions）时，
        自行构造 MemoryRecord 走 add_record。

        role: 记忆来源角色，例如 user 或 assistant。
        content: 记忆正文。
        kind: 记忆类型，默认 dialogue。
        tags: 可选标签列表；None 会变成空列表。

        返回说明:
        返回刚写入的 MemoryRecord。

        副作用说明:
        会原子写入正式快照；有embedder时尝试同步派生向量。"""

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
    #   副作用:正式快照原子提交与向量派生更新；文本索引由独立有界维护追平。
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

# LLM: 公共输入规范化后才执行持锁配额变更；迁移只精确清理已有legacy记录，不能扫描跨owner内容。
# 类用途: 原子提交长期记忆批次，并为迁移精确清除已有 legacy 记录。
# LLM: C原quota与formal锁跨业务仍在Root外，成功比较清旧pending，正式/索引/候选责任顺序保持。
# 类用途: 提交原批量记忆事实并保原删除清理partial。
class _JsonlMemoryMutationMixin:
    # LLM: 删除证据仍来自同一精确墓碑；布尔入口保留兼容，不能用其它来源或版本补证。
    # 函数用途: 判断指定正式删除是否已提交。
    def has_committed_removal(self, entry_id: str, prior_version: int | None, source: str) -> bool:
        return self.committed_removal_hashes(entry_id, prior_version, source) is not None

    # LLM: JSONL是唯一权威；None表示不匹配，空tuple表示旧墓碑无历史计划，不重放正式写入。
    # 函数用途: 为删除中断恢复读取精确墓碑上的持久历史hash集合。
    def committed_removal_hashes(self, entry_id: str, prior_version: int | None, source: str) -> tuple[str, ...] | None:
        if type(prior_version) is not int or prior_version < 1 or not source:
            return None
        record = self.committed_removal_record(entry_id)
        if record is not None and record.version == prior_version + 1 and record.source == source:
            return tuple(record.removed_content_hashes)
        return None

    # LLM: 持formal锁经严格权威读取精确墓碑；generation读取当前根并叠加永久屏障，根失败也不回滚已生效删除。
    #   仅legacy兼容读按旧墓碑→事件流取最后语义版本；新写入口不得以新版本绕过已提交的永久屏障。
    # 函数用途: 返回当前权威中的精确删除回执，不把旧归档副本当作独立清理许可。
    def committed_removal_record(self, entry_id: str) -> MemoryRecord | None:
        with self._locked_memory():
            events = _strict_memory_events(self.path)
        latest = next((record for record in reversed(events)
                       if record.entry_id == entry_id and record.action != "touch"), None)
        return latest if latest is not None and latest.action == "remove" else None

    # LLM: 锁内全量验证并原子替换；同一准入内脱敏，正式删除提交后的必要清理失败单独报告且保留墓碑。
    # 函数用途: 一次提交一批新增、替换或删除，任何权威校验失败都不写半批。
    # LLM: C原扫描/配额/编码/脱敏门外，formal准确一次末写成功才compare-delete本次pending；ops/清理仍原顺序且native不洗成功。
    # LLM: 统一quota→受管policy→formal锁；晋升活锁精确复用，锁ID与治理学习开关每次重核，source不授豁免。
    # LLM: 20k只触发同代转冷；ID/hash屏障先于批次内存变更，提交后清理只复用仍持有的外层准入。
    # 函数用途: 沿唯一原批量算法提交完整冷热事实，失败保真实已写与待落访问信号。
    def apply_batch(self, operations: list[dict[str, object]], *, _admission: OwnerQuotaAdmission | None = None) -> list[MemoryRecord]:
        """Validate and append a memory mutation batch as one locked commit.

        The committed logical snapshot remains the sole authority. All operations are applied
        to an in-memory copy while the file lock is held; a bad operation
        aborts before any bytes are replaced.
        """

        if not operations:
            raise ValueError("memory operations list is empty")
        committed: list[MemoryRecord]
        with _quota_admission(self.quota_enforcer, _admission) as admission, _managed_learning_batch_guard(self.path, operations):
            with self._locked_memory():
                current_events = _strict_memory_events(self.path)
                working = _materialize_memory_events(current_events, include_expired=True)
                active = {record.entry_id: record for record in working}
                from .generation_manifest import StorageTombstones

                barrier = StorageTombstones(self.path)
                barriers = barrier.load()
                _require_memory_write_barriers(operations, barrier, barriers)
                latest_events = {record.entry_id: record for record in current_events if record.action != "touch"}
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
                removed_content_hashes.update(digest for record in current_events
                    if record.entry_id in final_removed_ids for digest in record.removed_content_hashes)
                removed_contents = {
                    record.content
                    for record in [*current_events, *committed]
                    if record.entry_id in final_removed_ids and record.content
                }
                persisted = _persisted_remove_events(committed, current_events, final_removed_ids)
                if final_removed_ids:
                    batch_barriers = {**barriers, **{record.entry_id: record for record in persisted
                                      if record.action == "remove"}}
                    _require_memory_write_barriers(operations, barrier, batch_barriers)
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
                pending_snapshot = dict(self._pending_access)
                _write_compacted_memory(self, current_events, [*retained_events, *persisted], pending_snapshot, admission,
                                        keep_history=len(retained_events) + len(persisted) <= 128,
                                        redact_contents=removed_contents)
                _consume_pending_snapshot(self, pending_snapshot)
        from ._jsonl_indexing import removal_cleanup_phase

        with removal_cleanup_phase(formal_memory_changed=True, enabled=bool(final_removed_ids)):
            if removed_content_hashes and self.candidate_service is not None:
                self.candidate_service.redact_content_admitted(_admission, {
                    "content_hashes": removed_content_hashes,
                    "exclude_candidate_ids": {
                        source.removeprefix("candidate:")
                        for record in persisted
                        if record.action == "remove"
                        and (source := str(record.source or "")).startswith("candidate:")
                    },
                })
            if final_removed_ids:
                append_memory_purge_event(
                    self.ops_path,
                    entry_ids=final_removed_ids,
                    content_hashes=removed_content_hashes,
                    **({} if not self.coordinator_io else {"coordinator_io": True}),
                )
            append_memory_operation_events(self.ops_path, persisted, **({} if not self.coordinator_io else {"coordinator_io": True}))
            self._after_commit_indexes(persisted, _admission=_admission)
            # 已删除取全部历史版本；replace仅取被覆盖版本，新正文缓存不在清理范围内。
            if not final_removed_ids:
                self._forget_cached_vectors(
                    [(record.content, record.attributes) for record in superseded_versions if record.content]
                )
        return committed

    # LLM: 此入口仅供显式 Memory schema migration 在已持久备份后清除 legacy active 正文；
    #   它留下 hash tombstone 并清派生索引，但刻意不清 Candidate/工具账本，因为旧正文已迁入新事实链。
    # LLM: 重写前严格验证两文件，统一formal锁及墓碑压实，坏源零覆盖。
    # 函数用途: 按精确身份与hash移除legacy记忆并保留恢复责任。
    # LLM: 迁移同样先quota再policy最后formal；备份引用或source命名不能绕过管理员锁。
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
        from ..user_space.managed_user_governance import managed_memory_batch_guard

        with _quota_admission(self.quota_enforcer) as admission, managed_memory_batch_guard(
            self.path, [{"entry_id": entry_id} for entry_id in sorted(expected)]
        ):
            with self._locked_memory():
                current_events = [
                    _with_legacy_identity(record)
                    for record in _strict_memory_events(self.path)
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
                tombstones = _persisted_remove_events(tombstones, current_events, set(expected))
                _write_compacted_memory(self, current_events, [*retained, *tombstones], {}, admission)
        append_memory_purge_event(
            self.ops_path,
            entry_ids=set(expected),
            content_hashes={content_hash for _version, content_hash in expected.values()},
        )
        append_memory_operation_events(self.ops_path, tombstones)
        self._after_commit_indexes(tombstones)
        return tombstones

# LLM: 检索与治理分离；Search只消费本库冷热权威的已验证候选，有界partial不代表完整，投影不授予正文权。
# 类用途: 提供关键词/语义融合与 scope 内确定性召回。
# LLM: C只在实际embedding前复用原scope纯当前校验；实际native先同对象穿出，不重搜/重嵌，原排序保持。
# 类用途: 复用原JSONL与派生融合检索，显式访问flush归HTTP调用方。
class _JsonlMemorySearchMixin:
    # LLM: 向量空间身份/维度由真实store.search复核；命中先按所给权威快照的ID/正文过滤，索引不授予事实权。
    #   公共search传有界快照，向量返回后的权威漂移由search在短formal锁内刷新全部融合候选，再登记访问。
    #   独立旧调用仍在向量返回后all；网络始终不持formal写锁，C/native和召回计量/降级原因保持原合同。
    # 函数用途: 生成本轮语义候选和降级原因；公共调用还须经过最终权威代次与永久屏障复核。
    @counted_as("memory_recall")
    def _semantic_records_report(self, query: str, top_k: int, *, _authority_records=None) -> tuple[list[MemoryRecord], str]:
        from ..retrieval.vector_store import VectorIdentityError

        try:
            store = self._vector_store()
            if store is None:
                return [], "embedder_unavailable"
            ok, reason = store.identity_status()
            if not ok:
                raise VectorIdentityError(reason)
            if self.coordinator_io:
                _coordinator_memory_write(lambda: None)
            with embedding_input_type("query"):
                query_vector = self._embedder.embed([query])[0]
            hits = store.search(query_vector, top_k=top_k)
        except VectorIdentityError as exc:
            self._record_semantic_health("identity", exc)
            return [], str(getattr(exc, "reason", "") or "vector_identity_mismatch")
        except Exception as exc:
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            self._record_semantic_health("search", exc)
            return [], "embedding_failed"
        self._record_semantic_health("identity")
        self._record_semantic_health("search")
        authority = self.all() if _authority_records is None else _authority_records
        active = {record.entry_id: record for record in authority}
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

    # LLM: 只要记录、不关心降级原因的调用方用它（契约同 _semantic_records_report 的第一个返回值）。
    # 函数用途: 返回经过正式 JSONL 二次核验的语义检索结果。
    def _semantic_records(self, query: str, top_k: int) -> list[MemoryRecord]:
        return self._semantic_records_report(query, top_k)[0]

    # LLM: 短formal锁绑定初始快照与四个控制文件指纹，锁外嵌入/向量检索；返回前同锁核漂移及记访问。
    #   无漂移直接复用；漂移最多加一次原预算的有界重读，刷新融合候选的正文/版本，未覆盖项宁可丢弃，不all。
    # 函数用途: 保持原融合排名并返回复核时当前有效事实，不为查询期间已删记录登记访问。
    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """优先从owner内读索引选候选，回读同代正文并应用墓碑；缺索引从正式源检索。

        query是搜索文本，top_k是最多返回条数；返回当前有效的MemoryRecord。
        旧workspace的memory投影不参与任何召回。"""

        candidate_limit = max(top_k * 4, top_k + 8)
        with self._locked_memory():
            revision = _search_authority_revision(self.path)
            records, _load_errors, known_empty, authority = self._search_report_state(query, top_k=candidate_limit)
            if self._embedder is not None and authority is None and not known_empty:
                from ._jsonl_indexing import bounded_authority_records

                authority, _scanned = bounded_authority_records(self, max_records=_AUTHORITY_SCAN_MAX_RECORDS,
                    max_bytes=_AUTHORITY_SCAN_MAX_BYTES)
        if not records and known_empty:
            EMBEDDING_USAGE.record_retrieval("none", "")
            return []
        if self._embedder is None:
            selected = _rerank_memory_records(records, query, top_k)
            EMBEDDING_USAGE.record_retrieval("keyword", _semantic_unavailable_reason(self._semantic_status))
        else:
            fused = self._fuse_semantic(query, records, candidate_limit, _authority_records=authority)
            selected = _select_diverse_memory_records(fused, top_k)
        with self._locked_memory():
            if _search_authority_revision(self.path) != revision:
                from ._jsonl_indexing import bounded_authority_records

                current, _scanned = bounded_authority_records(self, max_records=_AUTHORITY_SCAN_MAX_RECORDS,
                    max_bytes=_AUTHORITY_SCAN_MAX_BYTES)
                by_id = {record.entry_id: record for record in current}
                selected = [by_id[record.entry_id] for record in selected if record.entry_id in by_id]
            now = time.time()
            selected = [record for record in selected if not record.expires_at or record.expires_at > now]
            for record in selected:
                self._note_access(record.entry_id)
        return selected

    # LLM: Runtime recall 先以 active JSONL 建 allowlist，再读取派生索引；陈旧 FTS/vector 命中不能复活删除版本。
    # LLM: 默认同步不变，前台须显式latency_safe=True，不隐式改变管理检索。
    # 函数用途: 在scope内选择同步或缓存召回。
    def search_scoped(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
        *, latency_safe: bool = False,
    ) -> list[MemoryRecord]:
        return self._search_scoped(query, top_k, predicate, record_access=True, latency_safe=latency_safe)[0]

    # LLM: 候选检索复用正式 allowlist 与混合排序，但候选尚未注入上下文，不能写访问信号。
    # LLM: 候选可显式latency_safe，默认保持同步嵌入合同。
    # 函数用途: 选择范围内候选而不登记访问。
    def search_scoped_candidates(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
        *, latency_safe: bool = False,
    ) -> list[MemoryRecord]:
        return self._search_scoped(query, top_k, predicate, record_access=False, latency_safe=latency_safe)[0]

    # LLM: 与 search_scoped_candidates 同一检索（同 allowlist、同排序、不写访问信号），另返回本次检索的结构化事实：
    #   mode（semantic/keyword/none）、fallback_reason、scoped_entries 与存储层 semantic_recall 状态。只读工具据此
    #   如实告诉模型这次走的是语义还是关键词；不得用它改变自动召回或访问信号。
    # LLM: 缓存缺失如实报告bm25_fallback，不把未补齐当语义成功。
    # 函数用途: 返回候选及同步/缓存检索的实际方式。
    def search_scoped_candidates_report(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
        *, latency_safe: bool = False,
    ) -> tuple[list[MemoryRecord], dict[str, object]]:
        return self._search_scoped(query, top_k, predicate, record_access=False, latency_safe=latency_safe)

    # LLM: 重读正式源核对原scope/完整版本及渲染门；保序映射只确认每个ID一次，隔离/撤销/替换项不得touch。
    # 函数用途: 只给有效且可注入的候选登记访问，保序去重后返回确认列表，不重复维护两套ID状态。
    def confirm_scoped_access(
        self,
        records: Iterable[MemoryRecord],
        predicate: Callable[[MemoryRecord], bool],
    ) -> list[MemoryRecord]:
        from ..prompting_parts.memory_context import is_memory_context_admissible
        current = {record.entry_id: record for record in self.all() if predicate(record)}
        confirmed: dict[str, MemoryRecord] = {}
        for record in records:
            active = current.get(record.entry_id)
            if (active is None or active.entry_id in confirmed or active.version != record.version
                    or active.content != record.content or active.attributes != record.attributes
                    or active.source != record.source or active.kind != record.kind
                    or active.role != record.role or active.expires_at != record.expires_at
                    or not is_memory_context_admissible(active)):
                continue
            confirmed[active.entry_id] = active
            self._note_access(active.entry_id)
        return list(confirmed.values())

    # LLM: 正式与候选检索只能在访问确认时分叉；索引、scope 与排序必须完全相同。检索事实（第二个返回值）只是观察，
    #   取自 HybridRetriever 的 last_retrieval_mode/last_fallback_reason，不参与排序。
    #   这里发出的嵌入请求（查询与缺向量的条目）按“召回”计入进程用量（S7，embedding_usage）。
    # LLM: scope先过滤再评分/补齐；latency_safe只读向量并有界排队，默认同步链不变。
    # 函数用途: 在范围内检索并报告实际方式，显式选择避免前台网络。
    @counted_as("memory_recall")
    def _search_scoped(
        self,
        query: str,
        top_k: int,
        predicate: Callable[[MemoryRecord], bool],
        *,
        record_access: bool,
        latency_safe: bool = False,
    ) -> tuple[list[MemoryRecord], dict[str, object]]:
        if top_k <= 0:
            return [], _scoped_retrieval_facts(None, 0, self._semantic_status)
        projected, coverage = self._search_tier_index(query, 1000) if self._embedder is None else (None, {"status": "semantic_full_scan"})
        if projected is None:
            from ._jsonl_indexing import bounded_authority_records

            projected, scanned = bounded_authority_records(self, max_records=_AUTHORITY_SCAN_MAX_RECORDS,
                max_bytes=_AUTHORITY_SCAN_MAX_BYTES)
            coverage = {**coverage, **scanned}
        active = [record for record in projected if predicate(record)]
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
                **({"latency_safe": True, "cached_query_vector": _query_vector(self, query)} if latency_safe else {}),
            )
            if latency_safe:
                _queue_vector_backfill(self, query, active, cached)
            else:
                _query_vector(self, query, retriever.last_query_vector)
                self._remember_cached_vectors(docs, retriever, cached)
            by_id = {record.entry_id: record for record in active}
            keyword = [by_id[entry_id] for entry_id, _score in ranked if entry_id in by_id]
        else:
            keyword = []
        selected = _rerank_memory_records(keyword, query, top_k)
        if record_access:
            for record in selected:
                self._note_access(record.entry_id)
        facts = _scoped_retrieval_facts(retriever, len(active), self._semantic_status)
        return selected, {**facts, "coverage": coverage}

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
    # LLM: C原cache/keep算法不变，准确写叶由73闭合；真实native先抛，不能只健康诊断后继续下一付费。
    # 函数用途: 保存本轮原新嵌向量，保原同实例缓存与跨实例删除语义。
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
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            self._record_semantic_health("cache_write", exc)

    # LLM: 锁内复核判据必须是最新 active 状态；只返回当前仍有效的缓存键集合。
    # 函数用途: 给 cache.put 提供"写入时刻仍然有效"的键集合。
    def _live_cache_keys(self, fingerprint: str) -> set[str]:
        return {self._cache_key(fingerprint, text) for text in self._cache_texts(self.all())}

    # LLM: replace/remove后统一按正式活跃文本回收所有模型指纹，不能误删其它同文事实的共享键。
    # 函数用途: 清理已无活跃引用的文本向量，保留当前仍有效的缓存。
    # LLM: C原清理native优先同对象抛，普通故障仍健康诊断；不因禁用embedding跳旧磁盘正文清理。
    # 函数用途: 沿原active事实回收正文缓存，保清理未结。
    def _forget_cached_vectors(self, contents: Iterable[tuple[str, object]]) -> None:
        from ._jsonl_indexing import _purge_text_vector_orphans

        try:
            _purge_text_vector_orphans(self)
            self._record_semantic_health("cache_purge")
        except Exception as exc:
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
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

    # LLM: RRF按稳定ID融合；公共search透传本次有界权威快照，不能在语义复核再次全读；每次仍记一次召回方式。
    # 函数用途: 合并关键词与语义召回顺序，并记下这次召回走的方式。
    def _fuse_semantic(self, query: str, keyword_records: list[MemoryRecord], top_k: int, *, _authority_records=None) -> list[MemoryRecord]:
        """关键词召回 + 语义召回 RRF 融合(检索拓宽 #1)。语义为空则退回纯关键词(不崩不退化)。"""
        semantic, fallback_reason = self._semantic_records_report(query, top_k,
            **({} if _authority_records is None else {"_authority_records": _authority_records}))
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

    # LLM: 保留原公开二元返回形状，诊断和有界权威事实来自同一内部扫描，不跨请求缓存空库结论。
    # 函数用途: 检索正式记忆并保留索引或预算降级诊断。
    def search_report(self, query: str, top_k: int = 5) -> tuple[list[MemoryRecord], list[dict]]:
        """搜索记忆并保留可恢复的索引读取错误。

        只查询owner读索引或同代逻辑权威，索引未追平时报告coverage和实际降级，
        不回退旧workspace或把索引不可用说成没有记忆。
        """

        records, diagnostics, _known_empty, _authority = self._search_report_state(query, top_k)
        return records, diagnostics

    # LLM: 同一次扫描产出候选、诊断、确知空库事实及语义臂可复用的权威快照；索引候选不是全量权威，返回None。
    # 函数用途: 让公共search与report复用读取结果，避免为判空再次扫描全库。
    def _search_report_state(self, query: str, top_k: int):
        candidate_limit = max(top_k * 4, top_k + 8)
        projected, coverage = self._search_tier_index(query, candidate_limit)
        if projected is not None:
            diagnostics = ([{"context": "memory_store.read_index", "error_type": "MEMORY_READ_INDEX_TRUNCATED",
                             "coverage": coverage}] if coverage.get("result_truncated") else [])
            return (_rerank_memory_records(projected, query, top_k), diagnostics,
                    coverage.get("indexed_records") == 0, None)
        load_errors = []
        if coverage["status"] != "unmigrated":
            load_errors.append({"context": "memory_store.read_index", "error_type": "MEMORY_READ_INDEX_PENDING",
                                "coverage": coverage, "fallback": "committed_authority_scan"})
        from ._jsonl_indexing import bounded_authority_records

        active_records, scanned = bounded_authority_records(self, max_records=_AUTHORITY_SCAN_MAX_RECORDS,
            max_bytes=_AUTHORITY_SCAN_MAX_BYTES)
        coverage = {**coverage, **scanned}
        if load_errors:
            load_errors[-1]["coverage"] = coverage
        elif scanned["partial"]:
            load_errors.append({"context": "memory_store.read_index", "error_type": "MEMORY_AUTHORITY_SCAN_PARTIAL",
                "coverage": coverage, "fallback": "bounded_authority_scan"})
        lexical = _MemoryLexicalQuery(query)
        source_records = _search_memory_records(active_records, query, candidate_limit, lexical=lexical)
        return (_rerank_memory_records(source_records, query, top_k, lexical=lexical), load_errors,
                not active_records and not scanned["partial"], active_records)


# 正式记忆权威记录一次有界核验最多扫描的记录条数。
_AUTHORITY_SCAN_MAX_RECORDS = 1000
# 正式记忆权威记录一次有界核验最多读取的字节数。
_AUTHORITY_SCAN_MAX_BYTES = 2 * 1024 * 1024


# LLM: 调用方持formal锁；仅stat固定四个控制源，不读无界manifest/分片，协议写者须原子更新根或legacy。
# 函数用途: 绑定一次搜索的权威修订及永久屏障，识别根失败但墓碑已提交和未迁移库replace。
def _search_authority_revision(path):
    from ._jsonl_indexing import _scan_stamp
    from .generation_manifest import MemoryGenerationStore

    store = MemoryGenerationStore(path)
    return tuple(_scan_stamp(source) for source in (
        path, _tombstone_path(path), store.manifest_path, store.tombstones.path))


# LLM: 生命周期治理与检索投影分离:Lifecycle 只做访问信号、总量闸与健康快照,不直接召回。
# 类用途: 提供访问记录、flush、condense、过期视图与运行快照。
# LLM: C专用snapshot flush仅追加同source固定末写；不改变普通维护或Count0的原flush策略。
# 类用途: 管理原记忆访问与维护生命周期，明确区分C成功访问的显式落盘。
# LLM: C共用当前转层/访问算法；generation屏障先行的partial保留，不新建恢复表或改变user_explicit不遗忘语义。
# 类用途: 管理原长期记忆访问元数据与配额治理。
class _JsonlMemoryLifecycleMixin:
    # LLM: all是同代active+cold减去永久墓碑的逻辑全集，绝不合并Daily/Candidate/ops。
    # 函数用途: 读取当前owner全部已承诺且未过期的长期事实。
    def all(self) -> list[MemoryRecord]:
        """从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        这个方法没有输入参数。

        返回说明:
        返回当前owner同代active+cold扣除永久墓碑和过期记录后的完整逻辑集合。

        异常说明:
        初始旧格式保留原逐行容错；迁移后根、分片摘要或永久删除账损坏会拒绝读取。"""

        return self._read_memory_file(self.path)

    # LLM: 配置只读唯一memory_policy.json，坏政策不降级默认；构造本身不迁移或写配置。
    # 函数用途: 取得本库实际生效的类型化库存及注入预算。
    def memory_policy(self):
        from ..common.nofollow_fs import read_text_beneath
        from .generation_manifest import MemoryPolicyV2

        root = self.path.parents[2] if tuple(self.path.parts[-3:]) == ("memory", "long_term", "memory.jsonl") else self.path.parent
        raw = read_text_beneath(root, ("memory_policy.json",))
        return MemoryPolicyV2.from_payload(json.loads(raw) if raw is not None else {})

    # LLM: 物理热层不是事实全集；公开检索、删除和CAS必须继续使用all的逻辑库。
    # 函数用途: 提供热工作集的单独视图供预算检查与维护。
    def active_records(self):
        from .generation_manifest import MemoryGenerationStore

        events = MemoryGenerationStore(self.path).events(tier="active")
        return self.all() if events is None else _materialize_memory_events(events)

    # LLM: 迁移不改正文/version，默认预留20%热空间；同代根已存在时幂等，不对真实库自动执行。
    # 函数用途: 预演或执行旧库的一次性冷热分层。
    def migrate_tiers(self, *, dry_run=False):
        from ._jsonl_maintenance import migrate_tiers

        return migrate_tiers(self, dry_run=dry_run)

    # LLM: 只供已授权恢复流程，旧备份不能覆盖当前墓碑或同ID更新版本。
    # 函数用途: 合并备份事实并应用永久遗忘屏障后发布新代。
    def restore_tier_records(self, records):
        from ._jsonl_maintenance import restore_tier_records

        return restore_tier_records(self, records)

    # LLM: 显式维护才创建SQLite；沿原quota→policy→formal锁序，一次至多1000条且水位同事务提交。
    # 函数用途: 有界追平冷热派生读索引，重启后沿耐久游标续作。
    def rebuild_read_index(self, *, batch_size=1000):
        from .read_index import MemoryReadIndex

        owner = self.path.parents[2] if len(self.path.parents) >= 3 else self.path.parent
        with _quota_admission(self.quota_enforcer) as admission, _managed_policy_scope(owner):
            with self._locked_memory():
                return MemoryReadIndex(self.path, coordinator_io=self.coordinator_io).rebuild_step(
                    batch_size=batch_size, admission=admission)

    # LLM: 兼容仅需记录的维护调用方；破坏性跨阶段操作使用带source_opened的快照，不以空列表推断缺库。
    # 函数用途: 读取严格维护快照中的全部活跃事实，保留普通召回all的兼容语义。
    def _maintenance_records(self) -> list[MemoryRecord]:
        return self._maintenance_snapshot().records

    # LLM: 调用方持权威锁保护需发布/重置的快照；这里不自取不可重入锁，不从exists或记录数量推断源存在。
    # LLM: 维护保留实际源打开证据，并复核墓碑归档，坏归档不能让旧投影复活。
    # 函数用途: 读取严格完整视图及源打开证据。
    def _maintenance_snapshot(self, *, require_source=False):
        from ._jsonl_maintenance import (
            MaintenanceSnapshot,
            read_maintenance_objects,
        )
        from .generation_manifest import MemoryGenerationStore

        if MemoryGenerationStore(self.path).manifest() is not None:
            return MaintenanceSnapshot(_materialize_memory_events(_strict_memory_events(self.path)), True)
        _payloads, source_opened = read_maintenance_objects(self.path, require_source=require_source)
        events = _strict_memory_events(self.path)
        return MaintenanceSnapshot(_materialize_memory_events(events), source_opened)

    # LLM: 访问信号只在召回命中时产生;不命中不 touch,命中才证明"这条被用过"。
    # 函数用途: 记录一次对某条记忆的访问(内存累积,随下次写原子落盘)。
    def _note_access(self, entry_id: str) -> None:
        if not entry_id:
            return
        self._pending_access[entry_id] = time.time()

    # LLM: 落盘仍是写路径职责(锁内原子);公开入口供无写场景时手动冲刷。
    # LLM: 遵循quota→policy→formal锁序（按parents[2]定位owner_home），折叠访问时间而不改变版本；成功后才比较清旧pending。
    # 函数用途: 原子压实访问热度，失败保留待刷信号。
    def flush_access_events(self) -> int:
        if not self._pending_access:
            return 0
        resolved = self.path.absolute()
        owner_home = resolved.parents[2] if len(resolved.parents) >= 3 else resolved.parent.parent
        with _quota_admission(self.quota_enforcer, None) as admission, _managed_policy_scope(owner_home):
            with self._locked_memory():
                if not self._pending_access:
                    return 0
                current_events = _strict_memory_events(self.path)
                active_ids = {record.entry_id for record in _materialize_memory_events(current_events)}
                snapshot = dict(self._pending_access)
                pending = {key: stamp for key, stamp in snapshot.items() if key in active_ids}
                _write_compacted_memory(self, current_events, current_events, pending, admission)
                _consume_pending_snapshot(self, snapshot)
        return len(pending)

    # LLM: 只给当前owner实际HTTP长scope使用；原formal锁在Root外持有，锁出生/固定一次replace各消费同source末门，失败保完整snapshot。
    #   只在成功search后显式调用，不在finally补写；原默认flush/apply_batch不变，C3-J另负责其它写链。
    # LLM: 遵循quota→policy→formal锁序并在同次source末门写压实结果，不保留单独touch；失败保snapshot，成功只清旧值。
    # 函数用途: 在协调来源下折叠访问信号。
    def flush_access_events_from_source(self) -> int:
        from ..common.global_write_commit import execute_global_write

        snapshot = dict(self._pending_access)
        if not snapshot:
            return 0

        # LLM: 仅将原lock FD出生或固定formal writer交给当前活source，不持有新callback、不接受外部path。
        # 函数用途: 要求本次C末门存在，缺源不得按Count0回退。
        def commit(operation):
            return execute_global_write(operation, require_scope=True)

        resolved = self.path.absolute()
        owner_home = resolved.parents[2] if len(resolved.parents) >= 3 else resolved.parent.parent
        with _quota_admission(self.quota_enforcer, None) as admission, _managed_policy_scope(owner_home):
            with locked_json_path(self.path, birth_commit=commit):
                current_events = _strict_memory_events(self.path, require_source=True)
                active_ids = {record.entry_id for record in _materialize_memory_events(current_events)}
                pending = {key: stamp for key, stamp in snapshot.items() if key in active_ids}
                _write_compacted_memory(self, current_events, current_events, pending, admission, commit=commit)
        _consume_pending_snapshot(self, snapshot)
        return len(pending)

    # LLM: 原参数仅保留调用形状，条数/比例不再决定遗忘；只按字符预算同代转冷，返回搬迁条目而非删除事件。
    # 函数用途: 超热工作集预算时无损分层，彻底替代archive后remove的旧算法。
    def condense(
        self,
        *,
        max_records: int = 2000,
        target_records: int = 1200,
        max_ratio: float = 0.25,
    ) -> list[MemoryRecord]:
        active = self.active_records()
        if sum(len(record.content) for record in active) <= self.memory_policy().active_library_max_chars:
            return []
        from ._jsonl_maintenance import migrate_tiers

        migrate_tiers(self, retier=True)
        remaining = {record.entry_id for record in self.active_records()}
        return [record for record in active if record.entry_id not in remaining]

    # LLM: 过期 active 记录仍可能含需迁移/清除的正文；此入口只供 retention、migration 和 doctor，召回不得使用。
    # 函数用途: 返回包括已过期但尚未删除的全部 active 长期记忆。
    def all_including_expired(self) -> list[MemoryRecord]:
        return _materialize_memory_events(
            self._read_memory_events(self.path),
            include_expired=True,
        )

    # LLM: 同代库只从根和分片统计；字段active_total保留逻辑可见数，另列物理cold_total，不暴露正文路径。
    # 函数用途: 为capabilities/doctor提供完整冷热健康与派生索引水位。
    def runtime_snapshot(self) -> dict[str, object]:
        """Project owner-local Memory health without exposing paths or content."""

        try:
            from ._jsonl_maintenance import require_maintenance_event
            from .generation_manifest import MemoryGenerationStore
            from .read_index import MemoryReadIndex

            store = MemoryGenerationStore(self.path)
            generation = store.manifest()
            report = read_jsonl_objects_report(
                self.path,
                context="memory_store.runtime_snapshot",
            ) if generation is None else None
            events: list[MemoryRecord] = []
            invalid_records = 0
            for payload in report.records if report is not None else ():
                record = _memory_record_from_obj(payload)
                if record is None:
                    invalid_records += 1
                    continue
                require_maintenance_event(record)
                events.append(_with_legacy_identity(record))
            if generation is not None:
                events = store.events(generation)
            active = _materialize_memory_events(events)
        except Exception as exc:
            return {
                "state": "unavailable",
                "health": "unavailable",
                "active_total": 0,
                "active_by_kind": {},
                "load_error_count": 1,
                "load_error_codes": [type(exc).__name__],
                "semantic_recall": dict(self._semantic_status),
            }
        by_kind: dict[str, int] = {}
        for record in active:
            kind = str(record.kind or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        error_codes = sorted(
            {
                str(error.get("error_type") or "MEMORY_LOAD_ERROR")
                for error in (report.load_errors if report is not None else ())
            }
        )
        if invalid_records:
            error_codes.append("MemoryRecordValidationError")
        load_error_count = (len(report.load_errors) if report is not None else 0) + invalid_records
        return {
            "state": "available",
            "health": "degraded" if load_error_count or self._semantic_errors or self._semantic_status.get("state") == "degraded" else "healthy",
            "active_total": len(active),
            "active_by_kind": by_kind,
            "event_total": len(events),
            "load_error_count": load_error_count,
            "load_error_codes": sorted(set(error_codes)),
            "text_index": "configured" if generation is not None else "unconfigured",
            "cold_total": generation["cold_total"] if generation is not None else 0,
            "read_index_coverage": MemoryReadIndex(self.path).coverage(generation),
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
# LLM: C实际native不可吞成没有缓存后付费重嵌；普通读取降级保持，仍只逐字相同文档复用。
# 函数用途: 按原空间身份读取既有文档向量。
def _stored_vectors_for(memory: object, docs: list[tuple[str, str]]) -> dict[str, list[float]]:
    if not docs:
        return {}
    try:
        store = memory._vector_store()
        stored = store.stored_vectors(doc_id for doc_id, _text in docs) if store is not None else {}
    except Exception as exc:
        if getattr(memory, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
            raise
        return {}
    texts = dict(docs)
    return {doc_id: vector for doc_id, (vector, text) in stored.items() if text == texts.get(doc_id)}


# LLM: 正文哈希缓存（memory_text_vectors.json）只补向量库没有的那部分；读失败等于没有缓存，并记入语义健康状态。
#   懒建缓存必须放进 try：构造缓存时会读盘，读错误不能冒出检索路径（模块合同：读失败等价于没有缓存，检索结果不变）。
#   放在模块级是为了不撑大检索 mixin。
# 函数用途: 按正文哈希取出已缓存的向量。
# LLM: C真实native在下一embedding前同对象抛，普通cache读错仍原空集合/诊断；不造第二cache。
# 函数用途: 读取原正文哈希向量缓存，保持原费用复用。
def _text_cache_vectors_for(memory: object, docs: list[tuple[str, str]]) -> dict[str, list[float]]:
    try:
        cache = memory._text_vector_cache()
        if cache is None or not docs:
            return {}
        from ..retrieval.text_vector_cache import embedder_fingerprint

        fingerprint = embedder_fingerprint(memory._embedder)
        keys = {doc_id: memory._cache_key(fingerprint, text) for doc_id, text in docs}
        found = cache.get(list(keys.values()))
        memory._record_semantic_health("cache_read", cache.last_read_error)
        return {doc_id: found[key] for doc_id, key in keys.items() if isinstance(found.get(key), list)}
    except Exception as exc:
        if getattr(memory, "coordinator_io", False) and isinstance(exc, _NATIVE_MEMORY_ERRORS):
            raise
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


# LLM: 此层从同一冷热逻辑权威读取，有界召回可只覆盖部分；派生索引和缓存都不能成为事实权威。
# 类用途: 从同代JSONL分片读取事实并执行关键词、FTS和可选向量召回。
# LLM: C各client可达writer/lock出生接当前scope，native先传播；未接管理维护接口不据此称可执行。
# 类用途: 复用原正式召回与投影，不赋予索引独立权威。
class _JsonlMemoryRecallMixin(_JsonlMemorySearchMixin, _JsonlMemoryLifecycleMixin, JsonlMemoryIndexMixin):
    # LLM: 按操作独立记录派生错误；只有该操作成功才清除，无embedder时不能覆盖禁用/配置故障原因。
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
        base = dict(self._semantic_status) if self._embedder is None else {
            "state": "degraded" if self._semantic_errors else "configured",
        }
        base.pop("errors", None)
        self._semantic_status = {
            **base,
            **({"errors": dict(self._semantic_errors)} if self._semantic_errors else {}),
        }

    # LLM: 向量库位于当前 owner memory 根且仅是 lazy projection；无 embedder 时必须完全关闭。
    # 函数用途: 获取当前 owner 的可重建向量索引实例。
    # LLM: C原lazy字段保存同一73向量对象，只有内部策略透传，无source字段或新registry。
    # 函数用途: 返回原同owner语义向量实例，默认身份/禁用语义保持。
    def _vector_store(self):
        """本地 per-owner 向量库(memory_vectors.json,在 owner home 内)。无 embedder 返回 None。

        只用本地文件,绝不碰共享向量库(零外部依赖、按 owner 天然隔离)。懒建缓存。
        P14: 带上当前空间身份，向量库按同一份快照里的身份与实际维度裁决读写，不一致时视为不存在。
        """
        if self._embedder is None:
            return None
        if self._vector_store_cache is None:
            from ..retrieval.vector_store import VectorStore

            self._vector_store_cache = VectorStore(self._vector_store_path(), identity=self._vector_identity, **({} if not self.coordinator_io else {"coordinator_io": True}))
        return self._vector_store_cache

    # LLM: 向量库固定在正式记忆 JSONL 同目录的 memory_vectors.json；状态预览和语义通道必须指向同一文件。
    # 函数用途: 返回当前 owner 向量库快照文件路径。
    def _vector_store_path(self) -> Path:
        return self.path.parent / "memory_vectors.json"

    # LLM: 正文哈希缓存独立成文件（memory_text_vectors.json），检索路径永不重写含明文向量库；
    #   无 embedder 时必须完全关闭。它只是派生数据，丢了按"没有缓存"重算。
    # 函数用途: 获取当前 owner 的正文哈希向量缓存实例。
    # LLM: C原lazy字段只保存73真实缓存引用，True仅透传IO策略，原指纹/实例寿命不变。
    # 函数用途: 复用同一个正文向量缓存，不靠重建假装命中。
    def _text_vector_cache(self):
        """本地 per-owner 正文哈希缓存(memory_text_vectors.json)。无 embedder 返回 None。"""
        if self._embedder is None:
            return None
        if self._text_vector_cache_obj is None:
            from ..retrieval.text_vector_cache import TextVectorCache

            self._text_vector_cache_obj = TextVectorCache(self.path.parent / "memory_text_vectors.json", **({} if not self.coordinator_io else {"coordinator_io": True}))
        return self._text_vector_cache_obj

    # LLM: 向量写失败不改变权威提交；metadata 仍需携带可与 active JSONL 复核的完整身份。
    #   身份不一致时拒绝写入（不把新模型的向量混进旧库），降级状态可见，等重建修正。
    #   嵌入请求按“记忆写入”计入进程用量（S7，embedding_usage）。
    # 函数用途: 尽力把一条正式记忆写入向量索引，失败进入健康诊断，不回滚正式记忆。
    # LLM: C实际native先抛；上一叶成功后在原document embedding前纯当前校验，网络和原领域锁仍Root外，普通降级保持。
    # 函数用途: 按原文本/身份/费用给已提交记忆写向量投影。
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
            if self.coordinator_io:
                _coordinator_memory_write(lambda: None)
            vector = self._embedder.embed([text])[0]
            if self._write_current_vector(store, record, (vector, text)):
                self._record_semantic_health("index")
        except Exception as exc:
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            self._record_semantic_health("index", exc)

    # LLM: 网络embedding不占权威锁；真正落盘时持锁复核至写完，删除或替换先提交则拒绝旧正文。
    # 函数用途: 原子核对并写入仍然属于当前事实的向量投影。
    # LLM: C仅formal锁FD出生进门，原版本核对和等待在外，73实际向量writer再末核同source。
    # 函数用途: 在原删除互斥锁内保存仍然有效的向量。
    def _write_current_vector(self, store, record, embedded) -> bool:
        vector, text = embedded
        with locked_json_path(self.path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_memory_write})):
            if not self._projection_is_current(record):
                return False
            store.upsert(_record_vec_id(record), vector, text=text, metadata=_record_payload(record))
        return True

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

    # LLM: 显式维护只操作owner内读索引，先严格预检再迁移/重建；一次至多1000条，完整性另见coverage。
    # 函数用途: 推进正式记忆派生索引并返回实际已覆盖条数，不再清空或重写workspace。
    def index_all(self, *, reset: bool = False, require_source: bool = False) -> int:
        if reset:
            from ._jsonl_maintenance import reset_local_index

            return reset_local_index(self, require_source=require_source)
        with self._locked_memory():
            self._maintenance_snapshot(require_source=require_source)
        self.migrate_tiers()
        progress = self.rebuild_read_index()
        self._retain_text_cache_keys()
        return int(progress.get("indexed_records") or 0)

    # LLM: 维护与正式提交统一按活跃index_text回收全部指纹，不依赖embedder；软写失败必须可见。
    # 函数用途: 在权威锁内回收孤儿缓存，成功后只清除本项健康错误。
    def _retain_text_cache_keys(self) -> int:
        from ._jsonl_indexing import _purge_text_vector_orphans

        try:
            dropped = _purge_text_vector_orphans(self)
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
    # LLM: 正式库严格读报告或回收失败（坏行/读错/缓存锁）必须与"确实没有孤儿"分得开，
    #   否则维护状态天天写 `reclaimed: 0`，看起来像"跑过、没孤儿"，实际是没跑成——
    #   这正是上一轮被复审抓到的"假的结构化事实"。失败时返回错误说明由调用方带进维护状态。
    # 函数用途: 回收孤儿键；返回 (回收条数, 错误说明)，无错误时说明为空串。
    # LLM: 可选原claim短门只覆盖cache最终改写；原keep集合长读取在T外，四实际类型原对象透传不作0成功。
    # LLM: C当前scope/source native同对象优先，原physical四类透传仍保；keep扫描门外，不把回收0当落盘成功。
    # 函数用途: 按原active文本清理孤儿缓存并保普通错误诊断。
    def reclaim_text_cache_orphans(self, *, commit_transition=None) -> tuple[int, str]:
        cache = self._text_vector_cache_for_reclaim()
        if cache is None:
            return 0, "cache_unavailable"
        try:
            from ..retrieval.text_vector_cache import index_text, text_content_hash

            keep_hashes = {
                text_content_hash(index_text(record.content, record.attributes))
                for record in self._maintenance_records()
                if record.content
            }
            # LLM: keep集合已在门外按原记录算出；这里只复用原cache锁内改写，不含embedder/网络。
            # 函数用途: 在原短提交门里回收派生缓存键。
            def operation():
                return cache.retain_content_hashes(keep_hashes)
            dropped = operation() if commit_transition is None else commit_transition(operation)
        except Exception as exc:
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            from ..backends.bounded_call import BoundedCallExitUnconfirmedError
            from ..common.request_execution_ports import (
                RequestExecutionContextUnavailable,
                RequestExecutionOutcomeUncertainError,
            )
            from ..common.tool_write_context import ToolWriteContextUnavailable
            if isinstance(exc, (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
                                ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError)):
                raise
            return 0, f"{type(exc).__name__}: {exc}"
        # 键被成功清点但没删掉任何东西，也可能是"读不出缓存、拿不到锁"——两种情况
        # retain_matching 都只会留下 last_write_error。把它带出来，免得又被当成"没孤儿"。
        error = getattr(cache, "last_write_error", None) or getattr(cache, "last_read_error", None)
        if error is not None:
            return dropped, f"{type(error).__name__}: {error}"
        return dropped, ""

    # LLM: 回收只需要缓存文件本身，不需要 embedder；这里给出一条不经过 _text_vector_cache() 的取用路径。
    # 函数用途: 取得用于孤儿回收的正文哈希缓存实例（无 embedder 也返回）。
    # LLM: C即使无embedder也复用原cache字段并透传73策略；默认原构造形状不变，真实native不吞成None。
    # 函数用途: 获取同owner原孤儿回收缓存，不漏禁用embedding后的旧文件。
    def _text_vector_cache_for_reclaim(self):
        try:
            from ..retrieval.text_vector_cache import TextVectorCache

            if self.coordinator_io:
                if self._text_vector_cache_obj is None:
                    self._text_vector_cache_obj = TextVectorCache(self.path.parent / "memory_text_vectors.json", coordinator_io=True)
                return self._text_vector_cache_obj
            return TextVectorCache(self.path.parent / "memory_text_vectors.json")
        except Exception as exc:
            if self.coordinator_io and isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            return None

    # LLM: 文件读取先 materialize 版本事件并过滤删除/过期，供正常召回使用。
    # LLM: 两文件stat指纹缓存物化视图；返回深拷贝防污染，命中仍按当前时间过滤过期。
    # 函数用途: 复用未换代的active快照，避免只读调用重复解析。
    def _read_memory_file(self, path: Path) -> list[MemoryRecord]:
        stamp = _memory_stamp(path)
        cached = getattr(self, "_active_view_cache", None)
        if cached is None or cached[0] != stamp:
            records = _materialize_memory_events(self._read_memory_events(path), include_expired=True)
            cached = (stamp, deepcopy(records))
            if stamp == _memory_stamp(path):
                self._active_view_cache = cached
        now = time.time()
        return [deepcopy(record) for record in cached[1] if not record.expires_at or record.expires_at > now]

    # LLM: 保留self虚方法链，原scanner仅下移到同一显式path叶子；覆写事件读取仍影响all，版本/坏行容错不变。
    # 函数用途: 沿唯一只读叶子读取JSONL事件，不创建目录或派生索引。
    def _read_memory_events(self, path: Path) -> list[MemoryRecord]:
        return _read_memory_events_from_path(path)


# LLM: 向量投影的管理动作（状态预览、全量重建）与日常召回分开：只读写可重建的 memory_vectors.json，不碰 memory.jsonl
#   权威；依赖 Recall mixin 的 _vector_store/_vector_store_path/_record_semantic_health。改动同步 cli/memory_admin_commands、
#   test_vector_identity 与 test_vector_snapshot_consistency。
# 类用途: 给管理员入口提供向量库状态预览和全有或全无的重建。
class _JsonlMemoryVectorAdminMixin:
    # LLM: 预览锁内严格读权威，不嵌入或清库；source_opened随读结果交给confirmed调用方，不用计数推断。
    # 函数用途: 返回向量身份、可靠的正式计数和实际读取证据；坏源报告未知而非空库。
    def vector_index_status(self) -> dict[str, object]:
        from ..retrieval.vector_store import VectorStore

        authority_error = ""
        source_opened = False
        try:
            with locked_json_path(self.path):
                snapshot = self._maintenance_snapshot()
            active_count, source_opened = len(snapshot.records), snapshot.source_opened
        except (OSError, ValueError, TypeError):
            active_count = None
            authority_error = "MEMORY_AUTHORITY_UNREADABLE"
        counts = {"active_count": active_count, "authority_error": authority_error, "source_opened": source_opened}
        store = self._vector_store()
        if store is None:
            summary = VectorStore(self._vector_store_path()).summary()
            return {"ok": False, "reason": "no_embedder", **counts, **summary}
        ok, reason = store.identity_status()
        return {"ok": ok, "reason": reason, **counts, **store.summary()}

    # LLM: 重建是管理员显式动作，全有或全无（P14 第 4 条）：逐条嵌入全部 active 记忆、在内存里拼出完整新快照，
    #   任一条失败立即停（后面的不再发请求，省 token），返回 ok=False + VECTOR_REBUILD_INCOMPLETE 和准确计数，旧库原样保留；
    #   全部成功才经 VectorStore.replace_all 一次原子替换。不修改 memory.jsonl 权威事实；失败原因只记类型名或原因码。
    # LLM: require_source只来自本次预览实际读取证据；首读与发布持权威锁，嵌入不持锁，已见空源也不能丢失。
    # 函数用途: 全量重嵌当前owner记忆，读取失败零嵌入，发布失败保留旧库并返回准确计数。
    def rebuild_vectors(self, *, require_source=False) -> dict[str, object]:
        from ..retrieval.vector_store import VectorStoreError

        store = self._vector_store()
        if store is None:
            return {"ok": False, "reason": "no_embedder", "attempted": 0, "embedded": 0, "failed": 0,
                    "rebuilt": 0, "vector_count": None}
        try:
            with locked_json_path(self.path):
                snapshot = self._maintenance_snapshot(require_source=require_source)
            records = snapshot.records
            require_source = require_source or snapshot.source_opened
        except (OSError, ValueError, TypeError) as exc:
            self._record_semantic_health("rebuild", exc)
            return {"ok": False, "reason": "MEMORY_AUTHORITY_UNREADABLE", "failed_stage": "read",
                    "attempted": 0, "embedded": 0, "failed": 0, "rebuilt": 0,
                    "active_count": None, "vector_count": store.summary()["vector_count"]}
        rows, failure = self._embed_rebuild_rows(records)
        # failed 只数嵌入失败的记录（首个失败即停，所以是 0 或 1）；写盘失败看 failed_stage=write。
        failed = 0 if failure is None else 1
        counts = {"active_count": len(records), "attempted": len(rows) + failed, "embedded": len(rows), "failed": failed}
        if failure is None:
            try:
                self._replace_current_vectors(store, records, rows, require_source=require_source)
            except Exception as exc:
                failure = ("write", getattr(exc, "reason", None) or type(exc).__name__)
        if failure is not None:
            self._record_semantic_health("rebuild", VectorStoreError("VECTOR_REBUILD_INCOMPLETE"))
            return {"ok": False, "reason": "VECTOR_REBUILD_INCOMPLETE", "failed_stage": failure[0], "failure": failure[1],
                    "rebuilt": 0, **counts, "vector_count": store.summary()["vector_count"]}
        for operation in ("rebuild", "index", "identity"):
            self._record_semantic_health(operation)
        return {"ok": True, "reason": "", "rebuilt": len(rows), **counts, "vector_count": store.summary()["vector_count"]}

    # LLM: 与删除共用权威锁；重读保留预览/首读已见源要求，不能把两次空列表相等当作来源有效。
    # 函数用途: 发布前验证来源及active版本，整份拒绝丢源/坏源/快照变化，禁止已删正文复活。
    def _replace_current_vectors(self, store, records, rows, *, require_source=False) -> None:
        from ..retrieval.vector_store import VectorStoreError

        with locked_json_path(self.path):
            try:
                current = self._maintenance_snapshot(require_source=require_source).records
            except (OSError, ValueError, TypeError) as exc:
                raise VectorStoreError("MEMORY_AUTHORITY_UNREADABLE") from exc
            if [(r.entry_id, r.version, r.content, r.attributes) for r in current] != [
                    (r.entry_id, r.version, r.content, r.attributes) for r in records]:
                raise VectorStoreError("MEMORY_SNAPSHOT_CHANGED")
            store.replace_all(rows)

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

# LLM: 持正式JSONL锁时调用；先按entry分开收集历史hash，再把责任与删除墓碑同一次原子替换。
# 函数用途: 删除后不再保留正文，但保留跨进程恢复清理历史候选所需的hash。
def _persisted_remove_events(committed, history, removed_ids):
    hashes = {entry_id: set() for entry_id in removed_ids}
    for record in [*history, *committed]:
        if record.entry_id not in hashes:
            continue
        hashes[record.entry_id].update(record.removed_content_hashes)
        if record.content:
            hashes[record.entry_id].add(memory_content_hash(record.content))
    persisted = []
    for record in committed:
        if record.entry_id not in removed_ids:
            persisted.append(record)
            continue
        if record.action == "remove":
            record.removed_content_hashes = tuple(sorted(hashes[record.entry_id]))
            persisted.append(record)
    return persisted


# LLM: 权威序列化省略空可选字段，历史hash仅随删除墓碑落盘，不引入正文或attributes。
# LLM: 未知字段在顶层且canonical优先；remove剥离未知字段、正文、attributes与tags。
# 函数用途: 无损序列化正式记录，删除墓碑不泄露扩展中的旧正文。
def _record_payload(record: MemoryRecord) -> dict:
    payload = asdict(record)
    extra = payload.pop("_extra_fields", {})
    if record.action == "remove":
        payload.update(content="", attributes=None, tags=[])
    else:
        payload = {**extra, **payload}
    if not payload.get("removed_content_hashes"):
        payload.pop("removed_content_hashes", None)
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


# LLM: 显式path不授owner权限；C调用方须先核原认证/owner/lease，读取只用原scanner与materializer，不构造带mkdir的仓库。
# 函数用途: 只读物化现有正式JSONL，供原Agent读取与已核C recent共享同一版本/删除/过期规则。
def read_memory_records_from_path(path: str | Path) -> list[MemoryRecord]:
    return _materialize_memory_events(_read_memory_events_from_path(Path(path)))


# LLM: 这是原_read_memory_events唯一scanner，下移原字节逻辑；缺失/坏行/读取report语义保持，不能合并候选或触碰缓存。
# LLM: 已迁移库只读同代根；旧库保留逐行容错且叠加永久墓碑，坏根不回退旧正文。
# 函数用途: 为显式path及实例召回统一读取冷热逻辑库。
def _read_memory_events_from_path(path: Path) -> list[MemoryRecord]:
    from .generation_manifest import GENERATION_SCHEMA, MemoryGenerationStore

    store = MemoryGenerationStore(path)
    committed = store.events()
    if committed is not None:
        return committed
    records: list[MemoryRecord] = []
    for source in (_tombstone_path(path), path):
        report = read_jsonl_objects_report(source, context="memory_store.read_memory_file")
        for obj in report.records:
            if obj.get("schema_version") == GENERATION_SCHEMA:
                raise MemoryAuthorityCorruptError("MEMORY_AUTHORITY_UNREADABLE")
            record = _memory_record_from_obj(obj)
            if record is not None:
                records.append(_with_legacy_identity(record))
    return [*records, *store.tombstones.load().values()]


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


# LLM: 批次变更前检查规范ID及与写入相同strip/hash语义；也用于本批新增删除账的发表前校验。
# 函数用途: 明确拒绝已遗忘正文，避免代次末端过滤制造假成功或覆盖活记录前像。
def _require_memory_write_barriers(operations, barrier, barriers):
    for index, raw in enumerate(operations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"memory operation {index} must be an object")
        if str(raw.get("action") or "add").strip().lower() not in {"add", "replace"}:
            continue
        entry_id = str(raw.get("entry_id") or "").strip()
        if entry_id in barriers:
            raise ValueError("MEMORY_TOMBSTONED_ID")
        content = str(raw.get("content") or "").strip()
        if content and barrier.excludes(entry_id, memory_content_hash(content), records=barriers):
            raise ValueError("MEMORY_TOMBSTONED_CONTENT")


# LLM: 所有操作只改变调用方临时active投影；先由公共入口验证永久遗忘屏障，再按原CAS准备。
# 函数用途: 生成待发表的批次事件，坏操作不写权威或派生索引。
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


# LLM: 替换/删除共用精确ID和乐观版本核验；目标CAS不因正文相似或同证据重放而刷新。
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
        raise MemoryVersionConflict(
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
# LLM: 替换保留未知顶层扩展及历史hash，删除时统一剥离扩展，不修改旧引用。
# 函数用途: 构造保留前向兼容元数据的替换版本。
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
        _extra_fields=deepcopy(prior._extra_fields),
        removed_content_hashes=prior.removed_content_hashes,
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
# LLM: 调用方可共享本次lexical对象，避免后续rerank重复query归一和记录评分。
# 函数用途: 选择有界词面命中并保存本次评分。
def _search_memory_records(records: list[MemoryRecord], query: str, top_k: int, *, lexical=None) -> list[MemoryRecord]:
    lexical = lexical or _MemoryLexicalQuery(query)
    scored: list[tuple[int, float, MemoryRecord]] = []
    for record in records:
        score = lexical.score(record)
        if score > 0 or not lexical.terms:
            scored.append((score, record.created_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:top_k]]


# LLM: 分数只用于排序，不能替代 evidence、status 或 scope 过滤。
# LLM: prepared查询只算一次归一及窗口；旧str调用仍支持，评分语义一致。
# 函数用途: 计算记录的确定性词面分数，复用查询窗口。
def _memory_search_score(record: MemoryRecord, query: str, query_terms: set[str]) -> int:
    text = fold_key(record.content)  # 与 query_terms 同走统一规范化(审计 #20):全角/NFD/大小写不漏命中
    score = sum(1 for term in query_terms if term in text)
    prepared = query if isinstance(query, _MemoryLexicalQuery) else _MemoryLexicalQuery(query)
    folded_query = prepared.folded
    if folded_query and folded_query in text:
        score += 3
    # 中文召回增强(真机缺口 2026-08-06):中文无空格,split 后整句匹配失败(如 query
    # "萧战认为最对的事是什么" vs 记忆"萧战认为最对的事是没让..."——整句不命中)。
    # 用 query 的滑动窗口子串在 content 中命中计分(中文短问也能召回)。纯结构化、确定性。
    for substrings in prepared.windows:
        hits = sum(1 for substring in substrings if substring and substring in text)
        if hits:
            score += hits
            break
    return score


# LLM: rerank 只能读取文本匹配、结构化来源和时间，不调用模型、不解析业务状态。
# LLM: 可复用候选阶段词面分数，来源权重及稳定顺序不变，不跨调用缓存。
# 函数用途: 以复用词面分数、来源及时间排列候选。
def _rerank_memory_records(
    records: list[MemoryRecord],
    query: str,
    top_k: int,
    *, lexical=None,
) -> list[MemoryRecord]:
    """Deterministically rank recalled facts without another model call."""

    if top_k <= 0:
        return []
    lexical = lexical or _MemoryLexicalQuery(query)
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
                lexical.score(record),
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


# LLM: 只消费当次真实scope；准确目录/FD出生及已批准付费前纯当前校验复用此门，不储存callback。
# 函数用途: 为C原JSONL固定本地叶逐次核对来源，网络/扫描/锁等待不入Root。
def _coordinator_memory_write(operation):
    return execute_global_write(operation, require_scope=True)


# LLM: 仅由本实例formal/archive调用方传原目标和现成raw，调用方已持原领域锁；默认writer不走这里。
# 函数用途: 将C原正式或归档JSONL做一次准确原子末写，不补重试。
def _write_coordinator_memory_raw(path, raw):
    return _coordinator_memory_write(lambda: write_private_text_file_atomic_unlocked(path, raw, replace_once=True))


# LLM: 仅原writer已成功后比较删除本次旧快照；新时间戳和任何失败保留原pending，不建第二账。
# 函数用途: 清除已经耐久随formal写入的旧访问信号。
def _consume_pending_snapshot(memory, snapshot):
    for key, stamp in snapshot.items():
        if memory._pending_access.get(key) == stamp:
            del memory._pending_access[key]


# LLM: 名字与正式库绑定；通用测试库不同basename也不能共享删除权威。
# 函数用途: 返回同目录的紧凑墓碑账本路径。
def _tombstone_path(path: Path) -> Path:
    name = "memory_tombstones.jsonl" if path.name == "memory.jsonl" else path.name + ".tombstones.jsonl"
    return path.with_name(name)


# LLM: 指纹覆盖根、永久墓碑和引用分片；等长原地改写仍失效，坏根不回退旧库。
# 函数用途: 获取完整逻辑库的缓存身份。
def _memory_stamp(path: Path) -> tuple:
    from .generation_manifest import MemoryGenerationStore

    store = MemoryGenerationStore(path)
    stamps = [str(path.absolute())]
    sources = [path, _tombstone_path(path), store.manifest_path, store.tombstones.path]
    root = store.manifest()
    if root:
        sources.extend(path.parent / item["path"] for item in root["segments"])
    for source in sources:
        try:
            stat = source.stat()
            stamps.append((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


# LLM: 结构化损坏错误不含正文；与维护入口兼容ValueError，调用方不得据此覆盖原文件。
# 类用途: 阻断损坏正式源上的任何整文件重写。
class MemoryAuthorityCorruptError(ValueError):
    code = "MEMORY_AUTHORITY_UNREADABLE"


# LLM: 迁移后验证根和全部分片；旧小库仍严格验证前像，缺失根不回退旧正文。
# 函数用途: 严格读取完整冷热事件，保护重写不丢冷事实。
def _strict_memory_events(path: Path, *, require_source: bool = False) -> list[MemoryRecord]:
    from ._jsonl_maintenance import require_maintenance_event
    from .generation_manifest import GENERATION_SCHEMA, MemoryGenerationStore

    store = MemoryGenerationStore(path)
    committed = store.events()
    if committed is not None:
        return committed
    events = []
    if require_source:
        try:
            path.stat()
        except OSError as exc:
            raise MemoryAuthorityCorruptError("MEMORY_AUTHORITY_UNREADABLE") from exc
    for source in (_tombstone_path(path), path):
        report = read_jsonl_objects_report(source, context="memory_store.prewrite")
        if report.load_errors:
            raise MemoryAuthorityCorruptError("MEMORY_AUTHORITY_UNREADABLE")
        for payload in report.records:
            if payload.get("schema_version") == GENERATION_SCHEMA:
                raise MemoryAuthorityCorruptError("MEMORY_AUTHORITY_UNREADABLE")
            record = _memory_record_from_obj(payload)
            try:
                require_maintenance_event(record)
                if not isinstance(record.removed_content_hashes, (tuple, list)) or any(
                        not isinstance(value, str) for value in record.removed_content_hashes):
                    raise ValueError("invalid removal hashes")
                if source != path and record.action != "remove":
                    raise ValueError("invalid tombstone ledger")
            except (TypeError, ValueError) as exc:
                raise MemoryAuthorityCorruptError("MEMORY_AUTHORITY_UNREADABLE") from exc
            events.append(_with_legacy_identity(record))
    return [*events, *store.tombstones.load().values()]


# LLM: 只保留最后semantic版本并合并访问最大值及历史hash；不按热度淘汰任何active（包括managed锁定及过期条目）。
# 函数用途: 生成无touch/旧版本正文的当前事件快照。
def _compact_memory_events(events, pending):
    active = {record.entry_id: deepcopy(record) for record in _materialize_memory_events(events, include_expired=True)}
    latest = {}
    hashes: dict[str, set[str]] = {}
    for record in events:
        if record.action == "touch":
            continue
        latest[record.entry_id] = record
        bucket = hashes.setdefault(record.entry_id, set())
        bucket.update(record.removed_content_hashes)
        if record.content:
            bucket.add(memory_content_hash(record.content))
    result = []
    for entry_id, latest_record in latest.items():
        record = active.get(entry_id)
        if record is None:
            record = replace(latest_record, content="", attributes=None, tags=[], _extra_fields={})
        else:
            record.last_accessed_at = max(record.last_accessed_at or 0, pending.get(entry_id, 0))
        historical = hashes[entry_id]
        if record.action != "remove":
            historical = (historical - {memory_content_hash(record.content)}) | set(record.removed_content_hashes)
        record.removed_content_hashes = tuple(sorted(historical))
        result.append(record)
    return result


# LLM: JSONL只有物理LF边界，序列化统一走隐私剥离后的payload，不创建额外正文副本。
# 函数用途: 编码一组正式记录为完整JSONL文本。
def _memory_events_text(events) -> str:
    return "".join(json.dumps(_record_payload(record), ensure_ascii=False) + "\n" for record in events)


# LLM: 调用方先quota→policy再持formal锁；legacy无最终删除且未超热预算时只压实旧两文件，可保留128条小历史。
#   已有根、最终删除或超热预算走generation：先写分片及永久墓碑屏障、后发布根，再GC/验根/marker收尾。
#   屏障成功而根失败时删除可能已生效，必须报告partial并由purge重启恢复，绝不承诺失败零写入或归档重放。
#   根成功后收尾失败保留已提交事实与清理责任；pending只在原调用方成功返回后消费，不在此反取quota/policy。
# 函数用途: 按legacy压实或永久屏障优先的generation协议提交现态，保留准确部分提交与恢复语义。
def _write_compacted_memory(memory, history, events, pending, admission, *, commit=None, keep_history=False,
                            redact_contents=None):
    from .generation_manifest import MemoryGenerationStore

    current = _compact_memory_events(events, pending)
    budget = memory.memory_policy().active_library_max_chars
    if (MemoryGenerationStore(memory.path).manifest() is not None or any(r.action == "remove" for r in current)
            or sum(len(r.content) for r in current) > budget):
        from ._jsonl_maintenance import publish_tier_generation

        publish_tier_generation(memory, current, admission=admission, commit=commit,
                                redact_contents=redact_contents, max_chars=budget)
        return
    archived = _historical_tombstones(history)
    archive_by_id = {record.entry_id: _record_payload(record) for record in archived}
    compacted = [record for record in _compact_memory_events(events, pending)
                 if record.action != "remove" or archive_by_id.get(record.entry_id) != _record_payload(record)]
    if keep_history:
        compacted = _fold_small_memory_history(events, compacted)
    archive_text, text = _memory_events_text(archived), _memory_events_text(compacted)
    archive_path = _tombstone_path(memory.path)
    if admission is not None:
        admission.check([OwnerQuotaChange(memory.path, len(text.encode("utf-8"))),
                         OwnerQuotaChange(archive_path, len(archive_text.encode("utf-8")))])
    if redact_contents:
        memory._redact_local_tool_ledgers(redact_contents)
    if commit is None and memory.coordinator_io:
        commit = _coordinator_memory_write

    # LLM: 仅编码后的固定叶进原末门，不把扫描、锁等待或上层准入搬进来源校验。
    # 函数用途: 复用普通或C私有原子writer。
    def write(path, raw):
        if commit is None:
            write_private_text_file_atomic_unlocked(path, raw)
        else:
            commit(lambda: write_private_text_file_atomic_unlocked(path, raw, replace_once=True))

    if archived and (not archive_path.exists() or archive_path.read_text(encoding="utf-8") != archive_text):
        write(archive_path, archive_text)
    write(memory.path, text)


# LLM: 小历史不跨最后删除屏障保留旧正文，即使同批重新add；最新记录承接访问/hash。
# 函数用途: 保留有界小历史，显式flush或超过128事件时仍完整压实。
def _fold_small_memory_history(events, compacted):
    latest = {record.entry_id: record for record in compacted}
    last_positions = {record.entry_id: index for index, record in enumerate(events) if record.action != "touch"}
    barriers = {record.entry_id: index for index, record in enumerate(events) if record.action == "remove"}
    return [latest[record.entry_id] if last_positions[record.entry_id] == index else record
            for index, record in enumerate(events) if record.action != "touch" and record.entry_id in latest
            and index >= barriers.get(record.entry_id, -1)]


# LLM: 归档只聚合已提交历史remove及截至该remove的hash；重新激活也不能在热库发布前抹掉旧归档证据。
# 函数用途: 保留每个ID最后的历史删除墓碑，不以当前active与否丢证据。
def _historical_tombstones(events):
    latest = {}
    hashes: dict[str, set[str]] = {}
    for record in events:
        bucket = hashes.setdefault(record.entry_id, set())
        bucket.update(record.removed_content_hashes)
        if record.content:
            bucket.add(memory_content_hash(record.content))
        if record.action == "remove":
            latest[record.entry_id] = replace(record, content="", attributes=None, tags=[], _extra_fields={},
                removed_content_hashes=tuple(sorted(bucket)))
    return list(latest.values())


# LLM: 生命周期仅一次搜索；query归一/滑窗一次，按对象身份记分，不混用其它查询分数。
# 类用途: 在词面候选和重排之间共享查询及评分。
class _MemoryLexicalQuery:
    # LLM: 窗口保留原重复计数及4/6/8/12顺序，不能集合化改变旧评分。
    # 函数用途: 准备本次查询的规范词与滑窗。
    def __init__(self, query):
        self.folded = fold_key(query)
        self.terms = {term for term in self.folded.split() if term}
        self.windows = [tuple(self.folded[start:start + window]
                       for start in range(0, max(1, len(self.folded) - window + 1), max(1, window // 2)))
                       for window in (4, 6, 8, 12)] if len(self.folded) >= 4 else []
        self.scores = {}

    # LLM: 记录只在同一次已验证候选内复用，首次真实评分，后续不重算。
    # 函数用途: 读取或计算本次记录的词面得分。
    def score(self, record):
        key = id(record)
        if key not in self.scores:
            self.scores[key] = _memory_search_score(record, self, self.terms)
        return self.scores[key]


_BACKFILL_SLOTS = threading.BoundedSemaphore(4)
_BACKFILL_GUARD = threading.Lock()
_BACKFILL_OWNERS: set[str] = set()


# LLM: 查询只缓存在当前owner实例，键绑定模型/端点；64项FIFO有界，不落查询明文。
# 函数用途: 读写当前空间下的进程内查询缓存。
def _query_vector(memory, query, vector=None):
    from ..retrieval.text_vector_cache import embedder_fingerprint

    key = (embedder_fingerprint(memory._embedder), query)
    with _BACKFILL_GUARD:
        cache = getattr(memory, "_query_vector_cache", {})
        if vector:
            cache[key] = tuple(vector)
            while len(cache) > 64:
                cache.pop(next(iter(cache)))
            memory._query_vector_cache = cache
        found = cache.get(key)
        return list(found) if found else None


# LLM: 绑定owner路径、embedder对象/指纹/维度、身份及最多32条版本快照，不持领域锁。
# 函数用途: 冻结一个有界向量补齐工作，不发网络或写文件。
def _vector_backfill_job(memory, query, records, cached):
    from ..retrieval.text_vector_cache import embedder_fingerprint

    query_vector = _query_vector(memory, query)
    cached = {entry_id: vector for entry_id, vector in cached.items()
              if isinstance(vector, (list, tuple)) and (not query_vector or len(vector) == len(query_vector))}
    return (memory, memory._memory_path_key(), memory._embedder, embedder_fingerprint(memory._embedder),
            deepcopy(memory._vector_identity), getattr(memory._embedder, "dim", None), query,
            deepcopy([record for record in records if record.entry_id not in cached][:32]))


# LLM: 全进程最多4个工作，每owner最多1个；C无跨线程来源不偷传source，保未补齐事实。
# 函数用途: 将缺失查询/文档向量排入后台，前台不等待网络。
def _queue_vector_backfill(memory, query, records, cached):
    if memory._embedder is None or memory.coordinator_io:
        return False
    job = _vector_backfill_job(memory, query, records, cached)
    if not job[-1] and _query_vector(memory, query):
        return False
    owner = memory._memory_path_key()
    with _BACKFILL_GUARD:
        if owner in _BACKFILL_OWNERS or not _BACKFILL_SLOTS.acquire(blocking=False):
            return False
        _BACKFILL_OWNERS.add(owner)

    # LLM: 每工作finally归还占用；失败留健康诊断，不无限重试或保存来源令牌。
    # 函数用途: 执行一次补齐并释放队列占用。
    def run():
        try:
            _run_vector_backfill(job)
        except Exception as exc:
            memory._record_semantic_health("backfill", exc)
        finally:
            with _BACKFILL_GUARD:
                _BACKFILL_OWNERS.discard(owner)
                _BACKFILL_SLOTS.release()

    try:
        threading.Thread(target=run, name="memory-vector-backfill", daemon=True).start()
    except BaseException:
        with _BACKFILL_GUARD:
            _BACKFILL_OWNERS.discard(owner)
            _BACKFILL_SLOTS.release()
        raise
    return True


# LLM: 只验证冻结结构化身份，不从线程名或文本补授权；模型或owner漂移即拒绝。
# 函数用途: 核对后台工作仍属于原owner及同一空间。
def _backfill_identity_current(job):
    from ..retrieval.text_vector_cache import embedder_fingerprint

    memory, owner, embedder, fingerprint, identity, dimension, _query, _records = job
    return (not memory.coordinator_io and memory._memory_path_key() == owner and memory._embedder is embedder
            and embedder_fingerprint(embedder) == fingerprint and memory._vector_identity == identity
            and getattr(embedder, "dim", None) == dimension)


# LLM: 网络不占quota/policy/formal锁；发布同formal锁复核owner/model/dim及entry/version/hash/active。
# 函数用途: 补齐最多32条文档及查询缓存，拒绝发布已删/变化的旧向量。
@counted_as("memory_recall")
def _run_vector_backfill(job):
    from ..retrieval.text_vector_cache import index_text

    memory, _owner, embedder, fingerprint, _identity, _dimension, query, records = job
    if not _backfill_identity_current(job):
        return
    with embedding_input_type("query"):
        query_vector = _query_vector(memory, query) or embedder.embed([query])[0]
    texts = [index_text(record.content, record.attributes) for record in records]
    with embedding_input_type("db"):
        vectors = embedder.embed(texts) if texts else []
    if len(vectors) != len(records) or any(len(vector) != len(query_vector) for vector in vectors):
        return
    with memory._locked_memory():
        if not _backfill_identity_current(job):
            return
        current = {record.entry_id: record for record in memory._maintenance_records()}
        rows = {}
        for record, text, vector in zip(records, texts, vectors):
            active = current.get(record.entry_id)
            if active is not None and active.version == record.version and memory_content_hash(
                    index_text(active.content, active.attributes)) == memory_content_hash(text):
                rows[memory._cache_key(fingerprint, text)] = vector
        cache = memory._text_vector_cache()
        if rows:
            cache.put(rows)
        if cache.last_write_error is not None:
            raise cache.last_write_error
        _query_vector(memory, query, query_vector)
        memory._record_semantic_health("backfill")
