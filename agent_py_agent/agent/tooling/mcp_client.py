
# LLM: MCP 客户端固定插件代次，托管启动失败未知不能消失；收发归 transport，联测原注册、关闭和旧代理拒绝。
# 模块用途: 组织普通及托管 MCP 的启动、发现、调用和重连，保留原资源清理事实，不用重连更换激活。
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
- 进程生命周期：每条 transport 固定进程出生身份；stop() 永久撤销后按原进程树回执清理。

并发模型：每条 transport 使用自己的请求锁串行化，不代表 JSON-RPC 不能多路复用。
后台读线程按在途 ID 投递响应；tools/list_changed 推进目录代次，下一运行边界重新完整发现工具。
请求排队、写入和等待共享 deadline；超时取消通知不代表远端动作一定已经停止或回滚。
"""

import atexit
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from ..common.log_redaction import redact_sensitive_text
from .background_process_launch import BackgroundLaunchError
from .mcp_protocol import MCPError, bounded_mcp_lock
from .mcp_transport import MCPCleanupReceipt, MCPTransport
from .process_registry import ProcessTerminationReceipt
from .process_session_cleanup import ProcessSessionCleanupError

if TYPE_CHECKING:
    from ..plugin_activation_ref import PluginActivationRef

# my-agent 侧协商的 MCP 协议版本。2024-11-05 是稳定且被绝大多数 server 接受的版本；
# server 在 initialize 回包里必须返回已支持版本；未知版本不能按成功握手继续。
MCP_PROTOCOL_VERSION = "2024-11-05"
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})

_DEFAULT_CONNECT_TIMEOUT = 30.0  # initialize 握手超时（秒）
_DEFAULT_TOOL_TIMEOUT = 60.0     # 单次 tools/call 超时（秒）
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


# LLM: 日志只显示公开环境值，插件设置整个对象均属私有，不能仅按内部字段名猜哪些是密钥。
# 函数用途: 隐藏显式凭证和插件私有设置，保留无敏感内容的环境诊断。
def redact_env_for_log(env: dict[str, str] | None) -> dict[str, str]:
    """构造一个可安全落日志的 env 视图：疑似 secret 的键值打成 ``<redacted>``。"""
    safe: dict[str, str] = {}
    for key, value in (env or {}).items():
        if _SECRET_ENV_HINT.search(str(key)) or str(key) == "MY_AGENT_PLUGIN_SETTINGS":
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


_Publication = TypeVar("_Publication")


# LLM: 客户端持有唯一当前连接和永久关闭状态；所有 IO 绑定固定 transport，永久关闭不能被运行边界恢复。
# 类用途: 管理 MCP 的启动、握手、临时重连和最终关闭，供注册层发布同连接的工具目录。
class MCPStdioClient:
    # LLM: activation 来自可信组合入口并永久固定；插件只能托管启动，未提供时保持普通 MCP 路径，构造不启动进程。
    # 函数用途: 保存服务声明、原激活及当前连接，防止旧客户端重连时换绑新代。
    def __init__(self, config: MCPServerConfig, *, activation: PluginActivationRef | None = None):
        self.config = config
        self._activation = activation
        self._transport: MCPTransport | None = None
        self._connect_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._closed = threading.Event()
        self._starting = False
        self._launch_failure: BackgroundLaunchError | ProcessSessionCleanupError | None = None
        self._exit_registered = False

    # LLM: 永久关闭是本客户端终态，不由进程存活、目录缓存或后续重连覆盖。
    # 函数用途: 供注册层跳过已明确关闭的客户端。
    def is_closed(self) -> bool:
        return self._closed.is_set()

    # LLM: 可用性只读当前连接，不握手、不重启；进程退出不自动恢复旧快照。
    # 函数用途: 判断当前客户端是否有可用的已握手连接。
    def is_running(self) -> bool:
        with self._state_lock:
            return not self.is_closed() and self._transport is not None and self._transport.is_running()

    # LLM: 通知计数属于当前 transport，旧 reader 不可改变这里的新连接投影。
    # 函数用途: 告诉注册层是否需要重新读取当前服务的工具目录。
    @property
    def tools_changed(self) -> bool:
        with self._state_lock:
            return self._transport is not None and self._transport.inbox.tools_changed

    # LLM: 握手数据只从当前连接读取；返回副本，调用方不能改变连接的协议事实。
    # 函数用途: 展示当前服务声明的名称与版本。
    @property
    def server_info(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._transport.server_info) if self._transport is not None else {}

    # LLM: 能力仅是协议声明，不授予权限；旧连接数据不复制到新连接。
    # 函数用途: 展示当前握手返回的能力列表。
    @property
    def capabilities(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._transport.capabilities) if self._transport is not None else {}

    # LLM: 启动串行化但不持状态锁等待握手；stop 可先永久撤销，候选返回后必须再次核对再发布。
    # 函数用途: 启动一个服务并返回本次固定连接，失败只清理本次候选。
    def start(self) -> MCPTransport:
        deadline = time.monotonic() + self.config.connect_timeout
        with bounded_mcp_lock(self._connect_lock, deadline, closed=self._closed):
            with self._state_lock:
                if self.is_closed():
                    raise MCPError("MCP 客户端已永久关闭", code="MCP_CONNECTION_CLOSED")
                if self._launch_failure is not None:
                    raise self._launch_failure
                if self.is_running():
                    assert self._transport is not None
                    return self._transport
                if self._transport is not None:
                    raise MCPError("MCP 旧连接已断开，须在运行边界重连", code="MCP_CONNECTION_CLOSED")
                self._starting = True
                if not self._exit_registered:
                    atexit.register(self.stop)
                    self._exit_registered = True
            return self._start_transport()

    # LLM: 插件只走原托管启动；接管前失败回收原资源，普通 MCP 保留原启动；候选关闭不能再次发布 ready。
    # 函数用途: 启动固定来源的进程和读线程，完成握手并保留未知清理事实。
    def _start_transport(self) -> MCPTransport:
        transport = None
        managed = None
        try:
            if self._activation is not None:
                from .mcp_managed_process import ManagedMCPProcess

                managed = ManagedMCPProcess.launch(
                    self._activation, argv=[self.config.command, *self.config.args], cwd=Path(self.config.cwd),
                    environment=build_safe_env(self.config.env), timeout=self.config.connect_timeout,
                )
                process = managed.hosted.process
            else:
                process = subprocess.Popen(
                    [self.config.command, *self.config.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=build_safe_env(self.config.env), cwd=self.config.cwd or None,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
                )
            transport = MCPTransport(process, self.config.name, max_line_chars=self.config.max_line_chars,
                                     connect_timeout=self.config.connect_timeout, managed=managed)
            with self._state_lock:
                self._transport = transport
                self._starting = False
                self._require_current(transport)
                transport.start_readers()
            self._handshake(transport)
            with self._state_lock:
                self._require_current(transport)
                transport.ready = True
            return transport
        except BaseException as exc:
            failure = exc
            try:
                if transport is not None:
                    transport.terminate(grace_seconds=0)
                elif managed is not None:
                    managed.close_unclaimed()
            except BaseException as cleanup_error:
                failure = cleanup_error
                raise
            finally:
                with self._state_lock:
                    self._starting = False
                    if transport is None and (isinstance(failure, ProcessSessionCleanupError)
                            or isinstance(failure, BackgroundLaunchError) and not failure.cleanup_confirmed):
                        self._launch_failure = failure
            if isinstance(exc, OSError):
                raise MCPError(f"MCP server '{self.config.name}' 启动失败：{exc}", code="MCP_SERVER_START_FAILED") from exc
            raise

    # LLM: 只接受支持的协议及对象能力；initialize 和 initialized 始终使用同一候选连接。
    # 函数用途: 完成三步握手，把返回的服务信息保存在本连接。
    def _handshake(self, transport: MCPTransport) -> None:
        result = transport.request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "my-agent", "version": "1.0"},
        }, timeout=self.config.connect_timeout)
        if (not isinstance(result, dict) or result.get("protocolVersion") not in _SUPPORTED_PROTOCOL_VERSIONS
                or not isinstance(result.get("capabilities"), dict) or not isinstance(result.get("serverInfo"), dict)):
            raise MCPError("MCP 握手版本或能力结构不受支持", code="MCP_PROTOCOL_ERROR")
        transport.server_info = result["serverInfo"]
        transport.capabilities = result["capabilities"]
        transport.notify("notifications/initialized", {})

    # LLM: 先撤销再清理；启动未返回保持 pending，未接管资源的启动/清理未知异常仍保留，不能改报 not_started。
    # 函数用途: 永久关闭客户端，返回原资源或进程树回执，禁止后续重连。
    def stop(self) -> MCPCleanupReceipt:
        with self._state_lock:
            self._closed.set()
            transport, starting, failure = self._transport, self._starting, self._launch_failure
            if transport is not None:
                transport.revoke()
        if transport is not None:
            receipt = transport.terminate()
            if receipt.confirmed:
                atexit.unregister(self.stop)
            return receipt
        if failure is not None:
            raise failure
        if not starting:
            atexit.unregister(self.stop)
        return ProcessTerminationReceipt("launch_pending" if starting else "not_started", not starting, None, 0)

    # LLM: 临时失败只关闭调用方持有的 transport；传入旧连接不能改变或清理新连接。
    # 函数用途: 清理一次失败的握手后发现或调用连接，保留客户端在下轮重连的资格。
    def disconnect(self, *, transport: MCPTransport) -> MCPCleanupReceipt:
        with self._state_lock:
            transport.revoke()
        return transport.terminate()

    # LLM: 重连先确认原树清理；未知保留原句柄，不启动替代进程。新连接不复用队列和事件。
    # 函数用途: 清理断开的原连接并创建下一条连接，返回新的固定句柄。
    def reconnect(self) -> MCPTransport:
        deadline = time.monotonic() + self.config.connect_timeout
        with bounded_mcp_lock(self._connect_lock, deadline, closed=self._closed):
            if self.is_running():
                return self.connection()
            with self._state_lock:
                previous = self._transport
            if previous is not None:
                receipt = self.disconnect(transport=previous)
                if not receipt.confirmed:
                    raise MCPError("MCP 原连接进程清理尚未确认，拒绝重连", code="MCP_CLEANUP_UNKNOWN")
            with self._state_lock:
                if self.is_closed():
                    raise MCPError("MCP 客户端已永久关闭", code="MCP_CONNECTION_CLOSED")
                self._transport = None
            return self.start()

    # LLM: 调用前冻结当前已握手连接，之后所有排队与收发只使用返回值；不能事后重取 current。
    # 函数用途: 为一次业务调用或目录发现获取固定连接。
    def connection(self) -> MCPTransport:
        with self._state_lock:
            transport = self._transport
            self._require_current(transport)
            if transport is None or not transport.ready:
                raise MCPError("MCP 尚未完成握手", code="MCP_CONNECTION_CLOSED")
            return transport

    # LLM: 必须在状态锁内核对原对象；连接 EOF、显式断开、替换或永久关闭都不能继续发布。
    # 函数用途: 拒绝过期或已关闭的候选连接。
    def _require_current(self, transport: MCPTransport | None) -> None:
        if (self.is_closed() or transport is None or self._transport is not transport
                or transport.inbox.closed.is_set()):
            raise MCPError("MCP 连接已关闭或被替换", code="MCP_CONNECTION_CLOSED")

    # LLM: 发布仅替换内存目录，插件必须先已提交 active；停用竞态后的旧目录仍受实际发送准入约束，不据目录授予执行权。
    # 函数用途: 复查原连接和激活后发布候选工具目录，禁止准备中或撤销代向普通调用方发布。
    def publish_tools(self, transport: MCPTransport, publisher: Callable[[], _Publication]) -> _Publication:
        with self._state_lock:
            with transport.admission_lock:
                self._require_current(transport)
                if not transport.is_running():
                    raise MCPError("MCP 连接在目录发布前已退出", code="MCP_CONNECTION_CLOSED")
                if self._activation is not None:
                    self._activation.require()
                return publisher()

    # LLM: 所有分页固定同一 transport；发布前仍须由注册层调用 publish_tools 校验有效性。
    # 函数用途: 获取完整工具目录，保留发现期间发生的目录变更通知。
    def list_tools(self, *, transport: MCPTransport | None = None) -> list[MCPToolInfo]:
        selected = transport if transport is not None else self.connection()
        with self._state_lock:
            self._require_current(selected)
        generation = selected.inbox.tools_generation
        tools = _discover_tools(selected.request, self.config.connect_timeout)
        with selected.inbox.condition:
            selected.inbox.tools_changed = generation != selected.inbox.tools_generation
        return tools

    # LLM: 已发布代理冻结 transport；选择失败明确未发送，实际请求后的副作用只由 transport 裁决，不转投新实例。
    # 函数用途: 调用固定连接上的 MCP 工具，区分旧连接拒绝与已发送失败，规范化完整结果。
    def call_tool(self, tool_name: str, arguments: dict[str, Any], *, transport: MCPTransport | None = None,
                  authority_check: Callable[[], None] | None = None) -> dict[str, Any]:
        try:
            selected = transport if transport is not None else self.connection()
            with self._state_lock:
                self._require_current(selected)
        except MCPError as exc:
            exc.effect_outcome = "not_started"
            raise
        result = selected.request("tools/call", {"name": tool_name, "arguments": arguments or {}},
                                  timeout=self.config.timeout, authority_check=authority_check)
        return _normalize_call_result(result, self.config.max_content_chars)
