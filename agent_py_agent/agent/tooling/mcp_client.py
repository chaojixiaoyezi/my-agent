
# LLM: MCP stdio 协议边界；工具结果完整保留到统一输出归档，协议错误不能作为空成功。
# 模块用途: 管理有期限、可取消的 MCP 子进程连接；仅承诺已实现的 tools 能力，不承诺 HTTP、sampling 或 tasks。
from __future__ import annotations

"""标准库实现的 MCP (Model Context Protocol) stdio 客户端。

这个文件让 my-agent 能连接外部 MCP server（社区现成的 GitHub/DB/Slack 等工具），
用 subprocess 起 server 子进程，按 JSON-RPC 2.0 over stdio（换行分隔的 JSON）收发消息，
完成 initialize 握手 → tools/list 发现工具 → tools/call 调用。

为什么不用官方 mcp SDK：标杆 长期助手 用官方 SDK（ClientSession/stdio_client），但本环境
没装该 SDK。MCP stdio 传输本质就是「子进程 + 按行收发 JSON-RPC」，标准库 subprocess 足矣，
零新增依赖。这里只实现 stdio tools 链路（同步、单连接、每请求总期限），不声称覆盖完整 MCP。

设计取舍（对齐严格约束）：
- 可选加法：没配 mcp_servers 就完全不起任何子进程（零开销）。
- 不破坏现有工具：MCP 工具动态注册成独立 BaseTool，名字加 ``mcp__<server>__<tool>`` 前缀防冲突。
- server 异常不崩主流程：server 起不来 / 握手超时 / 协议错 / 调用超时一律转成结构化错误，
  绝不向上抛裸异常打断主循环。
- 凭证不泄露：错误文本复用进程级统一脱敏边界（统一错误脱敏）；
  日志里 server env 的 secret 只打键名不打值。
- 进程生命周期：每个 server 一个长驻子进程，stop() 优雅关闭（先 terminate 再 kill 兜底）。

并发模型：本实现用 ``_lock`` 将同一 server 的请求串行化，不代表 JSON-RPC 不能多路复用。
后台读线程按在途 ID 投递响应；tools/list_changed 推进目录代次，下一运行边界重新完整发现工具。
请求排队、写入和等待共享 deadline；超时取消通知不代表远端动作一定已经停止或回滚。
"""

import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..common.log_redaction import redact_sensitive_text
from .cancellation import cancellation_requested, register_cancellation_callback

logger = logging.getLogger(__name__)

# my-agent 侧协商的 MCP 协议版本。2024-11-05 是稳定且被绝大多数 server 接受的版本；
# server 在 initialize 回包里必须返回已支持版本；未知版本不能按成功握手继续。
MCP_PROTOCOL_VERSION = "2024-11-05"
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})

_DEFAULT_CONNECT_TIMEOUT = 30.0  # initialize 握手超时（秒）
_DEFAULT_TOOL_TIMEOUT = 60.0     # 单次 tools/call 超时（秒）
_STOP_GRACE_SECONDS = 5.0        # 关闭时等子进程优雅退出的宽限期
# 仅限制独立 preview；完整 content 与 blocks 交给统一工具输出归档，不能在协议归一化阶段丢正文。
_DEFAULT_MAX_CONTENT_CHARS = 16 * 1024
# 读 server stdout 的单行字符上限（纯防 OOM 安全底线）。话痨/失控/被入侵的 server 吐一行超大 JSON
# 时，readline 会无上限读进内存；这里超限即丢弃该消息，绝不让一行把 agent 内存撑爆。
_DEFAULT_MAX_LINE_CHARS = 1024 * 1024

# 只放行给 stdio 子进程的安全基线环境变量（显式安全环境名单）：
# 不把父进程里的 API key/token 等敏感变量整盘漏给 MCP server 子进程。
_SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR"}
)

# server env 里键名命中这些子串时，日志只打 ``<redacted>``，不打值。
_SECRET_ENV_HINT = re.compile(r"(?:token|key|secret|password|passwd|pwd|auth|credential)", re.IGNORECASE)


def sanitize_credentials(text: str) -> str:
    """Compatibility name for the process-wide credential redactor."""

    return redact_sensitive_text(
        text,
        redacted_marker="[REDACTED]",
        redact_assignment_labels=True,
    )


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


# LLM: This config is the canonical per-server MCP declaration. Catalog placement only affects
# model exposure; it must never change effect, approval, owner, workspace, or runtime authority.
# 类用途: 保存一个 MCP 服务的启动、超时、展示分类和逐工具风险配置，供注册与断线重连复用。
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
    # 默认 MCP 工具继续进入渐进披露；部署者可为必须首轮可见的受控服务声明独立目录分类。
    # 该字段只影响模型工具目录，不授予权限，也不改变 effect/approval。
    catalog_category: str = "mcp"
    # MCP server 是外部执行边界，工具真实 effect 未知时按最严 dangerous 处理。
    # 只有部署者显式配置的逐工具声明才可降低风险；server 自报 metadata 不具授权效力。
    default_effect: str = "dangerous"
    tool_effects: dict[str, str] = field(default_factory=dict)

    # LLM: Parse deployment-owned structure only. Reject malformed catalog categories instead of
    # silently changing whether a server's tools are direct or deferred.
    # 函数用途: 从配置映射构造 MCP 服务声明，并校验超时、展示分类和风险等级。
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
            catalog_category=_configured_catalog_category(
                raw.get("catalog_category", "mcp"),
                server_name=name,
            ),
            default_effect=default_effect,
            tool_effects=tool_effects,
        )

    def effect_for_tool(self, tool_name: str) -> str:
        """Return the administrator-declared effect for a discovered tool."""

        return self.tool_effects.get(tool_name, self.tool_effects.get("*", self.default_effect))


_VALID_MCP_EFFECTS = frozenset({"read_only", "mutating", "dangerous"})
_VALID_CATALOG_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# LLM: Catalog category is a presentation/retrieval identifier, not an authorization label.
# 函数用途: 校验 MCP 工具目录分类，避免拼写错误静默改变首轮工具可见性。
def _configured_catalog_category(value: object, *, server_name: str) -> str:
    category = str(value or "").strip().lower()
    if not _VALID_CATALOG_CATEGORY.fullmatch(category):
        raise MCPError(
            f"MCP server '{server_name}' 的 catalog_category 必须是 "
            "1-64 位小写字母开头的字母、数字或下划线",
            code="MCP_CONFIG_INVALID",
        )
    return category


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


# LLM: 本函数只是文本投影，原始 typed blocks 另行保留；未适配的模态不能假装已经看懂。
# 函数用途: 提取 MCP 文本及内嵌资源正文，其它模态显示类型提示。
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
        text = res.get("text") if isinstance(res, dict) else None
        return f"[resource {uri}]" + (f"\n{text}" if isinstance(text, str) else "")
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


# LLM: 保留完整 canonical content 后才由 ToolExecutor 做预览/归档；协议错误不能归一化成空成功。
# 函数用途: 校验工具结果结构，保留 typed blocks 和全文，同时给出独立有界预览。
def _normalize_call_result(result: object, max_chars: int = _DEFAULT_MAX_CONTENT_CHARS) -> dict[str, Any]:
    if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
            or not isinstance(result.get("isError", False), bool)
            or any(not isinstance(block, dict) or not isinstance(block.get("type"), str) for block in result["content"])):
        raise MCPError("MCP tools/call 返回无效的 CallToolResult", code="MCP_PROTOCOL_ERROR")
    parts = [text for block in result["content"] if (text := _render_content_block(block))]
    text = "\n".join(parts)
    normalized: dict[str, Any] = {
        "content": text,
        "content_blocks": result["content"],
        "preview": _truncate_content(text, max_chars),
        "isError": bool(result.get("isError")),
    }
    if result.get("structuredContent") is not None:
        normalized["structuredContent"] = result["structuredContent"]
    return normalized


# LLM: 仅创建可丢弃的展示预览，调用方必须同时保留 canonical content。
# 函数用途: 缩短单独的 MCP 预览字段，不修改完整结果。
def _truncate_content(content: str, max_chars: int) -> str:
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n…[MCP 结果过长,已截断到 {max_chars} 字符]"


# LLM: 目录发现共享总 deadline；循环游标、重复名称和非法 schema 均拒绝发布，不降为空 schema。
# 函数用途: 遍历 tools/list 并校验完整工具表；网络调用仍由传入的唯一客户端请求入口负责。
def _discover_tools(request, timeout: float) -> list[MCPToolInfo]:
    tools: dict[str, MCPToolInfo] = {}
    deadline = time.monotonic() + timeout
    cursor = ""
    seen: set[str] = set()
    for _page in range(100):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("MCP 工具发现超过总期限", code="MCP_TIMEOUT")
        result = request("tools/list", {"cursor": cursor} if cursor else {}, timeout=remaining)
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise MCPError("MCP tools/list 返回格式错误", code="MCP_PROTOCOL_ERROR")
        for item in result["tools"]:
            tool = _parse_tool_info(item)
            if tool.name in tools:
                raise MCPError("MCP 工具名称重复", code="MCP_PROTOCOL_ERROR")
            tools[tool.name] = tool
        cursor = result.get("nextCursor")
        if cursor is None:
            return list(tools.values())
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise MCPError("MCP tools/list 游标错误或循环", code="MCP_PROTOCOL_ERROR")
        seen.add(cursor)
    raise MCPError("MCP tools/list 超过分页安全上限", code="MCP_PROTOCOL_ERROR")


# LLM: 远端工具定义属于不可信协议输入；此处验证形状，不扩大其 effect/approval 权限。
# 函数用途: 将合法的 tools/list 条目转换为本地只读定义，错误条目保留协议失败语义。
def _parse_tool_info(item: object) -> MCPToolInfo:
    if not isinstance(item, dict):
        raise MCPError("MCP 工具定义不是对象", code="MCP_PROTOCOL_ERROR")
    name = item.get("name")
    schema = item.get("inputSchema")
    description = item.get("description", "")
    if not isinstance(name, str) or not name.strip() or not isinstance(schema, dict) or not isinstance(description, str):
        raise MCPError("MCP 工具名称、说明或 inputSchema 格式错误", code="MCP_PROTOCOL_ERROR")
    return MCPToolInfo(name=name, description=description, input_schema=schema)


# LLM: 协议锁必须计入请求总期限；只检查本调用取消 token，拿到锁后所有退出路径都释放。
# 函数用途: 有期限、可取消地等待 MCP 请求锁或写锁，不把排队时间藏在 timeout 之外。
@contextmanager
def _bounded_lock(lock, deadline: float, *, allow_cancelled: bool = False):
    while True:
        if cancellation_requested() and not allow_cancelled:
            raise MCPError("MCP 排队已取消", code="MCP_CANCELLED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("MCP 排队超时", code="MCP_TIMEOUT")
        if lock.acquire(timeout=min(remaining, 0.05)):
            break
    try:
        yield
    finally:
        lock.release()


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
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._responses: dict[int, dict[str, Any]] = {}
        self._pending_request_ids: set[int] = set()
        self.tools_changed = False
        self._tools_generation = 0
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

    # LLM: 握手只接受已实现版本及对象能力；不支持的版本关闭传输，不声明空成功。
    # 函数用途: 协商协议并发送 initialized 通知。
    def _handshake(self) -> None:
        """initialize 请求 + 等待 capabilities + 发 notifications/initialized。"""
        init_params = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},  # client 暂不声明 sampling/roots 等高级能力（最小实现）
            "clientInfo": {"name": "my-agent", "version": "1.0"},
        }
        result = self._request("initialize", init_params, timeout=self.config.connect_timeout)
        if (not isinstance(result, dict) or result.get("protocolVersion") not in _SUPPORTED_PROTOCOL_VERSIONS
                or not isinstance(result.get("capabilities"), dict) or not isinstance(result.get("serverInfo"), dict)):
            raise MCPError("MCP 握手版本或能力结构不受支持", code="MCP_PROTOCOL_ERROR")
        self.server_info = result["serverInfo"]
        self.capabilities = result["capabilities"]
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
                if old_reader.is_alive():
                    raise MCPError("旧 MCP 读取线程尚未退出，拒绝混用两代连接", code="MCP_CONNECTION_CLOSED")
            with self._responses_cv:
                self._responses.clear()
                self._pending_request_ids.clear()
            self._reader_done.clear()
            self._reader = None
            self.tools_changed = False
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

    # LLM: tools/list 必须穷尽分页；重复 cursor、同名或错误结构拒绝发布残缺目录。
    # 函数用途: 获取完整远程工具表，并清除已消费的变更通知。
    def list_tools(self) -> list[MCPToolInfo]:
        """``tools/list``：发现 server 暴露的工具。失败抛 MCPError。"""
        generation = self._tools_generation
        tools = _discover_tools(self._request, self.config.connect_timeout)
        self.tools_changed = generation != self._tools_generation
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

    # LLM: 总 deadline 从排队前开始；取消/超时只终止等待，不证明远程副作用未发生。
    # 函数用途: 串行发送带唯一 ID 的请求，所有退出路径清理在途表并尽力通知远端取消。
    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        deadline = time.monotonic() + max(0.01, timeout)
        with _bounded_lock(self._lock, deadline):
            req_id = self._next_id
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            with self._responses_cv:
                self._pending_request_ids.add(req_id)
            sent = False
            try:
                self._send(message, deadline=deadline)
                sent = True
                return self._await_response(req_id, method, max(0, deadline - time.monotonic()))
            except MCPError as exc:
                if sent and method != "initialize" and exc.code in {"MCP_CANCELLED", "MCP_TIMEOUT"}:
                    try:
                        self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                    "params": {"requestId": req_id, "reason": exc.code}},
                                   deadline=time.monotonic() + 0.2, allow_cancelled=True)
                    except MCPError:
                        pass
                raise
            finally:
                with self._responses_cv:
                    self._pending_request_ids.discard(req_id)
                    self._responses.pop(req_id, None)

    # LLM: 通知与请求共用排队 deadline；没有 request ID 就不等待响应。
    # 函数用途: 在连接期限内发出初始化等通知，不无限等候请求锁。
    def _notify(self, method: str, params: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.config.connect_timeout
        with _bounded_lock(self._lock, deadline):
            self._send({"jsonrpc": "2.0", "method": method, "params": params}, deadline=deadline)

    # LLM: 每条连接只有一项写入；背压消耗总 deadline，半帧失败必须销毁连接，禁止续写取消帧。
    # 函数用途: 跨平台写入完整 JSON-RPC 帧；超时终止传输进程，不谎称远端副作用回滚。
    def _send(self, message: dict[str, Any], *, deadline: float | None = None, allow_cancelled: bool = False) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise MCPError(
                f"MCP server '{self.config.name}' 连接已关闭，无法发送请求",
                code="MCP_CONNECTION_CLOSED",
            )
        try:
            until = deadline if deadline is not None else time.monotonic() + self.config.connect_timeout
            with _bounded_lock(self._write_lock, until, allow_cancelled=allow_cancelled):
                payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
                if len(payload) > self.config.max_line_chars:
                    raise MCPError("MCP 请求超过单帧大小上限", code="MCP_PROTOCOL_ERROR")
                complete = threading.Event()
                failures: list[OSError] = []

                # LLM: 此线程只持有一帧和一个 fd；调用方超时会杀整个 transport 以解除管道背压。
                # 函数用途: 排空一帧字节并通知等待者；不使用文本缓冲，避免拆开 UTF-8 字符。
                def write_frame() -> None:
                    try:
                        remaining = memoryview(payload)
                        while remaining:
                            remaining = remaining[os.write(proc.stdin.fileno(), remaining):]
                    except (OSError, ValueError) as exc:
                        failures.append(OSError(str(exc)))
                    finally:
                        complete.set()

                writer = threading.Thread(target=write_frame, name=f"mcp-writer-{self.config.name}", daemon=True)
                writer.start()
                while not complete.wait(timeout=0.01):
                    if cancellation_requested() and not allow_cancelled:
                        self._kill_process()
                        raise MCPError("MCP 发送已取消", code="MCP_CANCELLED")
                    if time.monotonic() >= until:
                        self._kill_process()
                        raise MCPError("MCP 写入超时", code="MCP_TIMEOUT")
                if failures:
                    raise failures[0]
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(
                f"MCP server '{self.config.name}' 写入失败（子进程可能已退出）：{exc}",
                code="MCP_CONNECTION_CLOSED",
            ) from exc

    def _await_response(self, req_id: int, method: str, timeout: float) -> Any:
        """轮询等待指定 id 的响应；超时 / 进程提前死 都转成 MCPError。"""
        deadline = time.monotonic() + max(0, timeout)
        def wake_waiter() -> None:
            with self._responses_cv:
                self._responses_cv.notify_all()

        with register_cancellation_callback(wake_waiter):
            with self._responses_cv:
                while True:
                    if cancellation_requested():
                        raise MCPError(
                            f"MCP server '{self.config.name}' 调用 {method} 已取消",
                            code="MCP_CANCELLED",
                        )
                    if req_id in self._responses:
                        payload = self._responses.pop(req_id)
                        return _unwrap_jsonrpc(self.config.name, payload, method)
                    if self._reader_done.is_set() and req_id not in self._responses:
                        # 读线程已结束（子进程退出 / stdout EOF）但没拿到本请求的响应。
                        raise MCPError(
                            f"MCP server '{self.config.name}' 在响应 {method} 前断开连接"
                            f"{self._stderr_tail.hint()}",
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

    # LLM: method 区分服务端请求/通知，result/error 才是响应；仅接收精确在途 ID，迟到和未知响应不积压。
    # 函数用途: 分发 JSON-RPC 消息，标记目录变化并拒绝把服务端请求混入客户端响应。
    def _dispatch_message(self, message: object) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return
        msg_id = message.get("id")
        method = message.get("method")
        if isinstance(method, str):
            if msg_id is None:
                if method == "notifications/tools/list_changed":
                    self._tools_generation += 1
                    self.tools_changed = True
            else:
                response = ({"result": {}} if method == "ping" else
                            {"error": {"code": -32601, "message": "Client method not supported"}})
                try:
                    self._send({"jsonrpc": "2.0", "id": msg_id, **response})
                except MCPError:
                    pass
            return
        if type(msg_id) is not int or ("result" in message) == ("error" in message):
            return
        with self._responses_cv:
            if msg_id not in self._pending_request_ids:
                return
            self._responses.setdefault(msg_id, message)
            self._responses_cv.notify_all()

class _StderrTail:
    """抽干子进程 stderr 并保留末尾若干行，用于失败诊断（避免管道塞满死锁）。

    从 ``MCPStdioClient`` 拆出来：stderr 诊断是独立关注点，单独成类让客户端类更聚焦于
    JSON-RPC 收发主线。``drain`` 在后台线程跑，``hint`` 在主线程读末尾几行（脱敏后）。
    """

    def __init__(self, server_name: str, keep: int = 20):
        self._server_name = server_name
        self._keep = keep
        self._lines: list[str] = []

    # LLM: stderr 同时限制每段与保留段数；没有换行的失控输出也不能无限分配。
    # 函数用途: 持续排空错误管道，仅留有界、脱敏的末尾诊断。
    def drain(self, stream: object) -> None:
        if stream is None:
            return
        try:
            for raw in iter(lambda: stream.readline(4096), ""):  # type: ignore[attr-defined]
                line = raw.rstrip("\n")
                if not line:
                    continue
                self._lines.append(sanitize_credentials(line))
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
