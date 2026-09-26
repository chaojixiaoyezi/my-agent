# LLM: 自学习 S3（自动总结 Skill）的唯一落盘权威。目录固定为 owner 路径解析器登记的 owner_skill_learning_dir：
#   requests/ 过渡工作单、registry.json 自学 Skill 登记表（“是不是自学 Skill”只由它回答）、ledger.jsonl 只追加账本、
#   versions/ 版本全文、removed/ 用户删除归档。发布位置是 <owner_home>/skills/learned/<name>/SKILL.md。
#   .state 短锁保护登记表/队列读改写；.run 非阻塞长锁只保证同一 owner 同时只有一个总结在跑。
#   同步检查 skill_learning.py / skill_learning_publish.py / cli/skill_learning_commands.py 与 test_skill_learning*.py。
# 模块用途: 读写自动总结 Skill 的请求队列、登记表、账本和版本存档；不调用模型，也不做发布闸门判断。
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    append_jsonl_capped,
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
    write_text_file_atomic,
)

REQUEST_SCHEMA_VERSION = "my-agent.skill-learning-request.v1"
REGISTRY_SCHEMA_VERSION = "my-agent.skill-learning-registry.v1"
EVENT_SCHEMA_VERSION = "my-agent.skill-learning-event.v1"
LEARNED_DIR_NAME = "learned"
MAX_PENDING_REQUESTS = 20
MAX_REQUEST_ATTEMPTS = 2
MAX_KEPT_VERSIONS = 5
MAX_LEDGER_EVENTS = 2000
MAX_SOURCE_RUNS = 10

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

    # LLM: 字段名是持久化协议；元组写成 JSON 数组。
    # 函数用途: 转成写入 registry.json 的字典。
    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
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
        runs = payload.get("source_run_ids") or []
        if not isinstance(runs, list) or not all(isinstance(item, str) for item in runs):
            raise ValueError("learned skill source_run_ids must be a list of ids")
        return cls(_text(payload, "name"), version, sha, _text(payload, "created_at"),
                   _text(payload, "updated_at"), tuple(runs))


# LLM: skills 是自学 Skill 的唯一登记；blocked_names 是用户删过的名字，自动流程永不再新建；
#   daily_date/daily_calls 是按 UTC 日期滚动的模型调用计数。
# 类用途: registry.json 的完整内容。
@dataclass(frozen=True)
class SkillLearningRegistry:
    skills: dict[str, LearnedSkill] = field(default_factory=dict)
    blocked_names: tuple[str, ...] = ()
    daily_date: str = ""
    daily_calls: int = 0

    # LLM: 按名字排序输出，保证同一状态写出同一文件。
    # 函数用途: 转成写入 registry.json 的字典。
    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "skills": {name: self.skills[name].to_record() for name in sorted(self.skills)},
            "blocked_names": sorted(set(self.blocked_names)),
            "daily": {"date": self.daily_date, "calls": self.daily_calls},
        }


# LLM: 事件只含 ID、名字、版本、hash、结果码和有界 reason；绝不写 Skill 正文或对话内容。
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

    # LLM: event_id 与时间在落盘时生成；reason 截到 200 字防止账本变成正文副本。
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
        }


# LLM: 所有路径都从 owner 规范布局推导；构造不创建任何目录，第一次写入时才创建。
# 类用途: 一个 owner 的自学 Skill 存储（请求、登记表、账本、版本、删除归档）。
class SkillLearningStore:
    # LLM: skills_root 必须是 SkillsService 的 owner 根（owner_home/skills），发布目录在其下 learned/。
    # 函数用途: 绑定自学数据目录与 owner Skill 根。
    def __init__(self, directory: Path, skills_root: Path) -> None:
        self.directory = Path(directory)
        self.skills_root = Path(skills_root)
        self.learned_root = self.skills_root / LEARNED_DIR_NAME
        self.requests_dir = self.directory / "requests"
        self.registry_path = self.directory / "registry.json"
        self.ledger_path = self.directory / "ledger.jsonl"
        self.versions_dir = self.directory / "versions"
        self.removed_dir = self.directory / "removed"

    # LLM: 与 SkillProposalService 同一推导：数据目录来自 owner_skill_learning_dir，Skill 根是 owner_home/skills。
    # 函数用途: 按 owner 路径投影构造存储。
    @classmethod
    def for_home(cls, home_paths: object) -> SkillLearningStore:
        return cls(Path(home_paths.owner_skill_learning_dir), Path(home_paths.owner_home_dir) / "skills")

    # LLM: 短临界区：登记表、队列、版本和发布的读改写都在它里面完成，不能在持有期间调用模型。
    # 函数用途: 返回登记表与队列的互斥区。
    def state_lock(self):
        return locked_json_path(self.directory / ".state")

    # LLM: 非阻塞：拿不到抛 BlockingIOError，调用方按 busy 顺延；进程崩溃时 OS 锁自动释放。
    # 函数用途: 返回“同一 owner 同时只跑一个总结”的运行锁。
    def run_lock(self):
        return locked_json_path(self.directory / ".run", blocking=False)

    # LLM: 同一 request_key 已在队列里就不再写；队列满时不写并返回 queue_full，由调用方记账。
    #   副作用：首次写入时创建 requests/ 目录。
    # 函数用途: 把一条学习请求写入队列，返回 queued / duplicate / queue_full。
    def enqueue(self, request: dict[str, object]) -> str:
        key = str(request.get("request_key") or "")
        with self.state_lock():
            pending = self.pending_requests()
            if any(path.name.endswith(f"-{key}.json") for path in pending):
                return "duplicate"
            if len(pending) >= MAX_PENDING_REQUESTS:
                return "queue_full"
            name = f"{int(time.time() * 1000):013d}-{key}.json"
            write_json_file_atomic_unlocked(self.requests_dir / name, request)
        return "queued"

    # LLM: 只认宿主命名的请求文件；锁文件、原子写临时文件都以点开头被排除。文件名前缀是毫秒时间，排序即先进先出。
    # 函数用途: 返回按创建先后排列的待处理请求路径。
    def pending_requests(self) -> list[Path]:
        if not self.requests_dir.is_dir():
            return []
        return sorted(path for path in self.requests_dir.iterdir() if _REQUEST_FILE_RE.fullmatch(path.name))

    # LLM: 读不出、不是对象或 schema 不符都返回 None，由调用方丢弃并记 REQUEST_CORRUPT。
    # 函数用途: 读取一条请求。
    def read_request(self, path: Path) -> dict[str, object] | None:
        report = read_json_object_report(path, context="skill_learning.request")
        payload = report.payload if report.load_error is None else None
        if not isinstance(payload, dict) or payload.get("schema_version") != REQUEST_SCHEMA_VERSION:
            return None
        return payload

    # LLM: 只用于失败重试时把 attempts 写回；请求其余字段只读。
    # 函数用途: 原子覆盖一条请求。
    def rewrite_request(self, path: Path, request: dict[str, object]) -> None:
        with self.state_lock():
            if path.is_file():
                write_json_file_atomic_unlocked(path, request)

    # LLM: 幂等删除；处理完、丢弃或损坏的请求都走这里。
    # 函数用途: 从队列删除一条请求。
    def discard_request(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    # LLM: 文件不存在视为空登记表；存在但损坏时抛 REGISTRY_CORRUPT，绝不按空表覆盖写回。
    # 函数用途: 读取登记表。
    def load_registry(self) -> SkillLearningRegistry:
        if not self.registry_path.is_file():
            return SkillLearningRegistry()
        report = read_json_object_report(self.registry_path, context="skill_learning.registry")
        try:
            if report.load_error is not None:
                raise ValueError("unreadable registry")
            return _registry_from(report.payload)
        except (TypeError, ValueError) as exc:
            raise SkillLearningStoreError(CODE_REGISTRY_CORRUPT, str(exc)) from exc

    # LLM: 调用方必须已持 state_lock；原子替换整份登记表。
    # 函数用途: 写回登记表。
    def save_registry(self, registry: SkillLearningRegistry) -> None:
        write_json_file_atomic_unlocked(self.registry_path, registry.to_record())

    # LLM: 计数按 UTC 日期滚动；limit<=0 表示不限。在 state_lock 内读改写，发请求前扣减，失败也算一次调用。
    # 函数用途: 尝试占用今天的一次总结调用额度，返回是否成功。
    def consume_daily_call(self, limit: int) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.state_lock():
            registry = self.load_registry()
            calls = registry.daily_calls if registry.daily_date == today else 0
            if limit > 0 and calls >= limit:
                return False
            self.save_registry(replace(registry, daily_date=today, daily_calls=calls + 1))
        return True

    # LLM: 有界追加（保留最近 MAX_LEDGER_EVENTS 条），自带 per-path 锁，调用方不需要持 state_lock。
    # 函数用途: 往账本追加一条事件。
    def append_event(self, event: SkillLearningEvent) -> None:
        append_jsonl_capped(self.ledger_path, event.to_record(), max_records=MAX_LEDGER_EVENTS)

    # LLM: 坏行由 read_jsonl_objects_report 跳过；name 为空时返回全部事件的最后 limit 条。
    # 函数用途: 读取某个 Skill（或全部）的最近事件。
    def events(self, name: str = "", limit: int = 20) -> list[dict[str, object]]:
        if not self.ledger_path.is_file():
            return []
        records = read_jsonl_objects_report(self.ledger_path, context="skill_learning.ledger").records
        selected = [item for item in records if not name or item.get("skill_name") == name]
        return selected[-limit:] if limit > 0 else selected

    # LLM: 每个版本存全文供回滚；只保留最近 MAX_KEPT_VERSIONS 个版本文件。副作用：写 versions/<name>/。
    # 函数用途: 保存一个已发布版本的 SKILL.md 全文。
    def save_version(self, name: str, version: int, text: str) -> None:
        directory = self.versions_dir / name
        write_text_file_atomic(directory / f"v{version}.md", text)
        for stale in sorted(directory.glob("v*.md"), key=_version_number)[:-MAX_KEPT_VERSIONS]:
            stale.unlink(missing_ok=True)

    # LLM: 版本文件不存在（已被裁剪或从未保存）时返回 None，由回滚方给出结构化拒绝。
    # 函数用途: 读取某个版本的 SKILL.md 全文。
    def read_version(self, name: str, version: int) -> str | None:
        path = self.versions_dir / name / f"v{version}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    # LLM: 删除不物理删文件：整个 learned/<name>/ 目录移到 removed/<name>-<时间>/；目录不存在时什么都不做。
    # 函数用途: 把一个自学 Skill 目录移出扫描范围并返回归档位置。
    def archive_removed(self, name: str) -> Path | None:
        source = self.learned_root / name
        if not source.exists():
            return None
        target = self.removed_dir / f"{name}-{int(time.time() * 1000):013d}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return target

    # LLM: 发布目录固定为 skills/learned/<name>/SKILL.md，名字已由输出合同限定为安全目录名。
    # 函数用途: 返回某个自学 Skill 的 SKILL.md 路径。
    def skill_path(self, name: str) -> Path:
        return self.learned_root / name / "SKILL.md"


# LLM: request_key 只由运行身份决定，同一次运行重复收口不会重复入队。
# 函数用途: 计算学习请求的稳定键（16 位十六进制）。
def request_key(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


# LLM: 统一 UTC ISO 时间，只作展示与排序。
# 函数用途: 返回当前 UTC 时间字符串。
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: 与 SkillLearningRegistry.to_record 对称；键名不符、名字与键不一致或 daily 形状不对都抛 ValueError。
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
    return SkillLearningRegistry(skills, tuple(str(item) for item in blocked), str(daily.get("date") or ""), calls)


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
    "MAX_REQUEST_ATTEMPTS",
    "MAX_SOURCE_RUNS",
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
