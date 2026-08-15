#!/usr/bin/env python3
"""漏报四分类归因(T5 观测小补,纯标准库):把 fleet_score 的每条漏报自动归因到
「引擎没抬 / 抬了没判 / 判了没报 / 报了没进交付面」,省人工翻账。

证据链(全部结构化事实,零语义判断):
  引擎抬没抬  → watch_state/*.spool.ndjson 候选批里有没有该 event_id(摄取层落盘账);
  模型碰没碰  → owner home 的过程面(对话 jsonl / work 工具痕)有没有该 id;
  报没报成物  → work/child_outputs / findings.jsonl 等报告类产物里有没有该 id
               (有=子代理报了,但没被整合进交付面)。
分类:
  engine_never_escalated   spool 无此 id(候选都没成为——摄取/初筛层丢)
  escalated_never_judged   spool 有,过程面无(喂了模型但模型从未碰它/没消费到)
  judged_never_reported    spool 有,对话/工具痕有,报告类产物无(研判过但没写进任何报告)
  reported_not_delivered   spool 有,报告类产物有(子代理报了,整合层没收进交付面)
  unknown_spool_rotated    该 id 早于现存 spool 世代窗口(滚动轮转已冲掉,无法定案)

用法:
  python3 scripts/watch_harness/fleet_score.py --answer-key key.jsonl <targets...> --json > score.json
  python3 scripts/watch_harness/miss_attribution.py --score-json score.json \
      --watch-state <owner_home>/watch_state --owner-home <owner_home>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_EVENT_ID = re.compile(r"EVT-[A-Z]-\d{6}")
_PROCESS_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".log"}
# 报告类产物(子代理明确写出来的上报物,区别于对话/工具痕):协作槽报告 + 结论账。
_REPORT_ARTIFACT_MARKERS = ("/child_outputs/", "/findings.jsonl")


def _load_miss_ids(score_json: Path) -> list[str]:
    payload = json.loads(score_json.read_text(encoding="utf-8"))
    return [str(row.get("event_id") or "") for row in payload.get("miss_rows", []) if row.get("event_id")]


def _spool_ids_and_window(watch_state: Path) -> tuple[set[str], float]:
    """现存 spool 世代里出现过的全部候选 event_id,以及最早候选事件时间(窗口下沿)。"""
    ids: set[str] = set()
    earliest = float("inf")
    for spool in sorted(watch_state.glob("*.spool.ndjson")):
        earliest = _collect_spool_file(spool, ids, earliest)
    return ids, earliest


def _collect_spool_file(spool: Path, ids: set[str], earliest: float) -> float:
    for line in spool.read_text(encoding="utf-8", errors="replace").splitlines():
        ids.update(_EVENT_ID.findall(line))
        earliest = min(earliest, _line_earliest_ts(line, earliest))
    return earliest


def _line_earliest_ts(line: str, current: float) -> float:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return current
    values = [c.get("event", {}).get("ts") for c in row.get("candidates", []) if isinstance(c, dict)]
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    return min(numbers) if numbers else current


def _surfaces_with_id(owner_home: Path, wanted: set[str]) -> dict[str, dict[str, list[str]]]:
    """扫过程面:每个 id 出现在哪些「报告类产物」与哪些「对话/工具痕」文件。"""
    found: dict[str, dict[str, list[str]]] = {i: {"report_artifacts": [], "traces": []} for i in wanted}
    for path in sorted(owner_home.rglob("*")):
        if not (path.is_file() and path.suffix in _PROCESS_SUFFIXES):
            continue
        posix = path.as_posix()
        if "/watch_state/" in posix:
            continue  # 摄取层原始账不算模型痕迹
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        present = {i for i in wanted if i in text}
        if not present:
            continue
        is_report = any(marker in posix for marker in _REPORT_ARTIFACT_MARKERS)
        bucket = "report_artifacts" if is_report else "traces"
        for event_id in present:
            found[event_id][bucket].append(posix)
    return found


def _classify(miss: dict, spool: tuple[set[str], float], surfaces: dict) -> str:
    spool_ids, spool_floor = spool
    event_id = str(miss.get("event_id") or "")
    emitted_at = float(miss.get("emitted_at") or 0.0)
    if event_id not in spool_ids:
        if emitted_at and spool_floor != float("inf") and emitted_at < spool_floor:
            return "unknown_spool_rotated"
        return "engine_never_escalated"
    if surfaces["report_artifacts"]:
        return "reported_not_delivered"
    if surfaces["traces"]:
        return "judged_never_reported"
    return "escalated_never_judged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--score-json", required=True, help="fleet_score --json 的输出文件")
    parser.add_argument("--watch-state", required=True, help="owner home 的 watch_state 目录")
    parser.add_argument("--owner-home", required=True, help="owner home 根(扫过程面)")
    parser.add_argument("--answer-key", default="", help="可选:提供后按 emitted_at 判 spool 轮转窗口")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    score = json.loads(Path(args.score_json).read_text(encoding="utf-8"))
    miss_rows = score.get("miss_rows", [])
    miss_ids = [str(r.get("event_id")) for r in miss_rows if r.get("event_id")]
    if not miss_ids:
        print("没有漏报,无需归因。")
        return 0
    spool = _spool_ids_and_window(Path(args.watch_state))
    surfaces = _surfaces_with_id(Path(args.owner_home), set(miss_ids))
    rows = [_attributed_row(row, spool, surfaces) for row in miss_rows if row.get("event_id")]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    if args.json:
        print(json.dumps({"counts": counts, "rows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"漏报 {len(rows)} 条归因: {counts}")
    for row in rows:
        print(f"  {row['event_id']} ({row['source']}) → {row['category']}{_row_hint(row)}")
    return 0


def _attributed_row(miss: dict, spool: tuple[set[str], float], surfaces: dict) -> dict:
    event_id = str(miss.get("event_id") or "")
    return {
        "event_id": event_id,
        "source": miss.get("source"),
        "category": _classify(miss, spool, surfaces[event_id]),
        "report_artifacts": surfaces[event_id]["report_artifacts"][:5],
        "traces": surfaces[event_id]["traces"][:5],
    }


def _row_hint(row: dict) -> str:
    if row["report_artifacts"]:
        return f"  报告物: {Path(row['report_artifacts'][0]).name}"
    if row["traces"]:
        return f"  痕迹: {Path(row['traces'][0]).name}"
    return ""


if __name__ == "__main__":
    sys.exit(main())
