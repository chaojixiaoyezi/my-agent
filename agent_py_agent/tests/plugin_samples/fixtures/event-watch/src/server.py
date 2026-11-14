# LLM: v8 观察样例：握手声明事件与展示两个能力位；events.observe 只回空回执，display.render 只返回本进程内存计数。
#   计数只按结构化 type / dropped_before 累计，未知类型归"其它"、多余字段忽略，向前兼容不崩；不落盘、不联网、不执行输入。
# 模块用途: 演示"订阅全部 6 类事件 + 只读面板显示计数"，真实隔离与投递语义由宿主 B3/B7 施加。

from __future__ import annotations

import json
import sys
from pathlib import Path

DECLARATION = json.loads((Path(__file__).resolve().parents[1] / "declaration.json").read_text(encoding="utf-8"))
MAX_LINE = 131072
EVENTS_EXTENSION = "my-agent/events"
DISPLAY_EXTENSION = "my-agent/display"
OBSERVE_METHOD = "my-agent/events.observe"
RENDER_METHOD = "my-agent/display.render"
PANEL_ID = "watch"
EVENT_TYPES = (
    "prompt_submitted", "turn_started", "turn_ended", "tool_call_started", "tool_call_finished", "command_executed",
)
OTHER_LABEL = "其它"


# LLM: 每类事件的计数只存在内存里，进程退出即丢；渲染只读不改，回执与表格绝不携带正文或路径。
# 类用途: 累计每类事件的收到条数与合并丢弃条数。
class WatchCounts:
    # 函数用途: 建立 6 类已知事件加一个"其它"兜底计数。
    def __init__(self) -> None:
        self._received = {name: 0 for name in (*EVENT_TYPES, OTHER_LABEL)}
        self._dropped = {name: 0 for name in (*EVENT_TYPES, OTHER_LABEL)}

    # LLM: 只读 type 和 dropped_before 两个结构化字段；未知类型归"其它"，坏值按 0，不解释 facts 内容。
    # 函数用途: 按一批观察事件累计计数。
    def observe(self, events: object) -> None:
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, dict):
                continue
            name = event.get("type")
            label = name if name in EVENT_TYPES else OTHER_LABEL
            self._received[label] += 1
            dropped = event.get("dropped_before")
            if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped >= 0:
                self._dropped[label] += dropped

    # LLM: 输出是纯数字表格，不含正文、路径或身份；行序固定，未知类型只在出现过时才显示。
    # 函数用途: 生成只读计数表供面板渲染。
    def table(self) -> dict:
        rows = [[name, self._received[name], self._dropped[name]] for name in EVENT_TYPES]
        if self._received[OTHER_LABEL] or self._dropped[OTHER_LABEL]:
            rows.append([OTHER_LABEL, self._received[OTHER_LABEL], self._dropped[OTHER_LABEL]])
        return {"columns": ["事件类型", "收到", "合并丢弃"], "rows": rows}


# LLM: 协议错误只含固定中文说明，不回显输入；JSON-RPC 分类与业务数据分开。
# 函数用途: 构造协议错误帧。
def rpc_error(identifier: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


# LLM: 协议成功帧只封装结果；不重解释计数或授予权限。
# 函数用途: 构造统一的成功协议帧。
def rpc_result(identifier: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


# LLM: 握手两个 experimental 位必须与本文件方法同步：events 位对应 observe，display 位对应 render。
#   观察方法只回空回执当确认，绝不把收到的事实再写回宿主。
# 函数用途: 路由标准 MCP 与 v8 观察/展示方法，不执行输入、不产生宿主操作。
def handle(request: object, counts: WatchCounts) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "请求格式无效。")
    if "id" not in request:
        return None
    identifier, method = request["id"], request["method"]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "initialize":
        return rpc_result(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "experimental": {
            EVENTS_EXTENSION: {"versions": ["1"]}, DISPLAY_EXTENSION: {"versions": ["1"]}}},
            "serverInfo": {"name": DECLARATION["plugin_id"], "version": DECLARATION["version"]}})
    if method == "ping":
        return rpc_result(identifier, {})
    if method == "tools/list":
        return rpc_result(identifier, {"tools": []})
    if method == OBSERVE_METHOD:
        counts.observe(params.get("events"))
        return rpc_result(identifier, {})
    if method == RENDER_METHOD:
        if params.get("panel") != PANEL_ID or not isinstance(params.get("topics"), dict):
            return rpc_error(identifier, -32602, "面板或主题无效。")
        return rpc_result(identifier, {"display": counts.table()})
    return rpc_error(identifier, -32601, "不支持此方法。")


# LLM: 解析错误不终止后续请求；标准输出只允许 JSON 帧，不能混入调试 print。
# 函数用途: 解析并处理一行协议输入。
def process_line(line: str, counts: WatchCounts) -> dict | None:
    try:
        request = json.loads(line)
    except ValueError:
        return rpc_error(None, -32700, "请求不是有效 JSON。")
    return handle(request, counts)


# LLM: stdin EOF 自然结束，不拉起后台任务；读取限额控制单帧，诊断走 stderr，修改时联测打包后的 stdio。
# 函数用途: 运行单进程 MCP 服务，写响应帧但不写任何业务文件。
def main() -> int:
    counts = WatchCounts()
    while line := sys.stdin.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE:
            sys.stderr.write("插件请求超过读取上限。\n")
            return 2
        response = process_line(line, counts)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
