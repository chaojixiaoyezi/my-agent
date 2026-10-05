# LLM: B4 事件点只投影白名单标量并调用注入的 B3 发布口；装配诊断仅固定原因码、进程内去重，不读安装表或等待插件。
# 模块用途: 统一六类观察事件的减量与短路，以及启用后装配失败的一次性安全日志。
from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..common.log_redaction import redact_sensitive_value
from .protocol import EVENT_ACTOR_KINDS, MAX_PROMPT_CONTENT_CHARS, EventFact

logger = logging.getLogger(__name__)
_WARNED_PLUGIN_EVENT_ASSEMBLY_REASONS: set[str] = set()
_PLUGIN_EVENT_ASSEMBLY_WARNINGS_LOCK = threading.Lock()

# 事件目录 v1 的精确标量集合；额外输入（参数、输出、路径）永远不复制。
_EVENT_FACT_FIELDS = {
    "prompt_submitted": ("request_id", "chars", "has_attachments"),
    "turn_started": ("request_id", "model_name"),
    "turn_ended": ("request_id", "status", "duration_ms", "tool_calls", "error_code"),
    "tool_call_started": ("call_id", "tool", "effect", "args_hash"),
    "tool_call_finished": ("call_id", "tool", "ok", "error_code", "failure_stage", "duration_ms", "handler_executed"),
    "command_executed": ("command", "operation_id", "state"),
}


# LLM: 原因由各宿主入口固定给出；锁只保护首次资格，不持锁调用 logging，也不存 owner、配置或异常对象。
# 函数用途: 同一进程同一装配原因只认领一次，避免多线程故障刷屏。
def _claim_event_assembly_warning(reason_code: str) -> bool:
    with _PLUGIN_EVENT_ASSEMBLY_WARNINGS_LOCK:
        if reason_code in _WARNED_PLUGIN_EVENT_ASSEMBLY_REASONS:
            return False
        _WARNED_PLUGIN_EVENT_ASSEMBLY_REASONS.add(reason_code)
    return True


# LLM: 调用方必须已经确认开关严格 True；只传固定宿主原因码，不传配置、正文、路径、异常或 traceback。
# 诊断失败仍只丢这次日志，不能穿透 B4 隔离或业务链；资格先认领，因此坏日志 sink 也不会被重复调用。
# 函数用途: 记录一次结构化装配 warning，让开启却未生效与关闭静默可区分。
def warn_event_assembly_failure(reason_code: str) -> None:
    try:
        if not _claim_event_assembly_warning(reason_code):
            return
        logger.warning('插件事件观察已开启，但本次装配失败（reason_code=%s）；仅跳过观察，不影响业务。',
                       reason_code, extra={'event': 'plugin_events.assembly_failed', 'reason_code': reason_code})
    except Exception:  # noqa: BLE001 诊断本身不能反噬工具或 Gateway 业务
        return


# LLM: 身份、配置和发布回调只由宿主构造；正文与工具参数不能改变这些字段。
# 类用途: 保存事件点的可信上下文，不持久化或创建插件连接。
@dataclass(frozen=True)
class EventPointContext:
    config: object
    publish: Callable[[EventFact], None]
    channel: str = ""
    thread_id: str = ""
    actor: str = "main"


# LLM: 精确白名单字段来自设计第 6 节；只保留标量，命令仅接受命令名；B3 逐插件核对已确认激活的 text 声明。
# 函数用途: 将宿主字段投影为观察数据，正文不进入 facts，工具参数和输出始终丢弃。
def project_event(context: EventPointContext, event_type: str, source: dict) -> EventFact | None:
    fields = _EVENT_FACT_FIELDS.get(event_type)
    if fields is None:
        return None
    facts = {key: _fact_scalar(key, source.get(key, "")) for key in fields}
    content = ""
    if event_type == "prompt_submitted":
        prompt = str(source.get("prompt") or "")
        facts["chars"] = len(prompt)
        facts["has_attachments"] = bool(source.get("has_attachments", False))
        content = redact_sensitive_value(prompt)[:MAX_PROMPT_CONTENT_CHARS]
    actor = context.actor if context.actor in EVENT_ACTOR_KINDS else "main"
    thread_ref = hashlib.sha256(context.thread_id.encode("utf-8")).hexdigest() if context.thread_id else ""
    return EventFact(event_type, facts, context.channel, thread_ref, actor, content)


# LLM: 事件目录只允许标量；嵌套 arguments/output 不能借合法字段名混入，命令参数必须留在控制回执而不是观察里。
# 函数用途: 减量事实值，非法命令串和对象值直接置空，不解析正文决定宿主行为。
def _fact_scalar(key: str, value: object) -> object:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    if key == "command" and (not isinstance(value, str) or re.fullmatch(r"/[a-zA-Z][a-zA-Z0-9_-]*", value) is None):
        return ""
    return value


# LLM: 关闭必须先于任何投影/发布；观察故障不向工具或 Gateway 业务链抛出。
# 函数用途: 在事件点投递事实，关闭或故障时丢弃观察，不影响业务结果。
def emit_event(context: EventPointContext | None, event_type: str, source: dict) -> None:
    try:
        if context is None or getattr(context.config, "plugin_events_enabled", False) is not True:
            return
        event = project_event(context, event_type, source)
        if event is not None:
            context.publish(event)
    except Exception:  # noqa: BLE001 观察失败不能改变工具结果或 Gateway 回执
        return
