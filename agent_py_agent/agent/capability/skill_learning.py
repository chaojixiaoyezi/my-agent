# LLM: 自学习 S3（自动总结 Skill）的服务入口，只在 enable_self_learning 开启时由组合根装配到 agent.skill_learning。
#   收口侧 enqueue_from_finalize 只写一条有界请求；Gateway 后台记忆整理车道调用 run_pending：非阻塞运行锁、
#   前台同端点模型在忙就顺延、每日上限、无工具结构化调用（复用记忆整理的 backend 与 call_backend_with_timeout）、
#   严格解析、自动闸门发布、账本、删请求。模型失败最多重试 MAX_REQUEST_ATTEMPT_COUNT 次；输出不合规和闸门拒绝不重试。
#   不给模型注册任何工具。同步检查 core.py 装配、_finalization_service.py、cli/gateway_loops.py 与 test_skill_learning*.py。
# 模块用途: 把主代理完成的复杂任务在后台总结成 owner 的自学 Skill，全程不需要用户确认、全程留账。
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..backends.provider_headers import provider_session_scope
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
from .skill_learning_request import bounded_text, build_learning_request, recalled_memories
from .skill_learning_store import (
    CODE_QUEUE_FULL,
    CODE_REQUEST_CORRUPT,
    CODE_USER_REQUEST_INELIGIBLE,
    EVENT_DROPPED,
    EVENT_FAILED,
    EVENT_REJECTED,
    EVENT_SKIPPED,
    MAX_REQUEST_ATTEMPT_COUNT,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    request_key,
    utc_now,
)
from .skill_snapshot import skill_content_sha256
from .skills import parse_skill_file

STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_DAILY_LIMIT = "daily_limit"
STATUS_RETRY = "retry"
CODE_MODEL_TIMEOUT = "SKILL_LEARNING_MODEL_TIMEOUT"
CODE_MODEL_FAILED = "SKILL_LEARNING_MODEL_FAILED"
# 单轮最多更新几条已登记的自主学习 Skill。
UPDATABLE_COUNT = 3


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
#   owner_id 用于后台调用自绑宿主会话（要求会话头的服务商没有会话会直接拒绝请求），与 Curator 同一原语。
# 类用途: 自动总结在运行期依赖的外部对象。
@dataclass(frozen=True)
class SkillLearningRuntime:
    backend: object
    snapshot_provider: Callable[[], object]
    guard_config: object | None = None
    owner_id: str = "local/main"


# LLM: status 取 idle/busy/daily_limit/retry 或账本事件名（published/updated/skipped/rejected/failed/dropped）；
#   调用方只记录，不据此改变调度。
# 类用途: 一次 run_pending 的结构化结果。
@dataclass(frozen=True)
class SkillLearningRunResult:
    status: str
    code: str = ""
    skill_name: str = ""
    request_key: str = ""


# 同时记着的“用户要求总结”标记、以及按会话记着的“最近完成的那次活”各自的上限：超出丢最早的。
_USER_REQUESTED_LIMIT_COUNT = 64
# 用户给的总结重点最多保留的字符数。
FOCUS_LIMIT_CHARS = 500
# 入队结果里算"已经排上"的两种（新排上，或同一请求键早已在队里）。
_QUEUED = frozenset({"queued", "duplicate"})

# LLM: "用户要求总结"的两种落点，只在本进程内（Gateway 重启后找不到就照实说找不到）：
#   - marks：会话任务 id → 重点。这一轮正在做活时打标记，这个任务收尾（可能在后面的运行里，会话任务 id 不变）时取走；
#   - recent：会话线程 id → {request: 这个线程最近一次完成的会话任务的学习材料（按自动总结同一规则备好、再带上那次召回的
#     相关记忆，已截断脱敏），
#     user_requested: 这次活是否已按用户要求入队过}。用户在做完之后的下一句说"总结一下"时当场入队这一份；已按用户要求入队过的
#     不再重复（复审：同一次活不入队两次）。
#   两张表都有上限，超出丢最早的。不写文件、不调模型。
# 类用途: 记住用户要求总结的标记和每个会话最近完成的那次活。
class _UserRequests:
    # LLM: 构造只建两张空表和一把锁。
    # 函数用途: 初始化标记表与最近完成表。
    def __init__(self) -> None:
        self.marks: dict[str, str] = {}
        self.recent: dict[str, dict[str, object]] = {}
        self.lock = threading.Lock()

    # LLM: 见类注释；只改内存。
    # 函数用途: 给一个会话任务记下"收尾时按用户要求总结"。
    def mark(self, task_id: str, focus: str) -> None:
        with self.lock:
            _bounded_put(self.marks, task_id, focus)

    # LLM: 见类注释；取走即删，同一标记只用一次。
    # 函数用途: 取走某个会话任务的标记（没有返回 None）。
    def take_mark(self, task_id: str) -> str | None:
        with self.lock:
            return self.marks.pop(task_id, None) if task_id else None

    # LLM: 见类注释；同一会话后完成的覆盖先完成的。只改内存。
    # 函数用途: 记下某个会话最近完成的那次活的学习请求，以及它是否已按用户要求入队。
    def remember(self, thread_id: str, request: dict[str, object], user_requested: bool) -> None:
        with self.lock:
            _bounded_put(self.recent, thread_id, {"request": request, "user_requested": user_requested})

    # LLM: 取到就把它标成"已按用户要求入队"（调用方随后入队）；已标过的返回 (请求, True)，没有返回 (None, False)。只改内存。
    # 函数用途: 认领某个会话最近完成的那次活，用来按用户要求入队。
    def claim_latest(self, thread_id: str) -> tuple[dict[str, object] | None, bool]:
        with self.lock:
            entry = self.recent.get(thread_id) if thread_id else None
            if entry is None:
                return None, False
            already, entry["user_requested"] = entry["user_requested"], True
            return entry["request"], already

    # LLM: 认领后没能入队（队列满）时调用，让用户过一会儿还能再要一次。只改内存。
    # 函数用途: 撤回对某个会话最近完成那次活的认领。
    def release(self, thread_id: str) -> None:
        with self.lock:
            entry = self.recent.get(thread_id) if thread_id else None
            if entry is not None:
                entry["user_requested"] = False


# LLM: 两种落点共用：同一次运行的用户请求键固定为 "user:" + 原请求键（与自动总结的请求分开，两条路对同一次活键相同）；
#   重置时间与尝试次数，带上 requested_by 与截断脱敏后的重点。纯组装。
# 函数用途: 把一份备好的学习请求改成用户要求的版本。
def _user_request(request: dict[str, object], focus: str) -> dict[str, object]:
    return {**request, "request_key": request_key(f"user:{request['request_key']}"), "created_at": utc_now(), "attempts": 0,
            "requested_by": "user", "user_focus": bounded_text(focus, FOCUS_LIMIT_CHARS)}


# LLM: 有界表：放进去后超过上限就丢最早放进去的。只改内存。
# 函数用途: 往有界表里放一项。
def _bounded_put(table: dict, key: str, value: object) -> None:
    table.pop(key, None)
    table[key] = value
    while len(table) > _USER_REQUESTED_LIMIT_COUNT:
        table.pop(next(iter(table)))


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
        self._user = _UserRequests()

    # LLM: 这一轮正在做活（已升格的会话任务）时由 skill_summarize 调用：按会话任务 id 记标记，这个任务收尾时取走。不写文件。
    # 函数用途: 记下"这个会话任务收尾时要按用户要求总结"。
    def request_learning(self, task_id: str, focus: str) -> None:
        self._user.mark(str(task_id), str(focus or ""))

    # LLM: 只在会话任务完成时被收口调用。先按会话任务 id 取走用户标记；按自动总结同一规则（门槛 1 轮）备好这次的请求，再带上
    #   本轮召回的相关记忆，作为用户要求总结时的材料。有标记就按用户要求入队这份材料（不看最少工具轮数，其余结构化条件照旧）；
    #   没有标记就照常按门槛入队自动总结的请求（不带记忆，和原来逐字节一样）。最后（入队抛错也照做）把材料记成这个会话最近完成
    #   的那次活：按用户要求入队成功才记"已入队过"，队列满或入队出错都不记，用户还能再要（复审 3 轮）。不合格返回 ineligible；
    #   队列满时丢弃并记 dropped/QUEUE_FULL。副作用：写 requests/ 与账本、改内存里的两张表。
    # 函数用途: 收口时按结构化条件登记一次学习请求，返回 queued/duplicate/queue_full/ineligible。
    def enqueue_from_finalize(self, ctx: object) -> str:
        attrs = getattr(ctx, "task_attributes", None)
        focus = self._user.take_mark(str((attrs if isinstance(attrs, dict) else {}).get("conversation_task_id") or ""))
        prepared = build_learning_request(ctx, 1)
        material = None if prepared is None else {**prepared, "recalled_memories": recalled_memories(ctx)}
        outcome = "failed"
        try:
            if focus is not None:
                outcome = self._enqueue_requested(ctx, material, focus)
            else:
                request = build_learning_request(ctx, self.settings.min_tool_rounds)
                outcome = "ineligible" if request is None else self._enqueue(request)
        finally:
            if material is not None and material.get("thread_id"):
                self._user.remember(str(material["thread_id"]), material, focus is not None and outcome in _QUEUED)
        return outcome

    # LLM: 有用户标记的收尾：材料不合格（比如后台运行）时在自学习账本 events 里留一条丢弃事件（带 run_id，可追溯；复审小问题），
    #   合格就按用户要求入队。有写文件副作用。
    # 函数用途: 按用户要求把收尾的这次活入队。
    def _enqueue_requested(self, ctx: object, material: dict[str, object] | None, focus: str) -> str:
        if material is None:
            self.store.append_event(SkillLearningEvent(event=EVENT_DROPPED, code=CODE_USER_REQUEST_INELIGIBLE,
                                                       run_id=str(getattr(ctx, "run_id", "") or "")))
            return "ineligible"
        return self._enqueue(_user_request(material, focus))

    # LLM: 用户在做完之后的下一句说"总结一下"时由 skill_summarize 调用：认领这个会话最近完成的那次活备好的请求，按用户要求入队
    #   （和"正在做的活"那条路同一个用户请求键，不被自动总结的同一请求去重）。找不到返回 ("nothing_recent", None)；这次活已按
    #   用户要求入队过返回 ("already_requested", 请求)，不再重复。没入队成功（队列满或写入抛错）就撤回认领，抛错照常往外抛
    #   （复审 3 轮）。副作用：写 requests/ 与账本、改内存表。
    # 函数用途: 把这个会话刚做完的那次活交给自学习流水线，返回 (入队结果, 请求)。
    def summarize_recent(self, thread_id: str, focus: str) -> tuple[str, dict[str, object] | None]:
        prepared, already = self._user.claim_latest(str(thread_id or ""))
        if prepared is None:
            return "nothing_recent", None
        if already:
            return "already_requested", prepared
        request, outcome = _user_request(prepared, focus), "failed"
        try:
            outcome = self._enqueue(request)
        finally:
            if outcome not in _QUEUED:
                self._user.release(str(thread_id or ""))
        return outcome, request

    # LLM: 写一条请求；队列满时记 dropped/QUEUE_FULL。有写文件副作用。
    # 函数用途: 入队并在队列满时记账。
    def _enqueue(self, request: dict[str, object]) -> str:
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
            response = self._call_model(material)
        except CuratorModelStillRunningError:
            return SkillLearningRunResult(STATUS_BUSY, request_key=str(request.get("request_key") or ""))
        except Exception as exc:  # noqa: BLE001 - 供应商故障只影响这一条请求，按重试合同记账。
            return self._retry_or_drop(path, request, exc)
        event = self._decide(material, str(getattr(response, "text", "") or ""))
        return self._finish(path, request, event)

    # LLM: 后台线程没有前台的会话 ContextVar：按 owner_id + 请求键自绑宿主会话（同一请求的重试共用同一会话值、
    #   不冒用前台线程会话），bounded_call 的 copy_context 会把它带进供应商调用线程；退出即复位。
    # 函数用途: 发起一次无工具结构化总结调用。
    def _call_model(self, material: SkillLearningMaterial) -> object:
        session = "skill-learning:" + str(material.request.get("request_key") or "")
        with provider_session_scope((self.runtime.owner_id,), session):
            return call_backend_with_timeout(
                self.runtime.backend,
                prompt=skill_learning_prompt(material),
                response_schema=skill_learning_response_schema(),
                timeout_seconds=self.settings.timeout_seconds,
            )

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
        if attempts < MAX_REQUEST_ATTEMPT_COUNT:
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
#   快照条目确实指向 learned/<name>/SKILL.md 且磁盘 hash 等于登记值的自学 Skill，最多 UPDATABLE_COUNT 个。
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
    updatable = tuple(item for item in candidates if item is not None)[:UPDATABLE_COUNT]
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
    "FOCUS_LIMIT_CHARS",
    "STATUS_BUSY",
    "STATUS_DAILY_LIMIT",
    "STATUS_IDLE",
    "STATUS_RETRY",
    "SkillLearningRunResult",
    "SkillLearningRuntime",
    "SkillLearningService",
    "SkillLearningSettings",
]
