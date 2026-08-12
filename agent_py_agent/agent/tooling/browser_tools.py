
from __future__ import annotations

"""浏览器自动化工具 —— 单入口 `browser`,用 action 选 navigate/snapshot/click/type/close。

给模型一套浏览器能力,补 web_fetch 处理不了的"需要 JS 渲染/点击/填表/登录"的页面(SPA、动态站)。
会话/浏览器生命周期管理见 browser_session.py(惰性启动 Chromium)。

设计(原先拆成 5 个独立工具,现合成 1 个 action 参数化工具):
  - 这 5 个动作共享会话管理器/异常兜底/结果壳,且参数高度重合(session_id/ref/text/url),
    属同一"浏览器交互"动作族 —— 合成一个 `browser` + action 更紧凑(主目录少 4 个工具)。
  - 各 action 的逻辑/精确校验/错误码与原先逐工具版**完全一致**,只是入口收成一个;条件必填
    (navigate 要 url、click/type 要 ref、type 要 text)在 execute 里按 action 运行时校验。
  - effect 取整组最严:浏览器交互有状态、有副作用(开页面/建会话/点击改服务端状态)→ 整体声明
    mutating + idempotency_scope=operation（框架按调用身份派生幂等 key,对模型透明;只读的 snapshot 也被并入,
    代价仅是多一个不被用到的自动 key,不影响功能)。
  - a11y 快照(accessibility tree + ref 列表)直接放进输出,让没有视觉能力的模型也能理解页面。

SSRF:navigate 导航前复用 my-agent 现有 network_safety gate(和 web_fetch 同一把锁),默认拒私网/
loopback/云 metadata。异常兜底:浏览器不可用 → TOOL_UNAVAILABLE(带安装指引);导航失败/超时 →
NETWORK_REQUEST_FAILED / TOOL_TIMEOUT;无效 ref/参数 → TOOL_INVALID_ARGUMENTS;所有 playwright
异常都被捕获成结构化错误,不让异常冒泡崩主流程。
"""

import json
from typing import Any

from .browser_session import (
    BrowserSessionManager,
    BrowserUnavailableError,
    browser_session_manager,
)
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .web import _default_network_resolver, _network_safety_error, _normalize_url

# 模型不传 session_id 时用的默认会话名(单会话场景够用;多任务并发可显式分会话)。
_DEFAULT_SESSION = "default"
_BROWSER_ACTIONS = ("navigate", "snapshot", "click", "type", "close")


def _coerce_session_id(params: dict[str, Any]) -> str:
    value = params.get("session_id")
    text = str(value).strip() if value is not None else ""
    return text or _DEFAULT_SESSION


def _is_timeout_error(exc: Exception) -> bool:
    """判断一个 playwright 异常是不是超时(决定 error_code 用 TOOL_TIMEOUT 还是别的)。"""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    return "timeout" in str(exc).lower()


def _unavailable_result(tool_name: str, exc: BrowserUnavailableError) -> ToolHandlerOutcome:
    """浏览器不可用(没装 playwright / Chromium 二进制缺失)→ TOOL_UNAVAILABLE,带指引。"""
    payload = {
        "ok": False,
        "error": "browser_unavailable",
        "message": exc.hint,
    }
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="TOOL_UNAVAILABLE",
    )


def _action_error_result(tool_name: str, exc: Exception, *, default_msg: str) -> ToolHandlerOutcome:
    """把一次浏览器动作的异常转成结构化 error_code(不崩)。

    超时 → TOOL_TIMEOUT(retryable);其余 playwright 动作失败(元素找不到/导航被拒/页面崩)
    → NETWORK_REQUEST_FAILED(retryable,提示退避或换来源/重拍快照)。
    """
    if _is_timeout_error(exc):
        code = "TOOL_TIMEOUT"
        message = f"{default_msg}:操作超时({exc})。"
    else:
        code = "NETWORK_REQUEST_FAILED"
        message = f"{default_msg}:{exc}"
    payload = {"ok": False, "error": code.lower(), "message": message}
    return ToolHandlerOutcome(tool_name, False, json.dumps(payload, ensure_ascii=False), error_code=code)


def _invalid_args_result(tool_name: str, message: str) -> ToolHandlerOutcome:
    payload = {"ok": False, "error": "invalid_arguments", "message": message}
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _ok_result(tool_name: str, payload: dict[str, Any]) -> ToolHandlerOutcome:
    body = {"ok": True, **payload}
    return ToolHandlerOutcome(
        tool_name,
        True,
        json.dumps(body, ensure_ascii=False),
        result_envelope=body,
    )


# LLM: browser 仅在 Playwright Python 运行时可导入且现有浏览器连接健康时暴露，绝不为检查而启动 Chromium。
# 类用途: 用一个 action 化工具管理隔离浏览器会话、页面快照和交互动作。
class BrowserTool(BaseTool):
    """单入口浏览器自动化:action ∈ navigate/snapshot/click/type/close,共享一个会话管理器。"""

    model_spec = ToolModelSpec(
        name="browser",
        description=(
            "headless 浏览器自动化(处理 web_fetch 抓不到的 JS 渲染/SPA/需点击填表的动态页)。"
            "用 action 选动作:navigate=打开 URL 并返回 a11y 快照;snapshot=重取当前页快照;"
            "click=点击 ref 指向的元素;type=往 ref 输入框填 text;close=关闭会话。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_BROWSER_ACTIONS),
                    "description": "必填。navigate=打开 URL 并取快照；snapshot=重取快照；click=点 ref；type=往 ref 填 text；close=关会话。",
                },
                "url": {
                    "type": "string",
                    "description": "仅 action=navigate。完整 http/https 地址；导航前走 SSRF 检查。",
                },
                "ref": {
                    "type": "string",
                    "description": "action=click/type。快照给出的 ref 或 CSS selector。",
                },
                "text": {"type": "string", "description": "仅 action=type。要填入的字符串。"},
                "session_id": {
                    "type": "string",
                    "description": "可选。同名会话复用浏览器上下文；不传使用 default。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="web",
            use_cases=(
                "动态站/SPA:navigate 打开 → snapshot 看结构和 ref → click/type 交互",
                "登录/搜索/多步表单:navigate → type 填表 → click 提交",
                "web_fetch 抓回是 JS 空壳/前端渲染站时,改用 browser 渲染后再看内容",
            ),
            avoid_when=(
                "目标是静态页/纯 API/可直接下载的文档 → 用更轻量的 web_fetch",
                "只是批量抽取多个已知 URL 的正文 → 用 web_fetch 的 extract 模式",
            ),
            keywords=(
                "浏览器", "browser", "navigate", "snapshot", "click", "type", "close",
                "打开网页", "渲染", "SPA", "动态页面", "JS", "点击", "填表", "表单",
                "accessibility", "a11y", "快照", "ref", "selector", "headless", "登录",
            ),
            examples=(
                '{"tool": "browser", "action": "navigate", "url": "https://example.com"}',
                '{"tool": "browser", "action": "snapshot"}',
                '{"tool": "browser", "action": "click", "ref": "e5"}',
                '{"tool": "browser", "action": "type", "ref": "e3", "text": "hello@example.com"}',
                '{"tool": "browser", "action": "close", "session_id": "task-7"}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=(("action", (("snapshot", "read_only"),)),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id", "url"),
            parameter_kinds={"session_id": "logical", "url": "logical"},
        ),
        output_policy=OutputPolicy(trust="external_data"),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(self, manager: BrowserSessionManager | None = None):
        self.manager = manager or browser_session_manager

    # LLM: readiness_error 只检查依赖/既有连接，不创建浏览器、页面、cookie 或外部网络请求。
    # 函数用途: 在构建工具快照时隐藏当前环境无法工作的 browser 工具。
    def availability(self) -> ToolAvailability:
        reason = self.manager.readiness_error()
        return (
            ToolAvailability.unavailable(reason)
            if reason
            else ToolAvailability.ready()
        )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        handler = {
            "navigate": self._navigate,
            "snapshot": self._snapshot,
            "click": self._click,
            "type": self._type,
            "close": self._close,
        }.get(action)
        if handler is None:
            return _invalid_args_result(
                self.model_spec.name,
                f"action 必须是 {list(_BROWSER_ACTIONS)} 之一;收到 {action!r}。",
            )
        return handler(params)

    def _navigate(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            url = _normalize_url(params.get("url"))
        except ValueError as exc:
            return _invalid_args_result(self.model_spec.name, str(exc))
        # SSRF:复用 web_fetch 同一把锁,默认拒私网/loopback/云 metadata。
        network_error = _network_safety_error(self.model_spec.name, url, _default_network_resolver)
        if network_error is not None:
            return network_error
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.navigate(session_id, url)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.model_spec.name, exc)
        except Exception as exc:  # 导航失败/超时/页面崩 → 结构化错误,不崩
            return _action_error_result(self.model_spec.name, exc, default_msg=f"打开 {url} 失败")
        return _ok_result(self.model_spec.name, {"session_id": session_id, **result})

    def _snapshot(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.snapshot(session_id)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.model_spec.name, exc)
        except ValueError as exc:  # 未导航/会话不存在
            return _invalid_args_result(self.model_spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.model_spec.name, exc, default_msg="获取页面快照失败")
        return _ok_result(self.model_spec.name, {"session_id": session_id, **result})

    def _click(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        ref = str(params.get("ref") or "").strip()
        if not ref:
            return _invalid_args_result(self.model_spec.name, "action=click 需要 ref(快照里的 ref 或 CSS selector)。")
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.click(session_id, ref)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.model_spec.name, exc)
        except ValueError as exc:  # 未导航 / ref 过期
            return _invalid_args_result(self.model_spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.model_spec.name, exc, default_msg=f"点击 {ref} 失败")
        return _ok_result(self.model_spec.name, {"session_id": session_id, **result})

    def _type(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        ref = str(params.get("ref") or "").strip()
        if not ref:
            return _invalid_args_result(self.model_spec.name, "action=type 需要 ref(快照里的 ref 或 CSS selector)。")
        if params.get("text") is None:
            return _invalid_args_result(self.model_spec.name, "action=type 需要 text(要填入的文字)。")
        text = str(params.get("text"))
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.type_text(session_id, ref, text)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.model_spec.name, exc)
        except ValueError as exc:
            return _invalid_args_result(self.model_spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.model_spec.name, exc, default_msg=f"往 {ref} 填文字失败")
        return _ok_result(self.model_spec.name, {"session_id": session_id, **result})

    def _close(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.close(session_id)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.model_spec.name, exc)
        except Exception as exc:
            return _action_error_result(self.model_spec.name, exc, default_msg="关闭浏览器会话失败")
        return _ok_result(self.model_spec.name, result)


def browser_tools() -> list[BaseTool]:
    """构造浏览器工具(单 action 参数化入口)。共享进程内单实例会话管理器。"""
    return [BrowserTool()]


__all__ = ["BrowserTool", "browser_tools"]
