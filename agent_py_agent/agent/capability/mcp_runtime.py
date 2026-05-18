from __future__ import annotations

# LLM: MCP runtime bridges descriptor-only cards to executable tools without owning transport auth.
# 模块用途: 把 MCP tool 描述符包装成可执行 BaseTool，并统一结构化结果和失败。
import json
import os
import queue
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .grants import CapabilityGrantScope
from .mcp import McpToolDescriptor

SCHEMA_VERSION = "mcp_tool_result.v1"
_MCP_PROTOCOL_VERSION = "2024-11-05"


# LLM: McpExecutionRequest is the normalized command bundle for one MCP tool call.
# 类用途: 保存 MCP tool 调用参数，避免长期用 dict 传业务字段。
@dataclass(frozen=True)
class McpExecutionRequest:
    arguments: dict[str, Any] = field(default_factory=dict)

    # LLM: McpExecutionRequest.to_payload keeps MCP call parameters in one request bundle.
    # 函数用途: 把 MCP 调用请求转成工具执行层可接收的参数对象。
    def to_payload(self) -> dict[str, Any]:
        return {"arguments": dict(self.arguments)}


# LLM: InMemoryMcpExecutor is a deterministic executor for tests and local adapters.
# 类用途: 按 server/tool 注册可调用 handler；真实 transport 后续实现同样接口。
class InMemoryMcpExecutor:
    # LLM: InMemoryMcpExecutor.__init__ initializes the local handler table only.
    # 函数用途: 初始化内存 MCP handler 注册表，不连接外部服务。
    def __init__(self):
        self._handlers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {}

    # LLM: InMemoryMcpExecutor.register binds one server/tool name to a deterministic handler.
    # 函数用途: 注册一个 MCP tool 的测试或本地适配 handler。
    def register(self, server: str, name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        self._handlers[(server.strip(), name.strip())] = handler

    # LLM: InMemoryMcpExecutor.execute runs a previously registered MCP handler.
    # 函数用途: 执行一个 MCP handler，缺失时用 KeyError 交给上层映射为工具失败。
    def execute(self, server: str, name: str, arguments: dict[str, Any]) -> Any:
        handler = self._handlers.get((server, name))
        if handler is None:
            raise KeyError(f"MCP tool unavailable: {server}:{name}")
        return handler(arguments)


# LLM: McpStdioServerSpec describes one real stdio MCP server process without credentials.
# 类用途: 保存 MCP stdio server 启动命令、工作目录、环境和超时配置。
@dataclass(frozen=True)
class McpStdioServerSpec:
    name: str
    command: list[str]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0


# LLM: StdioMcpExecutor calls real MCP stdio server processes through JSON-RPC.
# 类用途: 懒启动 MCP server 子进程，执行 tools/call，并把进程/协议错误映射给工具层。
class StdioMcpExecutor:
    # LLM: StdioMcpExecutor.__init__ stores server specs and starts no process until first use.
    # 函数用途: 初始化 MCP stdio executor 的 server 配置表和会话缓存。
    def __init__(self, servers: list[McpStdioServerSpec]):
        self._server_specs = {server.name: server for server in servers}
        self._sessions: dict[str, _McpStdioSession] = {}

    # LLM: StdioMcpExecutor.execute delegates one tool call to the named server session.
    # 函数用途: 执行真实 MCP tools/call 请求，server 未配置时抛 KeyError。
    def execute(self, server: str, name: str, arguments: dict[str, Any]) -> Any:
        session = self._session(server)
        return session.call_tool(name, arguments)

    # LLM: StdioMcpExecutor.list_tools discovers tool schemas from a configured MCP server.
    # 函数用途: 调用真实 MCP tools/list，并返回 server 原始 tools 列表。
    def list_tools(self, server: str) -> list[dict[str, Any]]:
        session = self._session(server)
        return session.list_tools()

    # LLM: StdioMcpExecutor.close terminates all lazy-started server processes.
    # 函数用途: 关闭已启动的 MCP stdio 会话，供测试和长生命周期清理使用。
    def close(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()

    # LLM: StdioMcpExecutor._session creates one session per configured server name.
    # 函数用途: 按需启动 MCP server 子进程，并复用同名 server 会话。
    def _session(self, server: str) -> _McpStdioSession:
        key = str(server or "").strip()
        spec = self._server_specs.get(key)
        if spec is None:
            raise KeyError(f"MCP stdio server unavailable: {key}")
        if key not in self._sessions:
            self._sessions[key] = _McpStdioSession(spec)
        return self._sessions[key]


# LLM: _McpStdioSession owns one subprocess and its sequential JSON-RPC request ids.
# 类用途: 管理 MCP stdio 子进程生命周期、初始化握手和响应读取队列。
class _McpStdioSession:
    # LLM: _McpStdioSession.__init__ starts one server process and completes initialize.
    # 函数用途: 启动 MCP stdio 子进程，创建 stdout reader，并执行初始化握手。
    def __init__(self, spec: McpStdioServerSpec):
        if not spec.command:
            raise ValueError(f"MCP stdio server {spec.name!r} missing command")
        self.spec = spec
        self._next_id = 1
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._process = subprocess.Popen(
            spec.command,
            cwd=spec.cwd or None,
            env=_merged_env(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._initialize()

    # LLM: _McpStdioSession.call_tool sends a standard MCP tools/call request.
    # 函数用途: 调用一个 MCP tool，并返回 server 原始 result 对象。
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._request(
            "tools/call",
            {"name": str(name or "").strip(), "arguments": dict(arguments)},
            timeout=self.spec.request_timeout_seconds,
        )

    # LLM: _McpStdioSession.list_tools sends the standard MCP tools/list request.
    # 函数用途: 读取 MCP server 暴露的 tool 名称、描述和 inputSchema。
    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, timeout=self.spec.request_timeout_seconds)
        tools = result.get("tools") if isinstance(result, dict) else None
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    # LLM: _McpStdioSession.close stops the subprocess without raising cleanup noise.
    # 函数用途: 终止 MCP 子进程，并尽量回收资源。
    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    # LLM: _McpStdioSession._initialize performs the MCP initialize handshake.
    # 函数用途: 发送 initialize 请求和 initialized 通知，确认 server 可用。
    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "my-agent", "version": "0"},
            },
            timeout=self.spec.startup_timeout_seconds,
        )
        self._notify("notifications/initialized", {})

    # LLM: _McpStdioSession._request sends one JSON-RPC request and waits for its matching response.
    # 函数用途: 写入 JSON-RPC 请求行，并在超时内读取同 id 响应。
    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = self._read_response(request_id, timeout=timeout)
        if "error" in response:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ValueError(str(message or "MCP JSON-RPC error"))
        return response.get("result")

    # LLM: _McpStdioSession._notify sends a JSON-RPC notification without waiting.
    # 函数用途: 写入 MCP notification 消息，例如 initialized。
    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    # LLM: _McpStdioSession._write_json is the only subprocess stdin write point.
    # 函数用途: 序列化 JSON-RPC 消息为一行并刷新到 MCP server。
    def _write_json(self, payload: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise RuntimeError(f"MCP stdio server exited: {self.spec.name}")
        if self._process.stdin is None:
            raise RuntimeError(f"MCP stdio server stdin unavailable: {self.spec.name}")
        self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    # LLM: _McpStdioSession._read_stdout captures server stdout JSON lines.
    # 函数用途: 后台读取 MCP server stdout，并把合法 JSON response 放入队列。
    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        for line in stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._responses.put(payload)

    # LLM: _McpStdioSession._read_response waits for one matching response id.
    # 函数用途: 在 timeout 内读取指定 request id 的 JSON-RPC response。
    def _read_response(self, request_id: int, *, timeout: float) -> dict[str, Any]:
        skipped: list[dict[str, Any]] = []
        try:
            return self._read_matching_response(request_id, skipped, timeout=timeout)
        except queue.Empty as exc:
            self._requeue_skipped_responses(skipped)
            raise TimeoutError(f"MCP stdio request timed out: {self.spec.name}") from exc

    # LLM: _McpStdioSession._read_matching_response keeps response matching below nesting limits.
    # 函数用途: 从响应队列中读取目标 id，非目标响应暂存等待重新入队。
    def _read_matching_response(
        self,
        request_id: int,
        skipped: list[dict[str, Any]],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        while True:
            payload = self._responses.get(timeout=timeout)
            if payload.get("id") == request_id:
                self._requeue_skipped_responses(skipped)
                return payload
            skipped.append(payload)

    # LLM: _McpStdioSession._requeue_skipped_responses preserves out-of-order JSON-RPC messages.
    # 函数用途: 把等待目标响应时跳过的其它消息放回响应队列。
    def _requeue_skipped_responses(self, skipped: list[dict[str, Any]]) -> None:
        for item in skipped:
            self._responses.put(item)


# LLM: McpTool exposes one MCP tool through the normal ToolRegistry execution contract.
# 类用途: 把 MCP descriptor 包装为 BaseTool，可被目录搜索、授权过滤和执行结果协议复用。
class McpTool(BaseTool):
    # LLM: McpTool.__init__ captures descriptor, executor, and optional grant scope for one MCP tool.
    # 函数用途: 初始化 MCP tool 包装器，并构建普通 ToolSpec 供注册表展示。
    def __init__(
        self,
        *,
        descriptor: McpToolDescriptor,
        executor: InMemoryMcpExecutor,
        grant_scope: CapabilityGrantScope | None = None,
    ):
        self.descriptor = descriptor
        self.executor = executor
        self.grant_scope = grant_scope
        self.spec = _mcp_tool_spec(descriptor)

    # LLM: McpTool.execute validates scope and maps MCP handler failures to stable tool errors.
    # 函数用途: 执行一次 MCP tool 调用，输出统一成功 payload 或 capability failure envelope。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request = _mcp_execution_request(params)
        if isinstance(request, ToolExecutionResult):
            return request
        server = self.descriptor.server.strip()
        name = self.descriptor.name.strip()
        if self.grant_scope is not None and not self.grant_scope.allows_mcp_tool(server, name):
            return _error_result(
                self.spec.name,
                "WRITE_FORBIDDEN",
                f"MCP tool 未授权: {server}:{name}",
            )
        try:
            result = self.executor.execute(server, name, request.arguments)
        except TimeoutError as exc:
            return _error_result(self.spec.name, "TOOL_TIMEOUT", str(exc) or "MCP tool timed out")
        except KeyError as exc:
            return _error_result(self.spec.name, "TOOL_UNAVAILABLE", str(exc))
        except ValueError as exc:
            return _error_result(self.spec.name, "TOOL_INVALID_ARGUMENTS", str(exc))
        except (OSError, RuntimeError) as exc:
            return _error_result(self.spec.name, "TOOL_UNAVAILABLE", str(exc))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "server": server,
            "name": name,
            "result": result,
        }
        return ToolExecutionResult(self.spec.name, True, json.dumps(payload, ensure_ascii=False, sort_keys=True))


# LLM: _mcp_tool_spec maps external MCP metadata into the normal tool catalog shape.
# 函数用途: 构造 MCP tool 的 ToolSpec，让 registry 能统一展示和授权。
def _mcp_tool_spec(descriptor: McpToolDescriptor) -> ToolSpec:
    server = descriptor.server.strip()
    name = descriptor.name.strip()
    schema_details = _schema_parameter_details(descriptor.input_schema)
    return ToolSpec(
        name=f"mcp.{server}.{name}",
        category="mcp",
        description=descriptor.description.strip() or name,
        use_cases=list(descriptor.when_to_use) or [descriptor.description.strip() or name],
        avoid_when=list(descriptor.not_when_to_use),
        keywords=[server, name, *descriptor.keywords, *descriptor.capabilities],
        parameters={"arguments": "MCP tool 参数对象", **schema_details},
        parameter_details={"arguments": "传给 MCP server 的 JSON object 参数；不接受字符串或数组。", **schema_details},
        examples=[json.dumps({"tool": f"mcp.{server}.{name}", "arguments": {}}, ensure_ascii=False)],
    )


# LLM: _mcp_execution_request normalizes raw model params into an MCP request bundle.
# 函数用途: 校验 MCP arguments 必须是 object，并返回结构化请求或失败结果。
def _mcp_execution_request(params: dict[str, Any]) -> McpExecutionRequest | ToolExecutionResult:
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _error_result("mcp", "TOOL_INVALID_ARGUMENTS", "MCP arguments 必须是 object。")
    return McpExecutionRequest(arguments=arguments)


# LLM: _error_result keeps MCP failures aligned with the shared tool error taxonomy.
# 函数用途: 构造带 capability fallback envelope 的工具失败结果。
def _error_result(tool: str, code: str, message: str) -> ToolExecutionResult:
    contract = error_contract(code)
    return ToolExecutionResult(
        tool,
        False,
        message,
        error_code=code,
        result_envelope={
            "schema_version": "capability_failure.v1",
            "error_code": contract.code,
            "error_category": contract.category,
            "retryable": contract.retryable,
            "recommended_action": contract.recommended_action,
            "recovery_hint": contract.recovery_hint,
            "fallback_actions": _fallback_actions(contract.recommended_action),
        },
    )


# LLM: _fallback_actions gives the model concrete next steps after a capability/tool failure.
# 函数用途: 按推荐恢复动作生成可机器读取的兜底动作序列。
def _fallback_actions(recommended_action: str) -> list[str]:
    if recommended_action == "repair_tool_arguments":
        return ["capability_describe", "repair_arguments", "retry_same_tool"]
    if recommended_action == "retry_with_smaller_scope_or_longer_timeout":
        return ["retry_with_smaller_scope", "capability_search", "choose_lower_cost_tool"]
    if recommended_action == "request_capability_or_choose_available_tool":
        return ["capability_search", "choose_available_tool", "capability_request"]
    if recommended_action == "request_permission_or_choose_allowed_root":
        return ["capability_search", "choose_authorized_tool", "capability_request"]
    return ["capability_search", "record_diagnostic"]


# LLM: _merged_env keeps stdio server env additions explicit and local to the child process.
# 函数用途: 合并当前环境和 MCP server 附加环境变量。
def _merged_env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in extra.items()})
    return env


# LLM: _schema_parameter_details exposes MCP inputSchema properties as compact tool details.
# 函数用途: 从 MCP JSON schema 提取一层参数名和类型，帮助模型少写错 arguments。
def _schema_parameter_details(schema: dict[str, object]) -> dict[str, str]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {}
    details: dict[str, str] = {}
    for key, value in properties.items():
        if isinstance(value, dict):
            details[str(key)] = str(value.get("type") or value.get("description") or "object")
    return details
