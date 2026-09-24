# LLM: 独立 stdio MCP 进程只实现握手与只读 my-agent/display.render；不读文件、不联网、不创建线程或持久状态。
#   输入只有宿主裁剪后的公开 context 主题（数字白名单 + 压缩次数），输出是核心可校验的 status 面板描述。
#   只展示宿主给出的数字，不从正文或别处推断；known 为假时不显示任何数字。
# 模块用途: 把当前会话的上下文用量、触发线、三部分组成和压缩次数渲染成状态字段。

from __future__ import annotations

import json
import sys
from importlib.metadata import version

DISPLAY_EXTENSION = "my-agent/display"
RENDER_METHOD = "my-agent/display.render"
PANEL_ID = "context"
UNKNOWN_TEXT = "还没有本会话的上下文快照（发送一条消息后出现）"


# LLM: 非整数（含布尔）按 0 处理，不抛异常；宿主已保证是非负整数，这里只做防御。
# 函数用途: 取出一个 token 数字段。
def _count(context: dict, key: str) -> int:
    value = context.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# LLM: 窗口为 0 时不算百分比，避免除零或编出比例。
# 函数用途: 生成"数量 / 窗口（百分比）"文本，数字带千分位。
def _of_window(tokens: int, window: int) -> str:
    if window <= 0:
        return f"{tokens:,}"
    return f"{tokens:,} / {window:,}（{tokens / window:.1%}）"


# LLM: 纯函数；缺字段按 0 处理，不猜测宿主没给的事实；estimated 只按宿主给的布尔值标注。
# 函数用途: 根据公开 context 主题生成状态面板字段。
def render(topics: dict) -> dict:
    context = topics.get("context") if isinstance(topics.get("context"), dict) else {}
    if context.get("known") is not True:
        return {"fields": [{"label": "上下文", "value": UNKNOWN_TEXT}]}
    window = _count(context, "context_window_tokens")
    usage_label = "当前用量（估算）" if context.get("estimated") is True else "当前用量"
    fields = [
        {"label": usage_label, "value": _of_window(_count(context, "current_tokens"), window)},
        {"label": "自动压缩触发线", "value": _of_window(_count(context, "compact_trigger_tokens"), window)},
        {"label": "消息历史", "value": f"{_count(context, 'messages_tokens'):,}"},
        {"label": "运行引导", "value": f"{_count(context, 'runtime_guidance_tokens'):,}"},
        {"label": "工具目录", "value": f"{_count(context, 'tool_schema_tokens'):,}"},
        {"label": "已压缩次数", "value": f"{_count(context, 'compact_count'):,}"},
    ]
    return {"fields": fields}


# LLM: 协议错误用 JSON-RPC error；未初始化前拒绝业务方法。
# 类用途: 处理握手、ping、空工具目录和上下文面板渲染。
class ContextInspectorServer:
    # 函数用途: 初始化连接状态。
    def __init__(self):
        self.initialized = False

    # LLM: 通知不回复；未知方法返回 -32601；只接受声明过的 context 面板。
    # 函数用途: 分派单条 JSON-RPC 请求。
    def handle(self, request: object) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "请求格式无效。")
        if "id" not in request:
            return None
        request_id, method = request["id"], request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "参数格式无效。")
        if method == "initialize":
            self.initialized = True
            result = {"protocolVersion": "2024-11-05",
                      "serverInfo": {"name": "context-inspector", "version": version("my-agent-context-inspector")},
                      "capabilities": {"experimental": {DISPLAY_EXTENSION: {"versions": ["1"]}}}}
        elif not self.initialized:
            return self.error(request_id, -32000, "请先初始化连接。")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": []}
        elif method == RENDER_METHOD:
            if params.get("panel") != PANEL_ID or not isinstance(params.get("topics"), dict):
                return self.error(request_id, -32602, "面板或主题无效。")
            result = {"display": render(params["topics"])}
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: 单条输入有界；超长行直接退出，交由宿主记录退出。
# 函数用途: 运行 stdio 循环，每条请求即时刷新响应。
def main() -> int:
    server = ContextInspectorServer()
    while line := sys.stdin.buffer.readline(65537):
        if len(line) > 65536:
            print("插件请求超过读取上限。", file=sys.stderr)
            return 2
        try:
            response = server.handle(json.loads(line))
        except (ValueError, UnicodeError, RecursionError):
            response = server.error(None, -32700, "请求 JSON 无效。")
        if response is not None:
            print(json.dumps(response, ensure_ascii=True), flush=True)
    return 0
