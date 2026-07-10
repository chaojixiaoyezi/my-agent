
from __future__ import annotations

"""compact 续跑历史的 LLM 语义摘要（短板6 子项，长期助手 trajectory_compressor 蓝本）。

背景:
  compact 卸掉全量对话后,续跑靠 ``loop_support._reconstructed_runtime_state`` 从 carried
  archive 记录逐条重建 ``tool_context``(每条只剩 ``output_preview``)。这是机械搬运——窗口外
  的旧轮被截断/丢弃,没有"这段中间过程到底做了什么/学到什么"的语义叙述,长任务续跑容易丢
  "模型需要的"关键上下文。

摘要边界：
  - 保护首尾:开头的任务/早期动作 + 结尾的近期动作原样保留,只摘要中段。
  - 中段语义摘要:对要卸掉的中段 turn 调一次摘要模型,用一条摘要替换机械截断。
  - 失败回退 + 统计:摘要失败有兜底文案,记录压缩率/调用数/错误数。

适配 my-agent(关键差异与约束):
  - my-agent compact 不是"截断一个 {from,value} turn 列表",而是把全量对话卸成 handoff
    state(refs/work_state/artifact),续跑再从 archive 记录重建 ``tool_context``。所以本模块
    的"中段"是**carried archive 记录的中段**,摘要后拼成一条 ``[compact-semantic-summary]``
    tool_context 条目,夹在 head/tail 机械条目之间。
  - 摘要是**增强**:它只改 ``tool_context``(喂续跑 prompt 文本轨 + 重试/digest 守卫);事实
    源(raw archive / runtime_fact / work_state / artifact registry)一律不动,compact 包可
    恢复性不受影响。native 下 ``tool_context`` 本就不发 provider(IR 才发),所以这里也不碰
    IR 历史。
  - **失败必回退机械路径**:配置关、记录太少、无可用 backend、摘要调用异常/超时/空结果——
    任意一种都返回 None,调用方按原 ``_reconstructed_tool_context_entry`` 逐条重建,行为与
    打补丁前完全一致。绝不因摘要失败而让 compact 续跑崩。
  - 默认开 + 失败静默回退:语义摘要对长任务续跑是净增益,且失败零代价回退,所以默认开;每一
    步都有兜底,任何异常只记 debug 日志,绝不外抛。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# 续跑历史重建的语义摘要默认参数(均可被 config 覆盖)。
_DEFAULT_PROTECT_HEAD = 2
_DEFAULT_PROTECT_TAIL = 6
_DEFAULT_MIN_MIDDLE = 4
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_INPUT_CHARS = 12_000
# 单条中段记录进摘要 prompt 时的正文上限(保留头尾的截断,避免一条巨型输出
# 撑爆摘要输入)。
_PER_RECORD_VALUE_CHARS = 1_200

_SUMMARY_PREFIX = "[compact-semantic-summary]"


@dataclass
class SemanticSummaryStats:
    """一次中段摘要的统计(压缩率/调用数/错误数),口径对齐 长期助手 TrajectoryMetrics。"""

    attempted: bool = False
    succeeded: bool = False
    head_records: int = 0
    tail_records: int = 0
    middle_records: int = 0
    summary_calls: int = 0
    summary_errors: int = 0
    input_chars: int = 0
    summary_chars: int = 0
    fallback_used: bool = False
    skip_reason: str = ""

    @property
    def compression_ratio(self) -> float:
        """摘要后字符数 / 摘要前中段字符数(越小压得越狠;<=0 输入按 1.0)。"""
        if self.input_chars <= 0:
            return 1.0
        return round(self.summary_chars / self.input_chars, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "head_records": self.head_records,
            "tail_records": self.tail_records,
            "middle_records": self.middle_records,
            "summary_calls": self.summary_calls,
            "summary_errors": self.summary_errors,
            "input_chars": self.input_chars,
            "summary_chars": self.summary_chars,
            "compression_ratio": self.compression_ratio,
            "fallback_used": self.fallback_used,
            "skip_reason": self.skip_reason,
        }


@dataclass(frozen=True)
class SemanticSummaryConfig:
    """从 agent.config 读出的语义摘要开关与参数(缺失/异常一律退默认值)。"""

    enabled: bool = True
    protect_head: int = _DEFAULT_PROTECT_HEAD
    protect_tail: int = _DEFAULT_PROTECT_TAIL
    min_middle: int = _DEFAULT_MIN_MIDDLE
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS


@dataclass(frozen=True)
class SemanticSummaryRequest:
    """中段摘要的输入:已机械重建好的逐条 tool_context + 原始 carried 记录 + 渲染器。

    ``mechanical_entries`` 与 ``records`` 等长且一一对应(调用方先全量机械重建,再决定是否把
    中段折叠成一条摘要)。``render_entry`` 仅在需要兜底重渲时使用。
    """

    records: list[dict[str, Any]]
    mechanical_entries: list[str]
    config: SemanticSummaryConfig
    backend: Any = None


def semantic_summary_config(agent: object) -> SemanticSummaryConfig:
    """从 agent.config 解析语义摘要配置;任何缺失/坏值退默认,绝不抛。"""
    config = getattr(agent, "config", None)
    return SemanticSummaryConfig(
        enabled=_bool_field(config, "memory_compact_semantic_summary_enabled", True),
        protect_head=_int_field(config, "memory_compact_semantic_summary_protect_head", _DEFAULT_PROTECT_HEAD),
        protect_tail=_int_field(config, "memory_compact_semantic_summary_protect_tail", _DEFAULT_PROTECT_TAIL),
        min_middle=_int_field(config, "memory_compact_semantic_summary_min_middle", _DEFAULT_MIN_MIDDLE),
        timeout_seconds=_float_field(
            config, "memory_compact_semantic_summary_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS
        ),
        max_input_chars=_int_field(
            config, "memory_compact_semantic_summary_max_input_chars", _DEFAULT_MAX_INPUT_CHARS
        ),
    )


def summarize_carried_tool_context(
    request: SemanticSummaryRequest,
) -> tuple[list[str], SemanticSummaryStats] | None:
    """把 carried 记录的中段折叠成一条语义摘要,保护首尾;失败/不适用返回 None(=回退机械路径)。

    返回 ``(entries, stats)``:``entries`` = head 机械条目 + 一条摘要条目 + tail 机械条目。
    返回 ``None``:调用方必须原样使用全量机械 ``mechanical_entries``(行为不变)。

    本函数**绝不抛异常**:任何分支出错都退化为 None。
    """
    stats = SemanticSummaryStats()
    try:
        return _summarize_or_none(request, stats)
    except Exception:  # noqa: BLE001 — 摘要是增强,任何意外都必须静默回退机械路径。
        _LOGGER.debug("compact semantic summary failed; falling back to mechanical rebuild", exc_info=True)
        return None


def _summarize_or_none(
    request: SemanticSummaryRequest, stats: SemanticSummaryStats
) -> tuple[list[str], SemanticSummaryStats] | None:
    config = request.config
    records = request.records
    entries = request.mechanical_entries
    if not config.enabled:
        stats.skip_reason = "disabled"
        return None
    if len(records) != len(entries):
        # 入参不自洽(理论不应发生):宁可回退,不冒错配 head/tail 的风险。
        stats.skip_reason = "entries_records_mismatch"
        return None
    head_n, tail_n, middle = _partition(records, config)
    if middle is None:
        stats.skip_reason = "middle_too_small"
        return None
    middle_start, middle_end = middle
    middle_records = records[middle_start:middle_end]
    stats.head_records = head_n
    stats.tail_records = tail_n
    stats.middle_records = len(middle_records)

    generate = _resolve_generate(request.backend)
    if generate is None:
        stats.skip_reason = "no_backend"
        return None

    content, stats.input_chars = _extract_middle_content(middle_records, config.max_input_chars)
    if not content.strip():
        stats.skip_reason = "empty_middle_content"
        return None

    stats.attempted = True
    summary_text = _generate_summary(generate, content, config.timeout_seconds, stats)
    if not summary_text.strip():
        # 摘要调用失败/超时/空结果 → 严格回退机械路径（不折叠中段、不留占位），保证"LLM 摘要
        # 失败 == 现有机械 compact 路径"，中段记录的逐条 preview 一条不丢。
        stats.fallback_used = True
        stats.skip_reason = stats.skip_reason or "summary_unavailable"
        return None
    summary_entry = _summary_entry(summary_text, stats)
    stats.summary_chars = len(summary_entry)
    return [*entries[:head_n], summary_entry, *entries[middle_end:]], stats


def _partition(
    records: list[dict[str, Any]], config: SemanticSummaryConfig
) -> tuple[int, int, tuple[int, int] | None]:
    """决定保护多少 head/tail,以及中段 [start,end);中段不够 min_middle 时返回 None。"""
    total = len(records)
    head_n = max(0, config.protect_head)
    tail_n = max(0, config.protect_tail)
    min_middle = max(1, config.min_middle)
    # 至少要让 head + min_middle + tail 条都放得下,否则没必要(也没空间)折叠中段。
    if total < head_n + tail_n + min_middle:
        return head_n, tail_n, None
    middle_start = head_n
    middle_end = total - tail_n
    if middle_end - middle_start < min_middle:
        return head_n, tail_n, None
    return head_n, tail_n, (middle_start, middle_end)


def _extract_middle_content(records: list[dict[str, Any]], max_input_chars: int) -> tuple[str, int]:
    """把中段记录渲成给摘要模型的文本(单条正文截断,总量再设上限)。保留头尾的截断。"""
    parts: list[str] = []
    for index, record in enumerate(records):
        parts.append(_record_brief(index, record))
    content = "\n\n".join(parts)
    limit = max_input_chars if max_input_chars > 0 else _DEFAULT_MAX_INPUT_CHARS
    if len(content) > limit:
        # 总量超限:保头保尾(长期助手 同款),中间塞省略标记,避免丢最早/最近的中段线索。
        # 显式扣掉分隔符长度,保证最终结果 <= limit(否则统计/预算会被略微突破)。
        marker = "\n...[middle content truncated for summary]...\n"
        budget = max(0, limit - len(marker))
        keep_head = int(budget * 0.6)
        keep_tail = budget - keep_head
        content = content[:keep_head] + marker + (content[-keep_tail:] if keep_tail > 0 else "")
    return content, len(content)


def _record_brief(index: int, record: dict[str, Any]) -> str:
    tool = str(record.get("tool") or "unknown").strip() or "unknown"
    status = "ok" if record.get("ok") else "error"
    lines = [f"[middle-record {index} tool={tool} status={status}]"]
    lines.extend(_record_param_lines(record.get("parameters")))
    preview = str(record.get("output_preview") or "").strip()
    if preview:
        lines.append(f"- output_preview: {_clip(preview, _PER_RECORD_VALUE_CHARS)}")
    for key in ("scoped_call_id", "artifact_ref", "output_path", "error_code"):
        value = str(record.get(key) or "").strip()
        if value:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _record_param_lines(params: Any) -> list[str]:
    if not isinstance(params, dict):
        return []
    return [
        f"- {key}: {_clip(text, 240)}"
        for key, value in params.items()
        if key not in {"tool", "call_id"} and (text := str(value).strip())
    ]


def _generate_summary(
    generate: Callable[[str], str], content: str, timeout_seconds: float, stats: SemanticSummaryStats
) -> str:
    """调一次摘要模型;失败/超时/空结果返回 ""(调用方用兜底文案)。统计 calls/errors。"""
    prompt = _summary_prompt(content)
    stats.summary_calls += 1
    text = _generate_with_timeout(generate, prompt, timeout_seconds)
    summary = (text or "").strip()
    if not summary:
        stats.summary_errors += 1
        return ""
    stats.succeeded = True
    return summary


def _generate_with_timeout(generate: Callable[[str], str], prompt: str, timeout_seconds: float) -> str:
    """在工作线程里跑 ``generate(prompt)``,超时则放弃(不杀线程,只不等它)。

    backend.generate 是同步阻塞调用;用线程 + join(timeout) 给摘要设硬超时,避免一次卡死的
    摘要请求把整个 compact 续跑拖死。超时后线程可能仍在后台跑,但其结果被丢弃,不影响主链路。
    """
    try:
        budget = float(timeout_seconds)
    except (TypeError, ValueError):
        budget = _DEFAULT_TIMEOUT_SECONDS
    if budget <= 0:
        return _safe_generate(generate, prompt)

    box: dict[str, str] = {}

    def _worker() -> None:
        box["text"] = _safe_generate(generate, prompt)

    thread = threading.Thread(target=_worker, name="compact-semantic-summary", daemon=True)
    thread.start()
    thread.join(budget)
    if thread.is_alive():
        _LOGGER.debug("compact semantic summary timed out after %ss", budget)
        return ""
    return box.get("text", "")


def _safe_generate(generate: Callable[[str], str], prompt: str) -> str:
    try:
        return generate(prompt)
    except Exception:  # noqa: BLE001 — backend 调用异常一律吞成空,回退兜底文案。
        _LOGGER.debug("compact semantic summary backend.generate raised", exc_info=True)
        return ""


def _resolve_generate(backend: Any) -> Callable[[str], str] | None:
    """把任意 backend 包成 ``(prompt)->str``;无 generate 能力返回 None(=回退机械)。

    刻意只用文本协议形态(``generate(prompt)``)——摘要是独立的轻量调用,不带 tools/messages,
    与 run_learning_review 同款,echo/伪后端零改动也能跑。
    """
    if backend is None:
        return None
    generate = getattr(backend, "generate", None)
    if not callable(generate):
        return None

    def _call(prompt: str) -> str:
        response = generate(prompt)
        return str(getattr(response, "text", response) or "")

    return _call


def _summary_entry(summary_text: str, stats: SemanticSummaryStats) -> str:
    # 这里只在摘要非空时被调用（空结果已在 _summarize_or_none 提前回退机械路径）。
    body = summary_text.strip()
    header = (
        f"{_SUMMARY_PREFIX} 折叠了中段 {stats.middle_records} 条工具记录(保护首 "
        f"{stats.head_records} / 尾 {stats.tail_records} 条原样)。这是语义摘要,非事实源;"
        "精确字段/清单/引用以 archive 记录与 artifact_ref 为准。"
    )
    return f"{header}\n{body}"


def _summary_prompt(content: str) -> str:
    return (
        "你在帮一个长任务智能体压缩它自己的中段工作历史。下面是若干条已执行的工具调用记录"
        "(中段),它们即将从上下文里卸掉,只留这一份摘要供模型续接当前任务。\n\n"
        "请写一段中性、紧凑、可执行的摘要,覆盖:\n"
        "1. 这段过程做了哪些动作(读了/写了/搜了什么,派了哪些工);\n"
        "2. 得到的关键结果、结论、决策;\n"
        "3. 相关的文件名、路径、引用、数值等可定位线索;\n"
        "4. 还遗留/未完成的部分。\n\n"
        "只输出摘要正文,不要复述原始记录,不要加客套。精确长清单不必逐条抄(模型可按引用回取),"
        "但要保留'去哪儿取'的线索。\n\n"
        "---\n中段记录:\n"
        f"{content}\n---\n"
    )


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[clipped]"


def _bool_field(config: object, name: str, default: bool) -> bool:
    value = getattr(config, name, None)
    if value is None:
        return default
    return bool(value)


def _int_field(config: object, name: str, default: int) -> int:
    value = getattr(config, name, None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_field(config: object, name: str, default: float) -> float:
    value = getattr(config, name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "SemanticSummaryConfig",
    "SemanticSummaryRequest",
    "SemanticSummaryStats",
    "semantic_summary_config",
    "summarize_carried_tool_context",
]
