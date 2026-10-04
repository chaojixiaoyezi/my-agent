# LLM: 屏幕观察两个工具接进 Computer Use 适配器的胶水（J16 片 B）。固定的 MCP SDK 1.13 里 FastMCP/底层 Server 都不能把
#   CallToolResult 原样返回，而观察合同要求失败结果是 isError + structuredContent.my_agent_observation_error{code}，所以这两个
#   工具在底层 Server 的 tools/call 处理器里接管：按名字拦截、读 _meta 里宿主附的观察上下文、自己编码结果；其它工具原样交给
#   FastMCP。FastMCP 侧只用这两个函数的签名和说明生成 tools/list 的 schema，函数体不会被执行（执行到就是接管层没装，抛错让它显形）。
#   只在 OBSERVATION_ENV_FLAG 为 "1" 时注册与接管；mcp 包只在编码/安装时惰性 import，纯逻辑可用假 types 单测。
#   片 G：type_into_candidate 只在后端声明能给控件候选（observer.supports_ui_candidates，结构化能力，不按平台名）时注册；text 必填，
#   按片 D 的自动执行规则（schema 必填项 ⊆ {候选参数}）它在结构上就不会被自动执行。点击之后的失败在错误对象里带 clicked=true。
# 模块用途: 让 observe_window / click_candidate / type_into_candidate 用插件线同一份观察合同说话，又不碰 FastMCP 的私有实现。
# 注意：本模块故意不用 `from __future__ import annotations`——固定的 MCP 1.13 在 add_tool 时对每个参数注解做 issubclass
# 检查，字符串注解会直接 TypeError；所以两个声明函数的注解必须是真实类型对象。可选参数写成 `str = ""`（不写 Optional）：
# FastMCP 会把 Optional 生成 anyOf[string, null]，过不了宿主"候选参数必须是可选的 string"这条声明规则。
import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..plugin_observation import OBSERVATION_ERROR_KEY, OBSERVATION_META_EXTENSION
from .computer_use_profile import OBSERVATION_ENV_FLAG, OBSERVE_ONLY_ENV_FLAG
from .screen_observation import ObservationError

OBSERVATION_TOOL_NAMES = ("observe_window", "click_candidate", "type_into_candidate")


# LLM: 一次调用的宿主侧事实：_meta 里的观察上下文与"宿主已取消"的判定；cancelled 由接管层按协程取消设置，观察核心在拿到锁后、
#   复核完真正点击前各看一次，已取消就零副作用返回。
# 类用途: 观察工具调用的上下文（不是模型参数）。
@dataclass(frozen=True)
class CallContext:
    meta: object = None
    cancelled: Callable[[], bool] = lambda: False


# LLM: 两个标记都是"要装载观察工具"的同源事实：完整档写观察标记，只看档只写只看标记（宿主在只看档不写观察标记）。
#   只看标记本身就意味着要注册观察工具，所以这里认任意一个；否则适配器会一个工具都不注册（真机实测过的接缝）。
#   注册范围仍由 register_observation_tools 按档位收窄（只看档只出 observe_window）。
# 函数用途: 适配器子进程是否应装载屏幕观察工具（完整档或只看档任一标记为 "1"）。
def observation_tools_enabled(environ: Mapping[str, str]) -> bool:
    return any(str(environ.get(flag) or "") == "1"
               for flag in (OBSERVATION_ENV_FLAG, OBSERVE_ONLY_ENV_FLAG))


# LLM: 只看档标记由宿主在"总开关关、只开观察"时写入；适配器据此不装载上游执行器，也不注册上游工具。
#   这是显式档位判定（结构化标记），不是导入失败后的兜底。
# 函数用途: 判定本次适配器进程是否为只看档。
def observe_only_enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(OBSERVE_ONLY_ENV_FLAG) or "") == "1"


# LLM: 只提供 tools/list 的签名与说明；真正执行在 observation_call_handler。
# 函数用途: observe_window 的模型可见声明。
def observe_window(window: str = "") -> dict[str, object]:
    """只读采样一个窗口：不切焦点、不激活、不动鼠标。window 可填：留空（当前最前面的普通窗口）、上一次观察返回的别名 win:…、
    或与窗口标题完全相同的文字（只认唯一匹配；多个同名窗口会报 window_ambiguous 并列出别名；真实标题以 win: 开头的窗口请用别名）。
    找不到时结果里带当前可见窗口清单 windows（别名 + 标题）。结果里的 my_agent_observation 候选只能通过 click_candidate 的 candidate_id 使用；
    没有候选时不会凭空造候选。"""
    raise RuntimeError("observe_window 必须由适配器的观察接管层执行")


# 函数用途: click_candidate 的模型可见声明。
def click_candidate(candidate_id: str = "") -> dict[str, object]:
    """点击上一次 observe_window 给出的候选中心。只接受 candidate_id；点之前按当时快照逐项复核（窗口实例、可见、几何、遮挡、
    区域像素），任一项变了就不点并返回 stale。返回值只证明提交了点击，结果要靠下一次 observe_window 确认。"""
    raise RuntimeError("click_candidate 必须由适配器的观察接管层执行")


# 函数用途: type_into_candidate 的模型可见声明（text 必填，候选参数可选）。
def type_into_candidate(text: str, candidate_id: str = "", clear_existing: bool = False) -> dict[str, object]:
    """往上一次 observe_window 给出的可编辑控件（actions 里有 type_into_candidate 的候选）里输入 text：先点一下让它拿到焦点，
    确认有焦点才输入；clear_existing=true 先全选原有内容再替换（text 为空就是清空）。text 最多 500 个字符，不能有换行、Tab 等
    控制字符（多行文字暂不支持，需要回车请另行按键）。点之前按当时快照复核控件（还在、角色、可用、外框、名称、可编辑），变了就
    不点并返回 stale；点了但控件没拿到焦点返回 focus_not_acquired（已点击、未输入）。返回值只证明提交了输入，不读回内容，结果要靠
    下一次 observe_window 确认。"""
    raise RuntimeError("type_into_candidate 必须由适配器的观察接管层执行")


# LLM: 只看档（observe_only）只注册 observe_window——不交出 click_candidate / type_into_candidate，也没有上游工具；
#   这是档位决定的目录，不是按依赖可用性挑工具。
# 函数用途: 把观察工具的声明注册进 FastMCP（只为 tools/list）；只看档只注册 observe_window，with_typing 才注册 type_into_candidate。
def register_observation_tools(server: Any, *, with_typing: bool, observe_only: bool = False) -> None:
    server.add_tool(observe_window, name="observe_window", description=(observe_window.__doc__ or "").strip())
    if observe_only:
        return
    server.add_tool(click_candidate, name="click_candidate", description=(click_candidate.__doc__ or "").strip())
    if with_typing:
        server.add_tool(type_into_candidate, name="type_into_candidate", description=(type_into_candidate.__doc__ or "").strip())


# LLM: 返回 (正文, structuredContent, isError)。观察载荷只进 structuredContent、正文不重复（和 browser-lite 一致）；失败正文带
#   OBSERVATION_<CODE> 与中文说明，structuredContent 带 {my_agent_observation_error: {code, ...details}}（window_not_found /
#   window_ambiguous 的 details 是可见窗口清单 windows 与 truncated），宿主据此提升 stale / not_found，其它码原样给模型。
# 函数用途: 执行一个观察工具调用并给出可编码的三元组；context 为 None 表示没有宿主上下文（也不会被取消）。
def call_observation_tool(observer: Any, name: str, arguments: Mapping[str, Any], context: CallContext | None) -> tuple[dict, dict | None, bool]:
    context = context or CallContext()
    try:
        if context.cancelled():
            raise ObservationError("cancelled", "宿主已取消，未执行")
        body, structured = _BRANCHES[name](observer, arguments, context)
    except ObservationError as exc:
        error = {"code": exc.code, **({"clicked": True} if exc.clicked else {}),
                 **{key: value for key, value in exc.details.items() if key not in {"code", "clicked"}}}
        return {"code": "OBSERVATION_" + exc.code.upper(), "message": str(exc), **{k: v for k, v in error.items() if k != "code"}}, {OBSERVATION_ERROR_KEY: error}, True
    return body, structured, False


# 函数用途: observe_window 分支：window 是字符串（空 / win: 别名 / 展示标题，解析在 ScreenObserver._target）；正文不重复观察载荷。
def _observe(observer: Any, arguments: Mapping[str, Any], context: CallContext) -> tuple[dict, dict]:
    window = arguments.get("window")
    if window is not None and not isinstance(window, str):
        raise ObservationError("invalid_arguments", "window 必须是字符串")
    result = observer.observe(window or None)
    return {key: value for key, value in result.items() if key != "my_agent_observation"}, result


# 函数用途: click_candidate 分支：candidate_id 必须是非空字符串，候选事实只来自 _meta；复核完真正点击前再看一次是否已取消。
def _click(observer: Any, arguments: Mapping[str, Any], context: CallContext) -> tuple[dict, dict]:
    candidate_id = arguments.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ObservationError("invalid_arguments", "candidate_id 必须是非空字符串")
    result = observer.click_candidate(context.meta, cancelled=context.cancelled)
    return result, result


# 函数用途: type_into_candidate 分支：candidate_id 必须是非空字符串；text / clear_existing 的边界由观察核心在任何副作用之前校验。
def _type_into(observer: Any, arguments: Mapping[str, Any], context: CallContext) -> tuple[dict, dict]:
    candidate_id = arguments.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ObservationError("invalid_arguments", "candidate_id 必须是非空字符串")
    result = observer.type_into_candidate(context.meta, arguments.get("text"), arguments.get("clear_existing", False),
                                          cancelled=context.cancelled)
    return result, result


# 工具名 → 处理分支（键与 OBSERVATION_TOOL_NAMES 一致）
_BRANCHES: dict[str, Callable[[Any, Mapping[str, Any], CallContext], tuple[dict, dict]]] = {
    "observe_window": _observe, "click_candidate": _click, "type_into_candidate": _type_into,
}


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
#   协程被取消时置位本次调用的 cancelled 事件：排在锁后面的调用拿到锁先看它，已取消就不碰 observer；点击在复核完、真正点之前再看
#   一次——用户按了停止之后屏幕上不会多点一下。注意有锁：取消后的下一次观察要排在被丢弃的那次 OCR 之后，不被堵的是事件循环。
# 函数用途: 生成底层 tools/call 处理器：观察工具自己处理，其余交给原处理器。
def observation_call_handler(delegate: Callable[[Any], Any], observer: Any) -> Callable[[Any], Any]:
    gate = threading.Lock()

    def run(name: str, arguments: dict, context: CallContext) -> tuple[dict, dict | None, bool]:
        with gate:
            return call_observation_tool(observer, name, arguments, context)

    async def handler(request: Any) -> Any:
        params = request.params
        if params.name not in OBSERVATION_TOOL_NAMES:
            return await delegate(request)
        extra = getattr(getattr(params, "meta", None), "model_extra", None) or {}
        cancelled = threading.Event()
        context = CallContext(meta=extra.get(OBSERVATION_META_EXTENSION), cancelled=cancelled.is_set)
        try:
            body, structured, is_error = await asyncio.get_running_loop().run_in_executor(None, run, params.name, dict(params.arguments or {}), context)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return encode_call_result(body, structured, is_error)

    return handler


# 函数用途: 把观察接管层装到底层 Server 的 tools/call 处理器上（原处理器成为 delegate）。
def install_observation_handler(low_server: Any, observer: Any) -> None:
    from mcp import types

    delegate = low_server.request_handlers[types.CallToolRequest]
    low_server.request_handlers[types.CallToolRequest] = observation_call_handler(delegate, observer)


# LLM: 适配器进程的唯一装配入口（stdio 收发留在 computer_use_server.serve）：底层 Server 的 list_tools / call_tool 直接绑 FastMCP
#   的公开协程；观察标记为 "1" 时才调用 observer_factory（构造不碰屏幕）、按档位与后端能力注册观察工具并装接管层。标记关着时
#   tools/list 与只有上游工具时逐字节一致，tools/call 处理器就是原 delegate——"关时工具目录不变"靠这里保证。
#   只看档下 FastMCP 里既没有上游工具也没有点击工具（上游由 serve 决定不装载），注册完 observe_window 就够。
# 函数用途: 按环境标记装配底层 Server。
def build_adapter_server(fastmcp: Any, environ: Mapping[str, str], observer_factory: Callable[[], Any]) -> Any:
    from mcp.server.lowlevel import Server

    low = Server(fastmcp.name)
    low.list_tools()(fastmcp.list_tools)
    low.call_tool(validate_input=False)(fastmcp.call_tool)
    if observation_tools_enabled(environ):
        observer = observer_factory()
        register_observation_tools(fastmcp, with_typing=observer.supports_ui_candidates,
                                   observe_only=observe_only_enabled(environ))
        install_observation_handler(low, observer)
    return low


__all__ = [
    "OBSERVATION_TOOL_NAMES", "CallContext", "build_adapter_server", "call_observation_tool", "click_candidate", "encode_call_result",
    "install_observation_handler",
    "observation_call_handler", "observation_tools_enabled", "observe_only_enabled", "observe_window", "register_observation_tools",
    "type_into_candidate",
]
