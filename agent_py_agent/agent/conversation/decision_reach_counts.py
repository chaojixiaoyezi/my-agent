# LLM: 决策点诊断计数属于 conversation 决策服务，是“到达触发点 / 实际调用 / 没调用的宿主原因码”的唯一来源。
#   每次到达只在进程内按 owner、小时、点位、原因累加，不逐次写盘；有新计数时每个 owner 最多每 _FLUSH_SECONDS 秒持锁
#   读-加-写合并一次到规范路径 owner_decision_reach_counts_json，保留 _RETAIN_SECONDS；Gateway 正常停止时再把尾巴补写一次。
#   原因码只来自结构化判定；交付复核的少量 stale/重复记录也沿同一诊断，不解析自然语言或另建账。
#   开关复用 decision_skip_records_enabled（关闭时不计数也不写盘）。
#   新增原因码须同步 _LABELS（带数量界限的写进 _limit_labels，数字只读 decision_point_limits）、调用点与
#   test_decision_reach_counts.py；展示入口是 audit_records 与 decision_read。
# 模块用途: 展示各 Jev 点的检查、调用与未调用原因；写入后无需挑选及已请求的记录也讲清原因，同步标签与点位测试。
"""Bounded per-point reach and miss-reason counters for optional decision points."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from ..backends.decision_protocol import DecisionInputError, DecisionPrivacySkip
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from . import decision_point_limits as limits

SCHEMA = "decision_reach.v1"
CALLED = "called"
# observe 采样的成功样本计数用独立内部码：只服务采样判定，不进 reached/called/not_called 诊断（_point_row 里排除）。
SAMPLE_SUCCESS = "observe_sample_ok"
# observe 转后台时队列已满、这次没发出的内部码：到达时调用点已记过 called，这里只按小时留一份拒绝次数供审计核对，
# 同样不进 reached/called/not_called（避免同一次到达记两遍）；结果日志另有 skipped/observe_nonblocking_busy 一行。
NONBLOCKING_BUSY = "observe_nonblocking_busy"
# 本片接入到达计数的点位；其余点位展示时明确写“未统计”，不显示成 0 次。
COVERED_POINTS = ("planning", "delivery_quality", "action_candidate", "external_material_order",
                  "skill_proposal_review", "pre_recall", "recall", "curator", "curator_relation", "model_selection",
                  "skill_tool", "subagent_model")
# 点位的适用范围说明（宿主事实，不是原因码）：诊断里带出，审计与菜单照原样显示，免得把“这里本来就不判断”看成没接线。
_POINT_NOTES = {"model_selection": "只在经 Gateway 的对话里判断，本机直连 TUI 不判断"}
# 决策触达计数每 60 秒刷一次盘：控制写放大。
_FLUSH_SECONDS = 60.0
# 决策触达计数保留 7 天（秒）：过期清理。
_RETAIN_SECONDS = 7 * 86400
# 一小时折合 3600 秒：决策触达按小时桶计数的时间步长。
_HOUR_SECONDS = 3600
_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_LOCK = threading.Lock()
_PENDING: dict[str, dict[tuple[int, str, str], int]] = {}
_LAST_FLUSH: dict[str, float] = {}
# 原因码 → 给不懂技术的用户看的大白话；键是宿主短标识。
_LABELS = {
    CALLED: "已调用决策模型",
    "observe_sampled_out": "这个点位本小时的观察样本已经够了，本小时不再调用决策模型（observe 采样）",
    "point_off": "这个点位没打开（或决策模型总开关关着）",
    "admin_disabled": "管理员关闭了你使用决策模型的权限",
    "settings_busy": "那一刻决策设置正在保存，跳过了这一次",
    "configuration_unavailable": "决策设置读不出来（可能还没配好决策模型）",
    "invalid_identity": "这次缺少任务身份信息，按规定跳过",
    "run_mismatch": "这条结果不属于当前这一轮任务",
    "subagent": "子代理里不做这项判断，只在主对话里做",
    "halted": "这一轮因为连续失败或结果不明已经收尾",
    "no_request": "这一轮没有用户说的话可以参考",
    "privacy_url": "材料里有带查询参数的网址，为保护隐私没有发出去",
    "bad_material": "要发给决策模型的材料格式不对或太大，为稳妥没有发出去",
    "no_run_context": "当时没有正在进行的任务",
    "record_mismatch": "这条结果和系统存档对不上，为稳妥不做判断",
    "not_test_command": "这一步既不是测试命令，也不是让已有验证过期的文件写入",
    "not_executed": "这一步没有真正执行完",
    "not_verification": "这一步没有留下可用的验证记录或过期引用",
    "bad_verification": "这轮的测试结果记录格式不完整，为稳妥不做判断",
    "nothing_to_review": "测试都通过了，之后也没改过文件，没有需要复核的",
    "few_stale_focuses": "改后未复核的焦点至多一个，不需要决策模型挑选",
    "already_requested": "这条记录已经请求过复核建议，不再重复请求",
    "not_observation": "这一步没有产生可点选的页面操作（只有浏览器这类插件会产生）",
    "failed_call": "这一步执行失败了",
    "bad_observation": "页面操作清单不完整，为稳妥不做判断",
    "no_available_action": "候选操作需要的工具这一轮用不了",
    "stale_observation": "页面已经变了，这份操作清单过期了",
    "not_web_fetch": "这一步不是抓取网页",
    "fetch_failed": "网页抓取没有成功",
    "not_external_page": "抓回来的不是普通外部网页内容",
    "not_extract": "这次抓取不是摘录模式",
    "ledger_unreadable": "待办清单读不出来",
    "not_main_scope": "这不是主对话里的任务",
    "not_current_plan": "查看的是别的任务或历史任务的清单",
    "no_plan_version": "待办清单还没有版本信息",
    "no_query_fragments": "这轮的问题拆不出可以补充搜索的片段",
    "no_free_slots": "这轮能放进来的长期记忆名额已经满了",
    "no_room": "这轮能放记忆的字数已经用完了",
    "no_new_facts": "补充查询设成先预检片段：每个片段都补不出原召回之外的新事实，这次不问",
    "preview_failed": "补充查询预检片段时检索出错，这次不问",
    "nothing_to_label": "这批要整理的记忆材料里没有能标注的内容",
    "nothing_to_compare": "这批材料里没有能和已有记忆对照的内容",
    "memory_changed": "已有记忆刚刚被改过，这次先不判断",
    "no_candidates": "没有其它可以换用的模型",
    "turn_closed": "这一轮在判断之前就已经结束了",
    "tools_disabled": "这次对话没开工具，不用挑工具和 Skill",
    "isolated_scope": "这是隔离或控制类的任务，不做工具推荐",
    "nothing_to_recommend": "没有可以推荐的工具或 Skill",
    "experiment_forbidden": "实验没有放行这个点位",
    "models_given": "这批子任务都已指定模型（或是复用已有子代理），不用再选",
    "nothing_to_ask": "这批子任务还没准备好编号，这次不问",
    "candidate_scope_changed": "候选模型的范围刚被改过（或复核时读不出设置），这次先不问",
    "structure_unchanged": "选模型设成只在结构变化时问：会话没压缩、模型目录和当前模型都没变，这一轮沿用上次的判断",
}


# LLM: 热路径只做进程内累加；到期才合并写盘（写失败只记日志并把计数放回，绝不影响点位本身的判断与结果）。
#   开关 decision_skip_records_enabled 为假、或宿主没有规范路径时什么都不做；只认 HomePaths 字段的真实 Path，替身/mock
#   属性不算（否则会按 mock 的名字在当前目录写出垃圾文件）。reason 只能是宿主原因码，CALLED 表示真的调用了。
#   flush=False 只在内存累加、本次绝不落盘，给有“零 I/O”合同的路径用（如选模型关闭时的请求钩子）；这些计数随同一 owner
#   下一次到期的计数或 Gateway 正常停止一起写盘，读取时也会合并进来。
# 函数用途: 记录一次“点位到达触发点”及其结果（调用了，或没调用的原因）。
def note_decision_reach(agent: object, point: str, reason: str, *, flush: bool = True) -> None:
    if not bool(getattr(getattr(agent, "config", None), "decision_skip_records_enabled", True)):
        return
    path = getattr(getattr(agent, "home_paths", None), "owner_decision_reach_counts_json", None)
    if not isinstance(path, Path) or not point:
        return
    key = (int(time.time() // _HOUR_SECONDS) * _HOUR_SECONDS, str(point), str(reason or CALLED))
    with _LOCK:
        pending = _PENDING.setdefault(str(path), {})
        pending[key] = pending.get(key, 0) + 1
        due = flush and time.monotonic() - _LAST_FLUSH.get(str(path), float("-inf")) >= _FLUSH_SECONDS
    if due:
        _flush(str(path))


# LLM: observe 采样只统计“成功调用”：拿到合法响应后才记一次，失败/超时/被挡下都不记，所以出问题的点位会一直被试。
#   与到达计数共用同一账本、同一节流和保留期，但用独立内部码，不改变 reached/called/not_called 的口径。
# 函数用途: 记一次 observe 点位的成功调用样本（写盘由同一节流负责）。
def note_observe_sample_success(agent: object, point: str, *, flush: bool = True) -> None:
    note_decision_reach(agent, point, SAMPLE_SUCCESS, flush=flush)


# LLM: 只读本进程与盘上计数，取当前自然小时的样本数；路径缺失、文件损坏一律按 0 处理，采样判定不能因诊断数据缺失而改动主链路。
# 函数用途: 返回某个点位本自然小时里成功调用决策模型了几次。
def observe_sample_success_count(home_paths: object, point: str, *, now: float | None = None) -> int:
    path = getattr(home_paths, "owner_decision_reach_counts_json", None)
    if not isinstance(path, Path) or not point:
        return 0
    hour = int((time.time() if now is None else float(now)) // _HOUR_SECONDS) * _HOUR_SECONDS
    hours = _read_hours(path)
    with _LOCK:
        pending = dict(_PENDING.get(str(path), {}))
    for key, count in pending.items():
        _add(hours, key, count)
    return int(((hours.get(hour) or {}).get(str(point)) or {}).get(SAMPLE_SUCCESS, 0) or 0)


# LLM: 只包材料构建这一步：材料不合规或超出协议上限（DecisionInputError）时记一次 bad_material 再原样上抛，
#   各点位原有的放弃/告警路径不变；隐私跳过（DecisionPrivacySkip）不在这里计，由 material_or_skip 记 privacy_url。
# 函数用途: 准备决策材料，并把“材料不合格、没发给决策模型”计入诊断计数。
def counted_material(agent: object, point: str, build: Callable[[], _T]) -> _T:
    try:
        return build()
    except DecisionPrivacySkip:
        raise
    except DecisionInputError:
        note_decision_reach(agent, point, "bad_material")
        raise


# LLM: 只读阶段的结构化字段：阶段错误码原样作原因码；点位不在 enabled_points 记 point_off；给了 run_id 时身份不符记 run_mismatch。
# 函数用途: 统一算出“阶段这一关”为什么没让点位调用决策模型；返回空串表示可以继续。
def stage_miss_reason(stage: object, point: str, run_id: str | None = None) -> str:
    code = str(getattr(stage, "error_code", "") or "")
    if code:
        return code
    if point not in tuple(getattr(stage, "enabled_points", ()) or ()):
        return "point_off"
    if run_id is not None and getattr(stage, "run_id", None) != run_id:
        return "run_mismatch"
    return ""


# 函数用途: 把原因码翻成大白话；带数量界限的说明每次现算，没登记的码原样带上，便于排查，不拒绝。
def miss_reason_label(reason: str) -> str:
    return _LABELS.get(reason) or _limit_labels().get(reason) or f"其它原因（{reason}）"


# LLM: 数字只读 decision_point_limits（点位判定读的是同一处），调用时现算，常量一改标签随之变；不在这里写死任何界限。
# 函数用途: 生成“数量不够或太多”这类原因的大白话。
def _limit_labels() -> dict[str, str]:
    return {
        "focus_count": (f"这轮跑过的不同测试不到 {limits.DELIVERY_FOCUSES_MIN_COUNT} 组"
                        f"（或超过 {limits.DELIVERY_FOCUSES_MAX_COUNT} 组），不需要挑复核重点"),
        "few_candidates": f"页面上可选的操作不到 {limits.ACTION_CANDIDATES_MIN_COUNT} 个，不需要挑",
        "single_page": f"这次抓到的网页不到 {limits.MATERIAL_PAGES_MIN_COUNT} 个，不需要排阅读顺序",
        "todo_count": (f"未完成的待办不到 {limits.PLANNING_TODOS_MIN_COUNT} 个"
                       f"（或超过 {limits.PLANNING_TODOS_MAX_COUNT} 个），不需要排优先级"),
        "pending_count": (f"待确认的 Skill 提案不到 {limits.SKILL_PROPOSALS_MIN_COUNT} 条"
                          f"（或超过 {limits.SKILL_PROPOSALS_MAX_COUNT} 条），不需要排审核顺序"),
        "memory_count": f"这轮找到的普通记忆不到 {limits.RECALL_MEMORIES_MIN_COUNT} 条，不需要重新排序",
    }


# LLM: 立即把本进程所有未落盘计数写出；平时由 note_decision_reach 按节流自动写。调用方是 Gateway 正常停止收尾
#   （cli/gateway_process._flush_decision_reach_counts）与测试；不注册 atexit，异常退出仍会丢上次合并之后的计数。
# 函数用途: 强制落盘本进程积攒的到达计数（写文件副作用）。
def flush_decision_reach_counts() -> None:
    with _LOCK:
        paths = list(_PENDING)
    for path in paths:
        _flush(path)


# LLM: 只读；合并盘上计数与本进程尚未落盘的计数，按小时桶与 since 对齐（桶只要和时间窗有重叠就算入）。
# 函数用途: 为审计与菜单汇总每个点位在时间窗内的到达、调用和未调用原因分布。
def decision_reach_summary(home_paths: object, *, since: float) -> dict[str, object]:
    path = getattr(home_paths, "owner_decision_reach_counts_json", None)
    if not path:
        return {"available": False, "points": {}, "coverage_since": None}
    hours = _read_hours(Path(path))
    with _LOCK:
        pending = dict(_PENDING.get(str(path), {}))
    for key, count in pending.items():
        _add(hours, key, count)
    floor = int(since // _HOUR_SECONDS) * _HOUR_SECONDS
    totals: dict[str, dict[str, int]] = {}
    for hour, points in hours.items():
        if hour >= floor:
            _merge_points(totals, points)
    return {"available": True, "points": {point: _point_row(counts) for point, counts in totals.items()},
            "coverage_since": min(hours) if hours else None, "retention_days": _RETAIN_SECONDS // 86400}


# LLM: 纯函数；modes 是设置摘要里的 {点位: effective_mode}（读不到时传 None），reach 是 decision_reach_summary 的结果。
#   未接入计数的点位 covered=False 并写明“未统计”，不能显示成 0 次；点位全集来自调用方给的 points。
#   note 是点位适用范围的宿主说明（没有则为空串），展示方照原样显示。
# 函数用途: 生成每个点位一行的诊断：是否开启、检查次数、调用次数、没调用的原因（原因码 + 大白话 + 次数）。
def decision_point_diagnostics(points: tuple[str, ...], modes: Mapping | None, reach: Mapping) -> dict[str, dict]:
    rows = {}
    for point in points:
        counts = (reach.get("points") or {}).get(point) or _point_row({})
        mode = str((modes or {}).get(point) or "") if modes is not None else ""
        rows[point] = {"enabled": bool(mode) and mode != "off", "mode": mode or "unknown",
                       "covered": point in COVERED_POINTS, "note": _POINT_NOTES.get(point, ""), **counts}
    return rows


# 函数用途: 把一个点位的原因计数整理成 reached/called/not_called（按次数从多到少，带大白话）。
def _point_row(counts: Mapping[str, int]) -> dict[str, object]:
    # 采样成功计数与后台队列满的拒绝计数都不是“到达/未调用”，不计入 reached，也不进 not_called。
    diagnostics = {reason: count for reason, count in counts.items() if reason not in (SAMPLE_SUCCESS, NONBLOCKING_BUSY)}
    missed = sorted(((reason, count) for reason, count in diagnostics.items() if reason != CALLED), key=lambda item: (-item[1], item[0]))
    return {"reached": sum(diagnostics.values()), "called": int(diagnostics.get(CALLED, 0)),
            "not_called": [{"reason": reason, "label": miss_reason_label(reason), "count": count} for reason, count in missed]}


# LLM: 取走本进程该路径的未落盘计数再持锁合并；写失败把计数放回（同小时同键累加，体量有界），只记日志。
# 函数用途: 把一个 owner 的待写计数合并进盘上文件（写文件副作用，首次写入时创建目录）。
def _flush(path: str) -> None:
    with _LOCK:
        pending = _PENDING.pop(path, {})
        _LAST_FLUSH[path] = time.monotonic()
    if not pending:
        return
    try:
        _merge_file(Path(path), pending)
    except (OSError, ValueError, TypeError):
        _LOGGER.warning("决策点诊断计数写入失败：%d 个计数项已放回待写队列", len(pending))
        _restore(path, pending)


# 函数用途: 写盘失败时把取走的计数放回本进程待写队列。
def _restore(path: str, pending: dict[tuple[int, str, str], int]) -> None:
    with _LOCK:
        target = _PENDING.setdefault(path, {})
        for key, count in pending.items():
            target[key] = target.get(key, 0) + count


# LLM: 读-加-写整段在同一把路径锁内完成，多进程同时合并也不丢计数；只保留最近 _RETAIN_SECONDS 的小时桶。
# 函数用途: 把待写计数加进盘上的小时桶并原子写回（写文件副作用）。
def _merge_file(path: Path, pending: dict[tuple[int, str, str], int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        hours = _read_hours(path)
        for key, count in pending.items():
            _add(hours, key, count)
        floor = time.time() - _RETAIN_SECONDS
        kept = {str(hour): points for hour, points in hours.items() if hour >= floor}
        write_json_file_atomic_unlocked(path, {"schema": SCHEMA, "hours": kept})


# LLM: 形状不对的桶、点位或计数整条忽略（诊断数据，不为坏行报错）；文件缺失或损坏按空处理。
# 函数用途: 读取盘上的小时桶计数，返回 {小时起点: {点位: {原因: 次数}}}。
def _read_hours(path: Path) -> dict[int, dict[str, dict[str, int]]]:
    report = read_json_object_report(path, context="decision_reach_counts.read")
    raw = report.payload.get("hours") if report.load_error is None and report.payload.get("schema") == SCHEMA else None
    hours: dict[int, dict[str, dict[str, int]]] = {}
    for hour, points in (raw.items() if isinstance(raw, dict) else ()):
        if str(hour).isdigit() and isinstance(points, dict):
            _merge_points(hours.setdefault(int(hour), {}), points)
    return hours


# 函数用途: 把一个小时桶里的 {点位: {原因: 次数}} 累加进目标字典，跳过形状不对的项。
def _merge_points(target: dict[str, dict[str, int]], points: Mapping) -> None:
    for point, reasons in points.items():
        if not isinstance(reasons, Mapping):
            continue
        counts = target.setdefault(str(point), {})
        for reason, count in reasons.items():
            counts[str(reason)] = counts.get(str(reason), 0) + (count if type(count) is int and count > 0 else 0)


# 函数用途: 按 (小时起点, 点位, 原因) 键给小时桶加上次数。
def _add(hours: dict[int, dict[str, dict[str, int]]], key: tuple[int, str, str], count: int) -> None:
    hour, point, reason = key
    _merge_points(hours.setdefault(hour, {}), {point: {reason: count}})


__all__ = [
    "CALLED",
    "COVERED_POINTS",
    "NONBLOCKING_BUSY",
    "SAMPLE_SUCCESS",
    "SCHEMA",
    "counted_material",
    "decision_point_diagnostics",
    "decision_reach_summary",
    "flush_decision_reach_counts",
    "miss_reason_label",
    "note_decision_reach",
    "note_observe_sample_success",
    "observe_sample_success_count",
    "stage_miss_reason",
]
