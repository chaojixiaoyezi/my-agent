# LLM: 独立 stdio MCP 进程只实现握手与只读 my-agent/display.render；不读文件、不联网、不创建线程、定时器或持久状态。
#   设置只在启动时从 MY_AGENT_PLUGIN_SETTINGS 读取一次（改设置由宿主重启进程生效）；每次渲染只按当次主题输入作画。
# 模块用途: 把 run_state / activity 主题渲染成"小宠物图 + 一行状态文字"的 text 面板。

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version
from importlib.resources import files

from .art import ART

DISPLAY_EXTENSION = "my-agent/display"
RENDER_METHOD = "my-agent/display.render"
PANEL_ID = "pet"
MAX_LINE_CHARS = 200


# LLM: 仅加载本包固定资源；声明是设置默认值和取值范围的唯一来源。
# 函数用途: 读取随 wheel 发布的声明。
def declaration() -> dict:
    return json.loads(files("status_pet").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主已按 settings_schema 完整校验；这里只按同一份声明做防御性复核（未知字段、enum、长度），
#   失败抛 ValueError 由 main 转成"插件设置无效"退出，不回显设置值。
# 函数用途: 校验启动设置并补默认值。
def load_settings(raw: str, schema: dict) -> dict:
    value = json.loads(raw)
    properties = schema["properties"]
    if not isinstance(value, dict) or set(value) - set(properties):
        raise ValueError("设置包含未声明字段")
    result = {name: value.get(name, spec["default"]) for name, spec in properties.items()}
    for name, spec in properties.items():
        item = result[name]
        if not isinstance(item, str):
            raise ValueError("设置类型无效")
        if "enum" in spec and item not in spec["enum"]:
            raise ValueError("设置取值无效")
        if not spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", MAX_LINE_CHARS):
            raise ValueError("设置长度无效")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            raise ValueError("设置含控制字符")
    return result


# LLM: 纯函数；缺字段或未知状态按空闲处理，不猜测宿主没给的事实；等待审批必须与其他状态明显不同。
# 函数用途: 按外观、名字和当次主题生成面板文字行。
def render(topics: dict, style: str, name: str) -> dict:
    run_state = topics.get("run_state") if isinstance(topics.get("run_state"), dict) else {}
    activity = topics.get("activity") if isinstance(topics.get("activity"), dict) else {}
    state = run_state.get("state")
    if state not in ("working", "waiting"):
        state = "idle"
    subagents = run_state.get("subagent_count")
    subagents = subagents if type(subagents) is int and subagents >= 0 else 0
    if state == "working":
        status = f"{name} 正在工作 · 子代理 {subagents}"
        text = str(activity.get("activity") or "").strip()
        if text:
            status += f" · {text}"
    elif state == "waiting":
        status = f"【等待审批】{name} 在等你批准 · 子代理 {subagents}"
    else:
        status = f"{name} 空闲中，随时待命"
    if len(status) > MAX_LINE_CHARS:
        status = status[: MAX_LINE_CHARS - 1] + "…"
    return {"lines": [*ART[style][state], status]}


# LLM: 协议错误用 JSON-RPC error；未初始化前拒绝业务方法；对象只持有启动时已校验的设置。
# 类用途: 处理握手、ping、空工具目录和宠物面板渲染。
class StatusPetServer:
    # 函数用途: 保存已校验的外观与名字，初始化连接状态。
    def __init__(self, settings: dict):
        self.style = settings["style"]
        self.name = settings["name"]
        self.initialized = False

    # LLM: 通知不回复；未知方法返回 -32601；只接受本包声明的面板编号。
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
                      "serverInfo": {"name": "status-pet", "version": version("my-agent-status-pet")},
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
            result = {"display": render(params["topics"], self.style, self.name)}
        else:
            return self.error(request_id, -32601, "不支持此方法。")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # 函数用途: 创建标准 JSON-RPC 错误。
    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# LLM: 坏设置明确以退出码 2 失败且不回显设置值；单条输入有界，超长行直接退出交由宿主记录。
# 函数用途: 读取设置并运行 stdio 循环，每条请求即时刷新响应。
def main() -> int:
    try:
        settings = load_settings(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}"), declaration()["settings_schema"])
    except (ValueError, TypeError, KeyError, RecursionError):
        print("插件设置无效。", file=sys.stderr)
        return 2
    server = StatusPetServer(settings)
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
