# LLM: v8 收紧样例：只按结构化 gate_id / 工具名 / command、patch 字段裁决；绝不执行命令、不改参数、不放宽宿主。
#   回复只有 allow_as_is / ask / deny 三种，原因码大写；参数缺失宁严按 ask，不认识的组合不 allow。
#   宿主截断标记 arguments_truncated 为 true 时只能更严：片段已 deny 保持，其余升到 ask；false 或缺失照旧。
# 模块用途: 演示 run_command 的 rm 组合收紧与 apply_patch 删除文件段直接拒绝，真实隔离与合并由宿主 B5/B7 施加。

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DECLARATION = json.loads((Path(__file__).resolve().parents[1] / "declaration.json").read_text(encoding="utf-8"))
MAX_LINE = 131072
TOOL_GATE_EXTENSION = "my-agent/tool-gate"
REVIEW_METHOD = "my-agent/tool-gate.review"
# rm 必须是独立词；短选项串按字母解析，长选项按全名匹配；只看结构化 command，不按自然语言猜。
_RM_WORD = re.compile(r"\brm\b")
_SHORT_OPTION = re.compile(r"(?<![\w-])-([A-Za-z]+)(?![\w-])")
_LONG_OPTION = re.compile(r"(?<![\w-])--([A-Za-z][A-Za-z-]*)(?![\w-])")


# LLM: 只做字面结构化判断：命令里出现独立 rm 词，且选项里同时具备递归和强制；不是完整 shell 解析器。
#   覆盖 -rf、-fr、-r -f、--recursive --force 及等价混合写法；宁可多问一次，不放过删除整目录。
# 函数用途: 判断命令是否属于"rm 加 -r 和 -f"的删除整目录写法。
def deletes_tree(command: str) -> bool:
    if not _RM_WORD.search(command):
        return False
    shorts = _SHORT_OPTION.findall(command)
    longs = _LONG_OPTION.findall(command)
    recursive = any("r" in item.lower() for item in shorts) or "recursive" in longs
    force = any("f" in item.lower() for item in shorts) or "force" in longs
    return recursive and force


# LLM: 与宿主 apply_patch 的解析同款（agent/tooling/_filesystem_patch.py）：行首严格是 `*** Delete File: `
#   （冒号后一个空格）才算删除段，正文行里的同样字样（如加号行）不算；先归一换行再按行切分，不做子串匹配。
# 函数用途: 取出补丁里全部删除段头的路径（去掉行首空白后可能是空串，供调用方区分"删除但没写路径"）。
def delete_paths_in_patch(patch: str) -> list[str]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.removeprefix("*** Delete File: ").strip() for line in lines if line.startswith("*** Delete File: ")]


# LLM: 补丁删除门只读补丁文本的删除段头：有带路径的删除段就 deny；只有空路径删除段按参数不可用 ask。
#   不解析正文、不执行补丁，宁严勿松。
# 函数用途: 裁决一次 apply_patch 补丁的删除征询，返回三种裁决之一。
def review_delete_gate(call: dict) -> dict:
    arguments = call.get("arguments")
    patch = arguments.get("patch") if isinstance(arguments, dict) else None
    if not isinstance(patch, str):
        return {"verdict": "ask", "reason_code": "ARGUMENTS_UNAVAILABLE", "message": "看不到补丁内容，先确认一次"}
    paths = delete_paths_in_patch(patch)
    if any(paths):
        return {"verdict": "deny", "reason_code": "DELETE_FILE_BLOCKED", "message": "补丁要删除文件，拒绝"}
    if paths:
        return {"verdict": "ask", "reason_code": "ARGUMENTS_UNAVAILABLE", "message": "删除段没有写路径，先确认一次"}
    return {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}


# LLM: 裁决只依据清单声明的门与结构化调用事实；不认识的组合一律 deny 或 ask，绝不 allow。
#   截断只能更严：先按看到的片段正常判，再看宿主截断标记——片段已 deny 保持 deny（原原因码）；
#   片段本来就 ask 时保留它自己的原因码、消息补半句"参数还被截断了"；只有片段可放行才升到
#   ask + ARGUMENTS_TRUNCATED；标记必须是布尔 true 才生效（1、"true" 等真值按没截断处理）。
# 函数用途: 处理一次收紧征询，返回三种裁决之一。
def review_gate(params: dict) -> dict:
    gate = next((item for item in DECLARATION["tool_gates"] if item.get("id") == params.get("gate_id")), None)
    call = params.get("call") if isinstance(params.get("call"), dict) else {}
    decision = review_visible(call, gate)
    if call.get("arguments_truncated") is not True or decision["verdict"] == "deny":
        return decision
    if decision["verdict"] == "ask":
        message = decision.get("message", "")
        return {**decision, "message": f"{message}；参数还被截断了" if message else "参数还被截断了"}
    return {"verdict": "ask", "reason_code": "ARGUMENTS_TRUNCATED", "message": "参数太长被截断，看不全，先确认一次"}


# LLM: 只看得到的片段做原判定：门不匹配一律 deny，参数缺失宁严按 ask；截断合并由 review_gate 统一处理。
# 函数用途: 按门类型对看到的调用片段给出 allow_as_is / ask / deny 三种裁决之一。
def review_visible(call: dict, gate: dict | None) -> dict:
    if gate is None or call.get("tool") not in gate["tools"]:
        return {"verdict": "deny", "reason_code": "OUT_OF_SCOPE"}
    if gate["id"] == "guard-delete":
        return review_delete_gate(call)
    arguments = call.get("arguments")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
        return {"verdict": "ask", "reason_code": "ARGUMENTS_UNAVAILABLE", "message": "看不到完整命令，先确认一次"}
    if deletes_tree(arguments["command"]):
        return {"verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次"}
    return {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}


# LLM: 协议错误只含固定中文说明，不回显输入；JSON-RPC 分类与业务裁决分开。
# 函数用途: 构造协议错误帧。
def rpc_error(identifier: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


# LLM: 协议成功帧只封装结果；不重解释裁决或授予权限。
# 函数用途: 构造统一的成功协议帧。
def rpc_result(identifier: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


# LLM: 握手声明收紧能力位；清单声明了而握手没声明时宿主按 ask 处理（宁严），所以这里必须与方法同步。
#   通知不回复；未知方法返回 -32601；任何请求都不执行、不改参数。
# 函数用途: 路由标准 MCP 与 v8 收紧征询，不产生宿主操作。
def handle(request: object) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "请求格式无效。")
    if "id" not in request:
        return None
    identifier, method = request["id"], request["method"]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "initialize":
        return rpc_result(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "experimental": {
            TOOL_GATE_EXTENSION: {"versions": ["1"]}}},
            "serverInfo": {"name": DECLARATION["plugin_id"], "version": DECLARATION["version"]}})
    if method == "ping":
        return rpc_result(identifier, {})
    if method == "tools/list":
        return rpc_result(identifier, {"tools": []})
    if method == REVIEW_METHOD:
        return rpc_result(identifier, review_gate(params))
    return rpc_error(identifier, -32601, "不支持此方法。")


# LLM: 解析错误不终止后续请求；标准输出只允许 JSON 帧，不能混入调试 print。
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
