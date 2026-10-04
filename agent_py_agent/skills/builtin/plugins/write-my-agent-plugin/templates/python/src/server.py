# LLM: v8 文件入口，握手声明观察/收紧版本 1；事件只回执，审核只返回收紧裁决，绝不执行收到的命令。
# 模块用途: 用标准库演示单只读工具和两个 v8 方法；不读宿主状态，真实隔离必须由 B7 宿主施加。
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DECLARATION = json.loads((Path(__file__).resolve().parents[1] / "declaration.json").read_text(encoding="utf-8"))
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


# LLM: tools/list 与声明同源，两个 experimental 位须与实现同步；通知不响应，任何握手不授权限。
# 函数用途: 路由标准 MCP 和 v8 观察/收紧请求，各方法早返回，不执行输入或生成宿主操作。
def handle(request: object) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "请求格式无效。")
    if "id" not in request:
        return None
    identifier, method = request["id"], request["method"]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "initialize":
        return rpc_result(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "experimental": {
            "my-agent/events": {"versions": ["1"]}, "my-agent/tool-gate": {"versions": ["1"]}}},
            "serverInfo": {"name": DECLARATION["plugin_id"], "version": DECLARATION["version"]}})
    if method == "tools/list":
        return rpc_result(identifier, {"tools": tool_catalog()})
    if method == "tools/call":
        return rpc_result(identifier, call_tool(params))
    if method == "my-agent/events.observe":
        return rpc_result(identifier, {})
    if method == "my-agent/tool-gate.review":
        return rpc_result(identifier, review_gate(params))
    if method == "ping":
        return rpc_result(identifier, {})
    return rpc_error(identifier, -32601, "不支持此方法。")


# LLM: 仅检查清单精确工具范围内的结构化 command；参数缺失或截断时按 ask，不改参数、不执行命令、不放宽宿主。
#   宿主只在 arguments: full 时给 arguments_truncated，所以看到 true 就说明收到的 command 不完整；
#   此时按命令内容做任何判断都可能被填充参数绕过，必须先确认而不是放行。
# 函数用途: 演示 rm -rf 字面模式要求确认；这不是完整 shell 分析器，不能替代宿主审批和沙箱。
def review_gate(params: dict) -> dict:
    gate = next((item for item in DECLARATION["tool_gates"] if item["id"] == params.get("gate_id")), None)
    call = params.get("call") if isinstance(params.get("call"), dict) else {}
    if gate is None or call.get("tool") not in gate["tools"]:
        return {"verdict": "deny", "reason_code": "OUT_OF_SCOPE"}
    if call.get("arguments_truncated") is True:
        return {"verdict": "ask", "reason_code": "ARGUMENTS_TRUNCATED", "message": "参数被截断，看不全命令内容，先确认一次"}
    arguments = call.get("arguments")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
        return {"verdict": "ask", "reason_code": "ARGUMENTS_UNAVAILABLE"}
    if re.search(r"\brm\s+-rf\b", arguments["command"]):
        return {"verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次"}
    return {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}


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
