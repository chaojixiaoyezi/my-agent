# LLM: 独立 stdio MCP 进程只实现握手与只读 my-agent/display.render；不读文件、不联网、不创建线程或持久状态。
#   输入只有宿主裁剪后的公开 sessions 主题（会话编号、时间、渠道、是否当前，已按更新时间倒序），
#   输出是核心可校验的 text 面板描述。显示偏好（max_rows、hide_current）只从宿主启动时注入的
#   MY_AGENT_PLUGIN_SETTINGS 读取，并按随包声明补默认值；坏设置明确启动失败，不静默吞掉。
# 模块用途: 把本用户最近的会话渲染成"标记 编号 · 相对时间 · 渠道"的文本行，并提示回到原会话的命令。

from __future__ import annotations

import json
import os
import sys
import time
from importlib.metadata import version
from importlib.resources import files

DISPLAY_EXTENSION = "my-agent/display"
RENDER_METHOD = "my-agent/display.render"
PANEL_ID = "sessions"
EMPTY_TEXT = "还没有会话记录"
NO_OTHER_TEXT = "没有其他会话记录"
HINT_TEXT = "回到某个会话：my-agent resume <会话编号>"
# 核心 text 面板最多 20 行；末尾提示占一行，会话行最多 19 行，避免提示被核心截断
MAX_SESSION_LINES = 19


# LLM: 仅加载本包固定资源，不读 cwd、用户路径或宿主模块；返回调用方私有对象。
# 函数用途: 取得与安装清单构建同源的静态声明。
def declaration() -> dict:
    return json.loads(files("worktable_lite").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主已按 settings_schema 做完整校验；这里只防御本包声明的 integer/boolean 平面字段并补默认值，
#   未知字段、类型或范围不符都抛 ValueError，由 main 统一以"插件设置无效"退出，不回显设置值。
# 函数用途: 把注入的设置 JSON 校验成带默认值的显示偏好。
def load_settings(raw: str, schema: dict) -> dict:
    value = json.loads(raw)
    properties = schema["properties"]
    if not isinstance(value, dict) or set(value) - set(properties):
        raise ValueError("设置包含未声明字段")
    result = {}
    for name, spec in properties.items():
        item = value.get(name, spec["default"])
        if spec["type"] == "integer":
            valid = type(item) is int and spec["minimum"] <= item <= spec["maximum"]
        elif spec["type"] == "boolean":
            valid = type(item) is bool
        else:
            raise ValueError("声明使用了未实现的设置类型")
        if not valid:
            raise ValueError("设置类型或取值范围无效")
        result[name] = item
    return result


# LLM: 纯函数；时间缺失或不是数字时返回"时间未知"，未来时间（时钟偏差）按"刚刚"处理，不报错。
# 函数用途: 把 epoch 秒转成"刚刚 / N 分钟前 / N 小时前 / N 天前"。
def relative_time(timestamp: object, now: float) -> str:
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return "时间未知"
    seconds = now - timestamp
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# LLM: 纯函数；保持宿主给出的顺序（按更新时间倒序），不自行重排；更新时间缺失时退回创建时间。
#   坏行（非对象或缺会话编号）直接跳过；只展示宿主白名单字段，不推断标题或正文。
# 函数用途: 根据公开 sessions 主题和显示偏好生成文本面板行。
def render(topics: dict, settings: dict, now: float) -> dict:
    sessions = topics.get("sessions") if isinstance(topics.get("sessions"), dict) else {}
    items = sessions.get("items") if isinstance(sessions.get("items"), list) else []
    rows = [row for row in items if isinstance(row, dict) and isinstance(row.get("session_id"), str)]
    if not rows:
        return {"lines": [EMPTY_TEXT]}
    if settings["hide_current"]:
        rows = [row for row in rows if row.get("current") is not True]
        if not rows:
            return {"lines": [NO_OTHER_TEXT]}
    lines = []
    for row in rows[:min(settings["max_rows"], MAX_SESSION_LINES)]:
        current = row.get("current") is True
        stamp = row.get("updated_at") if row.get("updated_at") is not None else row.get("created_at")
        channel = row.get("channel") if isinstance(row.get("channel"), str) and row.get("channel") else "渠道未知"
        text = f"{'▶' if current else ' '} {row['session_id']} · {relative_time(stamp, now)} · {channel}"
        lines.append(text + ("（当前）" if current else ""))
    return {"lines": [*lines, HINT_TEXT]}


# LLM: 协议错误用 JSON-RPC error；未初始化前拒绝业务方法。
# 类用途: 处理握手、ping、空工具目录和会话列表面板渲染。
class WorktableServer:
    # LLM: 设置只从宿主启动时注入的专属环境值读取；坏设置在构造时抛 ValueError/TypeError。
    # 函数用途: 加载同源声明并校验显示偏好。
    def __init__(self):
        schema = declaration()["settings_schema"]
        self.settings = load_settings(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}"), schema)
        self.initialized = False

    # LLM: 通知不回复；未知方法返回 -32601；只接受声明过的 sessions 面板；"现在"取本进程 time.time()。
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
                      "serverInfo": {"name": "worktable-lite", "version": version("my-agent-worktable-lite")},
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
            result = {"display": render(params["topics"], self.settings, time.time())}
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: 坏设置以退出码 2 启动失败且不回显设置值；单条输入有界，超长行直接退出，交由宿主记录退出。
# 函数用途: 运行 stdio 循环，每条请求即时刷新响应。
def main() -> int:
    try:
        server = WorktableServer()
    except (ValueError, TypeError):
        print("插件设置无效。", file=sys.stderr)
        return 2
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
