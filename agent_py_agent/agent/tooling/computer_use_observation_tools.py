# LLM: 屏幕观察两个工具接进 Computer Use 适配器的胶水（J16 片 B）。固定的 MCP SDK 1.13 里 FastMCP/底层 Server 都不能把
#   CallToolResult 原样返回，而观察合同要求失败结果是 isError + structuredContent.my_agent_observation_error{code}，所以这两个
#   工具在底层 Server 的 tools/call 处理器里接管：按名字拦截、读 _meta 里宿主附的观察上下文、自己编码结果；其它工具原样交给
#   FastMCP。FastMCP 侧只用这两个函数的签名和说明生成 tools/list 的 schema，函数体不会被执行（执行到就是接管层没装，抛错让它显形）。
#   只在 OBSERVATION_ENV_FLAG 为 "1" 时注册与接管；mcp 包只在编码/安装时惰性 import，纯逻辑可用假 types 单测。
# 模块用途: 让 observe_window / click_candidate 用插件线同一份观察合同说话，又不碰 FastMCP 的私有实现。
# 注意：本模块故意不用 `from __future__ import annotations`——固定的 MCP 1.13 在 add_tool 时对每个参数注解做 issubclass
# 检查，字符串注解会直接 TypeError；所以两个声明函数的注解必须是真实类型对象。可选参数写成 `str = ""`（不写 Optional）：
# FastMCP 会把 Optional 生成 anyOf[string, null]，过不了宿主"候选参数必须是可选的 string"这条声明规则。
import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

from ..plugin_observation import OBSERVATION_ERROR_KEY, OBSERVATION_META_EXTENSION
from .computer_use_profile import OBSERVATION_ENV_FLAG
from .screen_observation import ObservationError

OBSERVATION_TOOL_NAMES = ("observe_window", "click_candidate")


# 函数用途: 适配器子进程是否应装载屏幕观察工具（宿主按主配置开关写入环境标记）。
def observation_tools_enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(OBSERVATION_ENV_FLAG) or "") == "1"


# LLM: 只提供 tools/list 的签名与说明；真正执行在 observation_call_handler。
# 函数用途: observe_window 的模型可见声明。
def observe_window(window: str = "") -> dict[str, object]:
    """只读采样一个窗口：不切焦点、不激活、不动鼠标。window 可填上一次观察返回的窗口别名（win:…），不填取当前最前的普通窗口。
    结果里的 my_agent_observation 候选只能通过 click_candidate 的 candidate_id 使用；没有候选时不会凭空造候选。"""
    raise RuntimeError("observe_window 必须由适配器的观察接管层执行")


# 函数用途: click_candidate 的模型可见声明。
def click_candidate(candidate_id: str = "") -> dict[str, object]:
    """点击上一次 observe_window 给出的候选中心。只接受 candidate_id；点之前按当时快照逐项复核（窗口实例、可见、几何、遮挡、
    区域像素），任一项变了就不点并返回 stale。返回值只证明提交了点击，结果要靠下一次 observe_window 确认。"""
    raise RuntimeError("click_candidate 必须由适配器的观察接管层执行")


# 函数用途: 把两个工具的声明注册进 FastMCP（只为 tools/list）。
def register_observation_tools(server: Any) -> None:
    server.add_tool(observe_window, name="observe_window", description=(observe_window.__doc__ or "").strip())
    server.add_tool(click_candidate, name="click_candidate", description=(click_candidate.__doc__ or "").strip())


# LLM: 返回 (正文, structuredContent, isError)。观察载荷只进 structuredContent、正文不重复（和 browser-lite 一致）；失败正文带
#   OBSERVATION_<CODE> 与中文说明，structuredContent 带 {my_agent_observation_error: {code}}，宿主据此提升 stale / not_found。
# 函数用途: 执行一个观察工具调用并给出可编码的三元组。
def call_observation_tool(observer: Any, name: str, arguments: Mapping[str, Any], meta: object) -> tuple[dict, dict | None, bool]:
    try:
        body, structured = _observe(observer, arguments) if name == "observe_window" else _click(observer, arguments, meta)
    except ObservationError as exc:
        return {"code": "OBSERVATION_" + exc.code.upper(), "message": str(exc)}, {OBSERVATION_ERROR_KEY: {"code": exc.code}}, True
    return body, structured, False


# 函数用途: observe_window 分支：window 只接受字符串别名；正文不重复观察载荷。
def _observe(observer: Any, arguments: Mapping[str, Any]) -> tuple[dict, dict]:
    window = arguments.get("window")
    if window is not None and not isinstance(window, str):
        raise ObservationError("invalid_arguments", "window 必须是字符串")
    result = observer.observe(window or None)
    return {key: value for key, value in result.items() if key != "my_agent_observation"}, result


# 函数用途: click_candidate 分支：candidate_id 必须是非空字符串，候选事实只来自 _meta。
def _click(observer: Any, arguments: Mapping[str, Any], meta: object) -> tuple[dict, dict]:
    candidate_id = arguments.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ObservationError("invalid_arguments", "candidate_id 必须是非空字符串")
    result = observer.click_candidate(meta)
    return result, result


# 函数用途: 把三元组编码成底层 Server 的 ServerResult(CallToolResult)。
def encode_call_result(body: dict, structured: dict | None, is_error: bool) -> Any:
    from mcp import types

    return types.ServerResult(types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        structuredContent=structured, isError=is_error,
    ))


# LLM: 宿主附的 _meta 以 RequestParams.Meta（extra=allow）到达，扩展键在 model_extra 里；没有就按缺上下文处理（由观察核心判定）。
#   采样 + OCR 是阻塞的慢活，放到工作线程里跑并 await：事件循环保持可读，宿主 /stop 发来的 notifications/cancelled 才能被
#   底层 Server 处理（取消这次等待、丢弃结果；线程里的 OCR 跑完自然结束）。同一时刻只跑一次观察（锁），快照环不被并发写。
# 函数用途: 生成底层 tools/call 处理器：观察工具自己处理，其余交给原处理器。
def observation_call_handler(delegate: Callable[[Any], Any], observer: Any) -> Callable[[Any], Any]:
    gate = threading.Lock()

    def run(name: str, arguments: dict, meta: object) -> tuple[dict, dict | None, bool]:
        with gate:
            return call_observation_tool(observer, name, arguments, meta)

    async def handler(request: Any) -> Any:
        params = request.params
        if params.name not in OBSERVATION_TOOL_NAMES:
            return await delegate(request)
        extra = getattr(getattr(params, "meta", None), "model_extra", None) or {}
        loop = asyncio.get_running_loop()
        body, structured, is_error = await loop.run_in_executor(None, run, params.name, dict(params.arguments or {}), extra.get(OBSERVATION_META_EXTENSION))
        return encode_call_result(body, structured, is_error)

    return handler


# 函数用途: 把观察接管层装到底层 Server 的 tools/call 处理器上（原处理器成为 delegate）。
def install_observation_handler(low_server: Any, observer: Any) -> None:
    from mcp import types

    delegate = low_server.request_handlers[types.CallToolRequest]
    low_server.request_handlers[types.CallToolRequest] = observation_call_handler(delegate, observer)


# LLM: 适配器进程的唯一装配入口（stdio 收发留在 computer_use_server.serve）：底层 Server 的 list_tools / call_tool 直接绑 FastMCP
#   的公开协程；只有环境标记为 "1" 时才注册两个观察工具并装接管层、才调用 observer_factory（它会碰 X11）。标记关着时 tools/list
#   与只有上游工具时逐字节一致，tools/call 处理器就是原 delegate——"关时工具目录不变"靠这里保证。
# 函数用途: 按环境标记装配底层 Server。
def build_adapter_server(fastmcp: Any, environ: Mapping[str, str], observer_factory: Callable[[], Any]) -> Any:
    from mcp.server.lowlevel import Server

    low = Server(fastmcp.name)
    low.list_tools()(fastmcp.list_tools)
    low.call_tool(validate_input=False)(fastmcp.call_tool)
    if observation_tools_enabled(environ):
        register_observation_tools(fastmcp)
        install_observation_handler(low, observer_factory())
    return low


__all__ = [
    "OBSERVATION_TOOL_NAMES", "build_adapter_server", "call_observation_tool", "click_candidate", "encode_call_result",
    "install_observation_handler",
    "observation_call_handler", "observation_tools_enabled", "observe_window", "register_observation_tools",
]
