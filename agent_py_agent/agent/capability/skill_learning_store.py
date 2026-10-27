# LLM: 自学习 S3（自动总结 Skill）的唯一落盘权威。目录固定为 owner 路径解析器登记的 owner_skill_learning_dir：
#   requests/ 过渡工作单、registry.json 自学 Skill 登记表（“是不是自学 Skill”只由它回答）、ledger.jsonl 只追加账本、
#   versions/ 版本全文、removed/ 用户删除归档。发布位置是 <owner_home>/skills/learned/<name>/SKILL.md。
#   .state 短锁保护登记表/队列读改写；.run 非阻塞长锁只保证同一 owner 同时只有一个总结在跑。
#   同步检查 skill_learning.py / skill_learning_publish.py / cli/skill_learning_commands.py 与 test_skill_learning*.py。
# 模块用途: 读写有界请求及溢出缓冲、每日总结调用历史、登记表和版本；不调用模型，不代替正式发布确认。
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import MISSING, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_capped,
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import (
    list_names_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy

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

_REQUEST_FILE_RE = re.compile(r"[0-9]{13}-[0-9a-f]{16}\.json")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


# LLM: code 是唯一机器判断字段；登记表损坏时 fail closed，调用方不能把它当成空登记表继续写。
# 类用途: 表示自学 Skill 落盘状态读不出或不合规。
class SkillLearningStoreError(Exception):
    # LLM: 只保存结果码和人读说明，不携带文件正文。
    # 函数用途: 构造一次带结果码的存储错误。
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}",_text(payload,"name")) or payload.get("layout","learned") not in {"owner","learned"}:
            raise ValueError("learned skill layout/name invalid")
        return cls(_text(payload, "name"), version, sha, _text(payload, "created_at"),
                   _text(payload, "updated_at"), tuple(runs), revision, str(payload.get("last_operation_id") or ""), dict(payload.get("maintenance") or {}), str(payload.get("layout") or "learned"))


# LLM: skills 是自学 Skill 的唯一登记；blocked_names 是用户删过的名字，自动流程永不再新建；
#   daily_date/daily_calls与最近30日daily_history仅统计总结模型调用，不统计Skill正文使用。
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

    # LLM: 保持原schema和单日快照，另存有界每日调用历史；不改正式Skill登记事实。
    # 函数用途: 转成写入 registry.json 的字典。
    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "skills": {name: self.skills[name].to_record() for name in sorted(self.skills)},
            "blocked_names": sorted(set(self.blocked_names)),
            "daily": {"date": self.daily_date, "calls": self.daily_calls},
            "daily_history": dict(self.daily_history),
            "import_operations":dict(self.import_operations),
            "assets": dict(self.assets),
            "last_maintenance_at": self.last_maintenance_at,
        }


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


# LLM: 所有路径都从 owner 规范布局推导；构造不创建任何目录，第一次写入时才创建。
# 类用途: 一个owner的学习存储，热请求20条、溢出100条并保留独立每日总结调用历史。
class SkillLearningStore:
    # LLM: 只绑定原owner根与热/溢出目录，不在构造中创建目录或自动消费请求。
    # 函数用途: 绑定自学数据目录与 owner Skill 根。
    def __init__(self, directory: Path, skills_root: Path) -> None:
        self.directory = Path(directory)
        self.skills_root = Path(skills_root)
        self.learned_root = self.skills_root / LEARNED_DIR_NAME
        self.requests_dir = self.directory / "requests"
        self.overflow_requests_dir = self.directory / "requests_overflow"
        self.registry_path = self.directory / "registry.json"
        self.ledger_path = self.directory / "ledger.jsonl"
        self.versions_dir = self.directory / "versions"
        self.removed_dir = self.directory / "removed"
        self._quota_admission = ContextVar("skill_learning_quota", default=None)

    # LLM: 与 SkillProposalService 同一推导：数据目录来自 owner_skill_learning_dir，Skill 根是 owner_home/skills。
    # 函数用途: 按 owner 路径投影构造存储。
    @classmethod
    def for_home(cls, home_paths: object) -> SkillLearningStore:
        return cls(Path(home_paths.owner_skill_learning_dir), Path(home_paths.owner_home_dir) / "skills")

    # LLM: owner配额→state锁固定顺序；登记、队列、版本、导入同源事务，不能在持锁期间调用模型或发网络请求。
    # 函数用途: 返回登记表与队列的互斥区。
    @contextmanager
    def state_lock(self):
        with owner_quota_enforcer_from_policy(self.skills_root.parent).admission() as admission:
            with locked_json_path(self.directory / ".state"):
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

    # LLM: 兼容保留的内部入口，直接委托给全局FIFO收敛与回填实现。
    # 函数用途: 恢复溢出请求并保持热区不超过20条。
    def _refill_requests_locked(self) -> None:
        self._recover_requests_locked()

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

    # LLM: 文件不存在视为空登记表；存在但损坏时抛 REGISTRY_CORRUPT，绝不按空表覆盖写回。
    # 函数用途: 读取登记表。
    def load_registry(self) -> SkillLearningRegistry:
        if not self.registry_path.is_file():
            return SkillLearningRegistry()
        report = read_json_object_report(self.registry_path, context="skill_learning.registry")
        try:
            if report.load_error is not None:
                raise ValueError("unreadable registry")
            registry = _registry_from(report.payload)
            # 旧登记没有高水位：只从既有不可变版本文件恢复编号，不重用回滚前已出现的版本名。
            records = report.payload.get("skills", {})
            migrated = {name: replace(item, revision=max(item.revision, max((_version_number(path) for path in (self.versions_dir / name).glob("v*.md")), default=item.version)))
                        if "revision" not in records[name] else item for name, item in registry.skills.items()}
            return replace(registry, skills=migrated)
        except (TypeError, ValueError) as exc:
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

    # LLM: feedback负责adopted权限与claim；过滤MISSING后按公开关键字形状调用台账，保留显式None与故障软失败。
    # 函数用途: 接通现有采用反馈的可选写账钩子，不确认或发布任何Skill内容。
    def record_skill_usage(
        self, *, event, skill_id, source, kind="skill",
        request_id=MISSING, thread_id=MISSING, task_id=MISSING, chars=MISSING,
    ) -> None:
        from .skill_usage_ledger import record_skill_usage_event

        params = dict(event=event, skill_id=skill_id, source=source, kind=kind,
                      request_id=request_id, thread_id=thread_id, task_id=task_id, chars=chars)
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
        self.check_write(path,text)
        write_text_atomic_beneath(self.directory, ("versions", name, path.name), text)

    # LLM: 版本文件不存在（已被裁剪或从未保存）时返回 None，由回滚方给出结构化拒绝。
    # 函数用途: 读取某个版本的 SKILL.md 全文。
    def read_version(self, name: str, version: int) -> str | None:
        return read_text_beneath(self.directory, ("versions", name, f"v{version}.md"))

    # LLM: 删除不物理删文件：整个 learned/<name>/ 目录移到 removed/<name>-<时间>/；目录不存在时什么都不做。
    # 函数用途: 把一个自学 Skill 目录移出扫描范围并返回归档位置。
    def archive_removed(self, name: str) -> Path | None:
        source = self.skill_path(name).parent
        if not source.exists():
            return None
        target = self.removed_dir / f"{name}-{int(time.time() * 1000):013d}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return target

    # LLM: 发布目录固定为 skills/learned/<name>/SKILL.md，名字已由输出合同限定为安全目录名。
    # 函数用途: 返回某个自学 Skill 的 SKILL.md 路径。
    def skill_path(self, name: str) -> Path:
        record = self.load_registry().skills.get(name)
        if record and record.layout == "owner":
            return self.skills_root / name / "SKILL.md"
        return self.learned_root / name / "SKILL.md"


# LLM: owner由Store路径隔离，root/task优先，旧请求才退thread/run；成对轨迹与谱系检查由_mergeable_requests承担。
# 函数用途: 计算规范请求上下文，不让不同task因共享thread而撞名。
def _request_context(request):
    for key in ('root_task_id', 'task_id', 'thread_id', 'run_id'):
        value = str(request.get(key) or '').strip()
        if value:
            return ('task_id' if key == 'root_task_id' else key), value
    return ()


# LLM: 规范root/task优先；谱系可压实，轨迹不对称缺失或冲突均保留；双方旧格式缺轨迹才按旧run合同兼容。
# 函数用途: 统一入队与崩溃恢复的安全替代规则，保留独立任务材料。
def _mergeable_requests(left, right):
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


# LLM: 原登记校验保持；新增每日历史严格校验且兼容缺字段旧表，损坏不得当空表覆盖。
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
    if not isinstance(blocked, list) or not isinstance(daily, dict):
        raise ValueError("registry blocked_names/daily have invalid shape")
    calls = daily.get("calls") or 0
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        raise ValueError("registry daily calls must be a non-negative integer")
    daily_date = str(daily.get("date") or "")
    history = _registry_daily_history(payload.get("daily_history", {}), daily_date, calls)
    assets, clock = payload.get('assets', {}), payload.get('last_maintenance_at', 0)
    if not isinstance(assets, dict) or type(clock) not in (int, float) or not 0 <= clock < float('inf'):
        raise ValueError('registry assets/maintenance invalid')
    return SkillLearningRegistry(skills, tuple(str(item) for item in blocked), daily_date, calls,
                                 dict(payload.get("import_operations") or {}), history, assets, clock)


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


# LLM: 版本文件名固定 v<N>.md；按数字而不是字典序排序，v10 排在 v9 之后。
# 函数用途: 取版本文件的版本号。
def _version_number(path: Path) -> int:
    digits = path.stem[1:]
    return int(digits) if digits.isdigit() else 0


# LLM: 发布与回滚共用：只在目标 SKILL.md 所在目录写临时文件再原子替换，保留用户放进同目录的其它文件。
# 函数用途: 原子替换一个 SKILL.md 文件。
def replace_skill_file(path: Path, text: str) -> None:
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
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
    "replace_skill_file",
    "request_key",
    "utc_now",
]
