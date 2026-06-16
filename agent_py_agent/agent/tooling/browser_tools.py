
from __future__ import annotations

"""浏览器自动化工具 —— browser_navigate / browser_snapshot / browser_click / browser_type / browser_close。

给模型一套精确 schema 的浏览器工具,补 web_fetch 处理不了的"需要 JS 渲染/点击/填表/登录"
的页面(SPA、动态站)。会话/浏览器生命周期管理见 browser_session.py(惰性启动 Chromium)。

对标 长期助手 的 browser_navigate/snapshot/click/type(那边是云/CLI 后端),这里:
  - 用进程内 playwright(headless Chromium),零子进程/零云依赖。
  - 拆成独立工具(每个精确 parameter_schema + required_parameters),对齐 my-agent 的
    native tool_use 路线(独立工具比"一个工具靠 action 分流"对模型更直白)。
  - a11y 快照(accessibility tree + ref 列表)直接放进工具输出,让没有视觉能力的模型
    也能理解页面、按 ref 点击/填表。

SSRF:browser_navigate 导航前复用 my-agent 现有 network_safety gate(和 web_fetch 完全
同一把锁:_normalize_url + _network_safety_error),默认拒私网/loopback/云 metadata。
异常兜底:浏览器不可用(没装/二进制缺失)→ TOOL_UNAVAILABLE(带安装指引);
导航失败/超时 → NETWORK_REQUEST_FAILED / TOOL_TIMEOUT;无效 ref/参数 → TOOL_INVALID_ARGUMENTS;
所有 playwright 异常都被捕获成结构化错误,不让异常冒泡崩主流程。
"""

import json
from typing import Any

from .browser_session import (
    BrowserSessionManager,
    BrowserUnavailableError,
    browser_session_manager,
)
from .models import BaseTool, ToolExecutionResult, ToolSpec
from .web import _network_safety_error, _normalize_url, _default_network_resolver

# 模型不传 session_id 时用的默认会话名(单会话场景够用;多任务并发可显式分会话)。
_DEFAULT_SESSION = "default"


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


def _unavailable_result(tool_name: str, exc: BrowserUnavailableError) -> ToolExecutionResult:
    """浏览器不可用(没装 playwright / Chromium 二进制缺失)→ TOOL_UNAVAILABLE,带指引。"""
    payload = {
        "ok": False,
        "error": "browser_unavailable",
        "message": exc.hint,
    }
    return ToolExecutionResult(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="TOOL_UNAVAILABLE",
    )


def _action_error_result(tool_name: str, exc: Exception, *, default_msg: str) -> ToolExecutionResult:
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
    return ToolExecutionResult(tool_name, False, json.dumps(payload, ensure_ascii=False), error_code=code)


def _invalid_args_result(tool_name: str, message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": "invalid_arguments", "message": message}
    return ToolExecutionResult(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


def _ok_result(tool_name: str, payload: dict[str, Any]) -> ToolExecutionResult:
    body = {"ok": True, **payload}
    return ToolExecutionResult(
        tool_name,
        True,
        json.dumps(body, ensure_ascii=False),
        result_envelope=body,
    )


class _BrowserToolBase(BaseTool):
    """共享浏览器会话管理器 + 统一异常兜底壳。"""

    def __init__(self, manager: BrowserSessionManager | None = None):
        self.manager = manager or browser_session_manager


class BrowserNavigateTool(_BrowserToolBase):
    """打开页面并返回标题 + accessibility 快照(无需视觉就能理解页面)。"""

    spec = ToolSpec(
        name="browser_navigate",
        category="web",
        effect="read_only",
        description=(
            "用 headless 浏览器打开一个 URL(支持 JS 渲染/SPA/动态站),返回页面标题和 "
            "accessibility 快照(页面结构 + 可点/可填元素的 ref)。处理 web_fetch 抓不到的动态页面。"
        ),
        use_cases=[
            "web_fetch 抓回来是空壳/JS 占位(SPA、前端渲染站),需要真浏览器渲染后再看内容",
            "需要在页面上点击、填表单、走多步交互(登录/搜索/翻页)才能拿到目标内容",
            "需要拿到页面可交互元素的 ref,后续用 browser_click / browser_type 操作",
        ],
        avoid_when=[
            "目标是静态页面/纯 API/可直接下载的文档时,用更轻量的 web_fetch",
            "只是要批量抽取多个已知 URL 的正文时,用 web_fetch 的 extract 模式",
        ],
        keywords=[
            "浏览器", "browser", "navigate", "打开网页", "渲染", "SPA", "动态页面",
            "JS", "点击", "填表", "accessibility", "a11y", "快照", "headless",
        ],
        parameters={
            "url": "要打开的完整 URL(http/https)。",
            "session_id": "可选。浏览器会话名,隔离 cookie/storage;不传用 default。",
        },
        parameter_details={
            "url": "完整 http 或 https 地址。导航前会走 SSRF 安全检查,默认拒绝私网/内网/云 metadata 地址。",
            "session_id": "可选字符串。同名会话复用同一浏览器上下文(登录态/cookie 保留);并发不同任务可分不同会话。",
        },
        parameter_schema={
            "url": {"type": "string"},
            "session_id": {"type": "string"},
        },
        required_parameters=["url"],
        examples=[
            '{"tool": "browser_navigate", "url": "https://example.com"}',
            '{"tool": "browser_navigate", "url": "https://app.example.com/login", "session_id": "task-7"}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_url = params.get("url")
        try:
            url = _normalize_url(raw_url)
        except ValueError as exc:
            return _invalid_args_result(self.spec.name, str(exc))
        # SSRF:复用 web_fetch 同一把锁,默认拒私网/loopback/云 metadata。
        network_error = _network_safety_error(self.spec.name, url, _default_network_resolver)
        if network_error is not None:
            return network_error
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.navigate(session_id, url)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.spec.name, exc)
        except Exception as exc:  # 导航失败/超时/页面崩 → 结构化错误,不崩
            return _action_error_result(self.spec.name, exc, default_msg=f"打开 {url} 失败")
        return _ok_result(self.spec.name, {"session_id": session_id, **result})


class BrowserSnapshotTool(_BrowserToolBase):
    """当前页的 accessibility 快照(给模型看页面有什么、能点什么)。"""

    spec = ToolSpec(
        name="browser_snapshot",
        category="web",
        effect="read_only",
        description=(
            "返回当前浏览器页面的 accessibility 快照(页面结构文本 + 可交互元素的 ref)。"
            "用于在点击/填表后重新观察页面,或获取可操作元素的 ref。"
        ),
        use_cases=[
            "browser_navigate / browser_click 之后,重新观察当前页面内容和可交互元素",
            "需要拿到某个按钮/输入框的 ref,再用 browser_click / browser_type 操作",
            "页面内容很多,先快照看结构再决定下一步操作",
        ],
        avoid_when=[
            "还没用 browser_navigate 打开任何页面时(会提示先导航)",
        ],
        keywords=["浏览器", "browser", "snapshot", "快照", "accessibility", "a11y", "页面结构", "ref", "可点元素"],
        parameters={
            "session_id": "可选。浏览器会话名;不传用 default。",
        },
        parameter_details={
            "session_id": "可选字符串。要取快照的会话;应和之前 browser_navigate 用的会话一致。",
        },
        parameter_schema={
            "session_id": {"type": "string"},
        },
        required_parameters=[],
        examples=[
            '{"tool": "browser_snapshot"}',
            '{"tool": "browser_snapshot", "session_id": "task-7"}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.snapshot(session_id)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.spec.name, exc)
        except ValueError as exc:  # 未导航/会话不存在
            return _invalid_args_result(self.spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.spec.name, exc, default_msg="获取页面快照失败")
        return _ok_result(self.spec.name, {"session_id": session_id, **result})


class BrowserClickTool(_BrowserToolBase):
    """点击元素(用快照里的 ref 或 CSS selector),返回点击后的新快照。"""

    spec = ToolSpec(
        name="browser_click",
        category="web",
        effect="mutating",
        # side-effecting 工具按 manifest 契约必须声明幂等策略,否则 tool_manifest 门会在
        # execute 之前 0.00s 判 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING 拦死(框架自动派生
        # idempotency_key,模型无需手填)。与 log_alert_poll 同根因(见 log_ops/tools.py)。
        requires_idempotency=True,
        description=(
            "点击当前页面上的一个元素,用 browser_snapshot/browser_navigate 返回的 ref(如 e5)"
            "或 CSS selector 定位。点击后返回更新的页面快照。"
        ),
        use_cases=[
            "点按钮/链接/标签页/菜单项,触发页面交互或跳转",
            "提交表单(点 submit 按钮)、展开折叠内容、翻页",
        ],
        avoid_when=[
            "还没打开页面时先用 browser_navigate",
            "要往输入框里填文字时用 browser_type 而不是 browser_click",
        ],
        keywords=["浏览器", "browser", "click", "点击", "按钮", "链接", "ref", "selector", "提交"],
        parameters={
            "ref": "要点击的元素:快照里的 ref(如 e5),或 CSS selector。",
            "session_id": "可选。浏览器会话名;不传用 default。",
        },
        parameter_details={
            "ref": "优先用 browser_snapshot/browser_navigate 快照里给出的 ref(形如 e5);也可传 CSS selector。ref 过期(页面已变)会提示重拍快照。",
            "session_id": "可选字符串。应和当前操作的页面会话一致。",
        },
        parameter_schema={
            "ref": {"type": "string"},
            "session_id": {"type": "string"},
        },
        required_parameters=["ref"],
        examples=[
            '{"tool": "browser_click", "ref": "e5"}',
            '{"tool": "browser_click", "ref": "button.submit", "session_id": "task-7"}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        ref = str(params.get("ref") or "").strip()
        if not ref:
            return _invalid_args_result(self.spec.name, "browser_click 需要 ref 参数(快照里的 ref 或 CSS selector)。")
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.click(session_id, ref)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.spec.name, exc)
        except ValueError as exc:  # 未导航 / ref 过期
            return _invalid_args_result(self.spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.spec.name, exc, default_msg=f"点击 {ref} 失败")
        return _ok_result(self.spec.name, {"session_id": session_id, **result})


class BrowserTypeTool(_BrowserToolBase):
    """往输入框填文字(用 ref 或 selector 定位,先清空再输入),返回新快照。"""

    spec = ToolSpec(
        name="browser_type",
        category="web",
        effect="mutating",
        # 同 browser_click:side-effecting 必须声明幂等策略,否则被 tool_manifest 门
        # 0.00s 拦成 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING(框架自动派生 key)。
        requires_idempotency=True,
        description=(
            "往当前页面的输入框/文本域填入文字,用 ref(如 e3)或 CSS selector 定位。"
            "会先清空再输入。填完返回更新的页面快照。"
        ),
        use_cases=[
            "填登录表单(用户名/密码)、搜索框、任意文本输入框",
            "多步表单:配合 browser_click 提交",
        ],
        avoid_when=[
            "还没打开页面时先用 browser_navigate",
            "只是点按钮/链接时用 browser_click",
        ],
        keywords=["浏览器", "browser", "type", "fill", "输入", "填表", "表单", "文本框", "ref", "selector"],
        parameters={
            "ref": "要填入的输入框:快照里的 ref(如 e3),或 CSS selector。",
            "text": "要填入的文字。",
            "session_id": "可选。浏览器会话名;不传用 default。",
        },
        parameter_details={
            "ref": "优先用快照里给出的 ref(形如 e3);也可传 CSS selector。指向一个可输入的元素(input/textarea/可编辑区)。",
            "text": "要填入的字符串;会先清空目标输入框再输入。",
            "session_id": "可选字符串。应和当前操作的页面会话一致。",
        },
        parameter_schema={
            "ref": {"type": "string"},
            "text": {"type": "string"},
            "session_id": {"type": "string"},
        },
        required_parameters=["ref", "text"],
        examples=[
            '{"tool": "browser_type", "ref": "e3", "text": "hello@example.com"}',
            '{"tool": "browser_type", "ref": "input[name=q]", "text": "playwright", "session_id": "task-7"}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        ref = str(params.get("ref") or "").strip()
        if not ref:
            return _invalid_args_result(self.spec.name, "browser_type 需要 ref 参数(快照里的 ref 或 CSS selector)。")
        if params.get("text") is None:
            return _invalid_args_result(self.spec.name, "browser_type 需要 text 参数(要填入的文字)。")
        text = str(params.get("text"))
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.type_text(session_id, ref, text)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.spec.name, exc)
        except ValueError as exc:
            return _invalid_args_result(self.spec.name, str(exc))
        except Exception as exc:
            return _action_error_result(self.spec.name, exc, default_msg=f"往 {ref} 填文字失败")
        return _ok_result(self.spec.name, {"session_id": session_id, **result})


class BrowserCloseTool(_BrowserToolBase):
    """关闭一个浏览器会话释放资源(幂等:不存在的会话也正常返回)。"""

    spec = ToolSpec(
        name="browser_close",
        category="web",
        effect="mutating",
        requires_idempotency=True,
        description="关闭一个浏览器会话(释放该会话的页面/上下文资源)。用完浏览器后收尾调用。",
        use_cases=[
            "完成一段浏览器交互后,关掉会话释放资源",
            "收尾阶段清理打开的浏览器会话",
        ],
        avoid_when=[
            "还要继续在同一会话上操作页面时不要关",
        ],
        keywords=["浏览器", "browser", "close", "关闭", "释放", "会话", "收尾"],
        parameters={
            "session_id": "可选。要关闭的浏览器会话名;不传关 default。",
        },
        parameter_details={
            "session_id": "可选字符串。要关闭的会话;不传则关 default 会话。关不存在的会话返回 already_closed,不报错。",
        },
        parameter_schema={
            "session_id": {"type": "string"},
        },
        required_parameters=[],
        examples=[
            '{"tool": "browser_close"}',
            '{"tool": "browser_close", "session_id": "task-7"}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        session_id = _coerce_session_id(params)
        try:
            result = self.manager.close(session_id)
        except BrowserUnavailableError as exc:
            return _unavailable_result(self.spec.name, exc)
        except Exception as exc:
            return _action_error_result(self.spec.name, exc, default_msg="关闭浏览器会话失败")
        return _ok_result(self.spec.name, result)


def browser_tools() -> list[BaseTool]:
    """构造全部浏览器工具(供 registry 注册)。共享进程内单实例会话管理器。"""
    return [
        BrowserNavigateTool(),
        BrowserSnapshotTool(),
        BrowserClickTool(),
        BrowserTypeTool(),
        BrowserCloseTool(),
    ]


__all__ = [
    "BrowserNavigateTool",
    "BrowserSnapshotTool",
    "BrowserClickTool",
    "BrowserTypeTool",
    "BrowserCloseTool",
    "browser_tools",
]
