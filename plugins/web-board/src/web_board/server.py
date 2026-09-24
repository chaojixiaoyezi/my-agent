# LLM: 独立 stdio MCP 服务；标准输出仅协议。serve 冻结本次调用的读取上下文交给网页服务，status/stop 不需要上下文。
#   同一进程最多一个网页服务；stdin EOF 或 SIGTERM 时 main 在 finally 中关闭服务，线程均为 daemon，不会拖住进程退出。
#   不写工作区、不调用模型、不访问外网；唯一的网络动作是在 127.0.0.1 随机端口监听。
# 模块用途: 将同源工具声明接到原 MCP 传输，并管理网页服务的单实例生命周期。

from __future__ import annotations

import json
import os
import signal
import sys
from importlib.metadata import version

from my_agent_plugin_api.nofollow_fs import NoFollowPathError
from my_agent_plugin_api.workspace_read_context import (
    WORKSPACE_READ_EXTENSION,
    WORKSPACE_READ_VERSION,
    WorkspaceReadContext,
)

from .board import BoardService
from .declarations import declaration, fields
from .reading import BoardError, authorized_root


# LLM: 对象持有静态声明、已验证设置和当前（或最近一次）网页服务；不缓存除 serve 冻结上下文之外的权限。
# 类用途: 处理标准 MCP 初始化、工具目录及 serve/status/stop 调用。
class WebBoardServer:
    # LLM: 设置只从宿主注入的 MY_AGENT_PLUGIN_SETTINGS 读取，坏配置明确启动失败，不静默回退。
    # 函数用途: 加载同源声明、工具索引和默认设置。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
        self.board: BoardService | None = None
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
                "name": "web-board", "version": version("my-agent-web-board")},
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

    # LLM: 权限只从 _meta 取得，arguments 不能冒充上下文；只有 serve 需要并冻结上下文。所有失败转成中文错误结果。
    # 函数用途: 验证参数后执行 serve/status/stop。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise BoardError(400, "UNKNOWN_TOOL", "工具不存在。")
            arguments = fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            if name == "serve":
                metadata = params.get("_meta")
                if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
                    raise BoardError(400, "MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。")
                context = WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
                return self.result(self.serve(context, arguments["path"]), False)
            if name == "stop":
                return self.result(self.stop("stopped"), False)
            return self.result(self.status(), False)
        except BoardError as exc:
            return self.result({"code": exc.code, "message": str(exc)}, True)
        except NoFollowPathError:
            return self.result({"code": "UNSAFE_PATH", "message": "路径含符号链接、非目录组件或平台缺少安全读取能力。"}, True)
        except OSError:
            return self.result({"code": "IO_FAILED", "message": "目录无法打开或本地端口无法监听。"}, True)
        except (ValueError, TypeError):
            return self.result({"code": "INVALID_CONTEXT", "message": "宿主工作区上下文无效。"}, True)

    # LLM: 先验证新目录再停旧服务，验证失败时旧服务保持不变。副作用：关闭旧端口、监听新端口、启动线程。
    # 函数用途: 启动（或替换）网页服务并返回访问地址。
    def serve(self, context: WorkspaceReadContext, path: str) -> dict:
        root = authorized_root(context, path)
        replaced = self.board is not None and self.board.stop_reason is None
        self.stop("replaced")
        self.board = BoardService(context, root, max_preview_bytes=self.settings["max_preview_bytes"],
                                  idle_stop_seconds=self.settings["idle_stop_seconds"])
        return {**self.board.snapshot(), "replaced_previous": replaced}

    # 函数用途: 返回当前服务状态；从未启动时返回未运行。
    def status(self) -> dict:
        if self.board is None:
            return {"serving": False, "url": None, "root": None, "port": None, "requests": 0, "stop_reason": None}
        return self.board.snapshot()

    # LLM: 幂等；服务已因空闲停止时只返回状态。副作用：关闭监听端口。
    # 函数用途: 停止当前服务并返回停止后的状态。
    def stop(self, reason: str) -> dict:
        if self.board is not None:
            self.board.close(reason)
        return self.status()

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


# LLM: SIGTERM 转成 SystemExit，让 finally 正常关闭服务；单条输入有界，超长行关闭本进程。
# 函数用途: 运行插件标准输入输出入口，退出时一并停止网页服务。
def main() -> int:
    try:
        server = WebBoardServer()
    except (ValueError, TypeError):
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
