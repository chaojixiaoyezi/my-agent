from __future__ import annotations

"""Minimal real LSP JSON-RPC client with lazy, sandboxed language servers."""

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolAvailability, ToolExecutionResult, ToolSpec
from .sandbox import SandboxUnavailable
from .shell import _sandbox_exec, _sandbox_write_roots, _subprocess_text_env

_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_NOTIFICATIONS = 256
_BLOCKED_METHODS = frozenset({"initialize", "initialized", "shutdown", "exit"})


class LspProtocolError(RuntimeError):
    pass


def _initialize_params(root: Path, initialization_options: dict[str, Any]) -> dict[str, Any]:
    """Declare only client features implemented by this minimal LSP transport."""
    return {
        "processId": os.getpid(),
        "rootPath": str(root),
        "rootUri": root.as_uri(),
        "workspaceFolders": [{"name": root.name or "workspace", "uri": root.as_uri()}],
        "initializationOptions": dict(initialization_options),
        "capabilities": {
            "workspace": {
                "configuration": False,
                "workspaceFolders": False,
            },
            "textDocument": {
                "synchronization": {
                    "dynamicRegistration": False,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                    "didSave": False,
                },
                "publishDiagnostics": {
                    "relatedInformation": True,
                    "tagSupport": {"valueSet": [1, 2]},
                    "versionSupport": False,
                    "codeDescriptionSupport": True,
                    "dataSupport": False,
                },
                "hover": {
                    "dynamicRegistration": False,
                    "contentFormat": ["markdown", "plaintext"],
                },
                "definition": {"dynamicRegistration": False, "linkSupport": True},
                "references": {"dynamicRegistration": False},
                "documentSymbol": {
                    "dynamicRegistration": False,
                    "hierarchicalDocumentSymbolSupport": True,
                },
            },
            "general": {"positionEncodings": ["utf-16"]},
        },
        "clientInfo": {"name": "my-agent", "version": "0.3.0"},
    }


@dataclass
class LspClient:
    name: str
    command: list[str]
    root: Path
    owner_home: str = ""
    write_roots: tuple[Path, ...] | None = None
    timeout: float = 20.0
    initialization_options: dict[str, Any] = field(default_factory=dict)
    process: subprocess.Popen | None = None
    stderr_tail: str = ""
    _next_id: int = 0
    _pending: dict[int, queue.Queue] = field(default_factory=dict)
    _notifications: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _write_lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        command_text = shlex.join(self.command)
        exec_arg, use_shell = _sandbox_exec(
            command_text,
            self.root,
            self.owner_home,
            write_roots=self.write_roots,
        )
        self.process = subprocess.Popen(
            exec_arg,
            shell=use_shell,
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            env=_subprocess_text_env(self.owner_home),
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        self._request(
            "initialize",
            _initialize_params(self.root, self.initialization_options),
        )
        self.notify("initialized", {})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if method in _BLOCKED_METHODS or method.startswith("$/cancelRequest"):
            raise LspProtocolError(f"LSP lifecycle method is managed internally: {method}")
        self.start()
        return self._request(method, params)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.start()
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def notifications(self, *, method: str = "") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._notifications)
        if method:
            items = [item for item in items if item.get("method") == method]
        return items

    def status(self) -> dict[str, Any]:
        process = self.process
        return {
            "name": self.name,
            "status": "stopped" if process is None else ("running" if process.poll() is None else "exited"),
            "pid": process.pid if process is not None else None,
            "exit_code": process.poll() if process is not None else None,
            "notifications": len(self._notifications),
            "stderr_tail": self.stderr_tail,
        }

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("shutdown", None)
                self._send({"jsonrpc": "2.0", "method": "exit"})
                process.wait(timeout=2)
            except Exception:
                self._terminate_process(process)
        self.process = None

    def _request(self, method: str, params: Any) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = response_queue.get(timeout=max(0.1, self.timeout))
        except queue.Empty as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise LspProtocolError(f"LSP request timed out method={method!r}") from exc
        if isinstance(response, Exception):
            raise response
        if response.get("error") is not None:
            raise LspProtocolError(
                f"LSP request failed method={method!r} error={json.dumps(response['error'], ensure_ascii=False)}"
            )
        return response.get("result")

    def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise LspProtocolError("LSP server is not running")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > _MAX_MESSAGE_BYTES:
            raise LspProtocolError(f"LSP message exceeds {_MAX_MESSAGE_BYTES} bytes")
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                process.stdin.write(framed)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LspProtocolError(f"LSP write failed: {exc}") from exc

    def _read_loop(self) -> None:
        process = self.process
        stream = process.stdout if process is not None else None
        if stream is None:
            return
        try:
            for payload in _iter_lsp_messages(stream):
                self._route_message(payload)
        except Exception as exc:
            self._fail_pending(LspProtocolError(f"LSP read failed: {type(exc).__name__}: {exc}"))
        finally:
            self._fail_pending(LspProtocolError("LSP server stream closed"))

    def _stderr_loop(self) -> None:
        process = self.process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self.stderr_tail = (self.stderr_tail + text)[-16_000:]

    def _route_message(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        if isinstance(request_id, int):
            with self._lock:
                response_queue = self._pending.pop(request_id, None)
            if response_queue is not None:
                response_queue.put(payload)
            return
        if payload.get("method"):
            with self._lock:
                self._notifications.append(payload)
                del self._notifications[:-_MAX_NOTIFICATIONS]

    def _fail_pending(self, exc: Exception) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            try:
                response_queue.put_nowait(exc)
            except queue.Full:
                pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                pass
        except OSError:
            pass


def _read_lsp_message(stream) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        key, separator, value = line.decode("ascii", errors="replace").partition(":")
        if separator:
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise LspProtocolError("invalid LSP Content-Length") from exc
    if length <= 0 or length > _MAX_MESSAGE_BYTES:
        raise LspProtocolError(f"invalid LSP message length: {length}")
    body = stream.read(length)
    if len(body) != length:
        raise LspProtocolError("truncated LSP message")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise LspProtocolError("LSP message root must be an object")
    return payload


def _iter_lsp_messages(stream):
    while (payload := _read_lsp_message(stream)) is not None:
        yield payload


class LspManager:
    def __init__(self, configs: dict[str, Any], root: Path, owner_home: str = ""):
        self.configs = dict(configs or {})
        self.root = root
        self.owner_home = owner_home
        self.clients: dict[str, LspClient] = {}

    @staticmethod
    def _scope_key(write_roots: tuple[Path, ...] | None) -> tuple[str, ...] | None:
        if write_roots is None:
            return None
        return tuple(sorted({str(root.expanduser().resolve(strict=False)) for root in write_roots}))

    def client(self, name: str, write_roots: tuple[Path, ...] | None = None) -> LspClient:
        scope_key = self._scope_key(write_roots)
        current = self.clients.get(name)
        if current is not None and self._scope_key(current.write_roots) == scope_key:
            return current
        if current is not None:
            current.close()
        config = self.configs.get(name)
        if not isinstance(config, dict):
            raise LspProtocolError(f"LSP server is not configured: {name}")
        command = str(config.get("command") or "").strip()
        args = config.get("args") or []
        initialization_options = config.get("initialization_options") or {}
        if not command or not isinstance(args, list) or not isinstance(initialization_options, dict):
            raise LspProtocolError(f"invalid LSP server config: {name}")
        client = LspClient(
            name=name,
            command=[command, *[str(item) for item in args]],
            root=self.root,
            owner_home=self.owner_home,
            write_roots=write_roots,
            timeout=float(config.get("timeout") or 20),
            initialization_options=dict(initialization_options),
        )
        self.clients[name] = client
        return client

    def status(self) -> dict[str, Any]:
        return {
            "configured": sorted(self.configs),
            "clients": {name: client.status() for name, client in sorted(self.clients.items())},
        }

    def close_all(self) -> None:
        for client in list(self.clients.values()):
            client.close()
        self.clients.clear()


# LLM: LSP 只有管理员至少配置一个 server 时才进入工具面；具体进程仍在首次 action 时惰性启动。
# 类用途: 通过受沙箱约束的真实 Language Server 提供诊断、定义、引用和请求能力。
class LspTool(BaseTool):
    def __init__(
        self,
        root: Path,
        workspace_roots: list[Path],
        owner_home: str,
        configs: dict[str, Any] | None,
    ):
        self.root = root.resolve()
        self.workspace_roots = [path.resolve() for path in workspace_roots]
        self.manager = LspManager(dict(configs or {}), self.root, owner_home)
        configured_servers = sorted(self.manager.configs)
        server_schema: dict[str, Any] = {"type": "string"}
        if configured_servers:
            server_schema["enum"] = configured_servers
        self.spec = ToolSpec(
            name="lsp",
            category="code",
            effect="mutating",
            promotes_task=True,
            requires_idempotency=True,
            description="通过管理员配置的真实 Language Server 执行 JSON-RPC 请求、打开文档和读取诊断。",
            use_cases=[
                "像代码编辑器一样检查类型错误和代码诊断",
                "查定义、引用、hover、符号或类型信息",
                "打开源码后读取 language server 诊断",
            ],
            avoid_when=["只需文本搜索时使用 search_text", "未配置对应 language server 时"],
            keywords=[
                "lsp", "language server", "definition", "references", "hover", "diagnostics",
                "代码诊断", "代码编辑器", "类型检查", "类型错误", "编辑器诊断", "ts", "typescript",
            ],
            parameters={
                "action": "status/request/open_document/diagnostics/close",
                "server": "lsp_servers 中的管理员配置名",
                "method": "request 的 LSP 方法",
                "params": "request 的 JSON object 参数",
                "path": "open_document 的工作区文件路径",
                "language_id": "open_document 的 LSP languageId",
            },
            parameter_schema={
                "action": {"type": "string", "enum": ["status", "request", "open_document", "diagnostics", "close"]},
                "server": server_schema,
                "method": {"type": "string"},
                "params": {"type": "object"},
                "path": {"type": "string"},
                "language_id": {"type": "string"},
            },
            required_parameters=["action"],
            examples=[
                '{"tool":"lsp","action":"status"}',
                '{"tool":"lsp","action":"request","server":"python","method":"textDocument/hover","params":{"textDocument":{"uri":"file:///workspace/app.py"},"position":{"line":0,"character":1}}}',
            ],
        )

    # LLM: 配置检查不启动 server、不探测文件系统命令，避免仅渲染 Schema 就产生进程副作用。
    # 函数用途: 没有任何 LSP server 配置时从本轮工具快照中隐藏 lsp。
    def availability(self) -> ToolAvailability:
        if self.manager.configs:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("管理员尚未配置任何 lsp_servers")

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            return self._execute_action(params)
        except SandboxUnavailable as exc:
            return self._error("SANDBOX_UNAVAILABLE", str(exc))
        except (LspProtocolError, OSError, ValueError) as exc:
            return self._error("TOOL_UNAVAILABLE", str(exc))

    def _execute_action(self, params: dict[str, Any]) -> ToolExecutionResult:
        action = str(params.get("action") or "").strip().lower()
        if action == "status":
            return self._ok(self.manager.status())
        server = self._server_name(params)
        if not server:
            configured = sorted(self.manager.configs)
            suffix = f"；configured={configured}" if configured else "；当前没有已配置 server"
            return self._error("TOOL_INVALID_ARGUMENTS", "该 action 需要 server" + suffix)
        client = self.manager.client(server, _sandbox_write_roots(params))
        handlers = {
            "open_document": lambda: self._open_document(client, server, params),
            "diagnostics": lambda: self._diagnostics(client, server),
            "close": lambda: self._close_client(client, server),
        }
        if action == "request":
            return self._request(client, server, params)
        handler = handlers.get(action)
        return handler() if handler else self._error("TOOL_INVALID_ARGUMENTS", "unsupported lsp action")

    def _server_name(self, params: dict[str, Any]) -> str:
        explicit = str(params.get("server") or "").strip()
        if explicit:
            return explicit
        configured = sorted(self.manager.configs)
        return configured[0] if len(configured) == 1 else ""

    def _request(self, client: LspClient, server: str, params: dict[str, Any]) -> ToolExecutionResult:
        method = str(params.get("method") or "").strip()
        request_params = params.get("params")
        if not method or not isinstance(request_params, dict):
            return self._error("TOOL_INVALID_ARGUMENTS", "request 需要 method 和 object params")
        return self._ok({"server": server, "method": method, "result": client.request(method, request_params)})

    def _diagnostics(self, client: LspClient, server: str) -> ToolExecutionResult:
        return self._ok(
            {
                "server": server,
                "notifications": client.notifications(method="textDocument/publishDiagnostics"),
            }
        )

    def _close_client(self, client: LspClient, server: str) -> ToolExecutionResult:
        client.close()
        return self._ok({"server": server, "status": "closed"})

    def _open_document(self, client: LspClient, server: str, params: dict[str, Any]) -> ToolExecutionResult:
        path = self._workspace_file(str(params.get("path") or ""))
        if isinstance(path, ToolExecutionResult):
            return path
        content = path.read_bytes()
        if len(content) > _MAX_DOCUMENT_BYTES:
            return self._error("ARTIFACT_TOO_LARGE", f"LSP document exceeds {_MAX_DOCUMENT_BYTES} bytes")
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": str(params.get("language_id") or path.suffix.lstrip(".")),
                    "version": 1,
                    "text": content.decode("utf-8", errors="replace"),
                }
            },
        )
        return self._ok({"server": server, "path": str(path), "status": "opened"})

    def _workspace_file(self, raw: str) -> Path | ToolExecutionResult:
        if not raw.strip():
            return self._error("TOOL_INVALID_ARGUMENTS", "open_document 需要 path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if not any(_is_relative_to(candidate, root) for root in self.workspace_roots):
            return self._error("PATH_OUTSIDE_WORKSPACE", f"LSP path outside workspace: {candidate}")
        if not candidate.is_file():
            return self._error("PATH_NOT_FOUND", f"LSP document not found: {candidate}")
        return candidate

    def _ok(self, payload: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, True, json.dumps(payload, ensure_ascii=False))

    def _error(self, code: str, message: str) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, False, message, error_code=code)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["LspClient", "LspManager", "LspProtocolError", "LspTool"]
