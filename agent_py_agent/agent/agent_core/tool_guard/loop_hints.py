# LLM: 仅追加软提示，不执行或拦截工具。渠道可用性读取 canonical 错误分类与 call_id；
# 测试/编译非零、参数、状态、权限、取消和未知失败不得冒充系统不可用，更不能提示绕过安全门。
# 修改须联测 _tool_loop_service 接线、error_taxonomy 及 test_tool_failure_channel_hint。
# 模块用途: 回显工具动作门原因，在确有渠道不可用证据时提醒核对其它授权来源，避免带偏正常排错。
from __future__ import annotations

from ...contracts.error_taxonomy import error_contract


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


# LLM: 只统计去重后的明确渠道不可用回执；沿用现有阈值、每工具一次及 0 关闭合同。
# 副作用仅为追加 tool_context，不自动重试、转发、改权限或判任务完成；正文不参与触发。
# 函数用途: 来源连不上或工具确实不可用时给一次有限提示，不把正常测试失败引向反复搜索。
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
            f"工具 {tool} 本轮有 {failures} 次明确的渠道不可用回执；不等于目标数据不存在。"
            "先结合原错误码、输出和恢复建议判断，在现有权限内考虑其它可用来源，"
            "不要因此直接下绝对结论，也不要为了枚举渠道重复无效调用。"
            "权限、审批或安全拒绝不得通过换工具绕过；没有可用来源时如实说明缺口。"
        )


# 函数用途: 读软引导阈值配置(坏值/缺省按默认 2,0=关闭)。
def _channel_hint_threshold(agent: object) -> int:
    try:
        return max(0, int(getattr(getattr(agent, "config", None), "tool_failure_channel_hint_threshold", 2) or 0))
    except (TypeError, ValueError):
        return 2


# LLM: 只认宿主 error_code 对应的现有错误合同；不读 output、提示文字或工具名推断错误性质。
# 函数用途: 区分真正的网络/能力不可用和业务命令失败，避免一次测试失败变成换渠道指令。
def _is_channel_availability_failure(record: dict[str, object]) -> bool:
    contract = error_contract(str(record.get("error_code") or ""))
    return contract.code == "TOOL_UNAVAILABLE" or (
        contract.category == "network" and contract.retryable
    )


# LLM: 同一 tool/call_id 只消费最新宿主回执，缺身份不计数；其它错误仍完整保留在原账本。
# 函数用途: 统计本轮各工具的真实渠道不可用次数，避免重复归档或旧失败被计成多次故障。
def _failure_counts_by_tool(params: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for record in reversed(getattr(params, "archive_tool_calls", None) or []):
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool") or "").strip()
        call_id = str(record.get("call_id") or "").strip()
        if not tool or not call_id or (tool, call_id) in seen:
            continue
        seen.add((tool, call_id))
        if record.get("ok") is False and _is_channel_availability_failure(record):
            counts[tool] = counts.get(tool, 0) + 1
    return counts


__all__ = [
    "append_tool_failure_channel_hint",
    "append_tool_guardrail_action_block_hint",
]
