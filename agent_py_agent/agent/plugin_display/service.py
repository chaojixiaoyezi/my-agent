# LLM: Gateway 进程内唯一的插件展示服务。只读已有公开活动投影，经共用插件通道（plugin_channel）调用只读
#   display.render；通道负责（owner, 激活代次）连接、单在途、代次复核、超时、退避与空闲关闭。
#   不写安装表、不创建任务或审批、不向 TUI 暴露插件代码。修改时同步 PLUGIN_DISPLAY.md 与 test_plugin_display_service。
# 模块用途: 为 /client/plugin-panels 提供面板内容；插件出错、超时或被停用只影响它自己的面板。
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..conversation.agent_activity import _MAIN_ACTIVITY_LIVE_PHASES
from ..plugin_channel import (
    ERROR_BACKOFF_SECONDS,
    IDLE_CLOSE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    ChannelCall,
    ChannelConnection,
    PluginChannelPool,
    PluginChannelRevoked,
    RetireScope,
)
from .protocol import (
    DISPLAY_EXTENSION,
    DISPLAY_RENDER_METHOD,
    DISPLAY_VERSION,
    MAX_LINE_CHARS,
    normalize_display,
)

logger = logging.getLogger(__name__)

# 面板渲染超时秒数；沿用共用通道的请求超时（值不变），超时按失败处理并回退，防止插件卡死主界面。
RENDER_TIMEOUT_SECONDS = REQUEST_TIMEOUT_SECONDS
# 单次请求最多请求的面板数；防止一次请求拉起所有面板。
MAX_REQUESTED_PANEL_COUNT = 8
# 会话列表最多展示的行数；超出截断，保持面板可读。
MAX_SESSION_ROW_COUNT = 20


# LLM: 请求方只能按 (插件, 面板) 选择要看的面板；owner、线程与活动投影由 Gateway 解析后传入，不信任客户端字段。
# 类用途: 一次面板查询的输入。
@dataclass(frozen=True)
class PanelQuery:
    owner: object
    owner_key: str
    thread_key: str
    activity: dict
    requested: tuple[tuple[str, str], ...]
    # 会话列表按需读取（扫描会话文件较重），只有被订阅 sessions 主题的面板请求时才调用一次
    sessions: Callable[[], list[dict]] | None = None


# LLM: 结果与激活代次、面板和输入摘要绑定；换代或输入变化都视为过期，不跨代复用。
# 类用途: 保存某个面板最近一次渲染的结果。
@dataclass
class _PanelResult:
    activation_id: str
    input_digest: str
    state: str
    display: dict | None = None
    error: str = ""
    rendered_at: float = 0.0
    retry_after: float = 0.0


# LLM: 展示专属状态（待发合并、串行标志、能力声明）留在服务；连接与在途由共用通道负责。
# 类用途: 保存一个插件激活的展示待发槽。
@dataclass
class _DisplaySlot:
    running: bool = False
    capable: bool | None = None
    pending: dict[tuple[str, str], _PendingRender] = field(default_factory=dict)


# LLM: 待发按（会话, 面板）只留最新一份；owner 供通道启动客户端，digest 供结果新鲜度比对。
# 类用途: 保存一次待渲染任务的全部输入。
@dataclass(frozen=True)
class _PendingRender:
    owner: object
    thread_key: str
    panel_id: str
    kind: str
    topics: dict
    digest: str


# LLM: 只读请求级快照，避免每个面板重复解析 owner、活动与会话列表；sessions 在锁外读一次后共享。
# 类用途: 一次 panels 调用内的公共上下文。
@dataclass(frozen=True)
class _PanelContext:
    owner: object
    owner_key: str
    thread_key: str
    activity: dict
    active: dict
    sessions: list
    now: float


# LLM: 展示服务的可注入依赖；参数收成一个小数据类，避免构造函数随依赖增长而超标。
#   池由调用方注入（Gateway 建唯一一个挂在 server 上，面板与以后的事件中心共用）；
#   没注入池时才按 client_factory 自建，且只有自建的池由本服务在 close 时负责关闭。
# 类用途: 打包展示服务的外部依赖（安装表读取、客户端工厂、时钟、后台执行器、共用池）。
@dataclass(frozen=True)
class DisplayWiring:
    installations: Callable[[object], tuple] | None = None
    client_factory: Callable[[object, object], object] | None = None
    clock: Callable[[], float] = time.monotonic
    executor: ThreadPoolExecutor | None = None
    pool: PluginChannelPool | None = None


# LLM: 服务持有的都是可丢弃的展示缓存；Gateway 重启后从空开始，不影响任何执行事实。
# 类用途: 按请求返回面板内容，并在后台有界地刷新。
class PluginDisplayService:
    # LLM: 依赖全部收在 DisplayWiring 里显式注入，便于用替身插件做合同测试；默认值接真实安装表与插件客户端。
    #   池由调用方注入（Gateway 建唯一一个挂在 server 上，面板与以后的事件中心共用）；
    #   没注入时才自建，且只有自建的池由本服务在 close 时负责关闭。
    # 函数用途: 创建展示服务（不启动任何插件进程）。
    def __init__(self, *, wiring: DisplayWiring | None = None) -> None:
        self._wire(wiring or DisplayWiring())

    # LLM: 只做字段落位与自建池的判定，不启动进程；抽出来让 __init__ 保持一屏可读。
    # 函数用途: 把注入的依赖落到实例字段，必要时自建共用池。
    def _wire(self, wiring: DisplayWiring) -> None:
        self._installations = wiring.installations or _enabled_installations
        self._clock = wiring.clock
        self._executor = wiring.executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="plugin-display")
        self._lock = threading.Lock()
        self._owns_pool = wiring.pool is None
        self._pool = wiring.pool if wiring.pool is not None else PluginChannelPool(
            client_factory=wiring.client_factory or plugin_display_client, clock=wiring.clock)
        self._connections = self._pool.connections
        self._slots: dict[tuple[str, str], _DisplaySlot] = {}
        self._results: dict[tuple[str, str, str, str], _PanelResult] = {}

    # LLM: 只做内存判定与投递后台任务，不在请求线程里启动进程或等待插件；返回的是当前已有结果。
    #   交给共用池的有效集合用「这个 owner 全部已启用的激活」，不是只有面板的那部分——同一 owner 下
    #   已启用但没有面板的插件（只带事件的 v8 插件）的连接不归面板服务管，绝不能顺手摘掉。
    #   安装表读不到时（None）本轮不做任何回收，宁可留着连接，也不能把别人的连接全停掉。
    # 函数用途: 返回请求面板的最新内容，必要时安排后台刷新，并回收已停用插件的连接与缓存。
    def panels(self, query: PanelQuery) -> list[dict]:
        now = self._clock()
        # 回收的时间界要在读有效集合之前取：界之后（含读表期间）别的调用方新建的连接一律不碰
        seq_before = self._pool.current_seq()
        rows = self._installations(query.owner)
        active = {} if rows is None else {row.manifest.plugin_id: row for row in rows
                                          if getattr(row.manifest, "panels", ())}
        valid = None if rows is None else {row.activation.activation_id for row in rows}
        self._retire(query.owner_key, valid, now, seq_before)
        wanted = {topic for plugin_id, panel_id in query.requested[:MAX_REQUESTED_PANEL_COUNT]
                  for panel in getattr(getattr(active.get(plugin_id), "manifest", None), "panels", ())
                  if panel.id == panel_id for topic in panel.topics}
        # 在锁外读取会话列表，避免文件扫描阻塞其他面板查询
        sessions = _session_rows(query.sessions) if "sessions" in wanted else []
        context = _PanelContext(query.owner, query.owner_key, query.thread_key, query.activity, active, sessions, now)
        with self._lock:
            return [self._panel_payload(context, plugin_id, panel_id)
                    for plugin_id, panel_id in query.requested[:MAX_REQUESTED_PANEL_COUNT]]

    # LLM: 调用方必须已持有 self._lock；只做内存判定与投递后台任务，不在请求线程里启动进程或等待插件。
    # 函数用途: 组装单个面板的返回内容，必要时把渲染任务放进连接待发。
    def _panel_payload(self, context: _PanelContext, plugin_id: str, panel_id: str) -> dict:
        row = context.active.get(plugin_id)
        panel = next((item for item in getattr(getattr(row, "manifest", None), "panels", ())
                      if item.id == panel_id), None) if row is not None else None
        if row is None or panel is None:
            return {"plugin_id": plugin_id, "panel_id": panel_id, "state": "unavailable",
                    "error": "插件未启用或没有这个面板"}
        activation_id = row.activation.activation_id
        conn_key = (context.owner_key, activation_id)
        connection = self._pool.acquire(context.owner_key, row, context.now)
        slot = self._slot(conn_key)
        topics = project_topics(context.activity, panel.topics, context.sessions)
        digest = _digest(topics)
        result_key = (context.owner_key, activation_id, context.thread_key, panel_id)
        result = self._results.get(result_key)
        # 只有成功渲染的结果才算最新；错误结果只控制退避，退避期过后必须重试
        fresh = (result is not None and result.state == "ready" and result.input_digest == digest
                 and result.activation_id == activation_id)
        backing_off = result is not None and result.state == "error" and context.now < result.retry_after
        if slot.capable is False:
            return _payload(plugin_id, panel, "unavailable", error="插件未声明展示能力")
        if not fresh and not backing_off:
            job = _PendingRender(context.owner, context.thread_key, panel_id, panel.kind, topics, digest)
            self._queue_render(conn_key, slot, job)
        if result is None or result.activation_id != activation_id:
            return _payload(plugin_id, panel, "loading")
        state = result.state if fresh or result.state == "error" else "refreshing"
        return _payload(plugin_id, panel, state, display=result.display, error=result.error,
                        rendered_at=result.rendered_at)

    # LLM: 调用方必须已持有 self._lock；pending 按（会话, 面板）只留最新一份，running 保证一次只有一个 drain。
    # 函数用途: 把一次渲染登记为待发，必要时提交后台串行执行。
    def _queue_render(self, conn_key: tuple[str, str], slot: _DisplaySlot, job: _PendingRender) -> None:
        slot.pending[(job.thread_key, job.panel_id)] = job
        if not slot.running:
            slot.running = True
            self._executor.submit(self._drain, conn_key)

    # LLM: 调用方必须已持有 self._lock；槽按（owner, 激活编号）与连接一一对应。
    # 函数用途: 取得或创建某连接的展示待发槽。
    def _slot(self, conn_key: tuple[str, str]) -> _DisplaySlot:
        slot = self._slots.get(conn_key)
        if slot is None:
            slot = self._slots[conn_key] = _DisplaySlot()
        return slot

    # LLM: 停用、卸载或换代的激活立即移出缓存；空闲超时的连接关闭客户端但保留条目，重新查看时再启动。
    #   池是共用的：回收必须带"正面过期证据"——只摘自己管过（有展示槽）、且在本轮时间界 seq_before
    #   之前就存在的连接。只凭一份可能过时的 valid 去摘整个 owner 的连接会误伤别人刚为新启用激活
    #   建的连接；seq_before 之后建的连接不属于本次判定对象。
    #   valid 为 None 表示安装表读不到：这一轮什么都不回收，避免把整个 owner 的共用连接全停掉。
    #   连接表用池的加锁快照（owner_keys），绝不直接遍历活字典——后台渲染撤销或 B3 建/删连接会
    #   在迭代中途改字典，抛 RuntimeError: dictionary changed size during iteration。
    # 函数用途: 回收本服务不再需要或长时间无人查看的连接及其服务侧状态。
    def _retire(self, owner_key: str, valid: set[str] | None, now: float, seq_before: int) -> None:
        with self._lock:
            busy = {key for key, slot in self._slots.items() if slot.running}
            # 先按"回收前"的槽表算管理人集合，再清失效槽，否则失效激活会被误判成别人的连接
            managed = {key[1] for key in self._slots if key[0] == owner_key}
            if valid is not None:
                self._drop_stale_slots(owner_key, valid)
        if valid is not None:
            scope = RetireScope(managed=frozenset(managed), created_before=seq_before)
            self._pool.retire_stale(owner_key, valid, scope)
        for key in self._pool.owner_keys(owner_key):
            self._pool.close_idle(key, now, key in busy)

    # LLM: 调用方必须已持有 self._lock；只清服务侧状态，连接由池负责。
    # 函数用途: 删除已失效激活对应的待发槽与结果缓存。
    def _drop_stale_slots(self, owner_key: str, valid: set[str]) -> None:
        for key in [k for k in self._slots if k[0] == owner_key and k[1] not in valid]:
            del self._slots[key]
            for result_key in [k for k in self._results if k[:2] == key]:
                del self._results[result_key]

    # LLM: 后台线程逐个消费该连接最新输入；通道每次调用前后都复核激活仍是同一代，失效的结果一律丢弃。
    # 函数用途: 为一个插件激活串行执行待渲染的面板请求。
    def _drain(self, conn_key: tuple[str, str]) -> None:
        while True:
            item = self._next_job(conn_key)
            if item is None:
                return
            if not self._consume_job(conn_key, *item):
                return

    # LLM: 撤销时返回 False 让 _drain 结束；结果只在连接没被替换时才写回。
    # 函数用途: 渲染一个待发任务并按需写回结果，撤销时返回 False。
    def _consume_job(self, conn_key: tuple[str, str], connection: ChannelConnection,
                     slot: _DisplaySlot, job: _PendingRender) -> bool:
        outcome = self._render_outcome(connection, slot, job)
        if outcome is None:
            return False
        with self._lock:
            if self._pool.holds(conn_key, connection):
                self._results[(conn_key[0], conn_key[1], job.thread_key, job.panel_id)] = outcome
        return True

    # LLM: 撤销返回 None 交给 _drain 结束本轮；其它异常转成面板错误结果并记退避，不向主流程抛出。
    # 函数用途: 执行一次渲染并把它转成面板结果，撤销时返回 None。
    def _render_outcome(self, connection: ChannelConnection, slot: _DisplaySlot, job: _PendingRender):
        try:
            display = self._render(connection, job, slot)
            return _PanelResult(connection.key[1], job.digest, "ready", display=display, rendered_at=time.time())
        except PluginChannelRevoked:
            self._discard(connection.key)
            return None
        except Exception as exc:  # noqa: BLE001 插件故障只标记该面板错误，不能影响核心或其他插件
            # 只记激活编号、异常类型和错误码，便于事后区分连接/握手失败与描述不合规；不记插件输出正文
            logger.warning("插件展示渲染失败 activation=%s type=%s code=%s", connection.key[1], type(exc).__name__,
                           getattr(exc, "code", ""))
            return _PanelResult(connection.key[1], job.digest, "error", error=_error_text(exc),
                                rendered_at=time.time(), retry_after=self._clock() + ERROR_BACKOFF_SECONDS)

    # LLM: 取任务与 running 标志都在服务锁内完成；连接或槽已被回收时结束 drain。
    # 函数用途: 取出该连接下一个待渲染任务，并管理串行标志。
    def _next_job(self, conn_key: tuple[str, str]):
        with self._lock:
            connection = self._pool.get(conn_key)
            slot = self._slots.get(conn_key)
            if connection is None or slot is None or not slot.pending:
                self._idle_slot(slot)
                return None
            key, job = next(iter(slot.pending.items()))
            del slot.pending[key]
            return connection, slot, job

    # LLM: 只在服务锁内调用；槽已消失时无事可做。
    # 函数用途: 标记该槽没有在途渲染，允许下一次查询重新提交后台任务。
    def _idle_slot(self, slot: _DisplaySlot | None) -> None:
        if slot is not None:
            slot.running = False

    # LLM: 通道负责启动、代次复核、超时与在途；这里只做面板协议（能力声明、方法名、展示内容校验）。
    # 函数用途: 通过固定代次连接调用一次 display.render，并校验返回内容。
    def _render(self, connection: ChannelConnection, job: _PendingRender, slot: _DisplaySlot) -> dict:
        call = ChannelCall(owner=job.owner, method=DISPLAY_RENDER_METHOD,
                           params={"panel": job.panel_id, "topics": job.topics},
                           timeout=RENDER_TIMEOUT_SECONDS,
                           before_send=lambda client: _require_display_capability(slot, client))
        result = self._pool.request(connection, call)
        return normalize_display(job.kind, result.get("display") if isinstance(result, dict) else None)

    # LLM: 撤销时池已移除连接；这里只清服务侧状态，绝不写回结果。
    # 函数用途: 丢弃某连接的全部待发槽与结果缓存。
    def _discard(self, conn_key: tuple[str, str]) -> None:
        with self._lock:
            self._slots.pop(conn_key, None)
            for key in [k for k in self._results if k[:2] == conn_key]:
                del self._results[key]

    # LLM: 池是共用资源：注入进来的池由 Gateway 负责关闭，本服务只在自建池时关它，
    #   否则面板服务关闭会把事件中心还在用的连接一起停掉。
    # 函数用途: 关闭自建池并停止后台线程；注入的池不动。
    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()
        with self._lock:
            self._slots.clear()
            self._results.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)


# LLM: 能力检查必须在发送前做；未声明展示扩展的插件不发请求，只把展示槽标记为不可用。
#   异常按普通失败处理（服务写错误结果并退避），与面板原行为一致。
# 函数用途: 按插件握手声明校验展示能力，未声明时抛错并把展示槽标记为不可用。
def _require_display_capability(slot: _DisplaySlot, client: object) -> None:
    capabilities = getattr(client, "capabilities", None) or {}
    experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
    declared = experimental.get(DISPLAY_EXTENSION) if isinstance(experimental, dict) else None
    versions = declared.get("versions") if isinstance(declared, dict) else None
    slot.capable = isinstance(versions, list) and DISPLAY_VERSION in versions
    if not slot.capable:
        raise ValueError("插件未声明展示能力")


# LLM: 只读 ConversationAgentActivity.to_dict() 的公开有界字段，不含路径、正文、配置或身份编号；
#   run_state 只按宿主写入的确切阶段值（agent_activity 的活动阶段白名单）和计数推出，不解析模型文字。
#   context 只转发 context_usage 公开白名单中的数字和压缩次数，不含正文、路径或工具参数。
#   sessions 只含会话编号、时间、渠道和是否当前会话，由 Gateway 从本 owner 会话记录读出，不含标题或正文。
# 函数用途: 按面板订阅的主题裁剪公开活动投影，作为 display.render 的唯一输入。
def project_topics(activity: dict, topics: tuple[str, ...], sessions: list[dict] | None = None) -> dict:
    main = activity.get("main_activity") if isinstance(activity.get("main_activity"), dict) else {}
    subagents = activity.get("subagents") if isinstance(activity.get("subagents"), list) else []
    active_tasks = _int(activity.get("active_task_count"))
    phase = str(main.get("phase") or "")
    result: dict[str, object] = {}
    if "activity" in topics:
        text = str(main.get("activity") or "")
        result["activity"] = {
            "phase": phase,
            "activity": text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1] + "…",
            "started_at": _number(main.get("started_at")),
            "updated_at": _number(main.get("updated_at")),
            "active_task_count": active_tasks,
            "subagent_count": len(subagents),
            "compact_count": _int(activity.get("compact_count")),
        }
    if "run_state" in topics:
        if phase == "waiting_permission":
            state = "waiting"
        elif phase in _MAIN_ACTIVITY_LIVE_PHASES or active_tasks > 0:
            state = "working"
        else:
            state = "idle"
        result["run_state"] = {"state": state, "active_task_count": active_tasks, "subagent_count": len(subagents)}
    if "context" in topics:
        # 只转发宿主已清洗的上下文数字白名单（最近一次模型调用前的快照），缺快照时标记未知而不是补估算
        usage = activity.get("context_usage") if isinstance(activity.get("context_usage"), dict) else {}
        result["context"] = {
            "known": bool(usage),
            "compact_count": _int(activity.get("compact_count")),
            **{key: _int(usage.get(key)) for key in _CONTEXT_FIELDS},
            "estimated": usage.get("estimated") is True,
        }
    if "sessions" in topics:
        result["sessions"] = {"items": [dict(row) for row in (sessions or ())[:MAX_SESSION_ROW_COUNT]]}
    return result


# LLM: 提供方来自 Gateway 的只读会话列表；异常或坏行一律丢弃，只保留白名单字段，不让面板影响核心。
# 函数用途: 调用会话列表提供方并清洗成公开行。
def _session_rows(provider: Callable[[], list[dict]] | None) -> list[dict]:
    if provider is None:
        return []
    try:
        rows = provider()
    except Exception:  # noqa: BLE001 会话列表只用于展示，读取失败按空列表处理
        logger.warning("插件展示会话列表读取失败")
        return []
    clean = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and isinstance(row.get("session_id"), str):
            clean.append({"session_id": row["session_id"][:80], "updated_at": _number(row.get("updated_at")),
                          "created_at": _number(row.get("created_at")), "channel": str(row.get("channel") or "")[:40],
                          "current": row.get("current") is True})
    return clean[:MAX_SESSION_ROW_COUNT]


_CONTEXT_FIELDS = ("context_window_tokens", "compact_trigger_tokens", "current_tokens", "messages_tokens",
                   "runtime_guidance_tokens", "tool_schema_tokens")


# LLM: 摘要只用于判断输入是否变化，不作身份或持久键。
# 函数用途: 计算主题投影的稳定摘要。
def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# LLM: 面板声明的标题/类型来自已安装包描述，不来自插件运行时返回。
# 函数用途: 组装一个面板的返回结构。
def _payload(plugin_id: str, panel, state: str, *, display: dict | None = None, error: str = "",
             rendered_at: float = 0.0) -> dict:
    row = {"plugin_id": plugin_id, "panel_id": panel.id, "title": panel.title, "kind": panel.kind, "state": state}
    if display is not None:
        row["display"] = display
    if error:
        row["error"] = error
    if rendered_at:
        row["rendered_at"] = rendered_at
    return row


# LLM: 错误只暴露类别，不带插件输出正文、路径或堆栈；通道超时沿用 MCP_TIMEOUT 码。
# 函数用途: 把渲染异常转成简短的中文错误说明。
def _error_text(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    if code == "MCP_TIMEOUT":
        return "插件响应超时"
    if isinstance(exc, ValueError):
        return "插件返回的展示内容无效"
    return "插件展示暂时不可用"


# LLM: 默认安装读取只取启用且有激活代次的记录；表不可读时返回 None（不是空元组），
#   让调用方能区分"读表失败"与"确实没有启用的插件"，失败时绝不能当成空集合去回收共用连接。
# 函数用途: 读取 owner 当前启用的插件；读表失败时返回 None。
def _enabled_installations(owner) -> tuple | None:
    from ..plugin_install_store import PluginInstallStore

    try:
        return tuple(row for row in PluginInstallStore(owner).snapshot()
                     if row.enabled and row.activation is not None)
    except (OSError, ValueError):
        logger.warning("插件安装表读取失败，本轮回不回收任何共用连接")
        return None


# LLM: 复用插件业务连接的同一客户端类型与进程资源账，不另建通道；process_sandbox 沿配置 plugin_process_sandbox。
# 函数用途: 为一个固定激活创建插件客户端（不启动进程）。
def plugin_display_client(owner, installation, *, process_sandbox: bool = False):
    from ..plugin_runtime import PluginMCPClient

    return PluginMCPClient(owner, installation, process_sandbox=process_sandbox)


# LLM: 非整数或布尔一律视为 0，不从字符串猜数。
# 函数用途: 读取投影中的计数字段。
def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# LLM: 时间戳只接受有限数字，其余为 None。
# 函数用途: 读取投影中的时间字段。
def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None
