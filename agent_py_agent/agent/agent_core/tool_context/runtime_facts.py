# LLM: 只投影canonical handler_details中的核验及进程事实，不解析工具正文或改变执行结果；调用方须继续统一脱敏。
# 模块用途: 把工具账里必要的执行事实带给模型，区分命令退出、进程清理和业务完成，不复制原始诊断或资源明细。
from __future__ import annotations

import json
import math
from collections.abc import Mapping

_PROCESS_FIELDS = {
    "status": (str,), "return_code": (int, type(None)), "exit_code": (int, type(None)),
    "command_succeeded": (bool,), "timeout_seconds": (int, float), "pipes_drained": (bool,),
    "cleanup_confirmed": (bool,), "session_id": (str,), "reason": (str,), "error_type": (str,),
}
_TERMINATION_FIELDS = {
    "method": (str,), "confirmed": (bool,), "return_code": (int, type(None)),
    "observed_processes": (int,), "unresolved_count": (int,), "instances_count": (int,),
}


# LLM: 保留已有verification块的字节顺序；process只取显式字段，不能用工具成功或空PID列表推断清理成功。
# 函数用途: 生成模型可见的结构化事实区；仅返回文字，不访问Agent、文件、进程或任何持久账。
def render_tool_runtime_facts(details: Mapping[str, object]) -> str:
    sections = []
    verification = {
        key: details[key] for key in ("verification_evidence", "verification_state") if key in details
    }
    if verification:
        sections.append(
            "[runtime-verification-facts]\n"
            "These facts come from executed commands and structured file-write events; "
            "use them when reporting test scope/status, and do not quote this internal label to the user.\n"
            + json.dumps(verification, ensure_ascii=False, sort_keys=True)
        )
    process = project_process_runtime_facts(details.get("process"))
    if process:
        sections.append(
            "[runtime-process-facts]\n"
            "这些字段来自本次工具执行回执；命令退出、资源清理和业务完成各自独立，缺失或未确认不代表成功。\n"
            + json.dumps({"process": process}, ensure_ascii=False, sort_keys=True)
        )
    return "\n".join(sections)


# LLM: 只复制既有显式标量，不转换字符串布尔值或互补退出码别名；超长诊断留原账，不把正文混进事实区。
# 函数用途: 选择当前合同中可直接展示的短字段，保留False、0和None，忽略类型不符及未声明内容。
def _scalar_facts(source: Mapping[str, object], fields: Mapping[str, tuple[type, ...]]) -> dict[str, object]:
    facts = {}
    for key, kinds in fields.items():
        if key not in source:
            continue
        value = source[key]
        if any(type(value) is kind for kind in kinds) and _bounded_scalar(value):
            facts[key] = value
    return facts


# LLM: 仅对已通过精确内置类型检查的值限量，避免巨整数编码异常或非有限JSON；略去字段不改变原账和状态。
# 函数用途: 限制展示标量的体积及JSON可表示性，不把畸形值转成成功、零或其它默认事实。
def _bounded_scalar(value: object) -> bool:
    if isinstance(value, str):
        return len(value) <= 256
    if type(value) is int:
        return value.bit_length() <= 64
    if type(value) is float:
        return math.isfinite(value)
    return True


# LLM: 聚合回执只展示原序列数量，绝不复制PID或实例身份；字段缺失不制造零数量或清理确认。
# 函数用途: 对原回执列表做有界数量投影，保留查原账的必要信息而不展开大量进程记录。
def _receipt_counts(source: Mapping[str, object]) -> dict[str, int]:
    return {
        target: len(source[key])
        for key, target in (("unresolved_pids", "unresolved_count"), ("instances", "instances_count"))
        if isinstance(source.get(key), (list, tuple))
    }


# LLM: child termination和session cleanup属于不同观察，必须分别保留；只读handler envelope，不解析output/structuredContent。
# 函数用途: 选择进程生命周期、单进程终止和完整会话清理事实，供当前模型输出和归档恢复共用；已投影的数量字段可再次读取。
def project_process_runtime_facts(source: object) -> dict[str, object]:
    if not isinstance(source, Mapping):
        return {}
    facts = _scalar_facts(source, _PROCESS_FIELDS)
    termination = source.get("termination")
    if not isinstance(termination, Mapping):
        return facts
    receipt = _scalar_facts(termination, _TERMINATION_FIELDS)
    receipt.update(_receipt_counts(termination))
    cleanup = termination.get("cleanup")
    if isinstance(cleanup, Mapping):
        cleanup_facts = _scalar_facts(cleanup, {"confirmed": (bool,), "instances_count": (int,), "unresolved_count": (int,)})
        cleanup_facts.update(_receipt_counts(cleanup))
        if cleanup_facts:
            receipt["cleanup"] = cleanup_facts
    if receipt:
        facts["termination"] = receipt
    return facts
