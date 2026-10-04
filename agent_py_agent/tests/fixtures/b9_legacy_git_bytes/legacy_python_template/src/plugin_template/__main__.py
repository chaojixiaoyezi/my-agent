# LLM: 独立 MCP stdio 入口；只读随包声明并在内存处理文本，stdout 只写协议，业务不访问路径或宿主状态。
# 模块用途: 给插件作者一个无需 SDK/第三方运行依赖的单工具起点；加文件功能时必须改用 SDK 逐次权限上下文。
from __future__ import annotations

import json
import sys
from pathlib import Path

DECLARATION = json.loads(Path(__file__).with_name("declaration.json").read_text(encoding="utf-8"))
MAX_LINE = 131072


# LLM: 业务只接受声明中的唯一字符串参数，拒绝附加字段，不把用户输入当路径或代码。
# 函数用途: 返回字符数或结构化业务错误，不产生文件和网络副作用。
def call_tool(params: dict) -> dict:
    arguments = params.get("arguments")
    if params.get("name") != "count_text":
        value, failed = {"code": "UNKNOWN_TOOL", "message": "工具不存在。"}, True
    elif (not isinstance(arguments, dict) or set(arguments) != {"text"}
          or not isinstance(arguments["text"], str) or len(arguments["text"]) > 4096):
        value, failed = {"code": "INVALID_ARGUMENTS", "message": "需要不超过 4096 字符的 text。"}, True
    else:
        value, failed = {"characters": len(arguments["text"])}, False
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": failed}


# LLM: tools/list 和包描述必须同源；只投影声明，不读取用户文件或维护第二份工具目录。
# 函数用途: 返回 MCP 工具目录，把投影与请求路由分开以保持浅层控制流。
def tool_catalog() -> list[dict]:
    return [{"name": tool["name"], "description": tool["description"],
             "inputSchema": tool["input_schema"]} for tool in DECLARATION["tools"]]


# LLM: 错误只包含固定中文说明，不回显输入；JSON-RPC 分类与业务 isError 分开。
# 函数用途: 构造协议错误帧。
def rpc_error(identifier: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


# LLM: 协议成功帧与业务 isError 独立；只封装结果，不重解释或授予权限。
# 函数用途: 构造统一的成功协议帧，供浅层路由直接返回。
def rpc_result(identifier: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


# LLM: tools/list 必须与最终包同源；通知不响应，握手不授予权限，各方法早返回避免嵌套累积。
# 函数用途: 路由一个 MCP 请求，只处理握手、目录、调用和 ping。
def handle(request: object) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "请求格式无效。")
    if "id" not in request:
        return None
    identifier, method = request["id"], request["method"]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "initialize":
        return rpc_result(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                                       "serverInfo": {"name": DECLARATION["plugin_id"], "version": "0.1.0"}})
    if method == "tools/list":
        return rpc_result(identifier, {"tools": tool_catalog()})
    if method == "tools/call":
        return rpc_result(identifier, call_tool(params))
    if method == "ping":
        return rpc_result(identifier, {})
    return rpc_error(identifier, -32601, "不支持此方法。")


# LLM: 解析错误不终止后续正常请求；标准输出只允许 JSON 帧，不能混入调试 print。
# 函数用途: 解析并处理一行协议输入。
def process_line(line: str) -> dict | None:
    try:
        request = json.loads(line)
    except ValueError:
        return rpc_error(None, -32700, "请求不是有效 JSON。")
    return handle(request)


# LLM: stdin EOF 自然结束，不拉起后台任务；读取限额控制单帧，诊断走 stderr，修改时联测打包后的 stdio。
# 函数用途: 运行单进程 MCP 服务，写响应帧但不写任何业务文件。
def main() -> int:
    while line := sys.stdin.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE:
            sys.stderr.write("插件请求超过读取上限。\n")
            return 2
        response = process_line(line)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
