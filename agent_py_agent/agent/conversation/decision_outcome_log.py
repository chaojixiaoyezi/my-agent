# LLM: 决策结果日志属于 conversation 决策服务。decide() 的每个返回（成功、到期、冷却跳过、配置不可用……）按接入点追加一行
#   结构化记录，只含点位、范围、模式、状态、原因、耗时和宿主身份编号，不含状态、题目、候选或回答正文。位置只认 owner 规范路径
#   owner_decision_outcomes_jsonl，有界保留最近 _MAX_RECORDS 条；写失败只记日志，绝不影响决策本身。审计工具 audit_records 读取汇总。
#   新增字段须同步 decision_outcome_row、decision_outcome_summary 与 test_decision_outcome_log.py。
# 模块用途: 让每个决策接入点"调用了没有、结果如何"有持久记录，不再只能从按用途汇总的用量账里猜某个点位是否接通。
"""Bounded per-point log of decision outcomes (structured facts only)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..common.json_io import append_jsonl_capped, read_jsonl_objects_report

SCHEMA = "decision_outcome.v1"
_MAX_RECORDS = 1000
_RECENT_ROWS = 20
_RECENT_FIELDS = ("created_at", "point", "scope", "mode", "status", "reason", "elapsed_ms")
_LOGGER = logging.getLogger(__name__)


# LLM: 纯函数，只读阶段身份与结果的结构化字段；不读 response、题目或候选，调用方不得把正文塞进 outcome.reason。
# 函数用途: 把一次决策结果投影成一行日志。
def decision_outcome_row(stage: object, point: str, outcome: object, elapsed_seconds: float) -> dict[str, object]:
    return {
        "schema": SCHEMA, "created_at": round(time.time(), 3), "point": str(point),
        "scope": str(getattr(stage, "scope", "") or ""), "mode": str(getattr(outcome, "mode", "") or ""),
        "status": str(getattr(outcome, "status", "") or ""), "reason": str(getattr(outcome, "reason", "") or ""),
        "elapsed_ms": max(0, int(float(elapsed_seconds) * 1000)),
        "thread_id": str(getattr(stage, "thread_id", "") or ""), "run_id": str(getattr(stage, "run_id", "") or ""),
        "task_id": str(getattr(stage, "task_id", "") or ""), "experiment": bool(getattr(stage, "experiment", False)),
    }


# LLM: 路径只认宿主 home_paths 的规范字段；没有该字段（旧替身或无 owner 的宿主）就不记录。I/O 失败吞掉并记日志，
#   因为这是观察记录，不能让可选决策因写盘失败而改变结果。
# 函数用途: 把一行决策结果追加进 owner 的有界结果日志（写文件副作用，首次写入时创建目录）。
def append_decision_outcome(agent: object, row: dict[str, object]) -> None:
    path = getattr(getattr(agent, "home_paths", None), "owner_decision_outcomes_jsonl", None)
    if not path:
        return
    try:
        append_jsonl_capped(Path(path), row, max_records=_MAX_RECORDS)
    except (OSError, ValueError, TypeError):
        _LOGGER.warning("决策结果日志写入失败：point=%s status=%s", row.get("point"), row.get("status"))


# LLM: 只读；按时间窗口汇总每个接入点各状态的次数，并给出最近几行（无正文）。坏行只计数，不中断审计。
# 函数用途: 为审计工具提供"每个决策点调用了几次、分别是什么结果"。
def decision_outcome_summary(home_paths: object, *, since: float) -> dict[str, object]:
    path = getattr(home_paths, "owner_decision_outcomes_jsonl", None)
    if not path:
        return {"available": False, "points": {}, "recent": [], "unreadable_rows": 0}
    report = read_jsonl_objects_report(Path(path), context="decision_outcome_log.read")
    rows = [row for row in report.records if row.get("schema") == SCHEMA and _created_at(row) >= since]
    points: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = points.setdefault(str(row.get("point") or ""), {})
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    recent = [{key: row.get(key) for key in _RECENT_FIELDS} for row in rows[-_RECENT_ROWS:]]
    return {"available": True, "points": points, "recent": recent, "unreadable_rows": len(report.load_errors)}


# 函数用途: 读取一行的记录时间，缺失或格式不对按 0 处理（落在任何窗口之外）。
def _created_at(row: dict[str, object]) -> float:
    try:
        return float(row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["SCHEMA", "append_decision_outcome", "decision_outcome_row", "decision_outcome_summary"]
