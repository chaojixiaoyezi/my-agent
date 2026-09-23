# LLM: 仅描述当前 Compact 的临时来源与候选投影；不新增历史权威、持久计划或 tokenizer，提交仍由 compact 原 checkpoint/CAS 控制。
# 模块用途: 携带历史及可选保留工具区，让完整恢复请求在原Compact计量，成功提交后取回同一候选。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .compact_guard import ConversationCompactError

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from .compact_summary_view import AppliedCompactContext
    from .models import ConversationThread, MessageLogEntry


# LLM: 显式宿主可附加同线程的已裁决scope/view；来源行已按该视图筛选，不能再用全线程摘要或游标覆盖。
# 类用途: 让只加载的宿主与后续 Compact 共用同一历史范围、摘要、策略和近期证据，不授予提交权。
@dataclass(frozen=True)
class ConversationCompactSource:
    thread: ConversationThread
    messages: tuple[MessageLogEntry, ...]
    policy: RuntimeCompactPolicy
    recent_operation_evidence: dict[str, object]
    compact_context: AppliedCompactContext | None = None


# LLM: is_candidate区分未提交代次；retained_tool_records为None表示不替换工具，空tuple表示已知全压，不可混淆。
# 类用途: 绑定候选摘要、保留原文/IR、证据及预计代次；IR的None是不替换，空元组是已知全压。
@dataclass(frozen=True)
class ConversationCompactView:
    thread_id: str
    compact_generation: int
    summary: str
    messages: tuple[MessageLogEntry, ...]
    operation_evidence: dict[str, object]
    recent_operation_evidence: dict[str, object]
    history_token_budget: int
    is_candidate: bool
    retained_tool_records: tuple[dict[str, object], ...] | None = field(default=None, repr=False)
    retained_ir_history: tuple[object, ...] | None = field(default=None, repr=False)


# LLM: 计量必须来自完整请求，unknown 应抛结构化错误而非填零；material 属宿主，Compact 不解释也不持久化。
# 类用途: 把每个候选的输入计量与其准备材料绑定，保留候选回退时不误取最后一次投影。
@dataclass(frozen=True)
class ConversationCompactProjection:
    projected_tokens: int
    material: object = field(repr=False)


CompactRequestProjector = Callable[[ConversationCompactView], ConversationCompactProjection]


# LLM: 有projector就必须得到完整结构化投影；None/负数/bool不能降级原粗估或冒充零token，不解析错误正文。
# 函数用途: 检查内部宿主返回的容量合同，缺事实时让原Compact失败链保存原因。
def project_compact_request(projector: CompactRequestProjector | None, view: ConversationCompactView) -> ConversationCompactProjection | None:
    if projector is None:
        return None
    result = projector(view)
    if (not isinstance(result, ConversationCompactProjection)
            or type(result.projected_tokens) is not int or result.projected_tokens < 0 or result.material is None):
        raise ConversationCompactError("完整恢复请求投影不可用", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    return result
