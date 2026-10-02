# LLM: 决策结果日志属于 conversation 决策服务。decide() 的每个返回（成功、到期、冷却跳过、配置不可用……）按接入点追加一行
#   结构化记录；observe 转后台时 decide 返回的 deferred 占位不写，由后台执行器完成后写这一行（blocking=false）。
#   只含点位、范围、模式、状态、原因、耗时、是否阻塞调用方和宿主身份编号，不含状态、题目、候选或回答正文；建了调用记录的行另带
#   transport 链路事实（各次 HTTP 尝试的分段毫秒、超时时所处阶段、发送前本地估算输入；attempts 为空即请求没发出），同样无正文。位置只认 owner 规范路径
#   owner_decision_outcomes_jsonl，有界保留最近 _MAX_RECORDS_COUNT 条；写失败只记日志，绝不影响决策本身。审计工具 audit_records 读取汇总，
#   汇总时把请求根本没发出去的失败类结果单列（not_sent），不计入 Jev 的超时率和失败率。
#   成功拿到供应商响应的行另带 requested_model（请求时的模型名，如 jev-latest）与 model_version（响应里供应商实际给的
#   版本号，如 jev-1.13.0）；汇总按（请求名, 实际版本）计次 model_versions，用于发现别名背后的版本变化，不参与任何判定。
#   新增字段须同步 decision_outcome_row、decision_outcome_summary 与 test_decision_outcome_log.py。
#   另有 status=skipped 行：可选决策点已到触发点、该点已开启，却被结构化条件挡下（材料含 URL 查询串）时记录，
#   reason 为宿主原因码；配置 decision_skip_records_enabled 关闭时不写。“没满足触发条件”的原因归 decision_reach_counts 计数。
# 模块用途: 让每个决策接入点"调用了没有、结果如何"有持久记录，不再只能从按用途汇总的用量账里猜某个点位是否接通。
"""Bounded per-point log of decision outcomes (structured facts only)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

from ..backends.decision_protocol import DecisionPrivacySkip, DecisionResponse
from ..common.json_io import append_jsonl_capped, read_jsonl_objects_report

SCHEMA = "decision_outcome.v1"
SKIPPED_STATUS = "skipped"
# 结果类别取值族：selected / non_selection:<取值> / dropped:<宿主原因码>；旧记录缺字段时归 RESULT_UNRECORDED（展示为未记录）。
RESULT_UNRECORDED = "unrecorded"
NO_SELECTION_RECORDED = "no_selection_recorded"
# 决策模型明确说“不要做选择”的取值集合：这些是正常结论，不是失败，也不是 provider 报错。
_NON_SELECTION_VALUES = frozenset({"not_needed", "no_match", "abstain", "need_data"})
# 宿主丢弃建议时用的既有原因码：来源变化、运行环境变化、采用期限已过、身份不再匹配。消费者直接引用，不新造同义码。
DROP_SOURCES_CHANGED = "sources_changed"
DROP_RUNTIME_CHANGED = "runtime_changed"
DROP_ADOPTION_DEADLINE = "adoption_deadline"
DROP_IDENTITY_CHANGED = "identity_changed"
# 采用前复核自己抛异常（不是判定不通过）时的兜底码：宁可标成复核失败，也不能把它当成通过。
DROP_REVIEW_FAILED = "review_failed"
_T = TypeVar("_T")
# 决策结果日志最多保留 1000 条记录：超限轮转。
_MAX_RECORDS_COUNT = 1000
# 决策结果日志近期展示 20 行。
_RECENT_ROWS_COUNT = 20
_RECENT_FIELDS = ("created_at", "point", "scope", "mode", "status", "result_category", "reason", "elapsed_ms", "blocking")
# 只有失败类结果才区分“发出去了没有”；成功、关闭、跳过、作废等照原状态计数。
_FAILURE_STATUSES = frozenset({"deadline", "error", "cooldown", "configuration_required"})
# 没有链路事实的行（没建调用记录，或链路计时上线前的旧行）按宿主固定原因码认出“没发出去”：发送前期限用完、准入忙、
# 在途登记已满、设置忙、输入不合规、配置不可用、连接或点位冷却。
_UNSENT_REASONS = frozenset({"budget_exhausted", "admission_busy", "notification_capacity", "settings_busy",
                             "invalid_input", "configuration_unavailable", "connection_backoff", "point_backoff"})
_LOGGER = logging.getLogger(__name__)


# LLM: 纯函数，只读阶段身份与结果的结构化字段；不读 response、题目或候选，调用方不得把正文塞进 outcome.reason。
#   建了调用记录的结果另带 transport（链路事实，见 decision_model_call.decision_transport_facts），调用前就结束的行没有这个键。
#   blocking 取结果对象的同名字段（缺省为真）：false 表示这次 observe 转到后台执行、调用方没有等待，
#   这类行的 elapsed_ms 只算后台 worker 开始执行到结束，不含排队。
#   结果带供应商响应（DecisionResponse）时另记 requested_model 与 model_version：两者都是结构化身份，不是正文；
#   没有响应的行（超时、冷却、跳过等）不带这两个键，不补猜。
# 函数用途: 把一次决策结果投影成一行日志。
def decision_outcome_row(stage: object, point: str, outcome: object, elapsed_seconds: float) -> dict[str, object]:
    row = {
        "schema": SCHEMA, "created_at": round(time.time(), 3), "point": str(point),
        "scope": str(getattr(stage, "scope", "") or ""), "mode": str(getattr(outcome, "mode", "") or ""),
        "status": str(getattr(outcome, "status", "") or ""), "reason": str(getattr(outcome, "reason", "") or ""),
        "result_category": decision_result_category(outcome),
        "elapsed_ms": max(0, int(float(elapsed_seconds) * 1000)),
        "thread_id": str(getattr(stage, "thread_id", "") or ""), "run_id": str(getattr(stage, "run_id", "") or ""),
        "task_id": str(getattr(stage, "task_id", "") or ""), "experiment": bool(getattr(stage, "experiment", False)),
        "blocking": bool(getattr(outcome, "blocking", True)),
    }
    transport = getattr(outcome, "transport", None)
    if isinstance(transport, dict):
        row["transport"] = transport
    response = getattr(outcome, "response", None)
    if isinstance(response, DecisionResponse):
        row["requested_model"] = str(response.requested_model)
        row["model_version"] = str(response.model)
    return row


# LLM: 纯函数，只读结果对象的结构化字段（有没有供应商响应、响应里选了什么、宿主有没有加丢弃原因码），
#   不读候选文字、题目或回答正文。带 dropped_reason 的对象一律判成 dropped:<码>，因为宿主已经决定丢掉这条建议；
#   有响应但没选中任何候选时，只有在答案取自封闭取值集合（not_needed/no_match/abstain/need_data）时才算“正常没选”，
#   其余（失败、拒绝、provider 报错）归 no_selection_recorded，不硬猜成某一类。
# 函数用途: 给一行决策结果算出封闭的结果类别字符串。
def decision_result_category(outcome: object) -> str:
    dropped = str(getattr(outcome, "dropped_reason", "") or "")
    if dropped:
        return f"dropped:{dropped}"
    response = getattr(outcome, "response", None)
    if not isinstance(response, DecisionResponse):
        return NO_SELECTION_RECORDED
    answer = _answer_category(response)
    return "selected" if answer is None else (f"non_selection:{answer}" if answer else NO_SELECTION_RECORDED)


# LLM: 只看答案的结构化取值，与逐题错误码：某题带 error_code 说明那题没得到有效回答，不计入选择；
#   只要还有一题的取值不是宿主已知的非选择取值，就是选中了候选（返回 None 表示“选中了”）。
# 函数用途: 从响应里取出作答类别；返回 None 表示选中，返回空串表示无法归类。
def _answer_category(response: object) -> str | None:
    values = [str(getattr(answer, "value", "") or "").strip().lower()
              for answer in tuple(getattr(response, "answers", ()) or ())
              if not str(getattr(answer, "error_code", "") or "")]
    if any(value and value not in _NON_SELECTION_VALUES for value in values):
        return None
    return " ".join(value for value in values if value in _NON_SELECTION_VALUES).strip()


# LLM: 写补充行的前提是“本来就有一条可采用的建议”：非 apply、没有响应或没给原因码时不记，避免把正常没选当丢弃。
#   点位取响应自身的绑定（stage 上没有点位）；写入复用同一条投影（多一个 record_kind 标识别它是补充行），
#   写失败只记日志，绝不影响主链路。
# 函数用途: 给被宿主丢弃的建议追加一行 dropped 结果（写文件副作用）。
def record_decision_dropped(agent: object, stage: object, outcome: object, reason: str) -> None:
    if not reason or outcome is None or not getattr(outcome, "may_apply", False) or getattr(outcome, "response", None) is None:
        return
    marked = SimpleNamespace(mode=getattr(outcome, "mode", ""), status=getattr(outcome, "status", ""), reason="",
                             response=outcome.response, dropped_reason=str(reason))
    point = str(getattr(getattr(outcome.response, "binding", None), "point", "") or "")
    row = decision_outcome_row(stage, point, marked, 0.0)
    row["record_kind"] = "dropped"
    # 补充行只记"这条建议被丢了"，不重复记录供应商模型身份：那些键属于原始调用行，重复会污染 model_versions 计次。
    row.pop("requested_model", None)
    row.pop("model_version", None)
    append_decision_outcome(agent, row)


# LLM: 消费者侧的最小共用出口：先登记丢弃（带宿主原有原因码），再把调用方自己的返回值原样交回，让调用点保持一行。
# 函数用途: 记下一条被丢弃的建议并返回调用方原定的结果。
def drop_and_return(agent: object, ctx: tuple, reason: str, value: object) -> object:
    stage, outcome = ctx
    record_decision_dropped(agent, stage, outcome, reason)
    return value


# LLM: 展示层唯一翻译点：把封闭取值翻成大白话，读旧记录（缺字段/空值）时显示“未记录”，未知取值原样返回，
#   免得审计和 TUI 各自猜一遍口径。只看字符串前半段的类前缀，不解析后半段原因码含义。
# 函数用途: 把一个结果类别字符串翻成给用户看的中文。
def result_category_label(category: object) -> str:
    text = str(category or "")
    if not text or text == RESULT_UNRECORDED:
        return "未记录"
    if text == "selected":
        return "选中了某个候选"
    if text.startswith("non_selection:"):
        return f"没有选择（{text.split(':', 1)[1]}）"
    if text.startswith("dropped:"):
        return f"建议被宿主丢弃（{text.split(':', 1)[1]}）"
    if text == NO_SELECTION_RECORDED:
        return "未记录可选结果"
    return text


# LLM: 路径只认宿主 home_paths 的规范字段；没有该字段（旧替身或无 owner 的宿主）就不记录。I/O 失败吞掉并记日志，
#   因为这是观察记录，不能让可选决策因写盘失败而改变结果。
# 函数用途: 把一行决策结果追加进 owner 的有界结果日志（写文件副作用，首次写入时创建目录）。
def append_decision_outcome(agent: object, row: dict[str, object]) -> None:
    path = getattr(getattr(agent, "home_paths", None), "owner_decision_outcomes_jsonl", None)
    if not path:
        return
    try:
        append_jsonl_capped(Path(path), row, max_records=_MAX_RECORDS_COUNT)
    except (OSError, ValueError, TypeError):
        _LOGGER.warning("决策结果日志写入失败：point=%s status=%s", row.get("point"), row.get("status"))


# LLM: 只在可选决策点已到触发点时调用；阶段有错或该点未开启时不写（这类原因由 decision_reach_counts 计数）。reason 只能是
#   宿主原因码（privacy_url），不含正文。配置 decision_skip_records_enabled 为假时不写；写失败只记日志。
# 函数用途: 给“触发了但被条件挡下”的决策点追加一行 skipped 结果（写文件副作用），避免审计里看起来像从未接线。
def record_decision_skip(agent: object, stage: object, point: str, reason: str) -> None:
    if not bool(getattr(getattr(agent, "config", None), "decision_skip_records_enabled", True)):
        return
    if getattr(stage, "error_code", "") or point not in tuple(getattr(stage, "enabled_points", ()) or ()):
        return
    skipped = SimpleNamespace(mode="", status=SKIPPED_STATUS, reason=str(reason))
    append_decision_outcome(agent, decision_outcome_row(stage, point, skipped, 0.0))


# LLM: build 只做材料准备（纯计算）；材料因隐私保护不能外发时记一条 skipped（原因码取异常的 reason），同一原因码也计入
#   decision_reach_counts 的未调用原因，然后返回 None；材料不合规或超限经 counted_material 计 bad_material 后原样上抛，
#   其它异常也原样上抛，由调用方原有的放弃路径处理。
# 函数用途: 准备决策材料；遇到隐私跳过时留下审计记录与诊断计数，并告诉调用方放弃本次决策。
def material_or_skip(agent: object, stage: object, point: str, build: Callable[[], _T]) -> _T | None:
    from .decision_reach_counts import counted_material, note_decision_reach

    try:
        return counted_material(agent, point, build)
    except DecisionPrivacySkip as skip:
        record_decision_skip(agent, stage, point, skip.reason)
        note_decision_reach(agent, point, skip.reason)
        return None


# LLM: 只读；按时间窗口汇总每个接入点各状态的次数，并给出最近几行（无正文，建了调用记录的行附链路计时）。坏行只计数，不中断审计。
#   请求根本没发出去的失败类结果（见 _unsent_code）不进 points，单列在 not_sent[点位][原因码]，不计入 Jev 的超时率和失败率；
#   只改汇总口径，不改日志行本身。丢弃补充行（record_kind=dropped）进类别统计与 recent，
#   但不进按状态的调用统计——它不是一次独立调用，否则会把“丢了一次”错算成“多调了一次”。
#   thread_ids 给出时只统计这些会话的行（调用方按可信范围解析，如 current_thread）；后台点位没有会话编号，随之排除。
#   None 表示 owner 全部（含后台点位）。model_versions 按（请求名, 实际版本）计次，只数带 model_version 的行（旧行没有）。
# 函数用途: 为审计工具提供"每个决策点调用了几次、分别是什么结果、供应商实际用的是哪个版本"。
def decision_outcome_summary(home_paths: object, *, since: float,
                             thread_ids: list[str] | None = None) -> dict[str, object]:
    path = getattr(home_paths, "owner_decision_outcomes_jsonl", None)
    if not path:
        return {"available": False, "points": {}, "not_sent": {}, "model_versions": [], "recent": [],
                "result_categories": [], "result_categories_by_point": {}, "unreadable_rows": 0}
    report = read_jsonl_objects_report(Path(path), context="decision_outcome_log.read")
    rows = [row for row in report.records if row.get("schema") == SCHEMA and _created_at(row) >= since
            and (thread_ids is None or str(row.get("thread_id") or "") in thread_ids)]
    points: dict[str, dict[str, int]] = {}
    unsent: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.get("record_kind") == "dropped":
            continue
        code = _unsent_code(row)
        target, key = (unsent, code) if code else (points, str(row.get("status") or ""))
        counts = target.setdefault(str(row.get("point") or ""), {})
        counts[key] = counts.get(key, 0) + 1
    recent = [_recent_row(row) for row in rows[-_RECENT_ROWS_COUNT:]]
    return {"available": True, "points": points, "not_sent": unsent, "model_versions": _model_versions(rows),
            "result_categories": _result_categories(rows),
            "result_categories_by_point": _result_categories_by_point(rows), "recent": recent,
            "unreadable_rows": len(report.load_errors)}


# LLM: 只读 result_category 结构化字符串；缺失、空值或不是字符串的旧记录归 RESULT_UNRECORDED（展示为未记录），
#   不按状态或 reason 猜类别。等次数时按类别名排序，与 decision_reach_counts 的展示口径一致。
# 函数用途: 统计时间窗内各结果类别的次数。
def _result_categories(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for row in rows:
        category = row.get("result_category")
        key = category if isinstance(category, str) and category else RESULT_UNRECORDED
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"category": category, "calls": calls} for category, calls in ordered]


# LLM: 只读 result_category 结构化字符串；按时间窗口全部行（含 dropped 补充行）逐点位归次数，
#   是 TUI 决策菜单与审计诊断共用的唯一来源：低频点位在窗口内有结果就显示，不再受最近行数限制。
#   旧记录缺字段归 RESULT_UNRECORDED（展示为未记录）；缺 point 的行不进表。
# 函数用途: 统计时间窗内各点位的结果类别次数 {点位: {类别: 次数}}。
def _result_categories_by_point(rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        point = str(row.get("point") or "")
        if not point:
            continue
        category = row.get("result_category")
        key = category if isinstance(category, str) and category else RESULT_UNRECORDED
        point_counts = counts.setdefault(point, {})
        point_counts[key] = point_counts.get(key, 0) + 1
    return counts


# LLM: 只读行里的 requested_model / model_version 两个字符串字段；缺失或不是字符串的行不计（旧行、失败行）。
#   输出按次数降序、再按名字排序，便于审计一眼看出“请求 jev-latest、实际 jev-1.13.0”这类别名解析。
# 函数用途: 统计时间窗内供应商实际返回的决策模型版本。
def _model_versions(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        version, requested = row.get("model_version"), row.get("requested_model")
        if isinstance(version, str) and version and isinstance(requested, str):
            counts[(requested, version)] = counts.get((requested, version), 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"requested_model": requested, "model_version": version, "calls": calls}
            for (requested, version), calls in ordered]


# LLM: 只看结构化事实：失败类状态才区分；有 transport 的行按有没有 HTTP 尝试判定，没有 transport 的按宿主原因码判定，
#   不读正文、不看耗时。返回“没发出去”的原因码（缺原因码时用状态名）；发出去了或不是失败类返回空串。
# 函数用途: 判断一行决策结果是不是“请求根本没发出去”。
def _unsent_code(row: dict[str, object]) -> str:
    status, reason = str(row.get("status") or ""), str(row.get("reason") or "")
    if status not in _FAILURE_STATUSES:
        return ""
    transport = row.get("transport")
    if isinstance(transport, dict):
        unsent = not transport.get("attempts")
    else:
        unsent = status == "cooldown" or reason in _UNSENT_REASONS
    return (reason or status) if unsent else ""


# LLM: 固定字段缺失时给 None（与原先一致）；链路计时与实际模型版本只在该行确有对应字段时带出，旧行不补键。
# 函数用途: 把一行结果日志压成审计最近行。
def _recent_row(row: dict[str, object]) -> dict[str, object]:
    recent = {key: row.get(key) for key in _RECENT_FIELDS}
    recent["result_category_label"] = result_category_label(row.get("result_category"))
    if isinstance(row.get("transport"), dict):
        recent["transport"] = row["transport"]
    if isinstance(row.get("model_version"), str):
        recent["model_version"] = row["model_version"]
    return recent


# 函数用途: 读取一行的记录时间，缺失或格式不对按 0 处理（落在任何窗口之外）。
def _created_at(row: dict[str, object]) -> float:
    try:
        return float(row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "SCHEMA",
    "SKIPPED_STATUS",
    "append_decision_outcome",
    "decision_outcome_row",
    "decision_outcome_summary",
    "material_or_skip",
    "record_decision_skip",
]
