# LLM: 子代理工具失败的系统级账本(开发计划 A1,根治 R4b 模型归因幻觉)。数据源是
#   工具循环的 archive_tool_calls(registry_invoke 产出的 ToolExecutionResult:
#   ok/error_code 是系统事实,不是模型转述)。契约:账本只做观测与对账事实源,
#   绝不做硬门、不拦任何主链路;tool_failures 为 None(超时/worker 异常,拿不到
#   archive)时不覆盖已有账本,为 [](正常跑完零失败)时写空账本——"系统记录
#   零失败"本身是强事实,用于拆穿模型口头的失败归因。消费方:
#   services/runner_result_service(写 task.attributes)、finalize_helpers(从
#   AgentRunResult.archive_tool_calls 提取)、delivery_closeout/subagent_aggregation
#   (unresolved_children.tool_failure_codes 对照投影)。改动时同步检查
#   tests/test_subagent_tool_failure_ledger.py 与 docs/audits/R4b-goattack-20260611.md。
# 模块用途: 把"子代理本轮哪些工具调用真的失败、系统错误码是什么"记成结构化账本,
#   让主代理和对账层读系统事实而不是模型口头转述——模型声称 WRITE_FORBIDDEN 但
#   账本为空时,幻觉在 closeout 报告里立刻可见。
from __future__ import annotations

from typing import Any

TOOL_FAILURE_LEDGER_ATTR = "tool_failure_ledger"
# 单次 attempt 最多记录的失败条数;超出截断,防止失控循环把 attributes 撑爆。
_LEDGER_MAX_ENTRIES = 50
# 从工具参数里提取"目标路径"时按序尝试的参数名(write_file 用 path)。
_PATH_PARAM_KEYS = ("path", "file", "target", "filename")


# LLM: 系统事实的唯一提取口:只认 archive 记录的 ok=False(registry 层判定),
#   不读模型文本。返回轻量摘要列表,字段固定 tool/call_id/error_code/target/message
#   ——message 是系统拒绝/错误原文截断(R8 接力取证实锤:此前只有 error_code,
#   WRITE_FORBIDDEN 的具体拒因〔边界/锁/危险目录〕无处可查,事后只能终态重放
#   考古且瞬态状态不可复现;拒绝原文是决策时刻的系统事实,必须随账本留痕)。
# 函数用途: 从一轮 agent.run 的工具归档里挑出真失败的调用,做成账本条目。
def tool_failures_from_archive(archive_tool_calls: list | None) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for record in archive_tool_calls or []:
        if not isinstance(record, dict) or record.get("ok", True):
            continue
        failures.append(
            {
                "tool": str(record.get("tool") or ""),
                "call_id": str(record.get("call_id") or ""),
                "error_code": str(record.get("error_code") or ""),
                "target": _target_from_parameters(record.get("parameters")),
                "message": _failure_message(record),
            }
        )
        if len(failures) >= _LEDGER_MAX_ENTRIES:
            break
    return failures


# 拒绝原文留痕长度上限:够写清"哪类拒+哪个边界",不撑爆 attributes。
_FAILURE_MESSAGE_MAX_CHARS = 240


# 函数用途: 从失败记录里取系统返回的拒绝/错误原文(优先 output,截断留痕)。
def _failure_message(record: dict) -> str:
    for key in ("output", "error", "message"):
        text = str(record.get(key) or "").strip()
        if text:
            return text[:_FAILURE_MESSAGE_MAX_CHARS]
    return ""


# 函数用途: 从工具调用参数里取出目标路径(取不到返回空串,不猜)。
def _target_from_parameters(parameters: object) -> str:
    if not isinstance(parameters, dict):
        return ""
    for key in _PATH_PARAM_KEYS:
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# LLM: 写账本的唯一入口。None=本轮拿不到系统数据(超时/异常路径),保留旧账本
#   不伪造"零失败";[]=系统确认零失败,写空账本。每个 attempt 整体覆盖(账本表达
#   "最近一次真实执行的系统事实",历史失败由 work_log/archive 审计)。副作用:改
#   task.attributes,落盘由调用方的 save 完成。
# 函数用途: 把本次 attempt 的工具失败清单写进任务属性,供对账和报告读取。
def record_tool_failure_ledger(task: Any, tool_failures: list | None, now: float) -> None:
    if tool_failures is None:
        return
    task.attributes[TOOL_FAILURE_LEDGER_ATTR] = {
        "updated_at": now,
        "failures": [dict(item) for item in tool_failures[:_LEDGER_MAX_ENTRIES]],
    }


# LLM: 对账投影:error_code → 次数;空 error_code 归 UNSPECIFIED。读 attributes
#   原始 dict(canonical_state.json 回读形态),容错任意脏数据。
# 函数用途: 把账本聚合成"错误码:次数",给 closeout 的 unresolved_children 做
#   "模型转述 vs 系统事实"对照展示。
def tool_failure_code_counts(attrs: object) -> dict[str, int]:
    attrs = attrs if isinstance(attrs, dict) else {}
    ledger = attrs.get(TOOL_FAILURE_LEDGER_ATTR)
    failures = ledger.get("failures") if isinstance(ledger, dict) else None
    counts: dict[str, int] = {}
    for item in failures if isinstance(failures, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("error_code") or "").strip() or "UNSPECIFIED"
        counts[code] = counts.get(code, 0) + 1
    return counts


__all__ = [
    "TOOL_FAILURE_LEDGER_ATTR",
    "record_tool_failure_ledger",
    "tool_failure_code_counts",
    "tool_failures_from_archive",
]
