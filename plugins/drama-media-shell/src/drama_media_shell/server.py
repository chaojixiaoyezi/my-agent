# LLM: MCP 入口只做协议、参数、逐次上下文恢复和业务分派；权限不缓存，stdout 只输出 JSON-RPC 帧。
# 模块用途: 暴露四个只读检查器和 fixture-only 生产作业外壳。

from __future__ import annotations

import json
import sys
from importlib.metadata import version

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

from . import container_check, image_prompt_check, motion_timing_check, music_spec_check
from .declarations import declaration, fields
from .errors import DramaShellError
from .production import collect_job, confirm_job, job_status, prepare_job, run_job
from .production_audit import audit_project
from .workspace_io import read_json_object, read_jsonl, split_sources

CallContexts = tuple[WorkspaceReadContext, WorkspaceWriteContext | None]


# LLM: 服务对象只缓存静态声明和握手状态；读取/写入上下文必须从每次 tools/call 的 _meta 单独恢复。
# 类用途: 处理 MCP 握手、工具目录和 drama-media-shell 工具调用。
class DramaMediaServer:
    # LLM: 唯一工具索引来自 declaration.json，避免 tools/list 与包描述漂移。
    # 函数用途: 载入插件声明并建立工具名索引。
    def __init__(self):
        self.declaration = declaration()
        self.tools = {tool["name"]: tool for tool in self.declaration["tools"]}
        self.initialized = False

    # LLM: 协议错误使用 JSON-RPC error；工具业务失败使用 isError 结果，标准输出不得混入日志。
    # 函数用途: 分派一条 MCP 请求。
    def handle(self, request: object) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "请求格式无效。")
        if "id" not in request:
            return None
        request_id, method = request["id"], request["method"]
        params = request.get("params", {})
        if type(request_id) not in (int, str) or not isinstance(params, dict):
            return self.error(None, -32600, "请求编号或参数格式无效。")
        if method == "initialize":
            self.initialized = True
            result = self._initialize_result()
        elif not self.initialized:
            return self.error(request_id, -32000, "请先初始化连接。")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": tool["name"], "description": tool["description"],
                                 "inputSchema": tool["input_schema"]} for tool in self.tools.values()]}
        elif method == "tools/call":
            result = self.call(params)
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # LLM: call 先按声明校验业务参数，再恢复宿主上下文；业务参数中的任何路径或伪 _meta 都不能授予权限。
    # 函数用途: 执行一次已声明 MCP 工具调用并封装结构化结果。
    def call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            return self.result({"code": "UNKNOWN_TOOL", "message": "工具不存在。"}, True)
        arguments = params.get("arguments", {})
        try:
            fields(arguments, self.tools[name]["input_schema"])
            read_context = self._read_context(params)
            write_context = self._write_context(params) if name in {"production_run", "production_collect"} else None
            value = self._dispatch(name, arguments, (read_context, write_context))
            return self.result(value, False)
        except DramaShellError as exc:
            return self.result({"code": exc.code, "message": str(exc), **exc.extra}, True)
        except ValueError as exc:
            return self.result({"code": "CHECK_FAILED", "message": str(exc)}, True)
        except (KeyError, TypeError, OSError):
            return self.result({"code": "INTERNAL_ERROR", "message": "插件处理工具调用失败。"}, True)

    # LLM: 分派只调用纯数据检查器或受控生产外壳，不按名字动态导入或执行任意函数。
    # 函数用途: 将工具名映射到固定实现。
    def _dispatch(self, name: str, arguments: dict, contexts: CallContexts) -> dict:
        read_context, write_context = contexts
        if name == "image_prompt_check":
            records = read_jsonl(read_context, arguments["path"])
            sources, specs = split_sources(records)
            return image_prompt_check.validate_records(specs, sources)
        if name == "music_spec_check":
            return music_spec_check.validate_records(read_jsonl(read_context, arguments["path"]))
        if name == "container_check":
            return container_check.reconcile(
                read_jsonl(read_context, arguments["containers_path"], True),
                read_jsonl(read_context, arguments["shots_path"], True),
            )
        if name == "motion_timing_check":
            return motion_timing_check.check(
                read_jsonl(read_context, arguments["motion_specs_path"], True),
                read_jsonl(read_context, arguments["shots_path"], True),
            )
        return self._dispatch_production(name, arguments, contexts)

    # LLM: 生产工具共享状态机；只有 run/collect 会收到非空写入上下文并触碰工作区。
    # 函数用途: 分派 prepare、confirm、run、status、audit 和 collect。
    def _dispatch_production(self, name: str, arguments: dict, contexts: CallContexts) -> dict:
        read_context, write_context = contexts
        if name == "production_prepare":
            return prepare_job(read_context, read_json_object(read_context, arguments["job_path"]))
        if name == "production_confirm":
            return confirm_job(read_context, arguments["job_id"], arguments["confirmation"])
        if name == "production_status":
            return job_status(read_context, arguments["job_id"])
        if name == "production_audit":
            return audit_project(read_context)
        if write_context is None:
            raise DramaShellError("MISSING_WRITE_CONTEXT", "宿主未提供本次工作区写入上下文。")
        if name == "production_run":
            return run_job(read_context, write_context, arguments["job_id"])
        if name == "production_collect":
            return collect_job(read_context, write_context, arguments["job_id"])
        raise DramaShellError("UNKNOWN_TOOL", "工具不存在。")

    # LLM: 读取上下文只能来自 request params._meta 的固定扩展键，缺失或畸形都失败关闭。
    # 函数用途: 恢复本次调用的工作区读取上下文。
    @staticmethod
    def _read_context(params: dict) -> WorkspaceReadContext:
        metadata = params.get("_meta")
        if not isinstance(metadata, dict) or WORKSPACE_READ_EXTENSION not in metadata:
            raise DramaShellError("MISSING_READ_CONTEXT", "宿主未提供本次工作区读取上下文。")
        try:
            return WorkspaceReadContext.from_payload(metadata[WORKSPACE_READ_EXTENSION])
        except ValueError as exc:
            raise DramaShellError("INVALID_READ_CONTEXT", "工作区读取上下文无效。") from exc

    # LLM: 写上下文同样逐次恢复且不从读取根推导；mutating 工具缺写权限时不能退化成直接文件写入。
    # 函数用途: 恢复本次调用的工作区写入上下文。
    @staticmethod
    def _write_context(params: dict) -> WorkspaceWriteContext:
        metadata = params.get("_meta")
        if not isinstance(metadata, dict) or WORKSPACE_WRITE_EXTENSION not in metadata:
            raise DramaShellError("MISSING_WRITE_CONTEXT", "宿主未提供本次工作区写入上下文。")
        try:
            return WorkspaceWriteContext.from_payload(metadata[WORKSPACE_WRITE_EXTENSION])
        except ValueError as exc:
            raise DramaShellError("INVALID_WRITE_CONTEXT", "工作区写入上下文无效。") from exc

    # LLM: 初始化明确声明 SDK 0.2.0 对应的读写扩展版本，宿主据此决定是否附带逐次上下文。
    # 函数用途: 生成 MCP initialize 成功结果。
    @staticmethod
    def _initialize_result() -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "drama-media-shell", "version": version("my-agent-drama-media-shell")},
            "capabilities": {"tools": {}, "experimental": {
                WORKSPACE_READ_EXTENSION: {"versions": [WORKSPACE_READ_VERSION]},
                WORKSPACE_WRITE_EXTENSION: {"versions": [WORKSPACE_WRITE_VERSION]},
            }},
        }

    # LLM: 工具正文始终是结构化 JSON 文本；不能回显文件正文、本机异常或插件私有路径。
    # 函数用途: 编码正常或错误的 MCP 工具结果。
    @staticmethod
    def result(value: dict, error: bool) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": error}

    # LLM: 这里只生成 JSON-RPC 错误对象，不输出 traceback。
    # 函数用途: 生成协议级失败响应。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: 单帧最多 128 KiB；超长或坏 JSON 明确失败，正常响应逐条刷新。
# 函数用途: 运行标准输入输出 MCP 主循环。
def main() -> int:
    server = DramaMediaServer()
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
