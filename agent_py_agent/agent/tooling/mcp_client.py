
from __future__ import annotations

"""标准库实现的 MCP (Model Context Protocol) stdio 客户端。

这个文件让 my-agent 能连接外部 MCP server（社区现成的 GitHub/DB/Slack 等工具），
用 subprocess 起 server 子进程，按 JSON-RPC 2.0 over stdio（换行分隔的 JSON）收发消息，
完成 initialize 握手 → tools/list 发现工具 → tools/call 调用。

为什么不用官方 mcp SDK：标杆 长期助手 用官方 SDK（ClientSession/stdio_client），但本环境
没装该 SDK。MCP stdio 传输本质就是「子进程 + 按行收发 JSON-RPC」，标准库 subprocess 足矣，
零新增依赖。这里只实现 stdio 传输的最小可用但完整子集（同步、单连接、按 server 超时）。

设计取舍（对齐严格约束）：
- 可选加法：没配 mcp_servers 就完全不起任何子进程（零开销）。
- 不破坏现有工具：MCP 工具动态注册成独立 BaseTool，名字加 ``mcp__<server>__<tool>`` 前缀防冲突。
- server 异常不崩主流程：server 起不来 / 握手超时 / 协议错 / 调用超时一律转成结构化错误，
  绝不向上抛裸异常打断主循环。
- 凭证不泄露：错误文本里的 token/key/Bearer 等用正则脱敏（统一错误脱敏）；
  日志里 server env 的 secret 只打键名不打值。
- 进程生命周期：每个 server 一个长驻子进程，stop() 优雅关闭（先 terminate 再 kill 兜底）。

并发模型：每个 ``MCPStdioClient`` 有一把 ``_lock``，同一 server 的 JSON-RPC 请求串行化
（stdio 是单条流，交叉读写会串号）。一个后台读线程把 server 的输出按行解析成 JSON-RPC 消息，
按 id 投递给等待的请求；通知（无 id）当前仅记录后丢弃（最小实现，不订阅 list_changed）。
"""

import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# my-agent 侧协商的 MCP 协议版本。2024-11-05 是稳定且被绝大多数 server 接受的版本；
# server 在 initialize 回包里给自己的 protocolVersion，我们不强校验（最大兼容）。
MCP_PROTOCOL_VERSION = "2024-11-05"

_DEFAULT_CONNECT_TIMEOUT = 30.0  # initialize 握手超时（秒）
_DEFAULT_TOOL_TIMEOUT = 60.0     # 单次 tools/call 超时（秒）
_STOP_GRACE_SECONDS = 5.0        # 关闭时等子进程优雅退出的宽限期
# 单条工具结果喂给模型的文本上限（≈4K token）。模型上下文才 1M token，一个工具结果再大也消化不了、
# 反而挤爆上下文，超了就截断带标记。对齐 codebase 其他工具输出量级（快照 8K / 进程 4K）。
_DEFAULT_MAX_CONTENT_CHARS = 16 * 1024
# 读 server stdout 的单行字符上限（纯防 OOM 安全底线）。话痨/失控/被入侵的 server 吐一行超大 JSON
# 时，readline 会无上限读进内存；这里超限即丢弃该消息，绝不让一行把 agent 内存撑爆。
_DEFAULT_MAX_LINE_CHARS = 1024 * 1024

# 只放行给 stdio 子进程的安全基线环境变量（显式安全环境名单）：
# 不把父进程里的 API key/token 等敏感变量整盘漏给 MCP server 子进程。
_SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR"}
)

# 错误文本里要脱敏的凭证形态。返回给模型 / 落日志前，
# 把这些 token/key/Bearer/password 等替换成 [REDACTED]，避免凭证经错误信息泄露。
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{6,255}"            # GitHub PAT
    r"|github_pat_[A-Za-z0-9_]{6,255}"    # GitHub fine-grained PAT
    r"|gh[oprs]_[A-Za-z0-9_]{6,255}"      # 其他 GitHub token 形态
    r"|sk-[A-Za-z0-9_-]{6,255}"           # OpenAI 风格 key
    r"|xox[baprs]-[A-Za-z0-9-]{6,255}"    # Slack token
    r"|Bearer\s+[A-Za-z0-9._\-]+"         # Bearer token
    r"|(?:token|key|api[_-]?key|password|secret|passwd|pwd)"
    r"\"?\s*[=:]\s*\"?[^\s&,;\"']{1,255}"  # token=... / "api_key": "..." (含 JSON 引号形态)
    r")",
    re.IGNORECASE,
)

# server env 里键名命中这些子串时，日志只打 ``<redacted>``，不打值。
_SECRET_ENV_HINT = re.compile(r"(?:token|key|secret|password|passwd|pwd|auth|credential)", re.IGNORECASE)


def sanitize_credentials(text: str) -> str:
    """把凭证形态的子串替换成 [REDACTED]，用于返回给模型 / 落日志前的最后一道脱敏。"""
    if not text:
        return ""
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", str(text))


def redact_env_for_log(env: dict[str, str] | None) -> dict[str, str]:
    """构造一个可安全落日志的 env 视图：疑似 secret 的键值打成 ``<redacted>``。"""
    safe: dict[str, str] = {}
    for key, value in (env or {}).items():
        if _SECRET_ENV_HINT.search(str(key)):
            safe[str(key)] = "<redacted>"
        else:
            safe[str(key)] = str(value)
    return safe


def build_safe_env(user_env: dict[str, str] | None) -> dict[str, str]:
    """构造给 stdio 子进程的隔离环境：只继承安全基线变量 + 用户显式声明的变量。

    默认 **不** 把父进程整盘 os.environ 漏给 MCP server，
    只放行 PATH/HOME 等无害基线和 XDG_*，再叠加 server 配置里 env 段显式声明的变量
    （那些是用户有意传给该 server 的凭证，如 GITHUB_PERSONAL_ACCESS_TOKEN）。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    for key, value in (user_env or {}).items():
        env[str(key)] = str(value)
    return env


class MCPError(Exception):
    """MCP 客户端层的统一异常。``code`` 给调用方做错误码映射；str(exc) 已脱敏。"""

    def __init__(self, message: str, *, code: str = "MCP_ERROR"):
        super().__init__(sanitize_credentials(message))
        self.code = code


@dataclass
class MCPToolInfo:
    """一个从 server ``tools/list`` 发现的工具（name/description/inputSchema）。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPServerConfig:
    """单个 stdio MCP server 的连接声明。

    ``command`` + ``args`` 起子进程；``env`` 是显式传给该 server 的额外环境变量
    （叠加在安全基线之上）；``timeout`` / ``connect_timeout`` 是按 server 的超时覆盖。
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = _DEFAULT_TOOL_TIMEOUT
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    cwd: str = ""
    max_content_chars: int = _DEFAULT_MAX_CONTENT_CHARS  # 工具结果喂模型的文本上限,超截断
    max_line_chars: int = _DEFAULT_MAX_LINE_CHARS        # 读 stdout 单行字符上限,超丢弃(防 OOM)
    # MCP server 是外部执行边界，工具真实 effect 未知时按最严 dangerous 处理。
    # 只有部署者显式配置的逐工具声明才可降低风险；server 自报 metadata 不具授权效力。
    default_effect: str = "dangerous"
    tool_effects: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, raw: object) -> MCPServerConfig:
        """从 config 的一条 ``mcp_servers.<name>`` 映射构造，做最小健壮性归一化。"""
        if not isinstance(raw, dict):
            raise MCPError(
                f"MCP server '{name}' 配置必须是映射，实际是 {type(raw).__name__}",
                code="MCP_CONFIG_INVALID",
            )
        command = str(raw.get("command") or "").strip()
        if not command:
            raise MCPError(
                f"MCP server '{name}' 缺少 command（stdio 传输必须指定要启动的 server 命令）",
                code="MCP_CONFIG_INVALID",
            )
        args_raw = raw.get("args") or []
        if not isinstance(args_raw, (list, tuple)):
            raise MCPError(f"MCP server '{name}' 的 args 必须是数组", code="MCP_CONFIG_INVALID")
        env_raw = raw.get("env") or {}
        if not isinstance(env_raw, dict):
            raise MCPError(f"MCP server '{name}' 的 env 必须是映射", code="MCP_CONFIG_INVALID")
        default_effect = _configured_effect(
            raw.get("default_effect", "dangerous"),
            server_name=name,
            field_name="default_effect",
        )
        tool_effects = _configured_tool_effects(raw.get("tool_effects"), server_name=name)
        return cls(
            name=name,
            command=command,
            args=[str(item) for item in args_raw],
            env={str(k): str(v) for k, v in env_raw.items()},
            timeout=_coerce_timeout(raw.get("timeout"), _DEFAULT_TOOL_TIMEOUT),
            connect_timeout=_coerce_timeout(raw.get("connect_timeout"), _DEFAULT_CONNECT_TIMEOUT),
            cwd=str(raw.get("cwd") or "").strip(),
            max_content_chars=_coerce_positive_int(raw.get("max_content_chars"), _DEFAULT_MAX_CONTENT_CHARS),
            max_line_chars=_coerce_positive_int(raw.get("max_line_chars"), _DEFAULT_MAX_LINE_CHARS),
            default_effect=default_effect,
            tool_effects=tool_effects,
        )

    def effect_for_tool(self, tool_name: str) -> str:
        """Return the administrator-declared effect for a discovered tool."""

        return self.tool_effects.get(tool_name, self.tool_effects.get("*", self.default_effect))


_VALID_MCP_EFFECTS = frozenset({"read_only", "mutating", "dangerous"})


def _configured_effect(value: object, *, server_name: str, field_name: str) -> str:
    effect = str(value or "").strip().lower()
    if effect not in _VALID_MCP_EFFECTS:
        raise MCPError(
            f"MCP server '{server_name}' 的 {field_name} 必须是 "
            "read_only、mutating 或 dangerous",
            code="MCP_CONFIG_INVALID",
        )
    return effect


def _configured_tool_effects(value: object, *, server_name: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise MCPError(
            f"MCP server '{server_name}' 的 tool_effects 必须是映射",
            code="MCP_CONFIG_INVALID",
        )
    return {
        str(tool_name): _configured_effect(
            effect,
            server_name=server_name,
            field_name=f"tool_effects.{tool_name}",
        )
        for tool_name, effect in value.items()
        if str(tool_name).strip()
    }


def _coerce_timeout(value: object, default: float) -> float:
    """把 config 里可能是字符串/None 的超时值安全转成正浮点，非法回退默认。"""
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result <= 0:
        return default
    return result


def _coerce_positive_int(value: object, default: int) -> int:
    """把 config 里可能是字符串/None 的字符上限安全转成正整数,非法/非正回退默认。"""
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _render_content_block(block: object) -> str:
    """把一个 MCP content 块渲染成一行文本（非 dict / 未知类型返回空串）。"""
    if not isinstance(block, dict):
        return ""
    btype = block.get("type")
    if btype == "text":
        return str(block.get("text") or "")
    if btype in ("image", "audio"):
        return f"[{btype} content, mimeType={block.get('mimeType') or 'application/octet-stream'}]"
    if btype == "resource":
        res = block.get("resource")
        uri = res.get("uri") if isinstance(res, dict) else ""
        return f"[resource {uri}]"
    return ""


def _unwrap_jsonrpc(server_name: str, payload: dict[str, Any], method: str) -> Any:
    """从 JSON-RPC 响应里取 result；若是 error 对象则转成 MCPError。"""
    err = payload.get("error") if isinstance(payload, dict) else None
    if err is None:
        return payload.get("result") if isinstance(payload, dict) else None
    detail = f"{err.get('message') or '未知 JSON-RPC 错误'}（code={err.get('code')}）" if isinstance(err, dict) else str(err)
    raise MCPError(
        f"MCP server '{server_name}' {method} 返回错误：{detail}",
        code="MCP_PROTOCOL_ERROR",
    )


def _normalize_call_result(result: object, max_chars: int = _DEFAULT_MAX_CONTENT_CHARS) -> dict[str, Any]:
    """把 MCP CallToolResult 的 content 块归一化成纯文本 + 错误标志;文本超 max_chars 截断带标记。"""
    if not isinstance(result, dict):
        return {"content": "", "isError": False}
    parts = [text for block in (result.get("content") or []) if (text := _render_content_block(block))]
    normalized: dict[str, Any] = {
        "content": _truncate_content("\n".join(parts), max_chars),
        "isError": bool(result.get("isError")),
    }
    if result.get("structuredContent") is not None:
        normalized["structuredContent"] = result["structuredContent"]
    return normalized


def _truncate_content(content: str, max_chars: int) -> str:
    """工具结果文本超上限就截断带标记(模型上下文有限,过长结果只会挤爆上下文)。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n…[MCP 结果过长,已截断到 {max_chars} 字符]"


# LLM: MCP 客户端持有唯一 server 子进程事实；工具可用性只能读取这里，不能另建健康检查连接。
# 类用途: 管理一个 stdio MCP server 的启动、JSON-RPC 请求、状态和幂等清理。
class MCPStdioClient:
    """单个 stdio MCP server 的同步客户端：起子进程 + JSON-RPC over stdio。

    生命周期：``start()`` 起子进程并完成 initialize 握手 + initialized 通知；
    ``list_tools()`` 发现工具；``call_tool()`` 调用；``stop()`` 关闭子进程。
    任何一步失败都抛 ``MCPError``（已脱敏），由上层 manager 兜底成「该 server 不可用」
    而不影响其他 server 与主流程。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()          # 串行化本 server 的 JSON-RPC 请求
        self._lifecycle_lock = threading.RLock()
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._responses: dict[int, dict[str, Any]] = {}
        self._responses_cv = threading.Condition()
        self._reader_done = threading.Event()
        self._stderr_tail = _StderrTail(config.name)  # 子进程 stderr 末尾若干行，用于诊断
        self._started = False
        self.server_info: dict[str, Any] = {}    # initialize 回包里的 serverInfo
        self.capabilities: dict[str, Any] = {}   # initialize 回包里的 capabilities

    # LLM: 运行状态检查只读取现有子进程与 reader 状态，不重启 server、不握手也不延长失败进程寿命。
    # 函数用途: 判断已注册 MCP 工具背后的 stdio server 是否仍可接受调用。
    def is_running(self) -> bool:
        proc = self._proc
        return bool(
            self._started
            and proc is not None
            and proc.poll() is None
            and not self._reader_done.is_set()
        )

    # -- 生命周期 ------------------------------------------------------------

    def start(self) -> None:
        """起子进程并完成 MCP 握手。失败抛 MCPError（子进程已清理）。"""
        if self._started:
            return
        safe_env = build_safe_env(self.config.env)
        argv = [self.config.command, *self.config.args]
        logger.debug(
            "启动 MCP server '%s': argv=%s env=%s",
            self.config.name, argv, redact_env_for_log(self.config.env),
        )
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=safe_env,
                cwd=self.config.cwd or None,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # 行缓冲：stdio JSON-RPC 按行分帧
                start_new_session=True,  # 独立进程组，关闭时可整组清理、避免信号误伤父进程
            )
        except FileNotFoundError as exc:
            raise MCPError(
                f"MCP server '{self.config.name}' 启动失败：找不到命令 '{self.config.command}'"
                f"（确认它已安装且在 PATH 中，或用绝对路径）",
                code="MCP_SERVER_START_FAILED",
            ) from exc
        except OSError as exc:
            raise MCPError(
                f"MCP server '{self.config.name}' 启动失败：{exc}",
                code="MCP_SERVER_START_FAILED",
            ) from exc

        # 后台读线程：把 stdout 按行解析成 JSON-RPC 消息，按 id 投递。
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-reader-{self.config.name}", daemon=True,
        )
        self._reader.start()
        # 单独的 stderr 抽干线程：避免 server 往 stderr 狂写把管道塞满导致死锁，
        # 同时保留末尾若干行用于失败诊断。
        threading.Thread(
            target=self._stderr_tail.drain, args=(self._proc.stderr,),
            name=f"mcp-stderr-{self.config.name}", daemon=True,
        ).start()

        try:
            self._handshake()
        except MCPError:
            # 握手失败：清理子进程，再把异常抛给上层（结构化错误）。
            self._kill_process()
            raise
        self._started = True
        # atexit 兜底：进程异常退出（未显式 close_mcp_clients）时也确保子进程被收掉，
        # 避免 MCP server 子进程变孤儿。stop() 幂等，重复调用安全。沿用 browser_session 的约定。
        atexit.register(self.stop)

    def _handshake(self) -> None:
        """initialize 请求 + 等待 capabilities + 发 notifications/initialized。"""
        init_params = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},  # client 暂不声明 sampling/roots 等高级能力（最小实现）
            "clientInfo": {"name": "my-agent", "version": "1.0"},
        }
        result = self._request("initialize", init_params, timeout=self.config.connect_timeout)
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo") or {}
            self.capabilities = result.get("capabilities") or {}
        # 握手第三步：通知 server 客户端已就绪（无需回包）。
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        """优雅关闭：关 stdin → 等退出 → terminate → kill 兜底。幂等。"""
        if self._proc is None:
            return
        proc = self._proc
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._kill_process()
        finally:
            self._reader_done.set()
            self._proc = None
            self._started = False

    def reconnect(self) -> None:
        """在两轮调用之间重建已失效的 stdio transport，并保留同一工具绑定。

        可用性查询仍然只读；只有显式的运行边界准备才调用这里。旧 reader 必须先退出，
        再清理响应表和 Event，否则上一代 transport 的 EOF 会把新连接误判为已关闭。
        """
        with self._lifecycle_lock:
            if self.is_running():
                return
            old_reader = self._reader
            self.stop()
            if old_reader is not None and old_reader.is_alive():
                old_reader.join(timeout=_STOP_GRACE_SECONDS)
            with self._responses_cv:
                self._responses.clear()
            self._reader_done.clear()
            self._reader = None
            self._next_id = 1
            self.server_info = {}
            self.capabilities = {}
            self.start()

    def _kill_process(self) -> None:
        """强制结束子进程（及其进程组），用于启动/握手失败或优雅关闭超时。"""
        proc = self._proc
        if proc is None:
            return
        try:
            # 优先杀整个进程组（start_new_session 让子进程自成组），覆盖它派生的孙进程。
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                try:
                    import signal

                    killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self._reader_done.set()

    # -- JSON-RPC over stdio -------------------------------------------------

    def list_tools(self) -> list[MCPToolInfo]:
        """``tools/list``：发现 server 暴露的工具。失败抛 MCPError。"""
        result = self._request("tools/list", {}, timeout=self.config.connect_timeout)
        tools_raw = []
        if isinstance(result, dict):
            tools_raw = result.get("tools") or []
        tools: list[MCPToolInfo] = []
        for item in tools_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema")
            tools.append(
                MCPToolInfo(
                    name=name,
                    description=str(item.get("description") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """``tools/call``：调用一个工具，返回归一化结果 dict。

        返回形如 ``{"content": <文本>, "isError": bool, "structuredContent": <可选>}``。
        协议错 / 超时 / 进程死 抛 MCPError；server 自己报的工具错（isError=true）不抛，
        而是把 isError 一起返回，由上层决定如何呈现给模型。
        """
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=self.config.timeout,
        )
        return _normalize_call_result(result, self.config.max_content_chars)

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        """发一条 JSON-RPC 请求并阻塞等结果（按 server 超时）。"""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            self._send(message)
            return self._await_response(req_id, method, timeout)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """发一条 JSON-RPC 通知（无 id，不等回包）。"""
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        """把一条 JSON-RPC 消息按行写进子进程 stdin。"""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise MCPError(
                f"MCP server '{self.config.name}' 连接已关闭，无法发送请求",
                code="MCP_CONNECTION_CLOSED",
            )
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(
                f"MCP server '{self.config.name}' 写入失败（子进程可能已退出）：{exc}",
                code="MCP_CONNECTION_CLOSED",
            ) from exc

    def _await_response(self, req_id: int, method: str, timeout: float) -> Any:
        """轮询等待指定 id 的响应；超时 / 进程提前死 都转成 MCPError。"""
        deadline = time.monotonic() + max(0.1, timeout)
        with self._responses_cv:
            while True:
                if req_id in self._responses:
                    payload = self._responses.pop(req_id)
                    return _unwrap_jsonrpc(self.config.name, payload, method)
                if self._reader_done.is_set() and req_id not in self._responses:
                    # 读线程已结束（子进程退出 / stdout EOF）但没拿到本请求的响应。
                    raise MCPError(
                        f"MCP server '{self.config.name}' 在响应 {method} 前断开连接"
                        f"{self._stderr_hint()}",
                        code="MCP_CONNECTION_CLOSED",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(
                        f"MCP server '{self.config.name}' 调用 {method} 超时"
                        f"（{timeout:.0f}s）",
                        code="MCP_TIMEOUT",
                    )
                self._responses_cv.wait(timeout=min(remaining, 0.5))

    # -- 后台读 / stderr -----------------------------------------------------

    def _read_loop(self) -> None:
        """后台线程：逐行读 stdout（单行有上限,防 OOM），解析 JSON-RPC 消息并按 id 投递。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._reader_done.set()
            return
        cap = self.config.max_line_chars
        try:
            while True:
                raw = proc.stdout.readline(cap + 1)  # 至多读 cap+1 字符或到换行,不无上限读进内存
                if raw == "":
                    break  # EOF：子进程退出
                if self._line_over_cap(raw, proc.stdout, cap):
                    continue  # 超长行已丢弃,跳过该消息(防话痨 server 撑爆内存)
                self._handle_line(raw.strip())
        except (OSError, ValueError):
            pass
        finally:
            # stdout EOF：子进程退出。唤醒所有还在等响应的请求，让它们走 MCPError。
            with self._responses_cv:
                self._reader_done.set()
                self._responses_cv.notify_all()

    def _line_over_cap(self, raw: str, stream: Any, cap: int) -> bool:
        """raw 未到换行且已达上限 → 超长行:吞掉本行剩余(到换行/EOF)并返回 True。"""
        if raw.endswith("\n") or len(raw) <= cap:
            return False
        while True:
            extra = stream.readline(cap + 1)
            if extra == "" or extra.endswith("\n"):
                break
        logger.warning("MCP server '%s' 单行超 %d 字符上限,已丢弃该消息(防 OOM)", self.config.name, cap)
        return True

    def _handle_line(self, line: str) -> None:
        """解析一行 JSON-RPC 并投递;非 JSON 行(有些 server 往 stdout 混日志)跳过留痕。"""
        if not line:
            return
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            logger.debug("MCP server '%s' stdout 非 JSON 行已忽略: %.200s", self.config.name, line)
            return
        self._dispatch_message(message)

    def _dispatch_message(self, message: object) -> None:
        """把一条解析好的消息投递：有 id 的是响应；无 id 的是通知（仅记录）。"""
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if msg_id is None:
            # 通知（如 notifications/tools/list_changed）：最小实现仅记录，不订阅刷新。
            method = message.get("method")
            if method:
                logger.debug("MCP server '%s' 通知: %s", self.config.name, method)
            return
        try:
            key = int(msg_id)
        except (TypeError, ValueError):
            return
        with self._responses_cv:
            self._responses[key] = message
            self._responses_cv.notify_all()

    def _stderr_hint(self) -> str:
        """把 stderr 末尾几行拼成（已脱敏的）诊断提示，附在断连错误后。"""
        return self._stderr_tail.hint()


class _StderrTail:
    """抽干子进程 stderr 并保留末尾若干行，用于失败诊断（避免管道塞满死锁）。

    从 ``MCPStdioClient`` 拆出来：stderr 诊断是独立关注点，单独成类让客户端类更聚焦于
    JSON-RPC 收发主线。``drain`` 在后台线程跑，``hint`` 在主线程读末尾几行（脱敏后）。
    """

    def __init__(self, server_name: str, keep: int = 20):
        self._server_name = server_name
        self._keep = keep
        self._lines: list[str] = []

    def drain(self, stream: object) -> None:
        if stream is None:
            return
        try:
            for raw in stream:  # type: ignore[attr-defined]
                line = raw.rstrip("\n")
                if not line:
                    continue
                self._lines.append(line)
                if len(self._lines) > self._keep:
                    del self._lines[: len(self._lines) - self._keep]
        except (OSError, ValueError):
            pass

    def hint(self) -> str:
        if not self._lines:
            return ""
        return f"（server stderr: {sanitize_credentials('; '.join(self._lines[-3:]))}）"


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCPError",
    "MCPServerConfig",
    "MCPStdioClient",
    "MCPToolInfo",
    "build_safe_env",
    "redact_env_for_log",
    "sanitize_credentials",
]
