# LLM: 独立 stdio MCP 服务只消费宿主逐次上下文；标准输出仅协议，不创建线程、Shell 或网络请求。
#   握手同时声明读取与写入上下文扩展；宿主只给 requested_effect=mutating 的 restore 附带写入上下文。
# 模块用途: 将同源工具声明和快照业务接到原 MCP 传输，输入错误不影响后续正常请求。

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import NoFollowPathError
from my_agent_plugin_api.workspace_read_context import (
    WORKSPACE_READ_EXTENSION,
    WORKSPACE_READ_VERSION,
    WorkspaceReadContext,
)
from my_agent_plugin_api.workspace_write_context import (
    WORKSPACE_WRITE_EXTENSION,
    WORKSPACE_WRITE_VERSION,
    WorkspaceWriteContext,
)

from .declarations import declaration, fields
from .operations import list_snapshots, restore_snapshot, save_snapshot
from .snapshots import SavepointError, SnapshotStore

DATA_DIR_ENV = "MY_AGENT_PLUGIN_DATA_DIR"


# LLM: 对象只持有静态声明、已验证设置和宿主传入的数据目录；不缓存调用权限，也不拥有宿主安装或激活状态。
# 类用途: 处理标准 MCP 初始化、工具目录及逐次快照调用。
class SavepointServer:
    # LLM: 设置只从宿主注入的 MY_AGENT_PLUGIN_SETTINGS 读取，坏配置明确启动失败；数据目录缺失或非绝对路径时
    #   不回退 cwd，推迟到调用时返回明确错误。
    # 函数用途: 加载同源声明、工具索引、默认设置和插件数据目录。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.settings = fields(json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}")),
                               self.declaration["settings_schema"])
        data_dir = os.environ.get(DATA_DIR_ENV, "")
        self.store = SnapshotStore(Path(data_dir)) if data_dir and Path(data_dir).is_absolute() else None
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
                "name": "savepoint-lite", "version": version("my-agent-savepoint-lite")},
                "capabilities": {"tools": {}, "experimental": {
                    WORKSPACE_READ_EXTENSION: {"versions": [WORKSPACE_READ_VERSION]},
                    WORKSPACE_WRITE_EXTENSION: {"versions": [WORKSPACE_WRITE_VERSION]}}}}
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

    # LLM: 权限只从 _meta 取得，arguments 不能冒充上下文；每次调用新建上下文。restore 缺写入上下文直接失败，不降级。
    # 函数用途: 验证参数、上下文和数据目录，再执行对应的快照动作，所有失败都转成中文错误结果。
    def call(self, params: dict) -> dict:
        try:
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                raise SavepointError("UNKNOWN_TOOL", "工具不存在。")
            arguments = fields(params.get("arguments", {}), self.tools[name]["input_schema"])
            metadata = params.get("_meta")
            if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
                raise SavepointError("MISSING_CONTEXT", "缺少宿主逐次工作区读取上下文。")
            if name == "restore" and WORKSPACE_WRITE_EXTENSION not in metadata:
                raise SavepointError("MISSING_WRITE_CONTEXT", "缺少宿主工作区写入上下文，不能恢复文件。")
            if self.store is None:
                raise SavepointError("MISSING_DATA_DIR", "宿主未提供插件数据目录，无法存取快照。")
            context = WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
            if name == "save":
                value = save_snapshot(context, self.store, arguments["path"], self.settings)
            elif name == "list":
                value = list_snapshots(context, self.store, arguments["path"])
            else:
                write = WorkspaceWriteContext.from_payload(metadata[WORKSPACE_WRITE_EXTENSION])
                value = restore_snapshot(context, write, self.store, arguments["path"], arguments["id"], arguments["expect"])
            return self.result(value, False)
        except SavepointError as exc:
            return self.result({"code": exc.code, "message": str(exc), **exc.extra}, True)
        except NoFollowPathError:
            return self.result({"code": "UNSAFE_PATH", "message": "路径含符号链接、多链接文件或非普通对象，已拒绝。"}, True)
        except OSError:
            return self.result({"code": "IO_FAILED", "message": "文件或快照读写失败，请稍后重试。"}, True)
        except (ValueError, TypeError):
            return self.result({"code": "INVALID_CONTEXT", "message": "宿主工作区上下文无效。"}, True)

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
        server = SavepointServer()
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
