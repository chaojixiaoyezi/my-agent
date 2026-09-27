# LLM: 子代理工具失败的系统级账本(开发计划 A1,根治 R4b 模型归因幻觉)。数据源是
#   工具循环的 archive_tool_calls(registry_invoke 产出的 ToolHandlerOutcome:
#   ok/error_code 是系统事实,不是模型转述)。契约:账本只做观测与对账事实源,
#   本身不拦任何主链路;tool_failures 为 None(超时/worker 异常,拿不到
#   archive)时不覆盖已有账本,为 [](正常跑完零失败)时写空账本——"系统记录
#   零失败"本身是强事实,用于拆穿模型口头的失败归因。消费方:
#   services/runner_result_service(写 task.attributes)、finalize_helpers(从
#   AgentRunResult.archive_tool_calls 提取)与子代理聚合状态
#   (unresolved_children.tool_failure_codes 对照投影)。改动时同步检查
#   tests/test_subagent_tool_failure_ledger.py 与 docs/audits/R4b-goattack-20260611.md。
#   连续失败段(tool_failure_streak)是同一批结构化事实的只读投影:父级视图从 owner 权威
#   runtime_events 的 tool_completed 事件现算,工具循环与收口从本 run 的 archive 记录现算,
#   两处共用同一函数,不另存第二份计数。授权阶段收口由工具循环读这份投影后自行裁决。
# 模块用途: 把"子代理本轮哪些工具调用真的失败、系统错误码是什么"记成结构化账本,
#   让主代理和对账层读系统事实而不是模型口头转述——模型声称 WRITE_FORBIDDEN 但
#   账本为空时,幻觉会在结构化运行事实里立刻可见;同时给父代理算出"最近一段同码连续失败"。
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.subagent_completion import (
    TOOL_FAILURE_HALT_SCHEMA_VERSION,
    completion_tool_failure_halt_facts,
)

TOOL_FAILURE_LEDGER_ATTR = "tool_failure_ledger"
# 工具循环因授权阶段同码连续失败收口时写入 runtime_reason 的结构化原因码。
REPEATED_TOOL_AUTHORIZATION_FAILURE = "REPEATED_TOOL_AUTHORIZATION_FAILURE"
# 单次 attempt 最多记录的失败条数;超出截断,防止失控循环把 attributes 撑爆。
_LEDGER_MAX_ENTRIES = 50
# 从工具参数里提取"目标路径"时按序尝试的参数名(write_file 用 path)。
_PATH_PARAM_KEYS = ("path", "file", "target", "filename")
_AUTHORIZATION_STAGE = "authorization"
# 宿主重复门自身的未执行拒绝不是模型行为,与 consecutive_same_failure_count 一致:不计入也不打断。
_GUARDRAIL_SELF_BLOCK_PREFIX = "TOOL_GUARDRAIL_"
# 父级视图最多回看的 tool_completed 事件条数;只影响本次投影,不删除任何事件。
_STREAK_EVENT_WINDOW = 200
# 连续失败段里最多列出的不同工具名/参数名。
_STREAK_NAME_LIMIT = 8


# LLM: 只装宿主 typed 字段(ok/error_code/failure_stage/handler_executed/参数名);参数值与输出正文不进入。
# 类用途: 一次工具调用在"最近失败段"统计里需要的最小事实,archive 记录与权威事件共用。
@dataclass(frozen=True)
class ToolCallFact:
    tool: str
    ok: bool
    error_code: str = ""
    failure_stage: str = ""
    handler_executed: bool = False
    at: float = 0.0
    argument_names: tuple[str, ...] = ()


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
#   "最近一次真实执行的系统事实",历史失败由 work_log/archive 审计)。halt 只在本 attempt
#   因授权阶段同码连续失败收口时随账本写入,下一 attempt 覆盖账本时自然消失。副作用:改
#   task.attributes,落盘由调用方的 save 完成。
# 函数用途: 把本次 attempt 的工具失败清单(以及可选的收口事实)写进任务属性,供对账和父级读取。
def record_tool_failure_ledger(
    task: Any,
    tool_failures: list | None,
    now: float,
    halt: dict[str, object] | None = None,
) -> None:
    if tool_failures is None:
        return
    ledger: dict[str, object] = {
        "updated_at": now,
        "failures": [dict(item) for item in tool_failures[:_LEDGER_MAX_ENTRIES]],
    }
    if halt:
        ledger["halt"] = dict(halt)
    task.attributes[TOOL_FAILURE_LEDGER_ATTR] = ledger


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


# LLM: 只读账本里经合同投影校验过的 halt;版本不符或字段损坏按"没有收口事实"处理,不猜不补。
# 函数用途: 取出最近一次 attempt 因授权阶段连续失败而收口的结构化事实,供完成信封交给父级。
def ledger_tool_failure_halt(attrs: object) -> dict[str, object]:
    ledger = attrs.get(TOOL_FAILURE_LEDGER_ATTR) if isinstance(attrs, dict) else None
    halt = ledger.get("halt") if isinstance(ledger, dict) else None
    projected = completion_tool_failure_halt_facts({"tool_failure_halt": halt})
    return dict(projected.get("tool_failure_halt") or {})


# LLM: archive 记录的 tool/ok/error_code/failure_stage/handler_executed 都是宿主 typed 字段;
#   参数只取键名,绝不复制参数值。缺 ok 字段按成功处理,与 tool_failures_from_archive 的口径一致。
# 函数用途: 把本 run 的工具归档记录转成失败段统计用的事实列表。
def archive_tool_call_facts(records: object) -> list[ToolCallFact]:
    facts: list[ToolCallFact] = []
    for record in records if isinstance(records, list | tuple) else ():
        if not isinstance(record, dict) or not str(record.get("tool") or "").strip():
            continue
        facts.append(
            ToolCallFact(
                tool=str(record.get("tool") or "").strip(),
                ok=record.get("ok") is not False,
                error_code=str(record.get("error_code") or "").strip().upper(),
                failure_stage=str(record.get("failure_stage") or "").strip().lower(),
                handler_executed=record.get("handler_executed") is True,
                argument_names=_argument_names(record.get("parameters")),
            )
        )
    return facts


# LLM: tool_completed 事件由 tool_runtime_ledger 追加;payload 只含工具名/ok/错误码/阶段等结构化字段,
#   事件时间取权威库的 created_at。旧事件没有 failure_stage 时保持空串,不从错误码推断阶段。
# 函数用途: 把权威事件流里的工具完成事件转成失败段统计用的事实列表。
def event_tool_call_facts(events: object) -> list[ToolCallFact]:
    facts: list[ToolCallFact] = []
    for event in events if isinstance(events, list | tuple) else ():
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or not str(payload.get("tool") or "").strip():
            continue
        facts.append(
            ToolCallFact(
                tool=str(payload.get("tool") or "").strip(),
                ok=payload.get("ok") is not False,
                error_code=str(payload.get("error_code") or "").strip().upper(),
                failure_stage=str(payload.get("failure_stage") or "").strip().lower(),
                handler_executed=payload.get("handler_executed") is True,
                at=_float_value(event.get("created_at")),
            )
        )
    return facts


# LLM: 从最新事实倒查:先跳过最近失败之后的成功(此时 ongoing=False),再累计与最近失败同一
#   (error_code, failure_stage) 的连续失败;任何成功或不同失败打断,宿主重复门自身拒绝透明。
#   window_full 表示事实来自截断窗口,连续段一直延伸到窗口起点时次数只是下限。
# 函数用途: 算出"最近一次工具失败是什么、同一原因连续失败了几次、现在是否仍在失败"。
def tool_failure_streak(
    facts: Sequence[ToolCallFact],
    *,
    window_full: bool = False,
) -> dict[str, object]:
    members: list[ToolCallFact] = []
    ongoing = True
    for fact in reversed(facts):
        if _is_guardrail_self_block(fact):
            continue
        if not members and fact.ok:
            ongoing = False
            continue
        if fact.ok or (members and _failure_key(fact) != _failure_key(members[0])):
            return _streak_payload(members, ongoing=ongoing, capped=False)
        members.append(fact)
    return _streak_payload(members, ongoing=ongoing, capped=window_full)


# LLM: 只在连续段仍在进行且阶段为 authorization(ActionPolicy 等授权门拒绝、handler 未执行)时返回;
#   不按错误码白名单判断,任何授权阶段错误码都走同一规则。
# 函数用途: 给子代理授权阶段收口判断当前是否正处在一段同码连续授权失败里。
def authorization_failure_streak(facts: Sequence[ToolCallFact]) -> dict[str, object]:
    streak = tool_failure_streak(facts)
    if streak.get("ongoing") is True and streak.get("failure_stage") == _AUTHORIZATION_STAGE:
        return streak
    return {}


# LLM: 只读 owner 权威 runtime_events:先按 run_id 找 AgentRun,再取最近一窗 tool_completed 事件;
#   库不可用、run 未登记或读取出错都返回空摘要(本函数只做展示投影,不能因此拦任何主链路)。
# 函数用途: 给父代理的子代理状态面和子代理自己的进度摘要算出最近工具失败段。
def recent_tool_failure(runtime_db: object, run_id: str) -> dict[str, object]:
    events = _recent_tool_events(runtime_db, str(run_id or "").strip())
    facts = event_tool_call_facts(events)
    return tool_failure_streak(facts, window_full=len(events) >= _STREAK_EVENT_WINDOW)


# LLM: 文案只由结构化字段拼出,供 last_progress_summary 如实展示;次数未知时不写次数,不猜。
# 函数用途: 生成"最近一次工具调用失败：<工具>（<错误码>，连续 N 次）"这样的短说明。
def tool_failure_progress_summary(streak: dict[str, object]) -> str:
    tool = str(streak.get("tool") or "").strip()
    code = str(streak.get("error_code") or "").strip()
    count = streak.get("consecutive_failures")
    details = [code] if code else []
    if type(count) is int and count > 0:
        details.append(f"连续{'至少 ' if streak.get('count_is_lower_bound') else ' '}{count} 次")
    suffix = f"（{'，'.join(details)}）" if details else ""
    return f"最近一次工具调用失败：{tool}{suffix}"


# LLM: 收口事实经完成合同的同一投影裁剪,只留原因码、工具、错误码、阶段、次数和参数名。
# 函数用途: 把一段授权阶段连续失败整理成交给父代理的结构化收口事实。
def tool_failure_halt(streak: dict[str, object], reason_code: str) -> dict[str, object]:
    if not streak:
        return {}
    raw = {**streak, "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "reason_code": reason_code}
    projected = completion_tool_failure_halt_facts({"tool_failure_halt": raw})
    return dict(projected.get("tool_failure_halt") or {})


# LLM: 只读 RuntimeRepository 的既有查询接口;任何库/结构异常都收成空列表,不写库、不重试。
# 函数用途: 按 run_id 读取权威库里最近一窗工具完成事件,读取失败返回空列表。
def _recent_tool_events(runtime_db: object, run_id: str) -> list[dict[str, object]]:
    if runtime_db is None or not run_id:
        return []
    try:
        agent_run = runtime_db.agent_run_for_run_id(run_id)
        if agent_run is None:
            return []
        events = runtime_db.events_for_agent_run(
            str(agent_run["agent_run_id"]),
            event_type="tool_completed",
            limit=_STREAK_EVENT_WINDOW,
        )
    except (sqlite3.Error, OSError, AttributeError, KeyError, TypeError):
        return []
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


# LLM: members 由 tool_failure_streak 按从新到旧收集;这里只整理字段,不再判断成功或失败。
# 函数用途: 组装连续失败段摘要;members 按从新到旧排列。
def _streak_payload(
    members: list[ToolCallFact],
    *,
    ongoing: bool,
    capped: bool,
) -> dict[str, object]:
    if not members:
        return {}
    latest = members[0]
    chronological = list(reversed(members))
    payload: dict[str, object] = {
        "tool": latest.tool,
        "tools": _unique_limited(fact.tool for fact in chronological),
        "error_code": latest.error_code,
        "failure_stage": latest.failure_stage,
        "consecutive_failures": len(members),
        "ongoing": ongoing,
    }
    names = _unique_limited(name for fact in chronological for name in fact.argument_names)
    if names:
        payload["argument_names"] = names
    if latest.at > 0:
        payload["last_failed_at"] = latest.at
    if capped:
        payload["count_is_lower_bound"] = True
    return payload


# LLM: "同一原因"只由错误码与失败阶段两个结构化字段决定,不看工具名、参数或输出。
# 函数用途: 返回判断两次失败是否属于同一连续段的键。
def _failure_key(fact: ToolCallFact) -> tuple[str, str]:
    return fact.error_code, fact.failure_stage


# LLM: 与 consecutive_same_failure_count 的口径一致:重复门自身的未执行拒绝既不累计也不打断连续段。
# 函数用途: 识别宿主重复门自己拦下的调用,让它在失败段统计里保持透明。
def _is_guardrail_self_block(fact: ToolCallFact) -> bool:
    return (
        not fact.ok
        and not fact.handler_executed
        and fact.error_code.startswith(_GUARDRAIL_SELF_BLOCK_PREFIX)
    )


# LLM: 只取参数键名并排序,参数值(路径、正文)一律不复制,避免把大段内容带进父级事件。
# 函数用途: 从工具参数字典里取出参数名列表。
def _argument_names(parameters: object) -> tuple[str, ...]:
    if not isinstance(parameters, dict):
        return ()
    return tuple(sorted(str(key) for key in parameters if str(key or "").strip()))


# LLM: 保持首次出现顺序去重,最多 _STREAK_NAME_LIMIT 个,保证摘要有界。
# 函数用途: 把工具名或参数名去重并截断成短列表。
def _unique_limited(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
        if len(result) >= _STREAK_NAME_LIMIT:
            break
    return result


# LLM: 事件时间来自权威库 created_at;无法解析时按 0 处理,摘要里就不带时间,不猜。
# 函数用途: 把事件时间安全地转成非负浮点数。
def _float_value(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "REPEATED_TOOL_AUTHORIZATION_FAILURE",
    "TOOL_FAILURE_LEDGER_ATTR",
    "ToolCallFact",
    "archive_tool_call_facts",
    "authorization_failure_streak",
    "event_tool_call_facts",
    "ledger_tool_failure_halt",
    "record_tool_failure_ledger",
    "recent_tool_failure",
    "tool_failure_code_counts",
    "tool_failure_halt",
    "tool_failure_progress_summary",
    "tool_failure_streak",
    "tool_failures_from_archive",
]
