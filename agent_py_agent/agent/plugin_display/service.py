# LLM: Gateway 进程内唯一的插件展示服务。只读已有公开活动投影，经固定激活代次的插件连接调用只读 display.render；
#   每个 (owner, 激活) 至多一条连接、一个在途请求，输入只保留最新一份；每次渲染前后复核激活，失效即丢弃结果并关闭连接。
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
from .protocol import (
    DISPLAY_EXTENSION,
    DISPLAY_RENDER_METHOD,
    DISPLAY_VERSION,
    MAX_LINE_CHARS,
    normalize_display,
)

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_SECONDS = 3.0
IDLE_CLOSE_SECONDS = 120.0
ERROR_BACKOFF_SECONDS = 5.0
MAX_REQUESTED_PANELS = 8
MAX_SESSION_ROWS = 20


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


# LLM: 一个固定激活只对应一条连接；pending 只保留每个面板最新一份输入，保证队列有界。
# 类用途: 管理一个插件激活的展示连接与待渲染输入。
@dataclass
class _Connection:
    installation: object
    client: object | None = None
    last_used: float = 0.0
    running: bool = False
    capable: bool | None = None
    pending: dict[tuple[str, str], tuple[str, dict, str]] = field(default_factory=dict)


# LLM: 服务持有的都是可丢弃的展示缓存；Gateway 重启后从空开始，不影响任何执行事实。
# 类用途: 按请求返回面板内容，并在后台有界地刷新。
class PluginDisplayService:
    # LLM: 依赖全部显式注入，便于用替身插件做合同测试；默认值接真实安装表与插件客户端。
    # 函数用途: 创建展示服务（不启动任何插件进程）。
    def __init__(
        self,
        *,
        installations: Callable[[object], tuple] | None = None,
        client_factory: Callable[[object, object], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._installations = installations or _enabled_installations
        self._client_factory = client_factory or plugin_display_client
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="plugin-display")
        self._lock = threading.Lock()
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._results: dict[tuple[str, str, str, str], _PanelResult] = {}

    # LLM: 只做内存判定与投递后台任务，不在请求线程里启动进程或等待插件；返回的是当前已有结果。
    # 函数用途: 返回请求面板的最新内容，必要时安排后台刷新，并回收已停用插件的连接与缓存。
    def panels(self, query: PanelQuery) -> list[dict]:
        now = self._clock()
        active = {row.manifest.plugin_id: row for row in self._installations(query.owner)
                  if getattr(row.manifest, "panels", ())}
        to_stop = self._retire(query.owner_key, active, now)
        output: list[dict] = []
        wanted = {topic for plugin_id, panel_id in query.requested[:MAX_REQUESTED_PANELS]
                  for panel in getattr(getattr(active.get(plugin_id), "manifest", None), "panels", ())
                  if panel.id == panel_id for topic in panel.topics}
        # 在锁外读取会话列表，避免文件扫描阻塞其他面板查询
        sessions = _session_rows(query.sessions) if "sessions" in wanted else []
        with self._lock:
            for plugin_id, panel_id in query.requested[:MAX_REQUESTED_PANELS]:
                row = active.get(plugin_id)
                panel = next((item for item in getattr(row.manifest, "panels", ()) if item.id == panel_id), None) if row else None
                if row is None or panel is None:
                    output.append({"plugin_id": plugin_id, "panel_id": panel_id, "state": "unavailable",
                                   "error": "插件未启用或没有这个面板"})
                    continue
                activation_id = row.activation.activation_id
                conn_key = (query.owner_key, activation_id)
                connection = self._connections.get(conn_key)
                if connection is None:
                    connection = self._connections[conn_key] = _Connection(installation=row)
                connection.last_used = now
                topics = project_topics(query.activity, panel.topics, sessions)
                digest = _digest(topics)
                result_key = (query.owner_key, activation_id, query.thread_key, panel_id)
                result = self._results.get(result_key)
                # 只有成功渲染的结果才算最新；错误结果只控制退避，退避期过后必须重试
                fresh = (result is not None and result.state == "ready" and result.input_digest == digest
                         and result.activation_id == activation_id)
                backing_off = result is not None and result.state == "error" and now < result.retry_after
                if connection.capable is False:
                    output.append(_payload(plugin_id, panel, "unavailable", error="插件未声明展示能力"))
                    continue
                if not fresh and not backing_off:
                    connection.pending[(query.thread_key, panel_id)] = (panel.kind, topics, digest)
                    if not connection.running:
                        connection.running = True
                        self._executor.submit(self._drain, conn_key, query.owner)
                if result is None or result.activation_id != activation_id:
                    output.append(_payload(plugin_id, panel, "loading"))
                else:
                    state = result.state if fresh or result.state == "error" else "refreshing"
                    output.append(_payload(plugin_id, panel, state, display=result.display,
                                           error=result.error, rendered_at=result.rendered_at))
        for client in to_stop:
            _stop(client)
        return output

    # LLM: 停用、卸载或换代的激活立即移出缓存；空闲超时的连接关闭但保留结果，重新查看时再启动。
    # 函数用途: 回收不再有效或长时间无人查看的连接，返回需要在锁外关闭的客户端。
    def _retire(self, owner_key: str, active: dict, now: float) -> list:
        valid = {row.activation.activation_id for row in active.values()}
        stop = []
        with self._lock:
            for key, connection in list(self._connections.items()):
                if key[0] != owner_key:
                    continue
                if key[1] not in valid:
                    del self._connections[key]
                    for result_key in [k for k in self._results if k[:2] == key]:
                        del self._results[result_key]
                    if connection.client is not None:
                        stop.append(connection.client)
                elif (connection.client is not None and not connection.running
                      and now - connection.last_used > IDLE_CLOSE_SECONDS):
                    stop.append(connection.client)
                    connection.client = None
        return stop

    # LLM: 后台线程逐个消费该连接最新输入；每次调用前后都复核激活仍是同一代，失效的结果一律丢弃。
    # 函数用途: 为一个插件激活串行执行待渲染的面板请求。
    def _drain(self, conn_key: tuple[str, str], owner: object) -> None:
        while True:
            with self._lock:
                connection = self._connections.get(conn_key)
                if connection is None or not connection.pending:
                    if connection is not None:
                        connection.running = False
                    return
                (thread_key, panel_id), (kind, topics, digest) = next(iter(connection.pending.items()))
                del connection.pending[(thread_key, panel_id)]
            result_key = (conn_key[0], conn_key[1], thread_key, panel_id)
            try:
                display = self._render(connection, owner, panel_id, topics, kind)
                outcome = _PanelResult(conn_key[1], digest, "ready", display=display, rendered_at=time.time())
            except _Revoked:
                with self._lock:
                    self._connections.pop(conn_key, None)
                    for key in [k for k in self._results if k[:2] == conn_key]:
                        del self._results[key]
                _stop(connection.client)
                return
            except Exception as exc:  # noqa: BLE001 插件故障只标记该面板错误，不能影响核心或其他插件
                # 只记激活编号、异常类型和错误码，便于事后区分连接/握手失败与描述不合规；不记插件输出正文
                logger.warning("插件展示渲染失败 activation=%s type=%s code=%s", conn_key[1], type(exc).__name__,
                               getattr(exc, "code", ""))
                outcome = _PanelResult(conn_key[1], digest, "error", error=_error_text(exc),
                                       rendered_at=time.time(), retry_after=self._clock() + ERROR_BACKOFF_SECONDS)
            with self._lock:
                if self._connections.get(conn_key) is connection:
                    self._results[result_key] = outcome

    # LLM: 连接惰性启动；未协商展示扩展的插件记为不可用，不回退到工具调用等其他通道。
    # 函数用途: 通过固定代次连接调用一次 display.render，并校验返回内容。
    def _render(self, connection: _Connection, owner: object, panel_id: str, topics: dict, kind: str) -> dict:
        activation_ref = getattr(connection.client, "activation_ref", None)
        if connection.client is None:
            connection.client = self._client_factory(owner, connection.installation)
            activation_ref = connection.client.activation_ref
            require_settled = getattr(connection.client, "require_settled_previous_resources", None)
            if callable(require_settled):
                require_settled()
        _require_current(activation_ref, connection.installation)
        transport = connection.client.start()
        experimental = (connection.client.capabilities or {}).get("experimental")
        declared = experimental.get(DISPLAY_EXTENSION) if isinstance(experimental, dict) else None
        versions = declared.get("versions") if isinstance(declared, dict) else None
        connection.capable = isinstance(versions, list) and DISPLAY_VERSION in versions
        if not connection.capable:
            raise ValueError("插件未声明展示能力")
        result = transport.request(
            DISPLAY_RENDER_METHOD, {"panel": panel_id, "topics": topics},
            timeout=RENDER_TIMEOUT_SECONDS,
            authority_check=lambda: _require_current(activation_ref, connection.installation),
        )
        display = normalize_display(kind, result.get("display") if isinstance(result, dict) else None)
        _require_current(activation_ref, connection.installation)
        return display

    # LLM: 只关闭本服务自己的连接；Gateway 退出时调用，未确认的清理留在原进程资源账。
    # 函数用途: 关闭全部展示连接并停止后台线程。
    def close(self) -> None:
        with self._lock:
            clients = [conn.client for conn in self._connections.values() if conn.client is not None]
            self._connections.clear()
            self._results.clear()
        for client in clients:
            _stop(client)
        self._executor.shutdown(wait=False, cancel_futures=True)


# LLM: 激活失效（停用、卸载、换代或表不可读）一律按撤销处理；不追随新代次。
# 类用途: 标记本次渲染因撤销而作废。
class _Revoked(Exception):
    pass


# LLM: 只读安装表当前快照；activation 必须仍是同一对象内容，不能用插件 ID 猜代次。
# 函数用途: 核对插件激活仍然有效且未换代，否则抛出撤销。
def _require_current(activation_ref, installation) -> None:
    try:
        current = activation_ref.require()
    except (OSError, ValueError) as exc:
        raise _Revoked() from exc
    if getattr(current, "activation", None) != installation.activation:
        raise _Revoked()


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
        result["sessions"] = {"items": [dict(row) for row in (sessions or ())[:MAX_SESSION_ROWS]]}
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
    return clean[:MAX_SESSION_ROWS]


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


# LLM: 错误只暴露类别，不带插件输出正文、路径或堆栈。
# 函数用途: 把渲染异常转成简短的中文错误说明。
def _error_text(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    if code == "MCP_TIMEOUT":
        return "插件响应超时"
    if isinstance(exc, ValueError):
        return "插件返回的展示内容无效"
    return "插件展示暂时不可用"


# LLM: 关闭失败不抛出，未确认的清理由原进程资源账保留。
# 函数用途: 尽力关闭一个展示连接。
def _stop(client) -> None:
    if client is None:
        return
    try:
        client.stop()
    except Exception:  # noqa: BLE001 清理未确认留在原资源账，不能影响面板查询
        logger.warning("插件展示连接关闭未确认")


# LLM: 默认安装读取只取启用且有激活代次的记录；表不可读时视为没有可用面板。
# 函数用途: 读取 owner 当前启用的插件。
def _enabled_installations(owner) -> tuple:
    from ..plugin_install_store import PluginInstallStore

    try:
        return tuple(row for row in PluginInstallStore(owner).snapshot()
                     if row.enabled and row.activation is not None)
    except (OSError, ValueError):
        return ()


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
