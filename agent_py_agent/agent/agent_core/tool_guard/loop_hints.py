# LLM: 工具循环软提示集合(全部非门:只 append tool_context,不拦任何调用)。
#   两类提示:①guardrail 拦截回显(registry 已拦,这里只是把原因摆给模型);
#   ②检索完备性软引导(REFACTORING_BACKLOG"检索完备性软引导",实锤 R5b/R6c:
#   单一渠道失败即下"数据不存在"绝对结论)——同一工具系统失败(archive ok=false,
#   A1 同源)累计达阈值时,引导模型枚举已试/未试渠道再下结论。触发全部结构化,
#   绝不解析模型文本;每工具幂等提示一次。改动时同步检查 _tool_loop_service.
#   _run_tool_round 接线与 tests/test_tool_failure_channel_hint.py。
# 模块用途: 模型在工具循环里走弯路时给的"轻拍肩膀":重复无效调用被拦会告知原因,
#   同一工具连续失败会提醒"换渠道枚举验证,别急着说不存在"。
from __future__ import annotations


def append_tool_guardrail_action_block_hint(request: object) -> None:
    """Append a model-facing hint when the registry blocks the repeated tool action."""
    current_round_prefix = f"{getattr(request, 'tool_rounds', '')}-"
    params = getattr(request, "params", None)
    archive_records = getattr(params, "archive_tool_calls", []) or []
    for record in reversed(list(archive_records)):
        if not isinstance(record, dict):
            continue
        if not str(record.get("call_id") or "").startswith(current_round_prefix):
            continue
        gate = record.get("runtime_gate")
        if not _is_tool_guardrail_action_block(gate):
            continue
        model_message = str(gate.get("model_message") or "").strip()
        reason = _first_gate_finding_code(gate) or str(gate.get("operator_message") or "TOOL_GUARDRAIL_ACTION_BLOCKED")
        tool_context = getattr(params, "tool_context", None)
        if isinstance(tool_context, list):
            tool_context.append(
                "[tool-loop-guardrail-hint]\n"
                f"{reason}：同一工具路径持续无效，这一次相同工具调用未执行。"
                f"{model_message or '请换关键词、换参数、换工具或换数据来源，再继续推进任务。'}"
            )
        return


def _is_tool_guardrail_action_block(gate: object) -> bool:
    if not isinstance(gate, dict):
        return False
    if gate.get("gate") != "tool_guardrail" or gate.get("allow_action") is not False:
        return False
    return any(
        code in {"TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED", "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"}
        for code in _gate_finding_codes(gate)
    )


def _gate_finding_codes(gate: dict[str, object]) -> tuple[str, ...]:
    findings = gate.get("findings")
    if not isinstance(findings, list):
        return ()
    codes: list[str] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            codes.append(code)
    return tuple(codes)


def _first_gate_finding_code(gate: dict[str, object]) -> str:
    for code in _gate_finding_codes(gate):
        return code
    return ""


_CHANNEL_HINT_MARKER = "[tool-failure-channel-hint]"


# LLM: 检索完备性软引导唯一入口(R5b 形态:web_search 系统失败 2 次后模型断言
#   "数据根本不存在"并放弃)。触发:同一工具的 archive ok=false 累计 ≥
#   config.tool_failure_channel_hint_threshold(默认 2,0=关闭)。动作:append
#   一条带枚举引导的 tool_context 软提示(指向不可行报告的 tried_channels/
#   untried_channels_known 字段,与 P5-1 schema 同语义)。每工具只提示一次
#   (tool_context 含同 tool 标记即跳过);不拦调用、不改任何状态。
# 函数用途: 同一个工具连着失败几次后,提醒模型"换渠道枚举验证,别急着下绝对结论"。
def append_tool_failure_channel_hint(request: object) -> None:
    threshold = _channel_hint_threshold(getattr(request, "agent", None))
    if threshold <= 0:
        return
    params = getattr(request, "params", None)
    tool_context = getattr(params, "tool_context", None)
    if not isinstance(tool_context, list):
        return
    for tool, failures in _failure_counts_by_tool(params).items():
        if failures < threshold:
            continue
        marker = f"{_CHANNEL_HINT_MARKER} tool={tool}"
        if any(marker in str(item) for item in tool_context):
            continue
        tool_context.append(
            f"{marker} failures={failures}\n"
            f"工具 {tool} 本轮已系统失败 {failures} 次。在因此得出“数据不存在/不可行/"
            "找不到”这类绝对结论之前，先枚举：①已试过哪些渠道、方法、查询口径（各自"
            "的失败证据）；②还有哪些已知但未试的渠道（其他工具、其他数据源、其他查询"
            "字段或站点）。换一条未试渠道再验证一次；若最终确认不可行，把上述枚举写进"
            "结构化不可行报告（tried_channels / untried_channels_known），再如实说明结论。"
        )


# 函数用途: 读软引导阈值配置(坏值/缺省按默认 2,0=关闭)。
def _channel_hint_threshold(agent: object) -> int:
    try:
        return max(0, int(getattr(getattr(agent, "config", None), "tool_failure_channel_hint_threshold", 2) or 0))
    except (TypeError, ValueError):
        return 2


# 函数用途: 按工具名统计本轮 archive 里的系统失败次数(只认 ok=false 系统事实)。
def _failure_counts_by_tool(params: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in getattr(params, "archive_tool_calls", None) or []:
        if not isinstance(record, dict) or record.get("ok", True):
            continue
        tool = str(record.get("tool") or "").strip()
        if tool and tool != "__parse_error__":
            counts[tool] = counts.get(tool, 0) + 1
    return counts


__all__ = [
    "append_tool_failure_channel_hint",
    "append_tool_guardrail_action_block_hint",
]
