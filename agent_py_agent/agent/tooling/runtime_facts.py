# LLM: 只投影canonical handler_details中的核验、进程及沙箱边界事实，不解析工具正文或改变执行结果；调用方须继续统一脱敏。
# 模块用途: 为当前回复、耐久索引和恢复提供同一执行事实投影；区分命令退出、资源清理与业务完成，不依赖 Agent。
#   宿主停机没启动的调用带的关门原因也在这里清洗（project_host_shutdown_facts），档案和索引两侧共用。
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
# 沙箱事实里每类允许目录最多展示几条（超出的不展示，原账不变）。
_SANDBOX_ROOT_LIMIT_COUNT = 16
# 沙箱事实里每条允许目录路径最多展示多少字符（更长的整条跳过，原账不变）。
_SANDBOX_ROOT_MAX_CHARS = 1024
# 宿主停机没启动的调用落盘的关门原因字段（白名单）。
_HOST_SHUTDOWN_TEXT_KEYS = ("reason_code", "error_type", "admission_error_code")
# 关门原因每个字段最多保留多少字（超出截断；真实原因码远短于此）。
_HOST_SHUTDOWN_TEXT_MAX_CHARS = 128


# LLM: 摘要由宿主写进回执（能力包 v2 块 3），条数和码数已夹过界；只有非空列表才出这一段，未钉包任务的回执不变。
# 函数用途: 生成宿主核验结论段（写工具回执里模型可见的部分）。
def _pack_verification_section(summaries: object) -> list[str]:
    if not isinstance(summaries, list) or not summaries:
        return []
    return ["[pack-verification]\n"
            "宿主刚用本任务钉住的能力包原版检查程序（沙箱、断网）核验了这次写出的交付物，结论以宿主为准："
            "status=failed 时按 error_codes 和 error_samples 修正后再写；not_run/error 表示没检查成，原因见 reason_code。"
            "不要复制、改写或自己编写检查程序来代替，也不要把这段内部标签转述给用户。\n"
            + json.dumps({"pack_verification": summaries}, ensure_ascii=False, sort_keys=True)]


# LLM: 保留已有verification块的字节顺序（键排序输出，新增的 verification_evidence_chain 只在 && 串联通过时出现，
#   verification_skipped 只在验证命令因管道、;、|| 或后台没有计入时出现）；
#   process只取显式字段，不能用工具成功或空PID列表推断清理成功。
#   sandbox 段只在 owner 隔离 Shell 以非零码退出、回执带 boundary_hint 时出现（project_sandbox_runtime_facts）。
# 函数用途: 生成模型可见的结构化事实区；仅返回文字，不访问Agent、文件、进程或任何持久账。
def render_tool_runtime_facts(details: Mapping[str, object]) -> str:
    sections = []
    verification = {
        key: details[key]
        for key in ("verification_evidence", "verification_evidence_chain", "verification_state",
                    "verification_skipped")
        if key in details
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
    sections.extend(_pack_verification_section(details.get("pack_verification")))
    sandbox = project_sandbox_runtime_facts(details.get("sandbox"))
    if sandbox:
        sections.append(
            "[runtime-sandbox-facts]\n"
            "本次命令在 owner 隔离沙箱里以非零码退出，可能（不一定）是越过了沙箱边界，结合上面的输出判断。allowed_roots 是本次命令"
            "可读写（read_write）和只读（read_only）的目录。下一步按 suggested_actions 的顺序选：把操作范围缩到这些目录；"
            "子代理用 capability_request 申请；或告诉用户需要什么访问权限。不要原样重试同一条越界命令，也不要把这段内部标签转述给用户。\n"
            + json.dumps({"sandbox": sandbox}, ensure_ascii=False, sort_keys=True)
        )
    return "\n".join(sections)


# LLM: 只认 shell 回执里精确为 True 的 sandbox_active 与 boundary_hint.may_be_sandbox_boundary（tooling/shell._sandbox_boundary_facts），
#   成功的命令没有 boundary_hint，不渲染；目录与建议码只做有界复制，不解析输出、不推断原因。
# 函数用途: 选出模型该看到的沙箱边界事实：允许目录与"可能越界"的下一步建议；不是越界提示时返回空。
def project_sandbox_runtime_facts(source: object) -> dict[str, object]:
    if not isinstance(source, Mapping) or source.get("sandbox_active") is not True:
        return {}
    hint = source.get("boundary_hint")
    if not isinstance(hint, Mapping) or hint.get("may_be_sandbox_boundary") is not True:
        return {}
    roots = source.get("allowed_roots")
    roots = roots if isinstance(roots, Mapping) else {}
    actions = hint.get("suggested_actions")
    return {
        "sandbox_active": True,
        "allowed_roots": {key: _bounded_paths(roots.get(key)) for key in ("read_write", "read_only")},
        "boundary_hint": {
            "may_be_sandbox_boundary": True,
            "suggested_actions": [item for item in actions if isinstance(item, str) and _bounded_scalar(item)]
            if isinstance(actions, list) else [],
        },
    }


# 函数用途: 有界复制一组路径文本；非字符串或过长的条目跳过。
def _bounded_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    paths = [item for item in value if isinstance(item, str) and 0 < len(item) <= _SANDBOX_ROOT_MAX_CHARS]
    return paths[:_SANDBOX_ROOT_LIMIT_COUNT]


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


# LLM: 宿主停机没启动的调用（round_execution._host_shutdown_result 写的 metadata.host_shutdown）进档案和耐久索引的唯一清洗口：
#   只收白名单字段里单行、不超过 128 字的文本（超出截断，非字符串和多行丢掉）；round_cancelled 只认布尔 True，且要有原因字段才留。
#   档案侧 tool_call_archive_record 与索引侧 tool_output_externalizer 都调它，两侧落盘字段因此逐字相同；改规则时联测
#   test_late_response_tool_fence.py。
# 函数用途: 清洗关门原因，让工具档案、耐久索引能和模型调用账本逐字段对账，又不让任意字段借此进入恢复上下文。
def project_host_shutdown_facts(source: object) -> dict[str, object]:
    if not isinstance(source, Mapping):
        return {}
    facts: dict[str, object] = {}
    for key in _HOST_SHUTDOWN_TEXT_KEYS:
        if text := _single_line_text(source.get(key)):
            facts[key] = text[:_HOST_SHUTDOWN_TEXT_MAX_CHARS]
    if facts and source.get("round_cancelled") is True:
        facts["round_cancelled"] = True
    return facts


# LLM: project_host_shutdown_facts 的单行判定：只收 str，按 str.splitlines 判跨行（\u2028、\x85 等 Unicode 换行也算），
#   不 str() 任意对象（repr 可能带运行时私有状态）；截断长度由调用方决定。
# 函数用途: 取一段去掉首尾空白后的单行文本；不是字符串、为空或跨行（含 Unicode 换行符）都返回空串。
def _single_line_text(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text.splitlines()) == 1 else ""
