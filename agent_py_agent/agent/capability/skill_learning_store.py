# LLM: 自学习 S3（自动总结 Skill）的唯一落盘权威。目录固定为 owner 路径解析器登记的 owner_skill_learning_dir：
#   requests/ 过渡工作单、registry.json 自学 Skill 登记表（“是不是自学 Skill”只由它回答）、ledger.jsonl 只追加账本、
#   versions/ 版本全文、removed/ 用户删除归档。发布位置是 <owner_home>/skills/learned/<name>/SKILL.md。
#   .state 短锁保护登记表/队列读改写；.run 非阻塞长锁只保证同一 owner 同时只有一个总结在跑。
#   同步检查 skill_learning.py / skill_learning_publish.py / cli/skill_learning_commands.py 与 test_skill_learning*.py。
# 模块用途: 读写有界请求及溢出缓冲、每日总结调用历史、登记表和版本；不调用模型，不代替正式发布确认。
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Collection
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import MISSING, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.json_io import (
    append_private_jsonl_capped,
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import (
    NoFollowPathError,
    _openat_file,
    _validate_relative_parts,
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaChange,
    owner_quota_enforcer_from_policy,
)
from .skill_snapshot import skill_content_sha256
from .skills import MAX_SKILL_BYTES

if TYPE_CHECKING:
    from ..runtime_db.repository import RuntimeRepository

REQUEST_SCHEMA_VERSION = "my-agent.skill-learning-request.v1"
REGISTRY_SCHEMA_VERSION = "my-agent.skill-learning-registry.v1"
EVENT_SCHEMA_VERSION = "my-agent.skill-learning-event.v1"
LEARNED_DIR_NAME = "learned"
# 学习请求队列最大积压条数。
MAX_PENDING_REQUESTS = 20
# 自动总结学习请求队列允许积压的硬上限。
MAX_OVERFLOW_REQUESTS = 100
# 单条学习请求最多重试次数。
MAX_REQUEST_ATTEMPT_COUNT = 2
MAX_LEDGER_EVENTS = 2000
# 同一来源运行最多保留条数。
MAX_SOURCE_RUN_COUNT = 10

# 结构化事件与结果码：账本、CLI 和测试只按这些值判断，reason 只给人看。
EVENT_PUBLISHED = "published"
EVENT_UPDATED = "updated"
EVENT_SKIPPED = "skipped"
EVENT_REJECTED = "rejected"
EVENT_FAILED = "failed"
EVENT_DROPPED = "dropped"
EVENT_REVERTED = "reverted"
EVENT_REMOVED = "removed"
CODE_QUEUE_FULL = "SKILL_LEARNING_QUEUE_FULL"
# 用户要求总结的会话任务，收尾时那次运行不合格（后台续跑、不落盘等），没能入队。
CODE_USER_REQUEST_INELIGIBLE = "SKILL_LEARNING_USER_REQUEST_INELIGIBLE"
CODE_REQUEST_CORRUPT = "SKILL_LEARNING_REQUEST_CORRUPT"
CODE_REGISTRY_CORRUPT = "SKILL_LEARNING_REGISTRY_CORRUPT"
CODE_EVIDENCE_INCOMPLETE = "SKILL_LEARNING_EVIDENCE_INCOMPLETE"
CODE_MAINTENANCE_OBSERVE_ONLY = "SKILL_LEARNING_OBSERVE_ONLY"
CODE_ARCHIVE_EXPIRED = "SKILL_LEARNING_ARCHIVE_EXPIRED"

_REQUEST_FILE_RE = re.compile(r"[0-9]{13}-[0-9a-f]{16}\.json")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_FAMILY_RE = re.compile(r"^$|^[0-9a-f]{64}$")
_VALID_MAINT_STATES = frozenset({"trial", "active", "cooled", "archived", "disabled"})
_VALID_MAINT_QUALITIES = frozenset({"trial", "established", "unreliable"})
_VALID_USAGE_COVERAGES = frozenset({"exact", "lower_bound", "unknown"})
_VALID_OWNERSHIPS = frozenset({"machine", "user_modified", "owner_layout", "unknown"})
_VALID_OP_KINDS = frozenset({"archive", "restore", "displace_publish", "evict", "remove", "rollback"})
_VALID_OP_PHASES = frozenset({"prepared", "moved", "committed", "cleanup"})
_VALID_CURSOR_PHASES = frozenset({"recover", "usage", "ownership", "lifecycle", "versions", "receipts", "done"})
_STABLE_VERSION_KEYS = frozenset({"version", "revision", "sha256", "established_at"})
_CURSOR_KEYS = frozenset({"phase", "after_name", "after_entry", "source_generation", "started_at", "retry_after"})
_LIFECYCLE_OP_KEYS = frozenset({
    "operation_id",
    "kind",
    "phase",
    "skill_name",
    "expected_revision",
    "expected_sha256",
    "source_rel",
    "target_rel",
    "versions_rel",
    "candidate_rel",
    "tombstone_rel",
    "candidate_name",
    "candidate_sha256",
    "candidate_revision",
    "cleanup_cursor",
    "event_id",
    "created_at",
    "reason_code",
})
_LIFECYCLE_OP_REQUIRED = frozenset({
    "operation_id",
    "kind",
    "phase",
    "skill_name",
    "expected_revision",
    "expected_sha256",
    "source_rel",
    "target_rel",
    "versions_rel",
    "created_at",
})


# LLM: code 是唯一机器判断字段；登记表损坏时 fail closed，调用方不能把它当成空登记表继续写。
# 类用途: 表示自学 Skill 落盘状态读不出或不合规。
class SkillLearningStoreError(Exception):
    # LLM: 只保存结果码、结构化细节和人读说明，不携带文件正文。
    # 函数用途: 构造一次带结果码的存储错误。
    def __init__(self, code: str, message: str = "", *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.detail = dict(detail or {})


# LLM: sha256 是发布时 SKILL.md 全文的 skill_content_sha256，与 SkillsService 快照条目的 content_sha256 同算法；
#   磁盘文件的 hash 不等于它就说明用户改过，自动流程必须把这个 Skill 当作用户所有。
# 类用途: 登记表里一条自学 Skill 的当前版本事实。
@dataclass(frozen=True)
class LearnedSkill:
    name: str
    version: int
    sha256: str
    created_at: str
    updated_at: str
    source_run_ids: tuple[str, ...] = ()
    revision: int = 1
    last_operation_id: str = ""
    maintenance: dict = field(default_factory=dict)
    layout: str = "learned"

    # LLM: 字段名是持久化协议；元组写成 JSON 数组。
    # 函数用途: 转成写入 registry.json 的字典。
    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "revision": self.revision,
            "last_operation_id":self.last_operation_id,
            "maintenance":dict(self.maintenance),
            "layout":self.layout,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_run_ids": list(self.source_run_ids),
        }

    # LLM: 严格解析；任何字段不合规都抛 ValueError，由 load_registry 统一转成 REGISTRY_CORRUPT。
    # 函数用途: 从 registry.json 的一项恢复登记事实。
    @classmethod
    def from_record(cls, payload: object) -> LearnedSkill:
        if not isinstance(payload, dict):
            raise ValueError("learned skill record must be an object")
        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("learned skill version must be a positive integer")
        sha = str(payload.get("sha256") or "")
        if not _SHA256_RE.fullmatch(sha):
            raise ValueError("learned skill sha256 is invalid")
        revision = payload.get("revision", version)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < version:
            raise ValueError("learned skill revision is invalid")
        runs = payload.get("source_run_ids") or []
        if not isinstance(runs, list) or not all(isinstance(item, str) for item in runs):
            raise ValueError("learned skill source_run_ids must be a list of ids")
        if not _SKILL_NAME_RE.fullmatch(_text(payload, "name")) or payload.get("layout", "learned") not in {"owner", "learned"}:
            raise ValueError("learned skill layout/name invalid")
        raw_op_id = payload.get("last_operation_id")
        if raw_op_id is not None and not isinstance(raw_op_id, str):
            raise ValueError("learned skill last_operation_id must be a string")
        raw_maint = payload.get("maintenance")
        if raw_maint is None:
            maint = {}
        elif not isinstance(raw_maint, dict):
            raise ValueError("learned skill maintenance must be an object")
        else:
            maint = _validate_maintenance(raw_maint)
        return cls(_text(payload, "name"), version, sha, _text(payload, "created_at"),
                   _text(payload, "updated_at"), tuple(runs), revision, raw_op_id or "", maint, str(payload.get("layout") or "learned"))


# LLM: skills 是自学 Skill 的唯一登记；blocked_names 是用户删过的名字，自动流程永不再新建；
#   blocked_task_families 保存用户/三次负反馈永久停用的同类方法指纹；pending_operation 记录可恢复目录转换意图；
#   usage_watermark 与 runtime receipt sequence 属于不同域，绝不混用比较。
# 类用途: registry.json 的完整内容。
@dataclass(frozen=True)
class SkillLearningRegistry:
    skills: dict[str, LearnedSkill] = field(default_factory=dict)
    blocked_names: tuple[str, ...] = ()
    daily_date: str = ""
    daily_calls: int = 0
    import_operations: dict = field(default_factory=dict)
    daily_history: dict[str, int] = field(default_factory=dict)
    assets: dict[str, dict] = field(default_factory=dict)
    last_maintenance_at: float = 0
    usage_watermark: int = 0
    maintenance_cursor: dict = field(default_factory=dict)
    pending_operation: dict | None = None
    blocked_task_families: tuple[str, ...] = ()
    catalog_generation: str = ""

    # LLM: 保持原schema和单日快照，另存有界每日调用历史与增量维护/意图状态；不改正式Skill登记事实。
    # 函数用途: 转成写入 registry.json 的字典。
    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "skills": {name: self.skills[name].to_record() for name in sorted(self.skills)},
            "blocked_names": sorted(set(self.blocked_names)),
            "daily": {"date": self.daily_date, "calls": self.daily_calls},
            "daily_history": dict(self.daily_history),
            "import_operations": dict(self.import_operations),
            "assets": dict(self.assets),
            "last_maintenance_at": self.last_maintenance_at,
            "usage_watermark": int(self.usage_watermark),
            "maintenance_cursor": dict(self.maintenance_cursor),
            "pending_operation": dict(self.pending_operation) if self.pending_operation is not None else None,
            "blocked_task_families": sorted(set(self.blocked_task_families)),
            "catalog_generation": str(self.catalog_generation),
        }

    # LLM: 纯计数接口；只统计单次磁盘归属核验通过的在役（非 archived）机器管辖条目，不触发隐藏磁盘IO。
    # 函数用途: 计算当前真实占用所有者配额上限的在役自学 Skill 数量。
    def live_count(self, *, managed_names: Collection[str]) -> int:
        allowed = set(managed_names)
        return sum(
            1
            for name, record in self.skills.items()
            if name in allowed and record.maintenance.get("state") != "archived"
        )


# LLM: 事件只含身份/hash/结果码/有界reason及可选载体；carrier仅展示静态选择，不授执行权，不写正文。
# 类用途: 账本里的一条自学事件。
@dataclass(frozen=True)
class SkillLearningEvent:
    event: str
    code: str = ""
    skill_name: str = ""
    version: int = 0
    sha256: str = ""
    request_key: str = ""
    run_id: str = ""
    reason: str = ""
    carrier: str = ""

    # LLM: event_id/时间落盘时生成，reason最多200字；只为有载体的新晋升附carrier，保留旧事件形状。
    # 函数用途: 转成写入 ledger.jsonl 的一行。
    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "at": utc_now(),
            "event": self.event,
            "code": self.code,
            "skill_name": self.skill_name,
            "version": self.version,
            "sha256": self.sha256,
            "request_key": self.request_key,
            "run_id": self.run_id,
            "reason": self.reason[:200],
            **({'carrier':self.carrier} if self.carrier else {}),
        }


# LLM: 只认同一规范owner根的原配额准入对象；他owner准入或任意对象都不能借道免取本owner配额锁。
# 函数用途: 校验调用方传入的配额准入属于本学习存储所在owner。
def _same_owner_admission(admission: object, owner_root: Path) -> bool:
    enforcer = getattr(admission, "_enforcer", None)
    return (isinstance(admission, OwnerQuotaAdmission)
            and getattr(enforcer, "owner_root", None) == Path(owner_root).expanduser().resolve(strict=False))


# LLM: 所有路径都从 owner 规范布局推导；构造不创建任何目录，第一次写入时才创建。
# 类用途: 一个owner的学习存储，热请求20条、溢出100条并保留独立每日总结调用历史。
class SkillLearningStore:
    # LLM: 只绑定原owner根与热/溢出目录，不在构造中创建目录、打开runtime.db或触发外部provider调用。
    # 函数用途: 绑定自学数据目录与 owner Skill 根，并接收延迟宿主运行库提供方。
    def __init__(
        self,
        directory: Path,
        skills_root: Path,
        *,
        usage_runtime_provider: Callable[[], RuntimeRepository | None] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.skills_root = Path(skills_root)
        self.learned_root = self.skills_root / LEARNED_DIR_NAME
        self.requests_dir = self.directory / "requests"
        self.overflow_requests_dir = self.directory / "requests_overflow"
        self.archived_dir = self.directory / "archived"
        self.registry_path = self.directory / "registry.json"
        self.ledger_path = self.directory / "ledger.jsonl"
        self.versions_dir = self.directory / "versions"
        self.removed_dir = self.directory / "removed"
        self._quota_admission = ContextVar("skill_learning_quota", default=None)
        self._usage_runtime_provider = usage_runtime_provider

    # LLM: 与 SkillProposalService 同一推导：数据目录来自 owner_skill_learning_dir，Skill 根是 owner_home/skills；
    #   仅转发宿主提供方而不提前求值，未传时保持传统只读/无精确回执语义，绝不从磁盘残留 DB 回退打开。
    # 函数用途: 按 owner 路径投影构造存储。
    @classmethod
    def for_home(
        cls,
        home_paths: object,
        *,
        usage_runtime_provider: Callable[[], RuntimeRepository | None] | None = None,
    ) -> SkillLearningStore:
        return cls(
            Path(home_paths.owner_skill_learning_dir),
            Path(home_paths.owner_home_dir) / "skills",
            usage_runtime_provider=usage_runtime_provider,
        )

    # LLM: 仅在真正需要读写运行库回执时才求值宿主提供方，并强制校验其规范库路径属于当前 owner。
    # 函数用途: 返回经属主校验的当前 owner 运行库实例；未配置或显式无托管时返回 None。
    def _runtime_repository(self) -> RuntimeRepository | None:
        provider = getattr(self, "_usage_runtime_provider", None)
        if provider is None:
            return None
        repo = provider()
        if repo is None:
            return None
        from ..runtime_db.schema import runtime_db_path

        expected = runtime_db_path(self.skills_root.parent).expanduser().resolve(strict=False)
        actual_raw = getattr(repo, "db_path", None)
        if not actual_raw:
            raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, "runtime repository missing db_path")
        actual = Path(actual_raw).expanduser().resolve(strict=False)
        if actual != expected:
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"runtime repository path mismatch: expected {expected}, got {actual}",
            )
        return repo

    # LLM: owner配额→state锁固定顺序；登记、队列、版本、导入同源事务，不能在持锁期间调用模型或发网络请求。
    #   admission仅供已先持本owner原配额准入、且在整个调用期间保持持有的宿主调用方（提炼）传入：复用它取state锁，
    #   不再重取不可重入的配额锁；他owner或非准入对象拒绝。默认None仍按原quota→state取锁。
    # 函数用途: 返回登记表与队列的互斥区。
    @contextmanager
    def state_lock(self, *, admission=None):
        with ExitStack() as stack:
            if admission is None:
                admission = stack.enter_context(owner_quota_enforcer_from_policy(self.skills_root.parent).admission())
            elif not _same_owner_admission(admission, self.skills_root.parent):
                raise RuntimeError("学习写入缺少本owner原配额事务")
            stack.enter_context(locked_json_path(self.directory / ".state"))
            token = self._quota_admission.set(admission)
            try:
                yield admission
            finally:
                self._quota_admission.reset(token)

    # LLM: 写入必须处于owner配额→state锁序内，按最终UTF-8字节检查，不新建第二配额权威。
    # 函数用途: 给发布、候选和版本文件复用同一owner磁盘限制。
    def check_write(self, path, text):
        admission = self._quota_admission.get()
        if admission is None:
            raise RuntimeError("学习写入缺少原配额事务")
        admission.check([OwnerQuotaChange(path, len(text.encode("utf-8")))])

    # LLM: 非阻塞：拿不到抛 BlockingIOError，调用方按 busy 顺延；进程崩溃时 OS 锁自动释放。
    # 函数用途: 返回“同一 owner 同时只跑一个总结”的运行锁。
    def run_lock(self):
        return locked_json_path(self.directory / ".run", blocking=False)

    # LLM: 热/溢出区共同按键与非空上下文去重，最老20条驻留热区，仅总容量满才拒绝，事件在state锁外追加。
    # 函数用途: 有界保存学习材料，返回queued/duplicate/queue_full，不调用模型或发布Skill。
    def enqueue(self, request: dict[str, object]) -> str:
        key = str(request.get("request_key") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", key):
            raise SkillLearningStoreError(CODE_REQUEST_CORRUPT, "invalid learning request key")
        with self.state_lock():
            outcome, event = self._enqueue_locked(request, key)
        if event is not None:
            self.append_event(event)
        return outcome

    # LLM: 默认只读全局FIFO投影；纯溢出返回完整排序路径，hot_only仅限物理热区；显式refill才在短门与state锁内写。
    # 函数用途: 发现可消费的学习请求，可选严格热区视图或执行崩溃收敛与溢出回填。
    def pending_requests(self, *, refill: bool = False, hot_only: bool = False) -> list[Path]:
        if not refill:
            return self._projected_requests(hot_only=hot_only)
        if self._needs_recovery_or_refill():
            from ..common.request_execution_ports import gateway_short_transaction

            with gateway_short_transaction("skill_learning_refill_requests"), self.state_lock():
                self._recover_requests_locked()
        return self._request_paths("requests")

    # LLM: 只列本owner固定目录中的宿主请求名，不跟随目录链接；不执行回填或修改队列。
    # 函数用途: 返回溢出缓冲的FIFO路径，供去重、报告与恢复使用。
    def overflow_requests(self) -> list[Path]:
        return self._request_paths("requests_overflow")

    # LLM: 目录名仅由本类固定调用提供，名单经nofollow枚举；每次正文访问仍重新验证父链和叶子。
    # 函数用途: 只列合法请求文件名，排除锁与临时文件。
    def _request_paths(self, folder: str) -> list[Path]:
        names = list_names_beneath(self.directory, (folder,))
        return [self.directory / folder / name for name in sorted(names) if _REQUEST_FILE_RE.fullmatch(name)]

    # LLM: 只读扫描两区并按宿主文件名保留同键/上下文最新材料；旧布局也按全局FIFO，纯溢出完整返回，不写盘建锁。
    # 函数用途: 计算崩溃后或正常运行时的去重投影，允许显式限制在物理热区。
    def _projected_requests(self, *, hot_only: bool = False) -> list[Path]:
        hot = self._request_paths("requests")
        overflow = self.overflow_requests()
        if not hot and not overflow:
            return []
        surviving, _superseded = self._classify_requests([*hot, *overflow])
        if hot_only:
            return [path for path in surviving if path.parent == self.requests_dir][:MAX_PENDING_REQUESTS]
        return surviving[:MAX_PENDING_REQUESTS] if hot else surviving

    # LLM: 只读比较物理热区与全局FIFO前20条，覆盖旧布局的更老溢出、未满热区以及任意规模中断重复。
    # 函数用途: 判断消费端refill=True是否需要进入短写事务收敛磁盘状态。
    def _needs_recovery_or_refill(self) -> bool:
        hot = self._request_paths("requests")
        overflow = self.overflow_requests()
        surviving, superseded = self._classify_requests([*hot, *overflow])
        return bool(superseded) or hot != surviving[:MAX_PENDING_REQUESTS]

    # LLM: 只读同文件/同键及可替代上下文；复用入队的同一比较器核轨迹与合并关系，不把相异任务误去重。
    # 函数用途: 识别最新材料及崩溃遗留旧副本，不为发现写盘或建立另一索引。
    def _classify_requests(self, paths: list[Path]) -> tuple[list[Path], list[Path]]:
        ordered = sorted(paths, key=lambda item: (item.name, 0 if item.parent.name == "requests" else 1))
        seen_names: set[str] = set()
        seen_keys: set[str] = set()
        kept_payloads: list[dict] = []
        kept_reversed: list[Path] = []
        superseded: list[Path] = []
        for path in reversed(ordered):
            if path.name in seen_names:
                superseded.append(path)
                continue
            payload = self.read_request(path)
            req_key = str((payload or {}).get("request_key") or path.stem.rsplit("-", 1)[-1])
            if req_key in seen_keys or any(_mergeable_requests(payload or {}, kept) for kept in kept_payloads):
                superseded.append(path)
                continue
            seen_names.add(path.name)
            if req_key:
                seen_keys.add(req_key)
            kept_payloads.append(payload or {})
            kept_reversed.append(path)
        kept_reversed.reverse()
        return kept_reversed, superseded

    # LLM: 保持13位毫秒前缀文件名协议；同一毫秒突发入队时按既有最大前缀递增1，确保文件名严格单调体现FIFO。
    # 函数用途: 为新入队请求生成严格单调递增的请求文件名。
    def _next_request_name(self, key: str, existing: list[Path]) -> str:
        now_ms = int(time.time() * 1000)
        last_ms = max((int(path.name[:13]) for path in existing), default=0)
        stamp = min(9_999_999_999_999, max(now_ms, last_ms + 1))
        return f"{stamp:013d}-{key}.json"

    # LLM: 调用方持原state/配额锁；先收敛中断遗留，再跨热/溢出双区去重与同上下文合并，维持热区始终是最老20条。
    # 函数用途: 执行请求去重、跨区同类替换和全局FIFO溢出，返回需锁外记录的告警。
    def _enqueue_locked(self, request: dict[str, object], key: str):
        self._recover_requests_locked()
        pending, overflow = self._request_paths("requests"), self.overflow_requests()
        all_paths = [*pending, *overflow]
        if request.get('completion_ref'):
            for path in all_paths:
                if path.name.endswith(f'-{key}.json'):
                    previous = self.read_request(path)
                    if previous is None:
                        raise SkillLearningStoreError(CODE_REQUEST_CORRUPT)
                    same = all(previous.get(field) == request.get(field) for field in
                               ('completion_identity', 'trajectory_digest', 'trajectory_fingerprint'))
                    return ('duplicate' if same else 'conflict'), None
            # 原账本先记结果再删FIFO；重启重放沿原request_key去重，不另建完成或权限账。
            if any(row.get('request_key') == key and row.get('event') in
                   {'published', 'updated', 'skipped', 'rejected', 'failed', 'proposed'} for row in self.events(limit=0)):
                return 'duplicate', None
        if any(path.name.endswith(f"-{key}.json") for path in all_paths):
            return "duplicate", None
        name = self._next_request_name(key, all_paths)
        if len(pending) >= MAX_PENDING_REQUESTS:
            match = next((path for path in all_paths if self._same_request_context(path, request)), None)
            if match is not None:
                target_folder = match.parent.name
                self._write_request_locked(target_folder, name, request)
                unlink_file_beneath(self.directory, (target_folder, match.name))
                self._recover_requests_locked()
                return "queued", None
            if len(overflow) >= MAX_OVERFLOW_REQUESTS:
                return "queue_full", None
            self._write_request_locked("requests_overflow", name, request)
            return "queued", SkillLearningEvent(
                "overflowed",
                "SKILL_LEARNING_QUEUE_OVERFLOW_SAVED",
                request_key=key,
            )
        self._write_request_locked("requests", name, request)
        self._recover_requests_locked()
        return "queued", None

    # LLM: 原队列与崩溃投影共用成对比较；准确task冲突不能退到thread，不同轨迹无合并关系不得覆盖。
    # 函数用途: 判断满队列中的旧材料是否允许由这条新请求替代。
    def _same_request_context(self, path: Path, request: dict[str, object]) -> bool:
        previous = self.read_request(path)
        return previous is not None and _mergeable_requests(previous, request)

    # LLM: 只在原state/配额事务内写固定目录；新记录先原子落盘，不改变请求正文协议或确认权限。
    # 函数用途: 安全写入一条热请求或重试记录。
    def _write_request_locked(self, folder: str, name: str, request: dict[str, object]) -> None:
        text = json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.check_write(self.directory / folder / name, text)
        write_text_atomic_beneath(self.directory, (folder, name), text)

    # LLM: 原admission联合核算目标写入和源删除的最终字节；先复制后删，崩溃同名副本可收口，异内容拒绝覆盖。
    # 函数用途: 在热区与缓冲区间移动一条请求，保持原文件名和FIFO顺序。
    def _transfer_request_locked(self, source: Path, folder: str) -> None:
        parts = self._request_parts(source)
        text = read_text_beneath(self.directory, parts)
        if text is None:
            return
        destination = (folder, source.name)
        existing = read_text_beneath(self.directory, destination)
        if existing is not None and existing != text:
            raise SkillLearningStoreError(CODE_REQUEST_CORRUPT, "conflicting buffered request")
        admission = self._quota_admission.get()
        if admission is None:
            raise RuntimeError("学习移动缺少原配额事务")
        admission.check([OwnerQuotaChange(self.directory.joinpath(*destination), len(text.encode("utf-8"))),
                         OwnerQuotaChange(self.directory.joinpath(*parts), None)])
        write_text_atomic_beneath(self.directory, destination, text)
        unlink_file_beneath(self.directory, parts)

    # LLM: 在持state锁内清理中断遗留的被替代旧文件，并将全局FIFO最老20条维持在requests/、其余维持在requests_overflow/。
    # 函数用途: 收敛跨区合并中断状态并按全局FIFO补齐热队列，杜绝溢出饿死。
    def _recover_requests_locked(self) -> None:
        all_paths = [*self._request_paths("requests"), *self.overflow_requests()]
        if not all_paths:
            return
        surviving, superseded = self._classify_requests(all_paths)
        for old_path in superseded:
            if old_path.parent.name in ("requests", "requests_overflow"):
                other = "requests_overflow" if old_path.parent.name == "requests" else "requests"
                other_text = read_text_beneath(self.directory, (other, old_path.name))
                own_text = read_text_beneath(self.directory, (old_path.parent.name, old_path.name))
                if other_text is not None and own_text is not None and other_text != own_text:
                    raise SkillLearningStoreError(CODE_REQUEST_CORRUPT, "conflicting buffered request")
            unlink_file_beneath(self.directory, (old_path.parent.name, old_path.name))
        desired_hot = surviving[:MAX_PENDING_REQUESTS]
        desired_overflow = surviving[MAX_PENDING_REQUESTS:]
        for path in desired_overflow:
            if path.parent.name != "requests_overflow":
                self._transfer_request_locked(path, "requests_overflow")
        for path in desired_hot:
            if path.parent.name != "requests":
                self._transfer_request_locked(path, "requests")

    # LLM: 请求路径必须是本owner两个固定目录的单个宿主文件名，拒绝外部路径、遍历段及任意目录。
    # 函数用途: 将可信队列路径转换为nofollow读写使用的相对分量。
    def _request_parts(self, path: Path) -> tuple[str, str]:
        parts = Path(path).relative_to(self.directory).parts
        if len(parts) != 2 or parts[0] not in {"requests", "requests_overflow"} or not _REQUEST_FILE_RE.fullmatch(parts[1]):
            raise SkillLearningStoreError(CODE_REQUEST_CORRUPT, "invalid request path")
        return parts

    # LLM: nofollow读取同owner精确请求；原路径刚轮转时找同名缓冲，坏内容/身份不符仍返回None，不读其它键。
    # 函数用途: 读取一条请求。
    def read_request(self, path: Path) -> dict[str, object] | None:
        try:
            folder, name = self._request_parts(path)
            text = read_text_beneath(self.directory, (folder, name))
            if text is None:
                other = "requests_overflow" if folder == "requests" else "requests"
                text = read_text_beneath(self.directory, (other, name))
            payload = json.loads(text) if text is not None else None
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != REQUEST_SCHEMA_VERSION:
            return None
        return payload

    # LLM: 重试只改原键，已轮转时写同名缓冲，已被同类新请求替代时不复活旧请求。
    # 函数用途: 原子覆盖一条请求。
    def rewrite_request(self, path: Path, request: dict[str, object]) -> None:
        with self.state_lock():
            _folder, name = self._request_parts(path)
            for folder in ("requests", "requests_overflow"):
                if read_text_beneath(self.directory, (folder, name)) is not None:
                    self._write_request_locked(folder, name, request)

    # LLM: state锁内先验路径并收敛中断替代关系，再按原文件名删除；删新不复活旧、删旧不误删新，最后补齐FIFO热区。
    # 函数用途: 在保留崩溃替代事实的前提下删除准确请求并补齐热队列。
    def discard_request(self, path: Path) -> None:
        with self.state_lock():
            _folder, name = self._request_parts(path)
            self._recover_requests_locked()
            for folder in ("requests", "requests_overflow"):
                unlink_file_beneath(self.directory, (folder, name))
            self._recover_requests_locked()

    # LLM: 文件不存在视为空登记表；存在、为符号链接或损坏时抛 REGISTRY_CORRUPT，绝不按空表覆盖写回。
    # 函数用途: 读取登记表。
    def load_registry(self) -> SkillLearningRegistry:
        if self.registry_path.is_symlink():
            raise SkillLearningStoreError(CODE_REGISTRY_CORRUPT, "skill learning registry must not be a symlink")
        if not self.registry_path.exists():
            return SkillLearningRegistry()
        report = read_json_object_report(self.registry_path, context="skill_learning.registry")
        try:
            if report.load_error is not None:
                raise ValueError("unreadable registry")
            registry = _registry_from(report.payload)
            # 旧登记没有高水位：只从受管版本目录已有版本文件名恢复编号，不跟随链接、不重用回滚前编号。
            records = report.payload.get("skills", {})
            migrated: dict[str, LearnedSkill] = {}
            for name, item in registry.skills.items():
                if "revision" not in records[name]:
                    version_names = list_names_beneath(self.directory, ("versions", name))
                    highest = max((_version_name_number(entry) for entry in version_names), default=item.version)
                    migrated[name] = replace(item, revision=max(item.revision, highest))
                else:
                    migrated[name] = item
            return replace(registry, skills=migrated)
        except (OSError, TypeError, ValueError) as exc:
            raise SkillLearningStoreError(CODE_REGISTRY_CORRUPT, str(exc)) from exc

    # LLM: 调用方必须已持 state_lock；原子替换整份登记表。
    # 函数用途: 写回登记表。
    def save_registry(self, registry: SkillLearningRegistry) -> None:
        self.check_write(self.registry_path, json.dumps(registry.to_record(),ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        write_json_file_atomic_unlocked(self.registry_path, registry.to_record())

    # LLM: 原state锁内发请求前扣额度，失败也计数；UTC快照和30天历史同步，拒绝额度不产生新计数。
    # 函数用途: 尝试占用今天的一次总结调用额度，返回是否成功。
    def consume_daily_call(self, limit: int) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.state_lock():
            registry = self.load_registry()
            history = dict(registry.daily_history)
            calls = max(registry.daily_calls if registry.daily_date == today else 0, history.get(today, 0))
            if limit > 0 and calls >= limit:
                return False
            history[today] = calls + 1
            self.save_registry(replace(registry, daily_date=today, daily_calls=calls + 1,
                                       daily_history=_recent_daily_history(history)))
        return True

    # LLM: 有界追加（保留最近 MAX_LEDGER_EVENTS 条），自带 per-path 锁，调用方不需要持 state_lock。
    # 函数用途: 往账本追加一条事件。
    def append_event(self, event: SkillLearningEvent) -> None:
        row = event.to_record()
        with owner_quota_enforcer_from_policy(self.skills_root.parent).admission() as admission:
            existing = read_jsonl_objects_report(self.ledger_path,context="skill_learning.ledger.quota").records if self.ledger_path.exists() else []
            text = "".join(json.dumps(item,ensure_ascii=False,sort_keys=True)+"\n" for item in [*existing,row][-MAX_LEDGER_EVENTS:])
            admission.check([OwnerQuotaChange(self.ledger_path,len(text.encode()))])
            append_private_jsonl_capped(self.ledger_path, row, max_records=MAX_LEDGER_EVENTS)

    # LLM: 坏行由 read_jsonl_objects_report 跳过；name 为空时返回全部事件的最后 limit 条。
    # 函数用途: 读取某个 Skill（或全部）的最近事件。
    def events(self, name: str = "", limit: int = 20) -> list[dict[str, object]]:
        if not self.ledger_path.is_file():
            return []
        records = read_jsonl_objects_report(self.ledger_path, context="skill_learning.ledger").records
        selected = [item for item in records if not name or item.get("skill_name") == name]
        return selected[-limit:] if limit > 0 else selected

    # LLM: 现有skill_feedback状态读口通过此显式钩子复用统一使用台账；延迟导入避免Store与台账循环依赖。
    # 函数用途: 只读返回本owner的Skill使用汇总，不创建另一份统计或访问其它owner。
    def usage_summary(self) -> dict[str, object]:
        from .skill_usage_ledger import load_skill_usage_summary

        return load_skill_usage_summary(self)

    # LLM: 只看requests与requests_overflow两个受管目录的合法宿主请求文件名；不读正文、不恢复、不解析registry.json。
    # 函数用途: O(1)目录扫描判断是否存在待处理学习请求。
    def has_requests(self) -> bool:
        for folder in ("requests", "requests_overflow"):
            for name in list_names_beneath(self.directory, (folder,)):
                if _REQUEST_FILE_RE.fullmatch(name):
                    return True
        return False

    # LLM: 唯一机器管辖归属核验边界；layout=owner、owner_layout/user_modified、正文hash失配，
    #   以及无prepared/moved未决操作时的missing正文均作排除；链接、超限、不可读、invalid_utf8、
    #   未收敛prepared/moved操作或unknown标记仍fail-closed抛SKILL_LEARNING_EVIDENCE_INCOMPLETE。
    # 函数用途: 返回当前磁盘核验通过的机器管辖自学 Skill 映射（可选择是否含已归档条目）。
    def managed_records(
        self,
        registry: SkillLearningRegistry,
        *,
        include_archived: bool = False,
    ) -> dict[str, LearnedSkill]:
        pending = registry.pending_operation
        if pending is not None and pending.get("phase") in {"prepared", "moved"}:
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"unrecovered pending lifecycle operation {pending.get('operation_id')}",
                detail={"operation_id": pending.get("operation_id"), "phase": pending.get("phase")},
            )
        verified: dict[str, LearnedSkill] = {}
        for name, record in sorted(registry.skills.items()):
            maint = record.maintenance
            is_archived = maint.get("state") == "archived"
            if is_archived and not include_archived:
                continue
            ownership = maint.get("ownership")
            if record.layout == "owner" or ownership in {"owner_layout", "user_modified"}:
                continue
            if ownership == "unknown":
                raise SkillLearningStoreError(
                    CODE_EVIDENCE_INCOMPLETE,
                    f"skill {name} ownership is unknown",
                    detail={"skill_name": name, "reason": "ownership_unknown"},
                )
            anchor = self.directory if is_archived else self.skills_root
            rel_parts = ("archived", name, "SKILL.md") if is_archived else (LEARNED_DIR_NAME, name, "SKILL.md")
            try:
                text = _read_managed_skill_text_strict(anchor, rel_parts, skill_name=name)
            except SkillLearningStoreError as exc:
                if (exc.detail or {}).get("reason") == "missing":
                    # 无未决操作时正文缺失按手动删除排除并释放槽位，后续归属核验再标记user_modified。
                    continue
                raise
            if skill_content_sha256(text) != record.sha256:
                # 磁盘存在完整常规文件且正文摘要明确不同，确认用户已接管修改，作排除项而非未决证据。
                continue
            verified[name] = record
        return verified

    # LLM: feedback负责adopted权限与claim；过滤MISSING后按公开关键字形状调用台账，保留显式None与故障软失败。
    # 函数用途: 接通现有采用反馈的可选写账钩子，不确认或发布任何Skill内容。
    def record_skill_usage(
        self,
        *,
        event: str,
        skill_id: str,
        source: str,
        kind: str = "skill",
        request_id: object = MISSING,
        thread_id: object = MISSING,
        task_id: object = MISSING,
        chars: object = MISSING,
        body_chars: object = MISSING,
        content_sha256: object = MISSING,
        task_key: object = MISSING,
        incarnation_id: object = MISSING,
        activation_id: object = MISSING,
        strike_epoch: object = MISSING,
    ) -> None:
        from .skill_usage_ledger import record_skill_usage_event

        params = dict(
            event=event,
            skill_id=skill_id,
            source=source,
            kind=kind,
            request_id=request_id,
            thread_id=thread_id,
            task_id=task_id,
            chars=chars,
            body_chars=body_chars,
            content_sha256=content_sha256,
            task_key=task_key,
            incarnation_id=incarnation_id,
            activation_id=activation_id,
            strike_epoch=strike_epoch,
        )
        record_skill_usage_event(self, **{key: value for key, value in params.items() if value is not MISSING})

    # LLM: 每个发布修订存不可变全文供回滚和在途快照；同编号异内容拒绝，暂不清理仍可能被引用的版本。调用方必须已持 state_lock，
    #   所以用不再取 per-path 锁的原子写，版本目录里不会留下 .lock 文件。副作用：写 versions/<name>/。
    # 函数用途: 保存一个已发布版本的 SKILL.md 全文。
    def save_version(self, name: str, version: int, text: str) -> None:
        directory = self.versions_dir / name
        path = directory / f"v{version}.md"
        existing = read_text_beneath(self.directory, ("versions", name, path.name))
        if existing is not None:
            if existing != text:
                raise SkillLearningStoreError("SKILL_LEARNING_VERSION_CONFLICT")
            return
        self.check_write(path, text)
        write_text_atomic_beneath(self.directory, ("versions", name, path.name), text)

    # LLM: 版本文件不存在（已被裁剪或从未保存）时返回 None，由回滚方给出结构化拒绝。
    # 函数用途: 读取某个版本的 SKILL.md 全文。
    def read_version(self, name: str, version: int) -> str | None:
        return read_text_beneath(self.directory, ("versions", name, f"v{version}.md"))

    # LLM: 始终返回在役路径（skills/<name>/SKILL.md 或 skills/learned/<name>/SKILL.md）；批量调用应传入已加载的 record 避免重读登记表。
    # 函数用途: 返回某个自学 Skill 的在役 SKILL.md 路径。
    def skill_path(self, name: str, *, record: LearnedSkill | None = None) -> Path:
        if record is None:
            record = self.load_registry().skills.get(name)
        if record and record.layout == "owner":
            return self.skills_root / name / "SKILL.md"
        return self.learned_root / name / "SKILL.md"

    # LLM: 返回归档目录内 SKILL.md 的规范路径；调用方不得把该路径与在役扫描目录混用。
    # 函数用途: 返回某个已归档自学 Skill 的 SKILL.md 规范路径。
    def archived_skill_path(self, name: str) -> Path:
        return self.archived_dir / name / "SKILL.md"

    # LLM: 按给定登记事实返回当前物理正文应驻留的规范 SKILL.md 路径（已归档则在 archived/，否则为在役路径）。
    # 函数用途: 根据布局与维护状态返回 Skill 正文规范路径。
    def canonical_skill_path(self, name: str, *, record: LearnedSkill) -> Path:
        if record.layout != "owner" and record.maintenance.get("state") == "archived":
            return self.archived_skill_path(name)
        return self.skill_path(name, record=record)

    # LLM: 外部/维护/重启恢复入口；在 state_lock 内推进任意未完结 pending_operation 及有界墓碑/版本目录清理。
    #   预算 0 解析为内置有界默认（128 项 / 8 MiB / 50 ms），绝不走无界扫描或忽略 cleanup 阶段。
    # 函数用途: 有界收敛中断的生命周期目录转换与后续清理。
    def recover_lifecycle(
        self,
        *,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
    ) -> dict[str, object]:
        with self.state_lock():
            registry = self.load_registry()
            _, outcome = self._recover_lifecycle_locked(
                registry,
                batch_count=batch_count,
                batch_bytes=batch_bytes,
                batch_ms=batch_ms,
            )
            return outcome

    # LLM: 调用方已持 state_lock；推进单条 pending_operation（prepared -> moved -> committed -> cleanup），
    #   用户修改一律安全退让并交出机器管辖，不覆盖用户文件；清理阶段按配额增量推进。
    # 函数用途: 在持有 state_lock 时有界恢复一条未完结的生命周期操作。
    def _recover_lifecycle_locked(
        self,
        registry: SkillLearningRegistry,
        *,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
        now: float | None = None,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        op = registry.pending_operation
        if op is None:
            return registry, {
                "status": "idle",
                "resumed": False,
                "completed": True,
                "actions": 0,
                "bytes_freed": 0,
                "remaining": False,
                "operation_id": "",
            }
        kind = str(op.get("kind") or "")
        op_id = str(op.get("operation_id") or "")
        skill_name = str(op.get("skill_name") or "")
        ts = time.time() if now is None else float(now)
        if op.get("phase") == "cleanup":
            return self._cleanup_operation_locked(
                registry,
                op,
                batch_count=batch_count,
                batch_bytes=batch_bytes,
                batch_ms=batch_ms,
            )
        if kind == "archive":
            return self._recover_archive_locked(registry, op, ts=ts)
        if kind == "restore":
            return self._recover_restore_locked(registry, op, ts=ts)
        if kind == "displace_publish":
            return self._recover_displace_publish_locked(registry, op, ts=ts)
        if kind == "evict":
            return self._recover_evict_locked(
                registry, op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
            )
        if kind == "remove":
            return self._recover_remove_locked(
                registry, op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
            )
        if kind == "rollback":
            return self._recover_rollback_locked(registry, op, ts=ts)
        raise SkillLearningStoreError(
            CODE_REGISTRY_CORRUPT,
            f"unsupported pending operation kind {kind}",
            detail={"operation_id": op_id, "skill_name": skill_name},
        )

    # LLM: 所有生命周期过渡入口先收敛遗留未完操作，保证任何时刻至多只有一条 pending_operation 推进。
    # 函数用途: 为新生命周期过渡准备干净且已收敛的登记表。
    def _prepare_clean_registry_locked(
        self,
        registry: SkillLearningRegistry,
        *,
        now: float | None = None,
    ) -> SkillLearningRegistry:
        current = registry
        while current.pending_operation is not None:
            current, outcome = self._recover_lifecycle_locked(current, now=now)
            if outcome.get("remaining") and outcome.get("actions", 0) == 0:
                raise SkillLearningStoreError(
                    CODE_EVIDENCE_INCOMPLETE,
                    f"pending lifecycle operation {current.pending_operation.get('operation_id')} blocked",
                    detail={"operation_id": current.pending_operation.get("operation_id")},
                )
        return current

    # LLM: 归档过渡：先写 pending_operation，再复核 hash 后移至 archived/，最后原子提交状态；支持进程中断前向收敛。
    # 函数用途: 在 state_lock 内安全归档一个机器管辖的在役 Skill。
    def _transition_archive_locked(
        self,
        registry: SkillLearningRegistry,
        name: str,
        *,
        now: float | None = None,
        reason_code: str = "idle_archive",
    ) -> tuple[SkillLearningRegistry, LearnedSkill]:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        current = registry.skills.get(name)
        if current is None or current.layout == "owner" or current.maintenance.get("state") == "archived":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {name} is not an active learned skill eligible for archive",
                detail={"skill_name": name},
            )
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_rel = self._encode_rel("skills", (LEARNED_DIR_NAME, name))
        dst_rel = self._encode_rel("store", ("archived", name))
        ver_rel = self._encode_rel("store", ("versions", name))
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "archive",
            "phase": "prepared",
            "skill_name": name,
            "expected_revision": current.revision,
            "expected_sha256": current.sha256,
            "source_rel": src_rel,
            "target_rel": dst_rel,
            "versions_rel": ver_rel,
            "created_at": ts,
            "reason_code": str(reason_code or "idle_archive"),
        }
        src_root, src_parts = self._resolve_op_rel(src_rel)
        dst_root, dst_parts = self._resolve_op_rel(dst_rel)
        status, observed_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        if status == "ok" and observed_sha != current.sha256:
            updated = replace(
                current,
                maintenance={**current.maintenance, "ownership": "user_modified"},
            )
            self.save_registry(replace(registry, skills={**registry.skills, name: updated}))
            raise SkillLearningStoreError(
                "SKILL_LEARNING_USER_MODIFIED",
                f"skill {name} modified by user before archival",
                detail={"skill_name": name, "state": "user_modified"},
            )
        if status != "ok":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {name} unreadable before archival",
                detail={"skill_name": name},
            )
        with ExitStack() as rollback:
            rollback.push(self._make_op_rollback(registry, op, undo_actions=[]))
            self.save_registry(replace(registry, pending_operation=op))
            undo_actions: list[Callable[[], None]] = []
            rollback.pop_all()
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            # 移动前一刻再核身份，拦截锁间隙的外部修改。
            status2, sha2 = self._dir_skill_sha256_nofollow(src_root, src_parts)
            if status2 == "ok" and sha2 != current.sha256:
                handoff_rec = replace(current, maintenance={**current.maintenance, "ownership": "user_modified"})
                self.save_registry(replace(registry, skills={**registry.skills, name: handoff_rec}, pending_operation=None))
                rollback.pop_all()
                raise SkillLearningStoreError(
                    "SKILL_LEARNING_USER_MODIFIED",
                    f"skill {name} modified by user immediately before archival rename",
                    detail={"skill_name": name, "state": "user_modified"},
                )
            if status2 != "ok":
                raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, f"skill {name} unavailable for archival")
            self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
            undo_actions.append(lambda: self._move_dir_nofollow(dst_root, dst_parts, src_root, src_parts))
            next_record = self._archived_record(current, op_id=op_id, ts=ts)
            committed = replace(
                registry,
                skills={**registry.skills, name: next_record},
                pending_operation=None,
            )
            self.save_registry(committed)
            rollback.pop_all()
        return committed, next_record

    # LLM: 恢复归档过渡：把 archived/<name> 安全迁回 learned/<name>，保留激活身份与打击纪元，重置空闲基线。
    # 函数用途: 在 state_lock 内把已归档的自学 Skill 恢复到在役目录。
    def _transition_restore_locked(
        self,
        registry: SkillLearningRegistry,
        name: str,
        *,
        now: float | None = None,
    ) -> tuple[SkillLearningRegistry, LearnedSkill]:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        current = registry.skills.get(name)
        if current is None or current.layout == "owner" or current.maintenance.get("state") != "archived":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {name} is not an archived learned skill",
                detail={"skill_name": name},
            )
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_rel = self._encode_rel("store", ("archived", name))
        dst_rel = self._encode_rel("skills", (LEARNED_DIR_NAME, name))
        ver_rel = self._encode_rel("store", ("versions", name))
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "restore",
            "phase": "prepared",
            "skill_name": name,
            "expected_revision": current.revision,
            "expected_sha256": current.sha256,
            "source_rel": src_rel,
            "target_rel": dst_rel,
            "versions_rel": ver_rel,
            "created_at": ts,
            "reason_code": "restore",
        }
        src_root, src_parts = self._resolve_op_rel(src_rel)
        dst_root, dst_parts = self._resolve_op_rel(dst_rel)
        status, observed_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        if status != "ok" or observed_sha != current.sha256:
            if status == "ok":
                handoff_rec = replace(current, maintenance={**current.maintenance, "ownership": "user_modified"})
                self.save_registry(replace(registry, skills={**registry.skills, name: handoff_rec}))
                raise SkillLearningStoreError(
                    "SKILL_LEARNING_USER_MODIFIED",
                    f"archived skill {name} hash mismatch",
                    detail={"skill_name": name, "state": "user_modified"},
                )
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"archived skill {name} missing or unreadable",
                detail={"skill_name": name},
            )
        if self._dir_exists_nofollow(dst_root, dst_parts):
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"restore destination already exists for {name}",
                detail={"skill_name": name},
            )
        with ExitStack() as rollback:
            undo_actions: list[Callable[[], None]] = []
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            self.save_registry(replace(registry, pending_operation=op))
            self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
            undo_actions.append(lambda: self._move_dir_nofollow(dst_root, dst_parts, src_root, src_parts))
            next_record = self._restored_record(current, op_id=op_id, ts=ts)
            committed = replace(
                registry,
                skills={**registry.skills, name: next_record},
                pending_operation=None,
            )
            self.save_registry(committed)
            rollback.pop_all()
        return committed, next_record

    # LLM: 单事务容量置换发布：同一配额/状态锁内把淘汰者在役目录归档并移入暂存候选，再提交双条目更新；
    #   受害者若在最后关头被用户改写，标记 user_modified 并安全终止而不碰用户正文。
    # 函数用途: 原子执行容量淘汰归档与新候选 Skill 发布。
    def _transition_displace_publish_locked(
        self,
        registry: SkillLearningRegistry,
        *,
        victim_name: str,
        candidate_record: LearnedSkill,
        candidate_text: str,
        candidate_assets: dict[str, dict[str, object]] | None = None,
        now: float | None = None,
    ) -> tuple[SkillLearningRegistry, LearnedSkill, LearnedSkill]:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        victim = registry.skills.get(victim_name)
        if victim is None or victim.layout == "owner" or victim.maintenance.get("state") == "archived":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"victim {victim_name} is not an active learned skill",
                detail={"skill_name": victim_name},
            )
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_rel = self._encode_rel("skills", (LEARNED_DIR_NAME, victim_name))
        dst_rel = self._encode_rel("store", ("archived", victim_name))
        stage_rel = self._encode_rel("store", ("tx_stage", op_id))
        ver_rel = self._encode_rel("store", ("versions", candidate_record.name))
        cand_live_path = self.learned_root / candidate_record.name / "SKILL.md"
        cand_ver_path = self.versions_dir / candidate_record.name / f"v{candidate_record.revision}.md"
        admission = self._quota_admission.get()
        if admission is None:
            raise RuntimeError("学习写入缺少原配额事务")
        cand_bytes = len(candidate_text.encode("utf-8"))
        admission.check([
            OwnerQuotaChange(cand_live_path, cand_bytes),
            OwnerQuotaChange(cand_ver_path, cand_bytes),
        ])
        if self._dir_exists_nofollow(self.skills_root, (LEARNED_DIR_NAME, candidate_record.name)):
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"candidate target directory already exists for {candidate_record.name}",
                detail={"skill_name": candidate_record.name},
            )
        src_root, src_parts = self._resolve_op_rel(src_rel)
        dst_root, dst_parts = self._resolve_op_rel(dst_rel)
        stage_root, stage_parts = self._resolve_op_rel(stage_rel)
        cand_parts = (LEARNED_DIR_NAME, candidate_record.name)
        status, observed_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        if status == "ok" and observed_sha != victim.sha256:
            handoff_rec = replace(victim, maintenance={**victim.maintenance, "ownership": "user_modified"})
            self.save_registry(replace(registry, skills={**registry.skills, victim_name: handoff_rec}))
            raise SkillLearningStoreError(
                "SKILL_LEARNING_VICTIM_USER_MODIFIED",
                f"victim {victim_name} modified by user before displacement",
                detail={"skill_name": victim_name, "state": "user_modified"},
            )
        if status != "ok":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"victim {victim_name} unreadable before displacement",
                detail={"skill_name": victim_name},
            )
        if self._dir_exists_nofollow(dst_root, dst_parts):
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"archived destination already exists for displacement victim {victim_name}",
                detail={"skill_name": victim_name},
            )
        final_candidate = replace(candidate_record, last_operation_id=op_id)
        victim_archived = self._archived_record(victim, op_id=op_id, ts=ts)
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "displace_publish",
            "phase": "prepared",
            "skill_name": victim.name,
            "expected_revision": victim.revision,
            "expected_sha256": victim.sha256,
            "source_rel": src_rel,
            "target_rel": dst_rel,
            "versions_rel": ver_rel,
            "candidate_rel": stage_rel,
            "candidate_name": final_candidate.name,
            "candidate_sha256": final_candidate.sha256,
            "candidate_revision": final_candidate.revision,
            "created_at": ts,
            "reason_code": "cap_displace",
        }
        with ExitStack() as rollback:
            undo_actions: list[Callable[[], None]] = []
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            write_text_atomic_beneath(self.directory, (*stage_parts, "SKILL.md"), candidate_text)
            undo_actions.append(lambda: self._purge_bounded_dir_beneath(
                stage_root, stage_parts, remaining_count=32, remaining_bytes=8_388_608,
                deadline=time.monotonic() + 2.0, allow_zero_start=True, prefix="abort",
            ))
            existed_ver = self.read_version(final_candidate.name, final_candidate.revision) is not None
            if not existed_ver:
                write_text_atomic_beneath(
                    self.directory,
                    ("versions", final_candidate.name, f"v{final_candidate.revision}.md"),
                    candidate_text,
                )
                undo_actions.append(lambda: unlink_file_beneath(
                    self.directory, ("versions", final_candidate.name, f"v{final_candidate.revision}.md")
                ))
            self.save_registry(replace(registry, pending_operation=op))
            # 移出受害者前一刻最后核对正文 hash。
            status2, sha2 = self._dir_skill_sha256_nofollow(src_root, src_parts)
            if status2 == "ok" and sha2 != victim.sha256:
                handoff_rec = replace(victim, maintenance={**victim.maintenance, "ownership": "user_modified"})
                next_reg = replace(registry, skills={**registry.skills, victim_name: handoff_rec}, pending_operation=None)
                self.save_registry(next_reg)
                # 清理暂存候选题与未提交版本文件。
                for fn in reversed(undo_actions):
                    fn()
                rollback.pop_all()
                raise SkillLearningStoreError(
                    "SKILL_LEARNING_VICTIM_USER_MODIFIED",
                    f"victim {victim_name} modified immediately before displacement rename",
                    detail={"skill_name": victim_name, "state": "user_modified"},
                )
            if status2 != "ok":
                raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, f"victim {victim_name} unavailable")
            self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
            undo_actions.append(lambda: self._move_dir_nofollow(dst_root, dst_parts, src_root, src_parts))
            self.save_registry(replace(registry, pending_operation={**op, "phase": "moved"}))
            self._move_dir_nofollow(stage_root, stage_parts, self.skills_root, cand_parts)
            undo_actions.append(lambda: self._move_dir_nofollow(self.skills_root, cand_parts, stage_root, stage_parts))
            committed = replace(
                registry,
                skills={
                    **registry.skills,
                    victim_name: victim_archived,
                    final_candidate.name: final_candidate,
                },
                assets=(registry.assets if candidate_assets is None else dict(candidate_assets)),
                pending_operation=None,
            )
            self.save_registry(committed)
            rollback.pop_all()
        self._try_rmdir_if_empty(self.directory, ("tx_stage",))
        return committed, victim_archived, final_candidate

    # LLM: 自动清理（过期淘汰或三次负反馈退场）：先核磁盘 hash 仍归机器所有，移至隔离墓碑再删行，最后有界清除 versions 与墓碑；
    #   闲置过期不写 blocked_names/family；三次负反馈退场必须同步封禁名字与任务家族指纹。
    # 函数用途: 在 state_lock 内可恢复地驱逐或退场一个机器管辖 Skill。
    def _transition_evict_locked(
        self,
        registry: SkillLearningRegistry,
        name: str,
        *,
        now: float | None = None,
        block: bool = False,
        block_family: str = "",
        reason_code: str = "idle_expired",
        cleanup_budget: tuple[int, int, int] = (0, 0, 0),
    ) -> SkillLearningRegistry:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        current = registry.skills.get(name)
        if current is None or current.layout == "owner":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {name} is not an eligible learned skill for eviction",
                detail={"skill_name": name},
            )
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_root, src_parts = self._skill_dir_location(name, record=current)
        src_rel = (
            self._encode_rel("store", src_parts)
            if src_root == self.directory
            else self._encode_rel("skills", src_parts)
        )
        tomb_name = f"{name}-{op_id[:12]}"
        tomb_rel = self._encode_rel("store", ("removed", tomb_name))
        ver_rel = self._encode_rel("store", ("versions", name))
        status, observed_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        extra_status = self._dir_extra_entries_status_nofollow(src_root, src_parts)
        if extra_status == "extra" or (status == "ok" and observed_sha != current.sha256):
            handoff = replace(current, maintenance={**current.maintenance, "ownership": "user_modified"})
            updated = replace(registry, skills={**registry.skills, name: handoff})
            self.save_registry(updated)
            raise SkillLearningStoreError(
                "SKILL_LEARNING_USER_MODIFIED",
                f"skill {name} modified by user before eviction",
                detail={"skill_name": name, "state": "user_modified"},
            )
        if status != "ok" or extra_status != "clean":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {name} unreadable before eviction",
                detail={"skill_name": name},
            )
        resolved_reason = "third_strike_retire" if block and reason_code in {"", "idle_expired"} else str(reason_code or "idle_expired")
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "evict",
            "phase": "prepared",
            "skill_name": name,
            "expected_revision": current.revision,
            "expected_sha256": current.sha256,
            "source_rel": src_rel,
            "target_rel": tomb_rel,
            "tombstone_rel": tomb_rel,
            "versions_rel": ver_rel,
            "created_at": ts,
            "reason_code": resolved_reason,
        }
        tomb_root, tomb_parts = self._resolve_op_rel(tomb_rel)
        family_to_block = block_family or str(current.maintenance.get("task_family_fingerprint") or "")
        with ExitStack() as rollback:
            undo_actions: list[Callable[[], None]] = []
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            self.save_registry(replace(registry, pending_operation=op))
            self._move_dir_nofollow(src_root, src_parts, tomb_root, tomb_parts)
            undo_actions.append(lambda: self._move_dir_nofollow(tomb_root, tomb_parts, src_root, src_parts))
            cleanup_op = {**op, "phase": "cleanup"}
            committed = self._apply_removal_commit(
                registry,
                current,
                pending_operation=cleanup_op,
                block_name=bool(block or resolved_reason == "third_strike_retire"),
                block_family=family_to_block if (block or resolved_reason == "third_strike_retire") else "",
            )
            self.save_registry(committed)
            rollback.pop_all()
        try:
            committed, _ = self._cleanup_operation_locked(
                committed,
                cleanup_op,
                batch_count=cleanup_budget[0],
                batch_bytes=cleanup_budget[1],
                batch_ms=cleanup_budget[2],
            )
        except OSError:
            pass
        return committed

    # LLM: 用户显式删除或回滚到 0 版（remove）：保留 removed/<name>-<stamp> 作为用户归档（不设 tombstone_rel），
    #   即使原目录已被用户手改或手动删除也允许完成登记移除，并封禁名字与已知家族，随后有界清理 versions/<name>。
    # 函数用途: 在 state_lock 内可恢复地执行用户删除操作。
    def _transition_remove_locked(
        self,
        registry: SkillLearningRegistry,
        name: str,
        *,
        now: float | None = None,
        reason_code: str = "user_removed",
        cleanup_budget: tuple[int, int, int] = (0, 0, 0),
    ) -> tuple[SkillLearningRegistry, SkillLearningEvent]:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        current = registry.skills.get(name)
        if current is None:
            raise SkillLearningStoreError("SKILL_LEARNING_NOT_LEARNED", f"skill {name} not learned")
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_root, src_parts = self._skill_dir_location(name, record=current)
        src_rel = (
            self._encode_rel("store", src_parts)
            if src_root == self.directory
            else self._encode_rel("skills", src_parts)
        )
        stamp = f"{int(ts * 1000):013d}-{op_id[:8]}"
        dst_name = f"{name}-{stamp}"
        dst_rel = self._encode_rel("store", ("removed", dst_name))
        ver_rel = self._encode_rel("store", ("versions", name))
        ver_exists = self._dir_exists_nofollow(self.directory, ("versions", name))
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "remove",
            "phase": "prepared",
            "skill_name": name,
            "expected_revision": current.revision,
            "expected_sha256": current.sha256,
            "source_rel": src_rel,
            "target_rel": dst_rel,
            "tombstone_rel": None,
            "versions_rel": ver_rel,
            "created_at": ts,
            "reason_code": str(reason_code or "user_removed"),
        }
        dst_root, dst_parts = self._resolve_op_rel(dst_rel)
        family = str(current.maintenance.get("task_family_fingerprint") or "")
        if not _SHA256_RE.fullmatch(family):
            text = read_text_beneath(src_root, (*src_parts, "SKILL.md"))
            if text is not None:
                family = skill_family_fingerprint(text)
            else:
                ver_text = self.read_version(name, current.version)
                family = skill_family_fingerprint(ver_text) if ver_text is not None else ""
        with ExitStack() as rollback:
            undo_actions: list[Callable[[], None]] = []
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            self.save_registry(replace(registry, pending_operation=op))
            if self._entry_exists_nofollow(src_root, src_parts):
                self._move_entry_to_removed_nofollow(src_root, src_parts, dst_root, dst_parts)
                undo_actions.append(lambda: self._move_entry_to_removed_nofollow(dst_root, dst_parts, src_root, src_parts))
            cleanup_op = {**op, "phase": "cleanup"} if ver_exists else None
            committed = self._apply_removal_commit(
                registry,
                current,
                pending_operation=cleanup_op,
                block_name=True,
                block_family=family,
            )
            self.save_registry(committed)
            rollback.pop_all()
        if cleanup_op is not None:
            try:
                committed, _ = self._cleanup_operation_locked(
                    committed,
                    cleanup_op,
                    batch_count=cleanup_budget[0],
                    batch_bytes=cleanup_budget[1],
                    batch_ms=cleanup_budget[2],
                )
            except OSError:
                pass
        return committed, SkillLearningEvent(EVENT_REMOVED, "", current.name, current.version, current.sha256)

    # LLM: 可恢复回滚：先保存 pending_operation，再写入新修订版本文件与替换 SKILL.md，最后提交登记状态；
    #   若调用 save_registry 前失败则未碰磁盘正文；若提交后异常则 readback 确认已提交绝不倒退版本。
    # 函数用途: 在 state_lock 内原子且可崩溃恢复地把 Skill 正文回滚到已证实历史版本。
    def _transition_rollback_locked(
        self,
        registry: SkillLearningRegistry,
        *,
        current: LearnedSkill,
        next_record: LearnedSkill,
        target_version: int,
        target_sha256: str,
        target_text: str,
        reason_code: str = "rollback",
        now: float | None = None,
    ) -> tuple[SkillLearningRegistry, LearnedSkill]:
        registry = self._prepare_clean_registry_locked(registry, now=now)
        ts = time.time() if now is None else float(now)
        op_id = uuid.uuid4().hex
        src_root, src_parts = self._skill_dir_location(current.name, record=current)
        if src_root != self.skills_root:
            raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, f"cannot rollback non-live skill {current.name}")
        live_rel = self._encode_rel("skills", src_parts)
        ver_rel = self._encode_rel("store", ("versions", current.name))
        cand_ver_rel = self._encode_rel("store", ("versions", current.name, f"v{target_version}.md"))
        live_path = self.skill_path(current.name, record=current)
        next_ver_path = self.versions_dir / current.name / f"v{next_record.revision}.md"
        admission = self._quota_admission.get()
        if admission is None:
            raise RuntimeError("学习写入缺少原配额事务")
        target_bytes = len(target_text.encode("utf-8"))
        changes = [OwnerQuotaChange(live_path, target_bytes)]
        existed_ver = self.read_version(current.name, next_record.revision) is not None
        if not existed_ver:
            changes.append(OwnerQuotaChange(next_ver_path, target_bytes))
        admission.check(changes)
        status, observed_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        if status == "ok" and observed_sha != current.sha256:
            handoff_rec = replace(current, maintenance={**current.maintenance, "ownership": "user_modified"})
            self.save_registry(replace(registry, skills={**registry.skills, current.name: handoff_rec}))
            raise SkillLearningStoreError(
                "SKILL_LEARNING_USER_MODIFIED",
                f"skill {current.name} modified by user before rollback",
                detail={"skill_name": current.name, "state": "user_modified"},
            )
        if status != "ok":
            raise SkillLearningStoreError(
                CODE_EVIDENCE_INCOMPLETE,
                f"skill {current.name} unreadable before rollback",
                detail={"skill_name": current.name},
            )
        previous_body = read_text_beneath(src_root, (*src_parts, "SKILL.md"))
        if previous_body is None:
            raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, f"skill {current.name} unreadable before rollback")
        final_record = replace(next_record, last_operation_id=op_id)
        op: dict[str, object] = {
            "operation_id": op_id,
            "kind": "rollback",
            "phase": "prepared",
            "skill_name": current.name,
            "expected_revision": current.revision,
            "expected_sha256": current.sha256,
            "source_rel": live_rel,
            "target_rel": live_rel,
            "versions_rel": ver_rel,
            "candidate_rel": cand_ver_rel,
            "candidate_name": current.name,
            "candidate_sha256": target_sha256,
            "candidate_revision": final_record.revision,
            "created_at": ts,
            "reason_code": str(reason_code or "rollback"),
        }
        with ExitStack() as rollback:
            undo_actions: list[Callable[[], None]] = []
            rollback.push(self._make_op_rollback(registry, op, undo_actions=undo_actions))
            self.save_registry(replace(registry, pending_operation=op))
            replace_skill_file(live_path, target_text)
            undo_actions.append(lambda: replace_skill_file(live_path, previous_body))
            if not existed_ver:
                self.save_version(current.name, final_record.revision, target_text)
                undo_actions.append(lambda: unlink_file_beneath(
                    self.directory, ("versions", current.name, f"v{final_record.revision}.md")
                ))
            else:
                self.save_version(current.name, final_record.revision, target_text)
            committed = replace(
                registry,
                skills={**registry.skills, current.name: final_record},
                pending_operation=None,
            )
            self.save_registry(committed)
            rollback.pop_all()
        return committed, final_record

    def _archived_record(self, current: LearnedSkill, *, op_id: str, ts: float) -> LearnedSkill:
        return replace(
            current,
            revision=max(current.revision, current.version) + 1,
            updated_at=utc_now(),
            last_operation_id=op_id,
            maintenance={**current.maintenance, "state": "archived", "archived_at": float(ts)},
        )

    def _restored_record(self, current: LearnedSkill, *, op_id: str, ts: float) -> LearnedSkill:
        maint = dict(current.maintenance)
        maint["state"] = "active" if maint.get("quality") == "established" else "trial"
        maint["restored_at"] = float(ts)
        maint.pop("archived_at", None)
        return replace(
            current,
            revision=max(current.revision, current.version) + 1,
            updated_at=utc_now(),
            last_operation_id=op_id,
            maintenance=maint,
        )

    def _apply_removal_commit(
        self,
        registry: SkillLearningRegistry,
        current: LearnedSkill,
        *,
        pending_operation: dict[str, object] | None,
        block_name: bool,
        block_family: str,
    ) -> SkillLearningRegistry:
        skills = {key: value for key, value in registry.skills.items() if key != current.name}
        skill_asset_ids = {f"owner:{current.name}", current.name}
        assets = {
            key: value
            for key, value in registry.assets.items()
            if not (isinstance(value, dict) and value.get("kind") == "skill" and value.get("asset_id") in skill_asset_ids)
        }
        blocked_names = tuple(dict.fromkeys([*registry.blocked_names, current.name])) if block_name else registry.blocked_names
        if block_family and _SHA256_RE.fullmatch(block_family):
            blocked_families = tuple(dict.fromkeys([*registry.blocked_task_families, block_family]))
        else:
            blocked_families = registry.blocked_task_families
        return replace(
            registry,
            skills=skills,
            assets=assets,
            blocked_names=blocked_names,
            blocked_task_families=blocked_families,
            pending_operation=pending_operation,
        )

    def _cleanup_operation_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        eff_count = int(batch_count) if batch_count > 0 else 128
        eff_bytes = int(batch_bytes) if batch_bytes > 0 else 8_388_608
        eff_ms = int(batch_ms) if batch_ms > 0 else 50
        deadline = time.monotonic() + eff_ms / 1000.0
        actions = 0
        freed = 0
        cursor = ""
        remaining = False
        try:
            ver_rel = str(op.get("versions_rel") or "")
            if ver_rel:
                root, parts = self._resolve_op_rel(ver_rel)
                done_dir, did, dbytes, cur = self._purge_bounded_dir_beneath(
                    root,
                    parts,
                    remaining_count=max(0, eff_count - actions),
                    remaining_bytes=max(0, eff_bytes - freed),
                    deadline=deadline,
                    allow_zero_start=(actions == 0),
                    prefix="versions",
                )
                actions += did
                freed += dbytes
                if not done_dir:
                    remaining = True
                    cursor = cur
            tomb_rel = op.get("tombstone_rel")
            if not remaining and tomb_rel:
                root, parts = self._resolve_op_rel(str(tomb_rel))
                done_dir, did, dbytes, cur = self._purge_bounded_dir_beneath(
                    root,
                    parts,
                    remaining_count=max(0, eff_count - actions),
                    remaining_bytes=max(0, eff_bytes - freed),
                    deadline=deadline,
                    allow_zero_start=(actions == 0),
                    prefix="tombstone",
                )
                actions += did
                freed += dbytes
                if not done_dir:
                    remaining = True
                    cursor = cur
        except OSError as exc:
            updated_op = {**op, "phase": "cleanup", "cleanup_cursor": cursor or str(op.get("cleanup_cursor") or "")}
            next_reg = replace(registry, pending_operation=updated_op)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "pending",
                "resumed": True,
                "completed": False,
                "kind": str(op.get("kind") or ""),
                "skill_name": str(op.get("skill_name") or ""),
                "operation_id": str(op.get("operation_id") or ""),
                "phase": "cleanup",
                "actions": actions,
                "bytes_freed": freed,
                "remaining": True,
                "error": type(exc).__name__,
            }
        if remaining:
            updated_op = {**op, "phase": "cleanup", "cleanup_cursor": cursor}
            next_reg = replace(registry, pending_operation=updated_op)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "pending",
                "resumed": True,
                "completed": False,
                "kind": str(op.get("kind") or ""),
                "skill_name": str(op.get("skill_name") or ""),
                "operation_id": str(op.get("operation_id") or ""),
                "phase": "cleanup",
                "actions": actions,
                "bytes_freed": freed,
                "remaining": True,
            }
        next_reg = replace(registry, pending_operation=None)
        self.save_registry(next_reg)
        return next_reg, {
            "status": "recovered",
            "resumed": True,
            "completed": True,
            "kind": str(op.get("kind") or ""),
            "skill_name": str(op.get("skill_name") or ""),
            "operation_id": str(op.get("operation_id") or ""),
            "phase": "done",
            "actions": actions,
            "bytes_freed": freed,
            "remaining": False,
        }

    def _handoff_corrupt_op_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        ownership: str,
        reason: str,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op.get("skill_name") or "")
        skills = dict(registry.skills)
        current = skills.get(name)
        if current is not None:
            skills[name] = replace(current, maintenance={**current.maintenance, "ownership": ownership})
        next_reg = replace(registry, skills=skills, pending_operation=None)
        self.save_registry(next_reg)
        return next_reg, {
            "status": "recovered",
            "resumed": True,
            "completed": True,
            "kind": str(op.get("kind") or ""),
            "skill_name": name,
            "operation_id": str(op.get("operation_id") or ""),
            "phase": "done",
            "actions": 1,
            "bytes_freed": 0,
            "remaining": False,
            "handoff": ownership,
            "reason": reason,
        }

    def _recover_archive_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        ts: float,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op["skill_name"])
        op_id = str(op["operation_id"])
        expected_sha = str(op["expected_sha256"])
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        dst_root, dst_parts = self._resolve_op_rel(str(op["target_rel"]))
        src_status, src_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        dst_status, dst_sha = self._dir_skill_sha256_nofollow(dst_root, dst_parts)
        current = registry.skills.get(name)
        if src_status == "ok" and src_sha == expected_sha and dst_status == "missing":
            self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
            src_status, dst_status, dst_sha = "missing", "ok", expected_sha
        if src_status == "missing" and dst_status == "ok" and dst_sha == expected_sha:
            if current is None:
                next_reg = replace(registry, pending_operation=None)
                self.save_registry(next_reg)
                return next_reg, {
                    "status": "recovered", "resumed": True, "completed": True, "kind": "archive",
                    "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
                }
            updated = current if current.maintenance.get("state") == "archived" else self._archived_record(current, op_id=op_id, ts=ts)
            next_reg = replace(registry, skills={**registry.skills, name: updated}, pending_operation=None)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "recovered", "resumed": True, "completed": True, "kind": "archive",
                "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
            }
        if src_status == "ok" and src_sha != expected_sha and dst_status == "missing":
            return self._handoff_corrupt_op_locked(registry, op, ownership="user_modified", reason="user_modified_before_archive")
        return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="archive_state_uncertain")

    def _recover_restore_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        ts: float,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op["skill_name"])
        op_id = str(op["operation_id"])
        expected_sha = str(op["expected_sha256"])
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        dst_root, dst_parts = self._resolve_op_rel(str(op["target_rel"]))
        src_status, src_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        dst_status, dst_sha = self._dir_skill_sha256_nofollow(dst_root, dst_parts)
        current = registry.skills.get(name)
        if src_status == "ok" and src_sha == expected_sha and dst_status == "missing":
            self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
            src_status, dst_status, dst_sha = "missing", "ok", expected_sha
        if src_status == "missing" and dst_status == "ok" and dst_sha == expected_sha:
            if current is None:
                next_reg = replace(registry, pending_operation=None)
                self.save_registry(next_reg)
                return next_reg, {
                    "status": "recovered", "resumed": True, "completed": True, "kind": "restore",
                    "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
                }
            updated = current if current.maintenance.get("state") != "archived" else self._restored_record(current, op_id=op_id, ts=ts)
            next_reg = replace(registry, skills={**registry.skills, name: updated}, pending_operation=None)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "recovered", "resumed": True, "completed": True, "kind": "restore",
                "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
            }
        if src_status == "ok" and src_sha != expected_sha:
            return self._handoff_corrupt_op_locked(registry, op, ownership="user_modified", reason="user_modified_in_archive")
        return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="restore_state_uncertain")

    def _recover_displace_publish_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        ts: float,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        victim_name = str(op["skill_name"])
        op_id = str(op["operation_id"])
        expected_sha = str(op["expected_sha256"])
        cand_name = str(op.get("candidate_name") or "")
        cand_sha = str(op.get("candidate_sha256") or "")
        cand_rev = int(op.get("candidate_revision") or 1)
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        dst_root, dst_parts = self._resolve_op_rel(str(op["target_rel"]))
        stage_rel = str(op.get("candidate_rel") or "")
        stage_root, stage_parts = self._resolve_op_rel(stage_rel)
        cand_parts = (LEARNED_DIR_NAME, cand_name)
        vic_live_status, vic_live_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        vic_arch_status, vic_arch_sha = self._dir_skill_sha256_nofollow(dst_root, dst_parts)
        stage_status, stage_sha = self._dir_skill_sha256_nofollow(stage_root, stage_parts)
        cand_live_status, cand_live_sha = self._dir_skill_sha256_nofollow(self.skills_root, cand_parts)

        # 若受害者仍在原位且已被用户修改，立即退让并清理未发布的暂存候选。
        if vic_live_status == "ok" and vic_live_sha != expected_sha and vic_arch_status == "missing":
            self._cleanup_aborted_candidate(stage_root, stage_parts, cand_name, cand_rev, registry)
            return self._handoff_corrupt_op_locked(registry, op, ownership="user_modified", reason="victim_user_modified")

        # 推进受害者移动（若尚未移出）。
        if vic_live_status == "ok" and vic_live_sha == expected_sha and vic_arch_status == "missing":
            if (stage_status == "ok" and stage_sha == cand_sha) or (cand_live_status == "ok" and cand_live_sha == cand_sha):
                self._move_dir_nofollow(src_root, src_parts, dst_root, dst_parts)
                vic_live_status, vic_arch_status, vic_arch_sha = "missing", "ok", expected_sha
            else:
                self._cleanup_aborted_candidate(stage_root, stage_parts, cand_name, cand_rev, registry)
                next_reg = replace(registry, pending_operation=None)
                self.save_registry(next_reg)
                return next_reg, {
                    "status": "recovered", "resumed": True, "completed": True, "kind": "displace_publish",
                    "skill_name": victim_name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
                }

        # 受害者已归档：推进候选移入并提交，或回退受害者。
        if vic_live_status == "missing" and vic_arch_status == "ok" and vic_arch_sha == expected_sha:
            if cand_live_status == "missing" and stage_status == "ok" and stage_sha == cand_sha:
                self._move_dir_nofollow(stage_root, stage_parts, self.skills_root, cand_parts)
                cand_live_status, cand_live_sha = "ok", cand_sha
            if cand_live_status == "ok" and cand_live_sha == cand_sha:
                cand_text = _read_managed_skill_text_strict(self.skills_root, (*cand_parts, "SKILL.md"), skill_name=cand_name)
                if self.read_version(cand_name, cand_rev) is None:
                    self.save_version(cand_name, cand_rev, cand_text)
                skills = dict(registry.skills)
                victim = skills.get(victim_name)
                if victim is not None:
                    skills[victim_name] = self._archived_record(victim, op_id=op_id, ts=ts)
                if cand_name not in skills:
                    now_iso = utc_now()
                    skills[cand_name] = LearnedSkill(
                        cand_name,
                        cand_rev,
                        cand_sha,
                        now_iso,
                        now_iso,
                        (),
                        cand_rev,
                        op_id,
                        {
                            "state": "trial",
                            "quality": "trial",
                            "quality_basis": "trial_unproven",
                            "pinned": False,
                            "baseline_at": float(op.get("created_at") or ts),
                            "independent_uses": 0,
                            "independent_uses_coverage": "exact",
                            "uses_since_hurt": 0,
                            "hurt_count": 0,
                            "strike_epoch": 0,
                            "incarnation_id": uuid.uuid4().hex,
                            "activation_id": uuid.uuid4().hex,
                            "epoch_started_at": float(op.get("created_at") or ts),
                            "usage_receipt_sequence": 0,
                            "ownership": "machine",
                            "observations": [],
                            "task_family_fingerprint": skill_family_fingerprint(cand_text),
                        },
                        "learned",
                    )
                next_reg = replace(registry, skills=skills, pending_operation=None)
                self.save_registry(next_reg)
                self._try_rmdir_if_empty(self.directory, ("tx_stage",))
                return next_reg, {
                    "status": "recovered", "resumed": True, "completed": True, "kind": "displace_publish",
                    "skill_name": victim_name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
                }
            # 候选未到位且暂存失效：安全回退受害者到原在役目录。
            if cand_live_status == "missing" and not self._dir_exists_nofollow(src_root, src_parts):
                self._move_dir_nofollow(dst_root, dst_parts, src_root, src_parts)
                self._cleanup_aborted_candidate(stage_root, stage_parts, cand_name, cand_rev, registry)
                next_reg = replace(registry, pending_operation=None)
                self.save_registry(next_reg)
                return next_reg, {
                    "status": "recovered", "resumed": True, "completed": True, "kind": "displace_publish",
                    "skill_name": victim_name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
                }
        return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="displace_state_uncertain")

    def _cleanup_aborted_candidate(
        self,
        stage_root: Path,
        stage_parts: tuple[str, ...],
        cand_name: str,
        cand_rev: int,
        registry: SkillLearningRegistry,
    ) -> None:
        self._purge_bounded_dir_beneath(
            stage_root, stage_parts, remaining_count=32, remaining_bytes=8_388_608,
            deadline=time.monotonic() + 2.0, allow_zero_start=True, prefix="abort",
        )
        self._try_rmdir_if_empty(self.directory, ("tx_stage",))
        if cand_name and cand_name not in registry.skills:
            unlink_file_beneath(self.directory, ("versions", cand_name, f"v{cand_rev}.md"))
            self._try_rmdir_if_empty(self.directory, ("versions", cand_name))

    def _recover_evict_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op["skill_name"])
        expected_sha = str(op["expected_sha256"])
        current = registry.skills.get(name)
        if current is None:
            return self._cleanup_operation_locked(
                registry, op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
            )
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        tomb_root, tomb_parts = self._resolve_op_rel(str(op.get("tombstone_rel") or op["target_rel"]))
        src_status, src_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        src_extra = self._dir_extra_entries_status_nofollow(src_root, src_parts)
        tomb_status, tomb_sha = self._dir_skill_sha256_nofollow(tomb_root, tomb_parts)
        tomb_extra = self._dir_extra_entries_status_nofollow(tomb_root, tomb_parts)
        if (src_extra == "extra" or (src_status == "ok" and src_sha != expected_sha)) and (
            tomb_status == "missing" and tomb_extra == "missing"
        ):
            return self._handoff_corrupt_op_locked(registry, op, ownership="user_modified", reason="user_modified_before_evict")
        if src_status == "ok" and src_sha == expected_sha and src_extra == "clean" and tomb_status == "missing" and tomb_extra == "missing":
            self._move_dir_nofollow(src_root, src_parts, tomb_root, tomb_parts)
            src_status, src_extra = "missing", "missing"
            tomb_status, tomb_sha, tomb_extra = "ok", expected_sha, "clean"
        if tomb_extra == "extra" and src_status == "missing" and not self._entry_exists_nofollow(src_root, src_parts):
            self._move_dir_nofollow(tomb_root, tomb_parts, src_root, src_parts)
            return self._handoff_corrupt_op_locked(
                registry, op, ownership="user_modified", reason="user_extra_files_before_evict"
            )
        if src_status == "missing" and tomb_status == "ok" and tomb_sha == expected_sha and tomb_extra == "clean":
            should_block = op.get("reason_code") == "third_strike_retire"
            family = str(current.maintenance.get("task_family_fingerprint") or "")
            if not _SHA256_RE.fullmatch(family):
                text = read_text_beneath(tomb_root, (*tomb_parts, "SKILL.md"))
                family = skill_family_fingerprint(text) if text is not None else ""
            cleanup_op = {**op, "phase": "cleanup"}
            committed = self._apply_removal_commit(
                registry,
                current,
                pending_operation=cleanup_op,
                block_name=should_block,
                block_family=family if should_block else "",
            )
            self.save_registry(committed)
            return self._cleanup_operation_locked(
                committed, cleanup_op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
            )
        return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="evict_state_uncertain")

    def _recover_remove_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op["skill_name"])
        current = registry.skills.get(name)
        if current is None:
            return self._cleanup_operation_locked(
                registry, op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
            )
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        dst_root, dst_parts = self._resolve_op_rel(str(op["target_rel"]))
        if self._entry_exists_nofollow(src_root, src_parts) and not self._entry_exists_nofollow(dst_root, dst_parts):
            self._move_entry_to_removed_nofollow(src_root, src_parts, dst_root, dst_parts)
        family = str(current.maintenance.get("task_family_fingerprint") or "")
        if not _SHA256_RE.fullmatch(family):
            text = read_text_beneath(dst_root, (*dst_parts, "SKILL.md"))
            if text is None:
                text = self.read_version(name, current.version)
            family = skill_family_fingerprint(text) if text is not None else ""
        ver_exists = self._dir_exists_nofollow(self.directory, ("versions", name))
        cleanup_op = {**op, "phase": "cleanup"} if ver_exists else None
        committed = self._apply_removal_commit(
            registry,
            current,
            pending_operation=cleanup_op,
            block_name=True,
            block_family=family,
        )
        self.save_registry(committed)
        if cleanup_op is None:
            return committed, {
                "status": "recovered", "resumed": True, "completed": True, "kind": "remove",
                "skill_name": name, "operation_id": str(op.get("operation_id") or ""), "phase": "done",
                "actions": 1, "bytes_freed": 0, "remaining": False,
            }
        return self._cleanup_operation_locked(
            committed, cleanup_op, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms
        )

    def _recover_rollback_locked(
        self,
        registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        ts: float,
    ) -> tuple[SkillLearningRegistry, dict[str, object]]:
        name = str(op["skill_name"])
        op_id = str(op["operation_id"])
        expected_sha = str(op["expected_sha256"])
        cand_sha = str(op.get("candidate_sha256") or "")
        cand_rev = int(op.get("candidate_revision") or (int(op["expected_revision"]) + 1))
        src_root, src_parts = self._resolve_op_rel(str(op["source_rel"]))
        ver_file_root, ver_file_parts = self._resolve_op_rel(str(op.get("candidate_rel") or ""))
        current = registry.skills.get(name)
        if current is None:
            next_reg = replace(registry, pending_operation=None)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "recovered", "resumed": True, "completed": True, "kind": "rollback",
                "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
            }
        ver_text = read_text_beneath(ver_file_root, ver_file_parts)
        if ver_text is None or not cand_sha or skill_content_sha256(ver_text) != cand_sha:
            return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="rollback_version_missing")
        target_ver = _version_name_number(ver_file_parts[-1]) or current.version
        live_status, live_sha = self._dir_skill_sha256_nofollow(src_root, src_parts)
        if live_status == "ok" and live_sha == expected_sha:
            replace_skill_file(self.skill_path(name, record=current), ver_text)
            live_status, live_sha = "ok", cand_sha
        if live_status == "ok" and live_sha == cand_sha:
            if self.read_version(name, cand_rev) is None:
                self.save_version(name, cand_rev, ver_text)
            if current.last_operation_id != op_id:
                from .skill_learning_publish import _historical_adoptions

                maint = dict(current.maintenance)
                hist_adoptions = _historical_adoptions(current)
                stable = maint.get("previous_stable")
                if is_proven_stable_version(stable) and stable["version"] == target_ver and stable["sha256"] == cand_sha:
                    est_at = float(stable["established_at"])
                    maint.update({
                        "state": "active",
                        "quality": "established",
                        "quality_basis": "rollback_established",
                        "established_at": est_at,
                        "helped_count": 0,
                        "hurt_count": 0,
                        "independent_uses": 0,
                        "independent_uses_coverage": "exact",
                        "uses_since_hurt": 0,
                        "strike_epoch": 0,
                        "activation_id": uuid.uuid4().hex,
                        "epoch_started_at": ts,
                        "historical_adoptions": hist_adoptions,
                        "observations": [],
                        "rolled_back_from": current.sha256,
                    })
                    maint.pop("previous_stable", None)
                    maint.pop("previous_verification", None)
                else:
                    maint.update({
                        "state": "active",
                        "quality": "trial",
                        "quality_basis": "manual_revert",
                        "helped_count": 0,
                        "hurt_count": 0,
                        "independent_uses": 0,
                        "independent_uses_coverage": "exact",
                        "uses_since_hurt": 0,
                        "strike_epoch": 0,
                        "activation_id": uuid.uuid4().hex,
                        "epoch_started_at": ts,
                        "historical_adoptions": hist_adoptions,
                        "observations": [],
                        "rolled_back_from": current.sha256,
                    })
                    maint.pop("established_at", None)
                maint["task_family_fingerprint"] = skill_family_fingerprint(ver_text)
                current = replace(
                    current,
                    version=target_ver,
                    revision=cand_rev,
                    sha256=cand_sha,
                    updated_at=utc_now(),
                    last_operation_id=op_id,
                    maintenance=maint,
                )
            next_reg = replace(registry, skills={**registry.skills, name: current}, pending_operation=None)
            self.save_registry(next_reg)
            return next_reg, {
                "status": "recovered", "resumed": True, "completed": True, "kind": "rollback",
                "skill_name": name, "operation_id": op_id, "phase": "done", "actions": 1, "bytes_freed": 0, "remaining": False,
            }
        if live_status == "ok":
            return self._handoff_corrupt_op_locked(registry, op, ownership="user_modified", reason="user_modified_during_rollback")
        return self._handoff_corrupt_op_locked(registry, op, ownership="unknown", reason="rollback_state_uncertain")

    # LLM: 生命周期调用方在state_lock内注册回调；正文已发布但同步失败时保留未决意图供恢复推进。
    # 函数用途: 为尚未发布的生命周期操作创建补偿回调。
    def _make_op_rollback(
        self,
        before_registry: SkillLearningRegistry,
        op: dict[str, object],
        *,
        undo_actions: list[Callable[[], None]],
    ):
        # LLM: 已发布错误不执行撤销或清除登记，未发布错误沿原读回与补偿路径处理。
        # 函数用途: 按文件发布结果保留恢复标记或撤销未提交操作。
        def _callback(exc_type, primary, traceback):
            if getattr(primary, "_skill_lifecycle_published", False):
                return False
            if exc_type is None:
                return False
            try:
                raw_text = read_text_beneath(self.directory, ("registry.json",))
                raw_doc = json.loads(raw_text) if raw_text is not None else {}
                if _is_op_committed_in_raw(raw_doc, op):
                    if primary is not None:
                        primary.add_note("lifecycle_commit_preserved_on_post_save_error")
                    return False
                for undo in reversed(undo_actions):
                    undo()
                if isinstance(raw_doc, dict):
                    raw_pending = raw_doc.get("pending_operation")
                    if isinstance(raw_pending, dict) and raw_pending.get("operation_id") == op["operation_id"]:
                        write_json_file_atomic_unlocked(self.registry_path, before_registry.to_record())
            except BaseException as rollback_error:
                if primary is None:
                    raise
                primary.add_note("lifecycle_rollback_failed:" + type(rollback_error).__name__)
            return False

        return _callback

    def _skill_dir_location(self, name: str, *, record: LearnedSkill | None) -> tuple[Path, tuple[str, ...]]:
        if record and record.layout == "owner":
            return self.skills_root, (name,)
        if record and record.maintenance.get("state") == "archived":
            return self.directory, ("archived", name)
        return self.skills_root, (LEARNED_DIR_NAME, name)

    def _store_rel_prefix_parts(self) -> tuple[str, ...]:
        owner_norm = Path(os.path.abspath(os.path.normpath(str(self.skills_root.parent.expanduser()))))
        store_norm = Path(os.path.abspath(os.path.normpath(str(self.directory.expanduser()))))
        try:
            parts = store_norm.relative_to(owner_norm).parts
            return parts if parts else ("__store__",)
        except ValueError:
            return ("__store__",)

    def _encode_rel(self, root_kind: str, subparts: tuple[str, ...] | list[str]) -> str:
        parts = tuple(subparts)
        _validate_relative_parts(parts)
        if root_kind == "skills":
            return "/".join((self.skills_root.name, *parts))
        return "/".join((*self._store_rel_prefix_parts(), *parts))

    def _resolve_op_rel(self, rel: str) -> tuple[Path, tuple[str, ...]]:
        parts = tuple(rel.split("/")) if rel else ()
        try:
            _validate_relative_parts(parts)
        except ValueError as exc:
            raise SkillLearningStoreError(CODE_REGISTRY_CORRUPT, f"invalid lifecycle path: {rel}") from exc
        if parts[0] == self.skills_root.name and len(parts) > 1:
            return self.skills_root, parts[1:]
        prefix = self._store_rel_prefix_parts()
        if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
            return self.directory, parts[len(prefix):]
        raise SkillLearningStoreError(CODE_REGISTRY_CORRUPT, f"lifecycle path outside owner root: {rel}")

    def _dir_exists_nofollow(self, root: Path, parts: tuple[str, ...]) -> bool:
        try:
            parent_fd = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        except FileNotFoundError:
            return False
        except NoFollowPathError:
            return True
        try:
            st = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            return stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
        except FileNotFoundError:
            return False
        finally:
            os.close(parent_fd)

    def _entry_exists_nofollow(self, root: Path, parts: tuple[str, ...]) -> bool:
        try:
            parent_fd = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        except FileNotFoundError:
            return False
        except NoFollowPathError:
            return True
        try:
            os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(parent_fd)

    def _dir_skill_sha256_nofollow(self, root: Path, dir_parts: tuple[str, ...]) -> tuple[str, str]:
        try:
            parent_fd = open_directory_beneath(root, dir_parts[:-1], require_dir_fd=True)
        except FileNotFoundError:
            return "missing", ""
        except OSError as exc:
            return "uncertain", type(exc).__name__
        try:
            try:
                st = os.stat(dir_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return "missing", ""
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return "uncertain", "not_regular_dir"
        finally:
            os.close(parent_fd)
        try:
            text = _read_managed_skill_text_strict(root, (*dir_parts, "SKILL.md"), skill_name=dir_parts[-1])
        except SkillLearningStoreError as exc:
            return "uncertain", str(exc)
        return "ok", skill_content_sha256(text)

    def _dir_extra_entries_status_nofollow(self, root: Path, dir_parts: tuple[str, ...]) -> str:
        """检查托管技能目录是否存在 SKILL.md 之外的额外文件或子目录。"""
        try:
            dir_fd = open_directory_beneath(root, dir_parts, require_dir_fd=True)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "uncertain"
        try:
            names = os.listdir(dir_fd)
        except OSError:
            return "uncertain"
        finally:
            os.close(dir_fd)
        return "extra" if any(name != "SKILL.md" for name in names) else "clean"

    def _move_dir_nofollow(
        self,
        src_root: Path,
        src_parts: tuple[str, ...],
        dst_root: Path,
        dst_parts: tuple[str, ...],
    ) -> None:
        src_parent_fd = open_directory_beneath(src_root, src_parts[:-1], require_dir_fd=True)
        try:
            src_st = os.stat(src_parts[-1], dir_fd=src_parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(src_st.st_mode) or not stat.S_ISDIR(src_st.st_mode):
                raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, "managed skill directory is not regular")
            dst_parent_fd = open_directory_beneath(dst_root, dst_parts[:-1], create=True, require_dir_fd=True)
            try:
                try:
                    os.stat(dst_parts[-1], dir_fd=dst_parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, f"destination already exists: {'/'.join(dst_parts)}")
                _rename_entry_with_exdev_fallback(
                    src_parent_fd,
                    src_parts[-1],
                    dst_parent_fd,
                    dst_parts[-1],
                    src_root=src_root,
                    src_parts=src_parts,
                    dst_root=dst_root,
                    dst_parts=dst_parts,
                )
            finally:
                os.close(dst_parent_fd)
        finally:
            os.close(src_parent_fd)

    def _move_entry_to_removed_nofollow(
        self,
        src_root: Path,
        src_parts: tuple[str, ...],
        dst_root: Path,
        dst_parts: tuple[str, ...],
    ) -> None:
        src_parent_fd = open_directory_beneath(src_root, src_parts[:-1], require_dir_fd=True)
        try:
            os.stat(src_parts[-1], dir_fd=src_parent_fd, follow_symlinks=False)
            dst_parent_fd = open_directory_beneath(dst_root, dst_parts[:-1], create=True, require_dir_fd=True)
            try:
                _rename_entry_with_exdev_fallback(
                    src_parent_fd,
                    src_parts[-1],
                    dst_parent_fd,
                    dst_parts[-1],
                    src_root=src_root,
                    src_parts=src_parts,
                    dst_root=dst_root,
                    dst_parts=dst_parts,
                )
            finally:
                os.close(dst_parent_fd)
        finally:
            os.close(src_parent_fd)

    def _purge_bounded_dir_beneath(
        self,
        root: Path,
        rel_parts: tuple[str, ...],
        *,
        remaining_count: int,
        remaining_bytes: int,
        deadline: float,
        allow_zero_start: bool,
        prefix: str,
    ) -> tuple[bool, int, int, str]:
        stack: list[tuple[str, ...]] = [rel_parts]
        done = 0
        freed = 0
        while stack:
            current_parts = stack[-1]
            try:
                dir_fd = open_directory_beneath(root, current_parts, require_dir_fd=True)
            except FileNotFoundError:
                stack.pop()
                continue
            child_subdir: tuple[str, ...] | None = None
            hit_limit = False
            cursor_name = ""
            try:
                entries = sorted(os.scandir(dir_fd), key=lambda item: item.name)
                for entry in entries:
                    st = os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                        child_subdir = (*current_parts, entry.name)
                        break
                    file_size = 0 if stat.S_ISLNK(st.st_mode) else max(0, int(st.st_size))
                    can_step = (done < remaining_count) and (
                        (done == 0 and allow_zero_start)
                        or (freed + file_size <= remaining_bytes and time.monotonic() < deadline)
                    )
                    if not can_step:
                        hit_limit = True
                        cursor_name = entry.name
                        break
                    os.unlink(entry.name, dir_fd=dir_fd)
                    done += 1
                    freed += file_size
                else:
                    os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            if child_subdir is not None:
                stack.append(child_subdir)
                continue
            if hit_limit:
                return False, done, freed, f"{prefix}:{cursor_name}"
            try:
                parent_fd = open_directory_beneath(root, current_parts[:-1], require_dir_fd=True)
            except FileNotFoundError:
                stack.pop()
                continue
            try:
                os.rmdir(current_parts[-1], dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
            finally:
                os.close(parent_fd)
            stack.pop()
        return True, done, freed, ""

    def _try_rmdir_if_empty(self, root: Path, parts: tuple[str, ...]) -> None:
        try:
            parent_fd = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        except OSError:
            return
        try:
            os.rmdir(parts[-1], dir_fd=parent_fd)
        except OSError:
            pass
        finally:
            os.close(parent_fd)


# LLM: owner由Store路径隔离，root/task优先，旧请求才退thread/run；成对轨迹与谱系检查由_mergeable_requests承担。
# 函数用途: 计算规范请求上下文，不让不同task因共享thread而撞名。
def _request_context(request):
    for key in ('root_task_id', 'task_id', 'thread_id', 'run_id'):
        value = str(request.get(key) or '').strip()
        if value:
            return ('task_id' if key == 'root_task_id' else key), value
    return ()


# LLM: 用户总结与自动总结不互吞；显式不同task先拒绝，共同root或谱系不能覆盖来源和子任务身份。
# 函数用途: 统一入队与崩溃恢复的安全替代规则，保留独立任务材料。
def _mergeable_requests(left, right):
    if left.get('completion_ref') or right.get('completion_ref'):
        return bool(left.get('completion_identity') and left.get('completion_identity') == right.get('completion_identity')
                    and left.get('trajectory_digest') == right.get('trajectory_digest')
                    and left.get('trajectory_fingerprint') == right.get('trajectory_fingerprint'))
    if (left.get('requested_by') or 'auto') != (right.get('requested_by') or 'auto'):
        return False
    if left.get('task_id') and right.get('task_id') and left['task_id'] != right['task_id']:
        return False
    contexts = _request_context(left), _request_context(right)
    common = [key for key in ('root_task_id', 'task_id', 'thread_id', 'run_id') if left.get(key) and right.get(key)]
    if all(context and context[0] == 'task_id' for context in contexts):
        matches = contexts[0] == contexts[1]
    else:
        matches = bool(common and left[common[0]] == right[common[0]])
    if not matches:
        return False
    for newer, older in ((left, right), (right, left)):
        refs = newer.get('merge_from_request_ids')
        if isinstance(refs, list) and older.get('request_key') in refs:
            return True
    first, second = left.get('trajectory_fingerprint'), right.get('trajectory_fingerprint')
    if any((left.get(key) or right.get(key)) and left.get(key) != right.get(key) for key in ('trajectory_digest', 'trajectory_fingerprint')):
        return False
    if first and second:
        return first == second
    runs = left.get('run_id'), right.get('run_id')
    return not all(runs) or runs[0] == runs[1]


# LLM: request_key 只由运行身份决定，同一次运行重复收口不会重复入队。
# 函数用途: 计算学习请求的稳定键（16 位十六进制）。
def request_key(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


# LLM: 统一 UTC ISO 时间，只作展示与排序。
# 函数用途: 返回当前 UTC 时间字符串。
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: 严格兼容解析 v1 登记表与新增有界状态字段（游标/待完操作/家族封禁/代次），损坏不得当空表覆盖。
# 函数用途: 从 registry.json 内容恢复登记表。
def _registry_from(payload: object) -> SkillLearningRegistry:
    if not isinstance(payload, dict) or payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported skill learning registry schema")
    raw_skills = payload.get("skills") or {}
    if not isinstance(raw_skills, dict):
        raise ValueError("registry skills must be an object")
    skills = {str(key): LearnedSkill.from_record(value) for key, value in raw_skills.items()}
    if any(key != item.name for key, item in skills.items()):
        raise ValueError("registry skill key does not match its name")
    blocked = payload.get("blocked_names") or []
    daily = payload.get("daily") or {}
    if not isinstance(blocked, list) or not all(isinstance(item, str) for item in blocked) or not isinstance(daily, dict):
        raise ValueError("registry blocked_names/daily have invalid shape")
    calls = daily.get("calls") or 0
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        raise ValueError("registry daily calls must be a non-negative integer")
    daily_date = daily.get("date")
    if daily_date is None:
        daily_date = ""
    elif not isinstance(daily_date, str):
        raise ValueError("registry daily date must be a string")
    history = _registry_daily_history(payload.get("daily_history", {}), daily_date, calls)
    assets = payload.get("assets", {})
    clock = payload.get("last_maintenance_at", 0)
    if (
        not isinstance(assets, dict)
        or isinstance(clock, bool)
        or not isinstance(clock, (int, float))
        or not math.isfinite(clock)
        or clock < 0
    ):
        raise ValueError("registry assets/maintenance invalid")
    usage_watermark = payload.get("usage_watermark", 0)
    if isinstance(usage_watermark, bool) or not isinstance(usage_watermark, int) or usage_watermark < 0:
        raise ValueError("registry usage_watermark must be a non-negative integer")
    cursor = _validate_maintenance_cursor(payload.get("maintenance_cursor"))
    pending_op = _validate_lifecycle_operation(payload.get("pending_operation"))
    raw_families = payload.get("blocked_task_families") or []
    if not isinstance(raw_families, list) or not all(
        isinstance(item, str) and _SHA256_RE.fullmatch(item) for item in raw_families
    ):
        raise ValueError("registry blocked_task_families has invalid shape")
    catalog_generation = payload.get("catalog_generation", "")
    if not isinstance(catalog_generation, str):
        raise ValueError("registry catalog_generation must be a string")
    return SkillLearningRegistry(
        skills=skills,
        blocked_names=tuple(dict.fromkeys(blocked)),
        daily_date=daily_date,
        daily_calls=calls,
        import_operations=dict(payload.get("import_operations") or {}),
        daily_history=history,
        assets=assets,
        last_maintenance_at=clock,
        usage_watermark=usage_watermark,
        maintenance_cursor=cursor,
        pending_operation=pending_op,
        blocked_task_families=tuple(dict.fromkeys(raw_families)),
        catalog_generation=catalog_generation,
    )


def _is_nonneg_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_nonneg_num(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


# LLM: 校验历史稳定指针（StableVersion）；保留缺省 established_at 的历史可读性，绝不虚构旧证明时间。
# 函数用途: 校验并规范化 previous_stable 字段。
def _validate_stable_version(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("stable version must be an object")
    keys = set(raw.keys())
    if not ({"version", "revision", "sha256"} <= keys <= _STABLE_VERSION_KEYS):
        raise ValueError("stable version has missing or unsupported keys")
    ver = raw["version"]
    rev = raw["revision"]
    sha = raw["sha256"]
    if not _is_nonneg_int(ver) or ver < 1 or not _is_nonneg_int(rev) or rev < ver:
        raise ValueError("stable version/revision invalid")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        raise ValueError("stable version sha256 invalid")
    out: dict[str, object] = {"version": ver, "revision": rev, "sha256": sha}
    if "established_at" in raw:
        est = raw["established_at"]
        if not _is_nonneg_num(est):
            raise ValueError("stable version established_at invalid")
        out["established_at"] = est
    return out


# LLM: 只有含正数 established_at 的历史稳定指针才是已证实可回滚目标，未证实试用版绝不充当回滚锚点。
# 函数用途: 判断回滚目标指针是否具备已证实稳定证明。
def is_proven_stable_version(raw: object) -> bool:
    try:
        cleaned = _validate_stable_version(raw)
    except ValueError:
        return False
    est = cleaned.get("established_at")
    return isinstance(est, (int, float)) and not isinstance(est, bool) and float(est) > 0


# LLM: 校验 Maintenance 已知字段类型（允许 unrelated/legacy 扩展字段保留，不抛弃原有数据，legacy hurt>3 保持可读）。
# 函数用途: 校验登记记录内的 maintenance 对象。
def _validate_maintenance(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("maintenance must be an object")
    data = dict(raw)
    enum_checks = (
        ("state", _VALID_MAINT_STATES),
        ("quality", _VALID_MAINT_QUALITIES),
        ("independent_uses_coverage", _VALID_USAGE_COVERAGES),
        ("ownership", _VALID_OWNERSHIPS),
    )
    for key, allowed in enum_checks:
        if key in data and data[key] not in allowed:
            raise ValueError(f"maintenance.{key} invalid")
    for key in ("quality_basis", "incarnation_id", "activation_id", "last_negative_receipt"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"maintenance.{key} must be a string")
    if "pinned" in data and not isinstance(data["pinned"], bool):
        raise ValueError("maintenance.pinned must be a boolean")
    if "ever_used" in data and data["ever_used"] is not None and not isinstance(data["ever_used"], bool):
        raise ValueError("maintenance.ever_used must be boolean or null")
    if (
        "legacy_last_activity_upper_bound" in data
        and data["legacy_last_activity_upper_bound"] is not None
        and not _is_nonneg_num(data["legacy_last_activity_upper_bound"])
    ):
        raise ValueError("maintenance.legacy_last_activity_upper_bound invalid")
    for key in ("baseline_at", "last_used_at", "restored_at", "cooled_at", "archived_at", "epoch_started_at", "established_at"):
        if key in data and not _is_nonneg_num(data[key]):
            raise ValueError(f"maintenance.{key} must be a non-negative number")
    for key in ("independent_uses", "uses_since_hurt", "hurt_count", "strike_epoch", "usage_receipt_sequence", "helped_count"):
        if key in data and not _is_nonneg_int(data[key]):
            raise ValueError(f"maintenance.{key} must be a non-negative integer")
    if "previous_stable" in data:
        data["previous_stable"] = _validate_stable_version(data["previous_stable"])
    if "observations" in data:
        obs = data["observations"]
        if not isinstance(obs, list) or len(obs) > 200 or not all(isinstance(item, dict) for item in obs):
            raise ValueError("maintenance.observations invalid")
    if "task_family_fingerprint" in data:
        fam = data["task_family_fingerprint"]
        if not isinstance(fam, str) or not _FAMILY_RE.fullmatch(fam):
            raise ValueError("maintenance.task_family_fingerprint invalid")
    return data


# LLM: 严格校验 MaintenanceCursor，禁止额外未声明键。
# 函数用途: 校验并复制维护进度游标。
def _validate_maintenance_cursor(raw: object) -> dict[str, object]:
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict) or not (set(raw.keys()) <= _CURSOR_KEYS):
        raise ValueError("maintenance_cursor has invalid shape or extra keys")
    if "phase" in raw and raw["phase"] not in _VALID_CURSOR_PHASES:
        raise ValueError("maintenance_cursor.phase invalid")
    for key in ("after_name", "after_entry", "source_generation"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"maintenance_cursor.{key} must be a string")
    for key in ("started_at", "retry_after"):
        if key in raw and not _is_nonneg_num(raw[key]):
            raise ValueError(f"maintenance_cursor.{key} invalid")
    return dict(raw)


# LLM: 严格校验 LifecycleOperation（additionalProperties=false）；保证崩溃恢复不会信任损坏意图。
# 函数用途: 校验并复制 pending_operation。
def _validate_lifecycle_operation(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("pending_operation must be an object or null")
    keys = set(raw.keys())
    if not (_LIFECYCLE_OP_REQUIRED <= keys <= _LIFECYCLE_OP_KEYS):
        raise ValueError("pending_operation has missing or unsupported keys")
    for key in ("operation_id", "skill_name", "source_rel", "target_rel", "versions_rel"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ValueError(f"pending_operation.{key} must be a non-empty string")
    if not _SKILL_NAME_RE.fullmatch(str(raw["skill_name"])):
        raise ValueError("pending_operation.skill_name invalid")
    if raw["kind"] not in _VALID_OP_KINDS or raw["phase"] not in _VALID_OP_PHASES:
        raise ValueError("pending_operation kind/phase invalid")
    if not _is_nonneg_int(raw["expected_revision"]) or int(raw["expected_revision"]) < 1:
        raise ValueError("pending_operation.expected_revision invalid")
    if not isinstance(raw["expected_sha256"], str) or not _SHA256_RE.fullmatch(raw["expected_sha256"]):
        raise ValueError("pending_operation.expected_sha256 invalid")
    if not _is_nonneg_num(raw["created_at"]):
        raise ValueError("pending_operation.created_at invalid")
    for key in ("candidate_rel", "tombstone_rel", "candidate_name", "candidate_sha256"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], str):
            raise ValueError(f"pending_operation.{key} must be string or null")
    if raw.get("candidate_name") is not None and not _SKILL_NAME_RE.fullmatch(str(raw["candidate_name"])):
        raise ValueError("pending_operation.candidate_name invalid")
    if raw.get("candidate_sha256") is not None and not _SHA256_RE.fullmatch(str(raw["candidate_sha256"])):
        raise ValueError("pending_operation.candidate_sha256 invalid")
    if "candidate_revision" in raw and raw["candidate_revision"] is not None:
        cand_rev = raw["candidate_revision"]
        if not _is_nonneg_int(cand_rev) or int(cand_rev) < 1:
            raise ValueError("pending_operation.candidate_revision invalid")
    for key in ("cleanup_cursor", "event_id", "reason_code"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"pending_operation.{key} must be a string")
    return dict(raw)


# LLM: 与 asset_promotion.task_family_fingerprint("skill", {"body": body}) 同算法，供底层发布/迁移复用而无循环依赖。
# 函数用途: 根据 SKILL.md 完整正文计算标准化任务家族指纹。
def skill_family_fingerprint(body: str) -> str:
    stripped = re.sub(r"\A---\n.*?\n---\n", "", body, flags=re.S)
    stripped = re.sub(r"^#{1,6}[^\n]*", "", stripped, flags=re.M)
    signature = re.findall(r"[\w]+", stripped.casefold())
    return hashlib.sha256(json.dumps(["skill", signature], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read_managed_skill_text_strict(root: Path, rel_parts: tuple[str, ...], *, skill_name: str) -> str:
    try:
        raw = read_bytes_beneath(root, rel_parts, max_bytes=MAX_SKILL_BYTES, require_dir_fd=True)
    except (OSError, ValueError) as exc:
        raise SkillLearningStoreError(
            CODE_EVIDENCE_INCOMPLETE,
            f"unreadable managed skill file for {skill_name}: {exc}",
            detail={"skill_name": skill_name, "reason": type(exc).__name__},
        ) from exc
    if raw is None:
        raise SkillLearningStoreError(
            CODE_EVIDENCE_INCOMPLETE,
            f"missing managed skill file for {skill_name}",
            detail={"skill_name": skill_name, "reason": "missing"},
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLearningStoreError(
            CODE_EVIDENCE_INCOMPLETE,
            f"corrupt UTF-8 in managed skill file for {skill_name}",
            detail={"skill_name": skill_name, "reason": "invalid_utf8"},
        ) from exc


def _is_op_committed_in_raw(raw_doc: object, op: dict[str, object]) -> bool:
    if not isinstance(raw_doc, dict):
        return False
    skills = raw_doc.get("skills")
    if not isinstance(skills, dict):
        return False
    op_id = str(op.get("operation_id") or "")
    kind = str(op.get("kind") or "")
    skill_name = str(op.get("skill_name") or "")
    if kind in {"remove", "evict"}:
        pending = raw_doc.get("pending_operation")
        pending_ok = pending is None or (
            isinstance(pending, dict)
            and pending.get("operation_id") == op_id
            and pending.get("phase") in {"committed", "cleanup"}
        )
        return (skill_name not in skills) and pending_ok
    if kind in {"archive", "restore", "rollback"}:
        row = skills.get(skill_name)
        return isinstance(row, dict) and row.get("last_operation_id") == op_id
    if kind == "displace_publish":
        cand_name = str(op.get("candidate_name") or "")
        row = skills.get(cand_name)
        return isinstance(row, dict) and row.get("last_operation_id") == op_id
    return False


# LLM: 同设备rename发布后同步报错须标记原异常；仅rename本身EXDEV时进入原跨设备复制路径。
# 函数用途: 移动目录项并区分发布前失败与发布后同步失败。
def _rename_entry_with_exdev_fallback(
    src_parent_fd: int,
    src_name: str,
    dst_parent_fd: int,
    dst_name: str,
    *,
    src_root: Path,
    src_parts: tuple[str, ...],
    dst_root: Path,
    dst_parts: tuple[str, ...],
) -> None:
    published = False
    try:
        os.rename(src_name, dst_name, src_dir_fd=src_parent_fd, dst_dir_fd=dst_parent_fd)
        published = True
        os.fsync(dst_parent_fd)
        os.fsync(src_parent_fd)
        return
    except OSError as exc:
        if published:
            exc._skill_lifecycle_published = True
            raise
        if exc.errno != errno.EXDEV:
            raise
    _copy_entry_nofollow(src_root, src_parts, dst_root, dst_parts)
    _remove_entry_nofollow(src_root, src_parts)


def _copy_entry_nofollow(
    src_root: Path,
    src_parts: tuple[str, ...],
    dst_root: Path,
    dst_parts: tuple[str, ...],
) -> None:
    src_parent_fd = open_directory_beneath(src_root, src_parts[:-1], require_dir_fd=True)
    try:
        st = os.stat(src_parts[-1], dir_fd=src_parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(src_parts[-1], dir_fd=src_parent_fd)
            dst_parent_fd = open_directory_beneath(dst_root, dst_parts[:-1], create=True, require_dir_fd=True)
            try:
                os.symlink(target, dst_parts[-1], dir_fd=dst_parent_fd)
                os.fsync(dst_parent_fd)
            finally:
                os.close(dst_parent_fd)
            return
        if not stat.S_ISDIR(st.st_mode):
            raise NoFollowPathError("managed source entry is neither directory nor symlink")
    finally:
        os.close(src_parent_fd)
    stack: list[tuple[tuple[str, ...], tuple[str, ...]]] = [(src_parts, dst_parts)]
    while stack:
        cur_src, cur_dst = stack.pop()
        src_fd = open_directory_beneath(src_root, cur_src, require_dir_fd=True)
        dst_fd = open_directory_beneath(dst_root, cur_dst, create=True, require_dir_fd=True)
        try:
            for entry in sorted(os.scandir(src_fd), key=lambda item: item.name):
                est = os.stat(entry.name, dir_fd=src_fd, follow_symlinks=False)
                if stat.S_ISLNK(est.st_mode):
                    os.symlink(os.readlink(entry.name, dir_fd=src_fd), entry.name, dir_fd=dst_fd)
                elif stat.S_ISDIR(est.st_mode):
                    stack.append(((*cur_src, entry.name), (*cur_dst, entry.name)))
                elif stat.S_ISREG(est.st_mode):
                    in_fd = _openat_file(src_fd, entry.name, os.O_RDONLY, 0o600)
                    out_fd = -1
                    try:
                        out_fd = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_fd)
                        while chunk := os.read(in_fd, 65536):
                            view = memoryview(chunk)
                            while view:
                                written = os.write(out_fd, view)
                                if written <= 0:
                                    raise OSError("short write during cross-device copy")
                                view = view[written:]
                        os.fsync(out_fd)
                    finally:
                        if out_fd >= 0:
                            os.close(out_fd)
                        os.close(in_fd)
            os.fsync(dst_fd)
        finally:
            os.close(dst_fd)
            os.close(src_fd)


def _remove_entry_nofollow(root: Path, parts: tuple[str, ...]) -> None:
    parent_fd = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    try:
        st = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            os.unlink(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
            return
    finally:
        os.close(parent_fd)
    stack: list[tuple[str, ...]] = [parts]
    while stack:
        cur = stack[-1]
        dir_fd = open_directory_beneath(root, cur, require_dir_fd=True)
        child: tuple[str, ...] | None = None
        try:
            for entry in os.scandir(dir_fd):
                est = os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISDIR(est.st_mode) and not stat.S_ISLNK(est.st_mode):
                    child = (*cur, entry.name)
                    break
                os.unlink(entry.name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        if child is not None:
            stack.append(child)
            continue
        pfd = open_directory_beneath(root, cur[:-1], require_dir_fd=True)
        try:
            os.rmdir(cur[-1], dir_fd=pfd)
            os.fsync(pfd)
        finally:
            os.close(pfd)
        stack.pop()


# LLM: 每日历史只保留UTC最近30天的整数计数；合法ISO日期作结构化键，不靠展示文字推断日期。
# 函数用途: 校验并裁剪每日总结调用统计，避免跨日覆盖或无限增长。
def _recent_daily_history(history: object) -> dict[str, int]:
    if not isinstance(history, dict):
        raise ValueError("registry daily_history must be an object")
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=29)).isoformat()
    result = {}
    for day, count in history.items():
        if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                or isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValueError("registry daily_history has invalid counts or dates")
        if cutoff <= day <= today.isoformat():
            result[day] = count
    return dict(sorted(result.items()))


# LLM: 旧表只有daily快照时将合法最近日期补入历史；已有历史不被更低旧快照覆盖，原daily字段保留。
# 函数用途: 恢复向后兼容的每日调用历史，不重置今日预算。
def _registry_daily_history(raw: object, daily_date: str, calls: int) -> dict[str, int]:
    history = _recent_daily_history(raw)
    try:
        valid_date = date.fromisoformat(daily_date).isoformat() == daily_date
    except ValueError:
        valid_date = False
    if valid_date:
        history[daily_date] = max(history.get(daily_date, 0), calls)
    return _recent_daily_history(history)


# LLM: 必填字符串字段；空值视为损坏。
# 函数用途: 读取一个非空字符串字段。
def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"learned skill field {key} must be a non-empty string")
    return value


def _version_name_number(name: str) -> int:
    if not (name.startswith("v") and name.endswith(".md")):
        return 0
    digits = name[1:-3]
    return int(digits) if digits.isdigit() else 0


# LLM: 发布与回滚共用：同目录临时文件同步后原子替换，拒绝链接且保留其它文件；替换后OSError标记正文已发布。
# 函数用途: 原子替换一个 SKILL.md 文件。
def replace_skill_file(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise NoFollowPathError("refusing to replace symlinked skill file")
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    published = False
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        published = True
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        if published:
            exc._skill_lifecycle_published = True
        raise
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
    "CODE_ARCHIVE_EXPIRED",
    "CODE_EVIDENCE_INCOMPLETE",
    "CODE_MAINTENANCE_OBSERVE_ONLY",
    "CODE_QUEUE_FULL",
    "CODE_REGISTRY_CORRUPT",
    "CODE_REQUEST_CORRUPT",
    "EVENT_DROPPED",
    "EVENT_FAILED",
    "EVENT_PUBLISHED",
    "EVENT_REJECTED",
    "EVENT_REMOVED",
    "EVENT_REVERTED",
    "EVENT_SKIPPED",
    "EVENT_UPDATED",
    "LEARNED_DIR_NAME",
    "MAX_REQUEST_ATTEMPT_COUNT",
    "MAX_SOURCE_RUN_COUNT",
    "REQUEST_SCHEMA_VERSION",
    "LearnedSkill",
    "SkillLearningEvent",
    "SkillLearningRegistry",
    "SkillLearningStore",
    "SkillLearningStoreError",
    "is_proven_stable_version",
    "replace_skill_file",
    "request_key",
    "skill_family_fingerprint",
    "utc_now",
]
