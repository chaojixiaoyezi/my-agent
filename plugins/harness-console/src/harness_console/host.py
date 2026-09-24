# LLM: 宿主只读 API 的插件侧客户端。地址与令牌只从宿主注入的 MY_AGENT_HOST_API_URL / MY_AGENT_HOST_API_TOKEN 读取，
#   令牌只放在请求头 X-Plugin-Host-Token，绝不进入网页、工具结果或日志。结果做 1 秒缓存，多窗口不放大宿主请求；
#   403 表示插件已停用/换代，令牌不会复活，所以记住失效后不再请求。副作用：向本机回环地址发 HTTP 请求。
# 模块用途: 调宿主 API 并把公开投影整理成网页 /api/state 需要的白名单结构。

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

URL_ENV = "MY_AGENT_HOST_API_URL"
TOKEN_ENV = "MY_AGENT_HOST_API_TOKEN"
CACHE_SECONDS = 1.0
UNAVAILABLE = "宿主 API 不可用（直连模式或未启用 Gateway 服务）"
REVOKED = "插件已停用或令牌失效"
FAILED = "宿主 API 暂时无法访问"
_THREAD_KEYS = ("thread_id", "title", "status", "updated_at", "compact_generation")
_PLUGIN_KEYS = ("plugin_id", "version", "enabled", "summary")
_ACTIVITY_KEYS = ("phase", "activity", "started_at", "updated_at", "active_task_count", "subagent_count")
_CONTEXT_KEYS = ("known", "compact_count", "context_window_tokens", "compact_trigger_tokens", "current_tokens",
                 "messages_tokens", "runtime_guidance_tokens", "tool_schema_tokens", "estimated")


# LLM: 同一实例在服务多个浏览器窗口的请求线程间共享；lock 同时保护缓存和串行化对宿主的请求。
# 类用途: 带短缓存和失效记忆的宿主只读 API 客户端。
class HostClient:
    # LLM: 构造时冻结环境里的地址与令牌；显式禁用环境代理，回环请求不能被代理截走。
    # 函数用途: 读取宿主 API 地址/令牌并准备缓存。
    def __init__(self, environ: dict | None = None):
        environ = os.environ if environ is None else environ
        self.url, self.token = environ.get(URL_ENV, ""), environ.get(TOKEN_ENV, "")
        self.lock = threading.Lock()
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.revoked = False
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 函数用途: 宿主 API 是否已注入（直连模式下为 False）。
    @property
    def available(self) -> bool:
        return bool(self.url and self.token)

    # LLM: 返回 (状态, 数据)；状态取 ok/unavailable/revoked/error。同键 1 秒内直接返回缓存。副作用：可能发一次 HTTP 请求。
    # 函数用途: 按主题查询宿主 API，带缓存与失效处理。
    def query(self, topics: tuple[str, ...], thread_id: str | None = None) -> tuple[str, dict]:
        if not self.available:
            return "unavailable", {}
        key = (topics, thread_id)
        with self.lock:
            if self.revoked:
                return "revoked", {}
            cached = self.cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
                return "ok", cached[1]
            status, data = self._post(topics, thread_id)
            if status == "revoked":
                self.revoked = True
            elif status == "ok":
                self.cache = {k: v for k, v in self.cache.items() if time.monotonic() - v[0] < CACHE_SECONDS}
                self.cache[key] = (time.monotonic(), data)
            return status, data

    # LLM: 只把 403 当作令牌失效；其它 HTTP/网络/JSON 错误是暂时失败，页面继续轮询。
    # 函数用途: 向宿主 API 发一次 POST 请求并解析 JSON。
    def _post(self, topics: tuple[str, ...], thread_id: str | None) -> tuple[str, dict]:
        body = {"topics": list(topics)}
        if thread_id is not None:
            body["thread_id"] = thread_id
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json", "X-Plugin-Host-Token": self.token})
        try:
            with self.opener.open(request, timeout=5) as response:
                data = json.loads(response.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            return ("revoked" if exc.code == 403 else "error"), {}
        except (OSError, ValueError):
            return "error", {}
        return ("ok", data) if isinstance(data, dict) and data.get("ok") is True else ("error", {})


# LLM: 页面每次轮询调用；没有指定线程时选第一条（宿主按最近排序）。只拷贝白名单字段，宿主以后多给的字段不会透传到浏览器。
# 函数用途: 组装网页 /api/state 的完整状态。
def console_state(client: HostClient, thread_id: str | None, poll_seconds: int) -> dict:
    state: dict = {"host": {"status": "ok", "message": ""}, "gateway": None, "owner_loaded": False, "threads": [],
                   "selected": None, "run": None, "context": None, "plugins": [], "at": time.time(),
                   "poll_seconds": poll_seconds}
    status, data = client.query(("threads", "plugins", "gateway"))
    if status != "ok":
        state["host"] = {"status": status, "message": {"unavailable": UNAVAILABLE, "revoked": REVOKED}.get(status, FAILED)}
        return state
    threads = [_pick(row, _THREAD_KEYS) for row in data.get("threads") or () if isinstance(row, dict)]
    gateway = data.get("gateway")
    state.update(threads=threads, owner_loaded=bool(data.get("owner_loaded")),
                 gateway=_pick(gateway, ("pid", "port")) if isinstance(gateway, dict) else None,
                 plugins=[_pick(row, _PLUGIN_KEYS) for row in data.get("plugins") or () if isinstance(row, dict)])
    known = {row.get("thread_id") for row in threads}
    selected = thread_id if thread_id in known else (threads[0].get("thread_id") if threads else None)
    state["selected"] = selected
    if selected:
        _add_activity(client, state, selected)
    return state


# LLM: activity 失败只影响主区，不清掉已拿到的线程/插件列表；403 仍整体标记失效。
# 函数用途: 为选中线程补运行状态与上下文用量。
def _add_activity(client: HostClient, state: dict, thread_id: str) -> None:
    status, data = client.query(("activity",), thread_id)
    if status != "ok":
        if status == "revoked":
            state["host"] = {"status": status, "message": REVOKED}
        return
    activity = data.get("activity") if isinstance(data.get("activity"), dict) else {}
    run_state = activity.get("run_state") if isinstance(activity.get("run_state"), dict) else {}
    main = activity.get("activity") if isinstance(activity.get("activity"), dict) else {}
    context = activity.get("context") if isinstance(activity.get("context"), dict) else {}
    state["run"] = {"state": str(run_state.get("state") or "idle"), **_pick(main, _ACTIVITY_KEYS)} if activity else None
    state["context"] = _pick(context, _CONTEXT_KEYS) if context else None


# LLM: 纯函数；只接受 JSON 标量，嵌套对象一律丢弃。
# 函数用途: 从一行宿主数据里只取白名单字段。
def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row[key] for key in keys if isinstance(row.get(key), (str, int, float, bool))}
