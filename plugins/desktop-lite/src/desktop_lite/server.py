# LLM: 独立 stdio MCP 服务只消费宿主逐次读取上下文；标准输出仅协议。不调用任何模型、不联网、不写工作区；
#   唯一的外部效果是调用本机系统程序（见 commands.py），所以三个工具都声明 mutating，走宿主原审批链。
# 模块用途: 将同源工具声明与通知、打开、剪贴板业务接到原 MCP 传输，输入错误不影响后续正常请求。

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version

from my_agent_plugin_api.nofollow_fs import NoFollowPathError
from my_agent_plugin_api.workspace_read_context import (
    WORKSPACE_READ_EXTENSION,
    WORKSPACE_READ_VERSION,
    WorkspaceReadContext,
)

from .commands import DesktopError, clipboard_command, notify_command, open_command, run
from .declarations import declaration, fields
from .opening import openable_file


# LLM: 对象只持有静态声明和已验证设置；不缓存调用权限，也不拥有宿主安装或激活状态。
# 类用途: 处理标准 MCP 初始化、工具目录及逐次桌面工具调用。
class DesktopLiteServer:
    # LLM: 设置只从宿主注入的 MY_AGENT_PLUGIN_SETTINGS 读取，坏配置明确启动失败。
    # 函数用途: 加载同源声明、工具索引和默认设置。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
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
            result = {"protocolVersion": "2024-11-05", "serverInfo": {
                "name": "desktop-lite", "version": version("my-agent-desktop-lite")},
                "capabilities": {"tools": {}, "experimental": {WORKSPACE_READ_EXTENSION: {"versions": [WORKSPACE_READ_VERSION]}}}}
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

    # LLM: 权限只从 _meta 取得，arguments 不能冒充上下文；每次调用新建上下文，三个工具一律要求上下文。
    #   所有失败转成中文错误结果；有副作用：成功路径会启动一个系统程序。
    # 函数用途: 验证参数与上下文，组装对应命令并运行，返回实际程序与退出码。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise DesktopError("UNKNOWN_TOOL", "工具不存在。")
            arguments = fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            metadata = params.get("_meta")
            if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
                raise DesktopError("MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。")
            context = WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
            if name == "notify":
                command = notify_command(self.settings, arguments["title"], arguments["message"])
                extra = {}
            elif name == "open":
                target = openable_file(context, arguments["path"])
                command, extra = open_command(self.settings, target), {"path": str(target)}
            else:
                command, extra = clipboard_command(self.settings, arguments["text"]), {"chars": len(arguments["text"])}
            return self.result({"tool": name, **run(command, self.settings["command_timeout_seconds"]), **extra}, False)
        except DesktopError as exc:
            return self.result({"code": exc.code, "message": str(exc), **exc.extra}, True)
        except NoFollowPathError:
            return self.result({"code": "UNSAFE_PATH", "message": "路径含符号链接、多链接文件或非普通对象，已拒绝。"}, True)
        except OSError:
            return self.result({"code": "IO_FAILED", "message": "文件检查失败，请稍后重试。"}, True)
        except (ValueError, TypeError):
            return self.result({"code": "INVALID_CONTEXT", "message": "宿主工作区上下文无效。"}, True)

    # LLM: 结果使用原 MCP 文本结构，正文为结构化 JSON；不回显通知或剪贴板正文。
    # 函数用途: 编码正常或错误的工具返回值。
    @staticmethod
    def result(value: dict, error: bool) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": error}

    # LLM: 这里只生成协议响应，不输出调试堆栈或输入正文。
    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: 单条输入有界；超长行关闭本进程交由宿主记录真实退出，不在坏帧后猜测请求边界。
# 函数用途: 运行插件标准输入输出入口，每条正常请求即时刷新响应。
def main() -> int:
    try:
        server = DesktopLiteServer()
    except (ValueError, TypeError):
        print("插件设置无效。", file=sys.stderr)
        return 2
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
