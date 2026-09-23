# LLM: 独立 stdio MCP 示例只消费宿主逐次上下文；标准输出仅协议，不创建线程、Shell、网络或持久状态。
# 模块用途: 将同源工具声明和只读业务接到原 MCP 传输，输入错误不影响后续正常请求。

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

from .declarations import declaration, fields
from .preview import preview
from .reading import ReadError
from .tree import directory_tree


# LLM: 对象仅持有本进程静态声明和已验证配置；不缓存调用权限，也不拥有宿主安装或激活状态。
# 类用途: 处理标准 MCP 初始化、工具目录及逐次只读调用。
class PeekServer:
    # LLM: 设置仅从宿主启动时注入的专属环境值读取；坏配置明确启动失败，不静默吞掉配置。
    # 函数用途: 加载同源声明、工具索引和默认设置。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
        self.initialized = False

    # LLM: 协议错误用 JSON-RPC error；业务读取错误是 isError 工具结果，不能输出本地 traceback 或配置。
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
                "name": "workspace-peek", "version": version("my-agent-workspace-peek")},
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

    # LLM: 权限仅从 _meta 取得，arguments 不能冒充上下文；每次调用新建上下文，错误不回退插件安装目录。
    # 函数用途: 验证参数与当前权限，再返回实际文件页或目录页。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise ReadError("UNKNOWN_TOOL", "工具不存在。")
            arguments = fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            metadata = params.get("_meta")
            if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
                raise ReadError("MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。")
            context = WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
            if name == "show":
                value = preview(context, arguments["path"], budget=arguments.get("bytes", self.settings["page_bytes"]),
                                cursor=arguments.get("cursor"))
            else:
                value = directory_tree(context, arguments["path"], depth=arguments["depth"],
                    limit=arguments.get("limit", self.settings["page_entries"]), scan_budget=self.settings["scan_entries"],
                    cursor=arguments.get("cursor"))
            return self.result(value, False)
        except ReadError as exc:
            return self.result({"code": exc.code, "message": str(exc)}, True)
        except NoFollowPathError:
            return self.result({"code": "UNSAFE_PATH", "message": "路径含链接、非普通对象或平台缺少安全读取能力。"}, True)
        except OSError:
            return self.result({"code": "READ_FAILED", "message": "对象不存在、无法读取或在读取期间变化。"}, True)
        except (ValueError, TypeError):
            return self.result({"code": "INVALID_CONTEXT", "message": "宿主读取上下文无效。"}, True)

    # LLM: 结果使用原 MCP 文本结构，正文为结构化 JSON；不伪装自定义 TUI 面板。
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
        server = PeekServer()
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
