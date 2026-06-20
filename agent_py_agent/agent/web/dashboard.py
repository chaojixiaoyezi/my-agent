"""Web 仪表盘(Phase 3,自建 stdlib http.server,零外部依赖)。

学 claw web/dashboard.py 的"瘦客户端只渲染状态、权威在别处"概念,但实现取舍按用户原则
"能自建就自建":my-agent 本就有自建的 stdlib `ThreadingHTTPServer`(gateway_parts/http_service.py),
故**不引 FastAPI**,这里同样用 stdlib http.server 自建一个**独立只读仪表盘**(新端口,不碰 gateway
热路径)。安全默认:默认仅回环;非回环绑定必须配 admin token,
缺 token 则拒绝公网暴露。

数据源用依赖注入(DashboardSources 提供 callable),便于测试 + 解耦真实运行时。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


@dataclass
class DashboardSources:
    """仪表盘数据源(注入 callable;默认空 → 渲染空盘,真实接线在 wiring 层提供读真实状态的函数)。"""

    status: Callable[[], dict[str, Any]] = lambda: {}
    tasks: Callable[[], list[dict[str, Any]]] = lambda: []
    sessions: Callable[[], list[dict[str, Any]]] = lambda: []


def build_dashboard_payload(sources: DashboardSources) -> dict[str, Any]:
    """聚合状态/任务/会话为仪表盘 JSON(纯函数,读源失败各自降级,绝不整盘崩)。"""

    def _safe(fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except Exception:
            return default

    status = _safe(sources.status, {})
    tasks = _safe(sources.tasks, [])
    sessions = _safe(sources.sessions, [])
    task_list = tasks if isinstance(tasks, list) else []
    return {
        "status": status if isinstance(status, dict) else {},
        "tasks": task_list,
        "task_counts": _count_by(task_list, "status"),
        "sessions": sessions if isinstance(sessions, list) else [],
        "session_count": len(sessions if isinstance(sessions, list) else []),
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        val = str(row.get(key, "")) if isinstance(row, dict) else ""
        counts[val] = counts.get(val, 0) + 1
    return counts


# 极简内嵌前端:轮询 /api/dashboard 渲染,无前端构建步骤、无外部 JS(自包含、离线可用)。
_DASHBOARD_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>my-agent 仪表盘</title><style>
body{font-family:-apple-system,system-ui,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}
h1{font-size:18px}h2{font-size:14px;color:#8ab4f8;margin-top:20px}
.card{background:#1a1d24;border-radius:8px;padding:12px 16px;margin:8px 0}
pre{white-space:pre-wrap;word-break:break-all;font-size:12px}
.tag{display:inline-block;background:#2a2f3a;border-radius:4px;padding:1px 8px;margin:2px;font-size:12px}
</style></head><body>
<h1>my-agent 仪表盘 <span id="ts" style="font-size:12px;color:#888"></span></h1>
<div class="card"><h2>状态</h2><pre id="status">加载中…</pre></div>
<div class="card"><h2>任务 <span id="taskcounts"></span></h2><pre id="tasks"></pre></div>
<div class="card"><h2>会话 <span id="sesscount"></span></h2><pre id="sessions"></pre></div>
<script>
async function refresh(){
 try{const r=await fetch('api/dashboard'+location.search);const d=await r.json();
 document.getElementById('status').textContent=JSON.stringify(d.status,null,2);
 document.getElementById('tasks').textContent=JSON.stringify(d.tasks,null,2);
 document.getElementById('sessions').textContent=JSON.stringify(d.sessions,null,2);
 document.getElementById('taskcounts').innerHTML=Object.entries(d.task_counts||{}).map(([k,v])=>`<span class="tag">${k||'?'}: ${v}</span>`).join('');
 document.getElementById('sesscount').textContent='('+(d.session_count||0)+')';
 document.getElementById('ts').textContent=new Date().toLocaleTimeString();
 }catch(e){document.getElementById('status').textContent='刷新失败: '+e}
}
refresh();setInterval(refresh,5000);
</script></body></html>"""


@dataclass
class DashboardConfig:
    port: int = 8765
    host: str = "127.0.0.1"  # 默认仅回环
    admin_token: str = ""  # 非回环绑定必须配;缺则拒绝公网暴露(fail-closed)


def _authorized(handler: BaseHTTPRequestHandler, config: DashboardConfig) -> bool:
    # 回环地址不强制 token;非回环必须带正确 admin token(header 或 ?token=)。
    client = handler.client_address[0] if handler.client_address else ""
    if client in ("127.0.0.1", "::1", "localhost") and not config.admin_token:
        return True
    if not config.admin_token:
        return False  # 公网暴露但没配 token → 一律拒绝(fail-closed)
    supplied = handler.headers.get("X-Admin-Token", "")
    if not supplied and "?" in handler.path:
        from urllib.parse import parse_qs, urlsplit

        supplied = (parse_qs(urlsplit(handler.path).query).get("token") or [""])[0]
    return bool(supplied) and supplied == config.admin_token


def make_handler(sources: DashboardSources, config: DashboardConfig) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # BaseHTTPRequestHandler 日志 override 约定
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            if not _authorized(self, config):
                self._send(403, b'{"error":"forbidden"}', "application/json; charset=utf-8")
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/dashboard", "/index.html"):
                self._send(200, _DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/dashboard":
                payload = json.dumps(build_dashboard_payload(sources), ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

    return DashboardHandler


@dataclass
class DashboardServer:
    sources: DashboardSources
    config: DashboardConfig = field(default_factory=DashboardConfig)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    def start(self) -> int:
        if self.config.host not in ("127.0.0.1", "::1", "localhost") and not self.config.admin_token:
            raise PermissionError("非回环绑定必须配 admin_token(fail-closed,拒绝无鉴权公网暴露)")
        handler = make_handler(self.sources, self.config)
        self._server = ThreadingHTTPServer((self.config.host, self.config.port), handler)
        self.config.port = self._server.server_address[1]  # port=0 时回填实际端口
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.config.port

    def stop(self, timeout: float = 3.0) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
