# LLM: 只持久/投影preflight数字与计量来源枚举，不存正文；原采样绑定不重签，typed执行未知仍交原请求收口。
# 模块用途: 保存空闲与重连的上下文数字，压缩或模型切换后的迟到快照不得覆盖新状态。
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..common.request_execution_ports import raise_if_transport_uncertain

_LOGGER = logging.getLogger(__name__)
_SCHEMA = "model_visible_context_usage.v1"
_TOKEN_FIELDS = (
    "context_window_tokens", "compact_trigger_tokens", "current_tokens", "prompt_tokens",
    "messages_tokens", "runtime_guidance_tokens", "tool_schema_tokens",
)
_MEASUREMENT_COUNTER_IDS = {"conservative_estimate": "conservative_cjk_utf8_v1",
                          "calibrated_estimate": "provider_calibrated_v1", "provider_actual": "provider_actual_v1"}


# LLM: 仅由原采样入口建立并随同一次快照传递，不序列化、不授权限；store 对象身份限制归属，数字使用原线程 CAS。
# 类用途: 保存一次上下文采样开始时的准确线程、压缩代次和模型选择版本。
@dataclass(frozen=True)
class ContextUsageBinding:
    store: object = field(repr=False, compare=False)
    thread_id: str
    compact_generation: int
    model_selection_revision: int


# LLM: task_attributes 只选准确宿主线程，不持有版本或权限；隔离表达轮不写任何线程遥测。
# 函数用途: 为采样与保存共用原主/子线程选择，child agent_thread_id 优先。
def _context_usage_thread_id(params: object) -> str:
    if str(getattr(params, "context_scope", "") or "").strip().lower() == "isolated":
        return ""
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, Mapping) else {}
    return str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "").strip()


# LLM: 只能在实际估算开始前调用；已绑定模型仍用原选择版本，不给旧工作片绑定新的线程 revision。
#   只读原 store，不创建线程；缺失、错代或普通读取故障仅弃遥测，原执行权未知必须交原请求处理，不能降格或重签。
# 函数用途: 在采样点冻结准确 store/线程及两种代次，供迟到保存时沿原 CAS 检查。
def capture_context_usage_binding(agent: object, params: object) -> ContextUsageBinding | None:
    from ..settings.model_scope import current_model_selection

    thread_id = _context_usage_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    loader = getattr(getattr(store, "threads", None), "load_report", None)
    if not thread_id or not callable(loader):
        return None
    try:
        thread, error = loader(thread_id)
        if error is not None or thread is None or thread.thread_id != thread_id:
            return None
        generation, revision = thread.compact_generation, thread.model_selection_revision
        if type(generation) is not int or type(revision) is not int or min(generation, revision) < 0:
            return None
        selection = current_model_selection(agent)
        if (selection is not None and selection.thread_id == thread_id
                and (selection.selection_thread_id or thread_id) == thread_id
                and selection.selection_source == "thread" and selection.revision != revision):
            return None
        return ContextUsageBinding(store, thread_id, generation, revision)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise_if_transport_uncertain(error)
        _LOGGER.warning("context usage binding could not be captured", exc_info=False)
        return None


# LLM: 计数字段不接受布尔值或无限值；展示无效数按零处理，不把任意正文带入持久快照。
# 函数用途: 清洗一个上下文数值，不将坏数据传播到 TUI。
def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


# LLM: 短标签只接受字符串、去首尾空白、截到 80 字符；非字符串按空串，不把任何正文带进快照。
# 函数用途: 清洗模型名、思考档位这类只给状态行看的短字段。
def _label(value: object) -> str:
    return value.strip()[:80] if isinstance(value, str) else ""


# LLM: 旧快照缺raw时按current解释；来源只收固定枚举，counter/scope由口径派生，不信任自由标签或伪造的校准总量。
# 函数用途: 给已有计量附加清洗后的原始值、校准值及计数来源。
def _measurement_fields(value: Mapping) -> dict[str, object]:
    current = _count(value.get("current_tokens"))
    raw = _count(value.get("raw_estimated_tokens", current))
    kind = value.get("measurement_kind")
    inferred = "calibrated_estimate" if current != raw else "conservative_estimate"
    if (not isinstance(kind, str) or kind not in _MEASUREMENT_COUNTER_IDS
            or (kind == "conservative_estimate" and current != raw)):
        kind = inferred
    return {"raw_estimated_tokens": raw, "calibrated_tokens": current, "measurement_kind": kind,
            "counter_id": _MEASUREMENT_COUNTER_IDS[kind], "count_scope": "full_outbound_request"}


# LLM: 同一白名单保存/回读新增来源；旧字段、准确thread与两种CAS合同不变，原始prompt、工具参数和未知字段不保留。
#   model_name / reasoning_level 是状态行
#   显示用的短标签（来自 agent_core.model.context_pressure 的快照），旧快照没有时为空串。
# 函数用途: 复制已有 preflight 快照中的合法数字与两个短标签，不重新估算模型上下文。
def public_context_usage(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("schema") != _SCHEMA:
        return {}
    protocol = value.get("protocol")
    return {
        "schema": _SCHEMA,
        "estimated": value.get("estimated") is True,
        **{key: _count(value.get(key)) for key in _TOKEN_FIELDS},
        "protocol": protocol if protocol in ("native", "text") else "unknown",
        "model_name": _label(value.get("model_name")),
        "reasoning_level": _label(value.get("reasoning_level")),
        **_measurement_fields(value),
    }


# LLM: 只读已加载的 exact thread，旧代或缺失记录保持未知；不回读 child 属性、累计账本或校准值。
# 函数用途: 取得该代理最近一次调用前的上下文数字，供空闲/运行/恢复页面共用。
def context_usage_from_thread(thread: object) -> dict[str, object]:
    value = getattr(thread, "model_context_usage", None)
    if not isinstance(value, Mapping) or type(value.get("compact_generation")) is not int:
        return {}
    if value["compact_generation"] != getattr(thread, "compact_generation", 0):
        return {}
    return public_context_usage(value)


# LLM: 只消费同次采样携带的 typed 绑定；禁止在迟到写入点 fresh-load 线程替旧采样签新代次，缺绑定只弃持久遥测。
# 函数用途: 将已绑定的主/子上下文数字交原线程 CAS 保存，不写历史、更新活跃时间或阻断模型。
def record_model_context_usage(
    agent: object, params: object, usage: object, *, binding: ContextUsageBinding | None = None,
) -> bool:
    thread_id = _context_usage_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    public = public_context_usage(usage)
    if (not thread_id or not public or not isinstance(binding, ContextUsageBinding)
            or binding.store is not store or binding.thread_id != thread_id
            or type(binding.compact_generation) is not int or type(binding.model_selection_revision) is not int
            or min(binding.compact_generation, binding.model_selection_revision) < 0):
        return False
    return save_context_usage_snapshot(store, binding, public)


# LLM: preflight/Compact共用已解析的owner线程和两种捕获代次；迟到遥测不得越过压缩或模型选择CAS，不改历史/费用。
# model_usage领域通过原线程锁更新；普通读写失败仅弃遥测，typed执行未知保持原对象抛出，不猜代次、释放责任或重放。
# 函数用途: 主子调用和压缩预检查共用一个数值保存入口；旧代或属性读取失败不写回，仅记录无内部明细告警。
def save_context_usage_snapshot(store: object, thread: object, usage: object) -> bool:
    updater = getattr(getattr(store, "model_usage", None), "update_context_usage", None)
    public = public_context_usage(usage)
    if thread is None or not public or not callable(updater):
        return False
    try:
        generation = thread.compact_generation
        revision = thread.model_selection_revision
        updated = updater(thread.thread_id, {**public, "model_selection_revision": revision}, expected_compact_generation=generation)
        return (updated.compact_generation == generation and updated.model_selection_revision == revision
                and context_usage_from_thread(updated) == public)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise_if_transport_uncertain(error)
        _LOGGER.warning("context usage telemetry could not be saved", exc_info=False)
        return False
