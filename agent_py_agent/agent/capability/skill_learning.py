# LLM: 自学习 S3（自动总结 Skill）的服务入口，只在 enable_self_learning 开启时由组合根装配到 agent.skill_learning。
#   收口侧 enqueue_from_finalize 只写一条有界请求；Gateway 后台记忆整理车道调用 run_pending：非阻塞运行锁、
#   前台同端点模型在忙就顺延、每日上限、无工具结构化调用（复用记忆整理的 backend 与 call_backend_with_timeout）、
#   严格解析、自动闸门发布、账本、删请求。模型失败最多重试 MAX_REQUEST_ATTEMPTS 次；输出不合规和闸门拒绝不重试。
#   不给模型注册任何工具。同步检查 core.py 装配、_finalization_service.py、cli/gateway_loops.py 与 test_skill_learning*.py。
# 模块用途: 把主代理完成的复杂任务在后台总结成 owner 的自学 Skill，全程不需要用户确认、全程留账。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..backends.request_scope import foreground_model_active
from ..memory_store.curator_backend import CuratorModelStillRunningError, call_backend_with_timeout
from .skill_learning_prompt import (
    DECISION_SKIP,
    EXISTING_DESCRIPTION_CHARS,
    SkillLearningMaterial,
    SkillLearningOutputError,
    parse_skill_learning_decision,
    skill_learning_prompt,
    skill_learning_response_schema,
)
from .skill_learning_publish import PublishRequest, SkillLearningGateError, publish_learned_skill
from .skill_learning_request import build_learning_request
from .skill_learning_store import (
    CODE_QUEUE_FULL,
    CODE_REQUEST_CORRUPT,
    EVENT_DROPPED,
    EVENT_FAILED,
    EVENT_REJECTED,
    EVENT_SKIPPED,
    MAX_REQUEST_ATTEMPTS,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
)
from .skill_snapshot import skill_content_sha256
from .skills import parse_skill_file

STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_DAILY_LIMIT = "daily_limit"
STATUS_RETRY = "retry"
CODE_MODEL_TIMEOUT = "SKILL_LEARNING_MODEL_TIMEOUT"
CODE_MODEL_FAILED = "SKILL_LEARNING_MODEL_FAILED"
UPDATABLE_LIMIT = 3


# LLM: 数值来自 AgentConfig 的 self_learning_* 字段（已规范化）；0 的上限表示不限。
# 类用途: 自动总结的触发门槛与预算。
@dataclass(frozen=True)
class SkillLearningSettings:
    min_tool_rounds: int = 6
    daily_limit: int = 20
    max_skills: int = 50
    timeout_seconds: int = 180

    # LLM: 读不到或类型不对时用默认值，不因配置损坏而关掉整条链。
    # 函数用途: 从 AgentConfig 读取自动总结设置。
    @classmethod
    def from_config(cls, config: object) -> SkillLearningSettings:
        defaults = cls()
        return cls(
            min_tool_rounds=_config_int(config, "self_learning_min_tool_rounds", defaults.min_tool_rounds),
            daily_limit=_config_int(config, "self_learning_daily_limit", defaults.daily_limit),
            max_skills=_config_int(config, "self_learning_max_skills", defaults.max_skills),
            timeout_seconds=_config_int(config, "self_learning_timeout_seconds", defaults.timeout_seconds),
        )


# LLM: backend 与记忆整理共用（owner 选定模型、非流式）；snapshot_provider 返回当前 owner 的 Skill 快照，
#   只读取 entries 的 name/description/source/path/stable_id；guard_config 只用于 guard 的文件数与大小上限。
# 类用途: 自动总结在运行期依赖的外部对象。
@dataclass(frozen=True)
class SkillLearningRuntime:
    backend: object
    snapshot_provider: Callable[[], object]
    guard_config: object | None = None


# LLM: status 取 idle/busy/daily_limit/retry 或账本事件名（published/updated/skipped/rejected/failed/dropped）；
#   调用方只记录，不据此改变调度。
# 类用途: 一次 run_pending 的结构化结果。
@dataclass(frozen=True)
class SkillLearningRunResult:
    status: str
    code: str = ""
    skill_name: str = ""
    request_key: str = ""


# LLM: 服务不持有线程或定时器；并发与顺延全靠 store 的运行锁和调用方车道。
# 类用途: 一个 owner 的自动总结 Skill 服务。
class SkillLearningService:
    # LLM: 构造不创建目录、不发请求。
    # 函数用途: 绑定存储、设置与运行期依赖。
    def __init__(self, store: SkillLearningStore, settings: SkillLearningSettings,
                 runtime: SkillLearningRuntime) -> None:
        self.store = store
        self.settings = settings
        self.runtime = runtime

    # LLM: 只写一条请求；不合格返回 ineligible；队列满时丢弃并记 dropped/QUEUE_FULL。副作用：写 requests/ 与账本。
    # 函数用途: 收口时按结构化条件登记一次学习请求，返回 queued/duplicate/queue_full/ineligible。
    def enqueue_from_finalize(self, ctx: object) -> str:
        request = build_learning_request(ctx, self.settings.min_tool_rounds)
        if request is None:
            return "ineligible"
        outcome = self.store.enqueue(request)
        if outcome == "queue_full":
            self.store.append_event(_request_event(EVENT_DROPPED, CODE_QUEUE_FULL, request))
        return outcome

    # LLM: 纯文件系统检查，供 Gateway 车道准入使用。
    # 函数用途: 判断是否有待处理的学习请求。
    def has_pending(self) -> bool:
        return bool(self.store.pending_requests())

    # LLM: 每次最多处理一条；前台同端点模型在忙、运行锁被占时返回 busy，请求原样保留。
    # 函数用途: 在后台处理最早的一条学习请求。
    def run_pending(self) -> SkillLearningRunResult:
        if not self.store.pending_requests():
            return SkillLearningRunResult(STATUS_IDLE)
        if foreground_model_active(self.runtime.backend):
            return SkillLearningRunResult(STATUS_BUSY)
        try:
            with self.store.run_lock():
                return self._run_first()
        except BlockingIOError:
            return SkillLearningRunResult(STATUS_BUSY)
        except SkillLearningStoreError as exc:
            return SkillLearningRunResult(EVENT_FAILED, exc.code)

    # LLM: 顺序固定：取最早请求→损坏即丢弃→占每日额度→材料→模型→决定→账本→删请求。
    # 函数用途: 在运行锁内处理一条请求。
    def _run_first(self) -> SkillLearningRunResult:
        pending = self.store.pending_requests()
        if not pending:
            return SkillLearningRunResult(STATUS_IDLE)
        path = pending[0]
        request = self.store.read_request(path)
        if request is None:
            self.store.discard_request(path)
            return self._finish(path, {}, SkillLearningEvent(EVENT_DROPPED, CODE_REQUEST_CORRUPT))
        if not self.store.consume_daily_call(self.settings.daily_limit):
            return SkillLearningRunResult(STATUS_DAILY_LIMIT, request_key=str(request.get("request_key") or ""))
        material = _material(self.store, request, _snapshot_entries(self.runtime.snapshot_provider))
        try:
            response = call_backend_with_timeout(
                self.runtime.backend,
                prompt=skill_learning_prompt(material),
                response_schema=skill_learning_response_schema(),
                timeout_seconds=self.settings.timeout_seconds,
            )
        except CuratorModelStillRunningError:
            return SkillLearningRunResult(STATUS_BUSY, request_key=str(request.get("request_key") or ""))
        except Exception as exc:  # noqa: BLE001 - 供应商故障只影响这一条请求，按重试合同记账。
            return self._retry_or_drop(path, request, exc)
        event = self._decide(material, str(getattr(response, "text", "") or ""))
        return self._finish(path, request, event)

    # LLM: skip 直接记账；create/update 走自动闸门；输出不合规与闸门拒绝都记 rejected（不重试）。
    # 函数用途: 把模型输出变成一条账本事件（可能伴随发布）。
    def _decide(self, material: SkillLearningMaterial, text: str) -> SkillLearningEvent:
        request = material.request
        updatable = frozenset(str(item.get("name") or "") for item in material.updatable_skills)
        try:
            decision = parse_skill_learning_decision(text, updatable)
        except SkillLearningOutputError as exc:
            return replace(_request_event(EVENT_REJECTED, exc.code, request), reason=exc.field)
        if decision.decision == DECISION_SKIP:
            return replace(_request_event(EVENT_SKIPPED, "", request), reason=decision.reason)
        publish = PublishRequest(decision, request, material.taken_names, self.settings.max_skills,
                                 self.runtime.guard_config)
        try:
            return publish_learned_skill(self.store, publish)
        except SkillLearningGateError as exc:
            rejected = _request_event(EVENT_REJECTED, exc.code, request)
            return replace(rejected, skill_name=decision.name, reason=decision.reason)

    # LLM: 模型失败按请求上的 attempts 计数：未到上限写回并保留，到上限丢弃并记 failed。
    # 函数用途: 处理一次模型调用失败。
    def _retry_or_drop(self, path: Path, request: dict[str, object], exc: Exception) -> SkillLearningRunResult:
        code = CODE_MODEL_TIMEOUT if isinstance(exc, TimeoutError) else CODE_MODEL_FAILED
        attempts = int(request.get("attempts") or 0) + 1
        if attempts < MAX_REQUEST_ATTEMPTS:
            self.store.rewrite_request(path, {**request, "attempts": attempts})
            return SkillLearningRunResult(STATUS_RETRY, code, request_key=str(request.get("request_key") or ""))
        failed = replace(_request_event(EVENT_FAILED, code, request), reason=type(exc).__name__)
        return self._finish(path, request, failed)

    # LLM: 账本先写、请求后删：崩在两者之间最多重复处理一次（发布侧有重名/哈希闸门兜住），不会丢账。
    # 函数用途: 记账并结束一条请求。
    def _finish(self, path: Path, request: dict[str, object], event: SkillLearningEvent) -> SkillLearningRunResult:
        self.store.append_event(event)
        self.store.discard_request(path)
        return SkillLearningRunResult(event.event, event.code, event.skill_name,
                                      str(request.get("request_key") or event.request_key))


# LLM: 快照读取失败（例如 Skill 总闸配置损坏）按空索引处理：这只会让重名检查少一层来源，
#   发布侧仍会检查 learned/<name> 目录与登记表，不会覆盖任何文件。
# 函数用途: 取当前快照的 Skill 条目。
def _snapshot_entries(provider: Callable[[], object]) -> tuple[object, ...]:
    try:
        snapshot = provider()
    except Exception:  # noqa: BLE001 - 快照不可用不能阻断学习，重名检查退回到磁盘与登记表。
        return ()
    return tuple(getattr(snapshot, "entries", ()) or ())


# LLM: existing 是全部来源的名字索引（描述截断）；updatable 只收本轮 skill_search get 读过、登记在册、
#   快照条目确实指向 learned/<name>/SKILL.md 且磁盘 hash 等于登记值的自学 Skill，最多 UPDATABLE_LIMIT 个。
#   读取登记表；登记表损坏时抛 SkillLearningStoreError，由 run_pending 记 failed 且保留请求。
# 函数用途: 组装一次总结调用的材料。
def _material(store: SkillLearningStore, request: dict[str, object], entries: tuple[object, ...]) -> SkillLearningMaterial:
    registry = store.load_registry()
    existing = tuple(
        {
            "name": str(getattr(entry, "name", "") or ""),
            "source": str(getattr(entry, "source", "") or ""),
            "description": str(getattr(entry, "description", "") or "")[:EXISTING_DESCRIPTION_CHARS],
        }
        for entry in entries
    )
    used = {str(item) for item in (request.get("used_skill_ids") or [])}
    candidates = (_updatable(store, registry, entry) for entry in entries if getattr(entry, "stable_id", "") in used)
    updatable = tuple(item for item in candidates if item is not None)[:UPDATABLE_LIMIT]
    taken = frozenset(item["name"] for item in existing if item["name"])
    return SkillLearningMaterial(request, existing, updatable, taken)


# LLM: 任何一项所有权条件不满足都返回 None；返回的是模型可整篇替换的字段（不含 frontmatter 原文）。
# 函数用途: 把一个快照条目转成可更新的自学 Skill 材料。
def _updatable(store: SkillLearningStore, registry: SkillLearningRegistry, entry: object) -> dict[str, object] | None:
    name = str(getattr(entry, "name", "") or "")
    record = registry.skills.get(name)
    path = store.skill_path(name)
    if record is None or getattr(entry, "source", "") != "owner" or not path.is_file():
        return None
    if Path(str(getattr(entry, "path", "") or "")).resolve() != path.resolve():
        return None
    text = path.read_text(encoding="utf-8")
    if skill_content_sha256(text) != record.sha256:
        return None
    card = parse_skill_file(path, source="owner", require_frontmatter=True)
    return {"name": name, "version": record.version, "description": card.description,
            "when_to_use": card.when_to_use, "tags": list(card.tags), "body": _body_of(text)}


# LLM: 自学 Skill 由本模块的渲染器写出，frontmatter 形状固定（首行 ---，下一个 --- 行结束）。
# 函数用途: 取出 SKILL.md 的正文部分。
def _body_of(text: str) -> str:
    lines = text.split("\n")
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    return "\n".join(lines[closing + 1:]).strip() if lines and lines[0].strip() == "---" else text.strip()


# LLM: 请求类事件只带 request_key/run_id；需要 reason 或 skill_name 的调用方用 dataclasses.replace 补上。
# 函数用途: 生成与某条请求绑定的账本事件。
def _request_event(kind: str, code: str, request: dict[str, object]) -> SkillLearningEvent:
    return SkillLearningEvent(
        event=kind,
        code=code,
        request_key=str(request.get("request_key") or ""),
        run_id=str(request.get("run_id") or ""),
    )


# LLM: 配置已由规范化层保证是整数；这里只防御测试桩或旧对象缺字段。
# 函数用途: 读取一个整数配置并在异常时返回默认值。
def _config_int(config: object, key: str, default: int) -> int:
    try:
        return int(getattr(config, key, default))
    except (TypeError, ValueError):
        return default


__all__ = [
    "CODE_MODEL_FAILED",
    "CODE_MODEL_TIMEOUT",
    "STATUS_BUSY",
    "STATUS_DAILY_LIMIT",
    "STATUS_IDLE",
    "STATUS_RETRY",
    "SkillLearningRunResult",
    "SkillLearningRuntime",
    "SkillLearningService",
    "SkillLearningSettings",
]
