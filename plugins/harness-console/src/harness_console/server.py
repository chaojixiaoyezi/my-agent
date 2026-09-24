# LLM: 独立 stdio MCP 服务；标准输出仅协议。同一进程最多一个工作台网页服务和一个应用窗口子进程；
#   stdin EOF 或 SIGTERM 时 main 在 finally 中关闭服务与窗口。宿主只读 API 令牌只由 HostClient 在服务端使用，
#   不进入工具结果、网页或日志；工具结果里的地址不含会话令牌，完整链接只写入插件数据目录的 0600 文件。
# 模块用途: 将同源工具声明接到原 MCP 传输，并管理 open/desktop/stop 的服务与窗口生命周期。

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from .console import ConsoleService
from .declarations import ConsoleError, declaration, fields
from .host import UNAVAILABLE, HostClient
from .window import close_window, find_browser, launch_app_window, open_in_browser


# LLM: 数据目录由宿主注入；没有时返回 None（不写链接文件、桌面窗口无专属 profile 可用）。
# 函数用途: 返回插件数据目录，不存在时为 None。
def data_dir() -> Path | None:
    base = os.environ.get("MY_AGENT_PLUGIN_DATA_DIR", "")
    return Path(base) if base and os.path.isdir(base) else None


# LLM: 完整链接含会话令牌，宿主会从工具结果里脱敏，所以只写到插件数据目录下 0600 文件。副作用：写文件。
# 函数用途: 保存最近一次服务的完整链接，返回文件路径或 None。
def write_link_file(url: str) -> str | None:
    base = data_dir()
    if base is None:
        return None
    path = str(base / "last-link.txt")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(url + "\n")
    return path


# LLM: 对象持有静态声明、已验证设置、宿主 API 客户端、当前网页服务和应用窗口进程。
# 类用途: 处理标准 MCP 初始化、工具目录及 open/desktop/stop 调用。
class ConsoleServer:
    # LLM: 设置只从宿主注入的 MY_AGENT_PLUGIN_SETTINGS 读取，坏配置明确启动失败，不静默回退。
    # 函数用途: 加载同源声明、工具索引、设置和宿主 API 客户端。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
        self.host = HostClient()
        self.service: ConsoleService | None = None
        self.window: subprocess.Popen | None = None
        self.initialized = False

    # LLM: 协议错误用 JSON-RPC error；业务错误是 isError 工具结果，不能输出本地 traceback 或配置。
    # 函数用途: 分派单条协议请求，通知无需响应。
    def handle(self, request: object) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "请求格式无效。")
        if "id" not in request:
            return None
        request_id, method = request["id"], request["method"]
        if type(request_id) not in (int, str):
            return self.error(None, -32600, "请求编号无效。")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "参数格式无效。")
        if method == "initialize":
            self.initialized = True
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "harness-console", "version": version("my-agent-harness-console")}}
        elif not self.initialized:
            return self.error(request_id, -32000, "请先初始化连接。")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
                                for tool in self.tools.values()]}
        elif method == "tools/call":
            result = self.call(params)
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # LLM: 三个工具都没有参数；所有失败转成中文错误结果，端口无法监听归为 IO_FAILED。
    # 函数用途: 验证参数后执行 open/desktop/stop。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise ConsoleError("UNKNOWN_TOOL", "工具不存在。")
            fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            value = self.open() if name == "open" else self.desktop() if name == "desktop" else self.stop("stopped")
            return self.result(value, False)
        except ConsoleError as exc:
            return self.result({"code": exc.code, "message": str(exc)}, True)
        except OSError:
            return self.result({"code": "IO_FAILED", "message": "本地端口无法监听或链接文件无法写入。"}, True)

    # LLM: 服务在运行就复用（同一会话令牌，已打开的窗口继续有效）；否则新建。副作用：监听端口、写链接文件。
    # 函数用途: 确保工作台服务在运行，返回基础结果字段。
    def ensure_service(self) -> dict:
        reused = self.service is not None and self.service.running
        if not reused:
            self.service = ConsoleService(self.host, idle_stop_seconds=self.settings["idle_stop_seconds"],
                                          poll_seconds=self.settings["poll_seconds"])
        return {**self.service.snapshot(), "reused": reused, "link_file": write_link_file(self.service.url),
                "host_api": "ok" if self.host.available else "unavailable",
                **({} if self.host.available else {"host_api_message": UNAVAILABLE})}

    # LLM: open_browser 设置决定是否调用系统默认浏览器；副作用见 ensure_service 与 open_in_browser。
    # 函数用途: 启动（或复用）工作台并按设置用默认浏览器打开。
    def open(self) -> dict:
        base = self.ensure_service()
        opened = open_in_browser(self.service.url) if self.settings["open_browser"] else False
        return {**base, "browser_opened": opened,
                "hint": "带令牌的完整链接已写入 link_file（仅本人可读）" + ("，并已在默认浏览器打开。" if opened else "。")}

    # LLM: 窗口仍在运行就复用；否则找 Chrome/Chromium 以 --app 启动（profile 在插件数据目录），
    #   找不到或启动失败时回退默认浏览器，都不行时 window=unavailable。副作用：启动子进程或打开浏览器。
    # 函数用途: 启动（或复用）工作台并以独立应用窗口打开。
    def desktop(self) -> dict:
        base = self.ensure_service()
        if self.window is not None and self.window.poll() is None:
            return {**base, "window": "app", "window_pid": self.window.pid, "window_reused": True}
        executable, directory = find_browser(self.settings["chrome_path"]), data_dir()
        self.window = (launch_app_window(executable, self.service.url, directory / "app-profile")
                       if executable is not None and directory is not None else None)
        if self.window is not None:
            return {**base, "window": "app", "window_pid": self.window.pid, "window_reused": False}
        if open_in_browser(self.service.url):
            return {**base, "window": "browser", "window_pid": None,
                    "hint": "未找到可用的 Chrome/Chromium（或缺插件数据目录），已改用系统默认浏览器打开工作台。"}
        return {**base, "window": "unavailable", "window_pid": None,
                "hint": "桌面窗口不可用：没有可用的 Chrome/Chromium，也无法调用系统默认浏览器；请从 link_file 取链接手动打开。"}

    # LLM: 幂等；副作用：关闭监听端口、结束由 desktop 启动的窗口进程。
    # 函数用途: 停止服务和应用窗口并返回停止后的状态。
    def stop(self, reason: str) -> dict:
        if self.service is not None:
            self.service.close(reason)
        closed = close_window(self.window)
        self.window = None
        state = self.service.snapshot() if self.service is not None else {
            "serving": False, "address": None, "port": None, "requests": 0, "stop_reason": None}
        return {**state, "window_closed": closed}

    # LLM: 结果使用原 MCP 文本结构，正文为结构化 JSON。
    # 函数用途: 编码正常或错误的工具返回值。
    @staticmethod
    def result(value: dict, error: bool) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": error}

    # LLM: 这里只生成协议响应，不输出调试堆栈或输入正文。
    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: SIGTERM 转成 SystemExit，让 finally 正常关闭服务与窗口；单条输入有界，超长行关闭本进程。
# 函数用途: 运行插件标准输入输出入口，退出时一并停止网页服务和应用窗口。
def main() -> int:
    try:
        server = ConsoleServer()
    except (ValueError, TypeError, ConsoleError):
        print("插件设置无效。", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while line := sys.stdin.buffer.readline(131073):
            if len(line) > 131072:
                print("插件请求超过读取上限。", file=sys.stderr)
                return 2
            try:
                response = server.handle(json.loads(line))
            except (ValueError, UnicodeError, RecursionError):
                response = server.error(None, -32700, "请求 JSON 无效。")
            if response is not None:
                print(json.dumps(response, ensure_ascii=True), flush=True)
        return 0
    finally:
        server.stop("process_exit")
