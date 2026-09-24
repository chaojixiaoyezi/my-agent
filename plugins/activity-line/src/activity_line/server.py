# LLM: 独立 stdio MCP 进程只实现握手与只读 my-agent/display.render；不读文件、不联网、不创建线程或持久状态。
#   输入只有宿主裁剪后的公开主题投影，输出是核心可校验的 text 面板描述。
# 模块用途: 把当前运行状态、活动描述、耗时和子代理数渲染成两行文字。

from __future__ import annotations

import json
import sys
import time
from importlib.metadata import version

DISPLAY_EXTENSION = "my-agent/display"
RENDER_METHOD = "my-agent/display.render"
_STATE_LABEL = {"working": "● 工作中", "waiting": "◐ 等待审批", "idle": "○ 空闲"}


# LLM: 纯函数；缺字段按空值处理，不抛异常，不猜测宿主没给的事实。
# 函数用途: 根据公开主题生成面板文字。
def render(topics: dict, now: float) -> dict:
    activity = topics.get("activity") if isinstance(topics.get("activity"), dict) else {}
    run_state = topics.get("run_state") if isinstance(topics.get("run_state"), dict) else {}
    state = _STATE_LABEL.get(str(run_state.get("state") or "idle"), "○ 空闲")
    text = str(activity.get("activity") or "").strip()
    first = f"{state} · {text}" if text else state
    started = activity.get("started_at")
    elapsed = ""
    if isinstance(started, (int, float)) and run_state.get("state") != "idle":
        elapsed = f"已进行 {max(0, int(now - started))} 秒 · "
    second = (f"{elapsed}活动任务 {int(activity.get('active_task_count') or 0)} · "
              f"子代理 {int(activity.get('subagent_count') or 0)} · 压缩 {int(activity.get('compact_count') or 0)}")
    return {"lines": [first, second]}


# LLM: 协议错误用 JSON-RPC error；未初始化前拒绝业务方法。
# 类用途: 处理握手、ping、空工具目录和展示渲染。
class ActivityLineServer:
    # 函数用途: 初始化连接状态。
    def __init__(self):
        self.initialized = False

    # LLM: 通知不回复；未知方法返回 -32601。
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
                      "serverInfo": {"name": "activity-line", "version": version("my-agent-activity-line")},
                      "capabilities": {"experimental": {DISPLAY_EXTENSION: {"versions": ["1"]}}}}
        elif not self.initialized:
            return self.error(request_id, -32000, "请先初始化连接。")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": []}
        elif method == RENDER_METHOD:
            if params.get("panel") != "line" or not isinstance(params.get("topics"), dict):
                return self.error(request_id, -32602, "面板或主题无效。")
            result = {"display": render(params["topics"], time.time())}
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
    server = ActivityLineServer()
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
