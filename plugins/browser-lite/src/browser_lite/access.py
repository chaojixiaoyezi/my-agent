# LLM: 地址白名单的唯一裁决：file:// 只经宿主逐次读取上下文 check 放行，http(s) 只放行设置 allowed_hosts 里的主机，
#   其余协议一律拒绝（about:blank 仅作为插件自己的空白页）。这里不做 DNS 解析，按 URL 主机名字面比较。
# 模块用途: 判断浏览器可访问的地址，并把 open 的输入整理成完整 URL。

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .errors import BrowserError

BLANK_URL = "about:blank"


# LLM: 规范化 http(s) origin（scheme://host:port），拒绝内嵌账号密码或非 http(s) 协议；允许页面当前地址带 hash/query。
# 函数用途: 计算浏览器页面或请求目标的标准同源标识，非合法 http(s) 地址返回 None。
def http_origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    resolved_port = port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{resolved_port}"


# LLM: 一次工具调用一个实例；读取上下文来自宿主本次 _meta，主机名单来自已校验设置，均不缓存到下一次调用。
#   已绑定受保护凭据的会话额外携带 allowed_origins，防止跳到同主机其它端口或 file 页面。
# 类用途: 本次调用的地址守卫。
class UrlGuard:
    # LLM: 主机名统一小写比较；不接受通配符。
    # 函数用途: 绑定读取上下文、允许的主机名单与可选同源集合。
    def __init__(self, context: WorkspaceReadContext, hosts: list[str], *,
                 allowed_origins: set[str] | frozenset[str] | None = None):
        self.context = context
        self.hosts = {host.lower() for host in hosts}
        self.allowed_origins = frozenset(allowed_origins) if allowed_origins else frozenset()

    # 函数用途: 生成限定到已授权凭据 origin 集合的新守卫副本，不修改原守卫。
    def with_allowed_origins(self, origins: set[str] | frozenset[str]) -> UrlGuard:
        return UrlGuard(self.context, list(self.hosts), allowed_origins=origins)

    # LLM: 返回拒绝原因（中文）或 None；file 路径先 unquote 再交给 SDK check（check 内会 resolve 符号链接）。
    # 函数用途: 裁决一个地址是否允许浏览器访问。
    def reason(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return "地址格式无效"
        scheme = parts.scheme.lower()
        if scheme in ("http", "https"):
            if host not in self.hosts:
                return f"主机 {host or '(空)'} 不在设置 allowed_hosts 里"
            if self.allowed_origins:
                origin = http_origin(url)
                if origin is None or origin not in self.allowed_origins:
                    return f"地址 {origin or url[:120]} 不属于当前凭据会话绑定的同源站点"
            return None
        if scheme == "file":
            if self.allowed_origins:
                return "已绑定凭据的浏览器会话不允许切换到本地 file 页面"
            path = Path(unquote(parts.path))
            if parts.netloc not in ("", "localhost") or not path.is_absolute():
                return "file 地址必须是本机绝对路径"
            if not self.context.check(path).allowed:
                return "file 页面不在本次允许读取的工作区范围内"
            return None
        if url == BLANK_URL:
            return None
        return f"不支持的地址协议 {scheme or '(无)'}"

    # LLM: 拒绝时抛 URL_NOT_ALLOWED，并把地址（截断）放进结构化字段。
    # 函数用途: 要求地址被允许。
    def require(self, url: str) -> None:
        reason = self.reason(url)
        if reason is not None:
            raise BrowserError("URL_NOT_ALLOWED", f"地址不允许：{reason}。", url=url[:500])

    # LLM: 没有协议的输入按当前工作区相对/绝对文件路径处理并转成 file:// URL；带协议的原样返回，由 require 裁决。
    # 函数用途: 把 open 的 --url 输入整理成完整 URL。
    def target(self, value: str) -> str:
        if "\x00" in value:
            raise BrowserError("INVALID_ARGUMENTS", "地址不能包含空字符。")
        if urlsplit(value).scheme:
            return value
        return (self.context.cwd / value).as_uri()
