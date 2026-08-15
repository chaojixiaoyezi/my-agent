#!/usr/bin/env python3
"""蹲守跑批精确对账:日志里的真命中行 vs 会话里的逐轮上报,逐条配对算延迟/漏报/误报。

用法: watch_report_audit.py <日志路径> <会话线程 jsonl 路径>
判定口径:命中上报=消息里同时出现 CRITICAL/[INCIDENT]/命中;配对按 seq(允许 ±1);
另统计"以提问收尾"的消息数(无人应答场景应为 0)。
"""
from __future__ import annotations

import json
import re
import sys
import time


def _hms_to_s(t: str) -> int:
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s


def _hit_from_line(line: str) -> tuple[int, str] | None:
    if "[INCIDENT]" not in line or "CRITICAL" not in line:
        return None
    m = re.search(r"^(\d\d:\d\d:\d\d).*seq=(\d+)", line)
    return (int(m.group(2)), m.group(1)) if m else None


def _true_hits(log_path: str) -> list[tuple[int, str]]:
    with open(log_path, encoding="utf-8") as stream:
        parsed = [_hit_from_line(line) for line in stream]
    return [hit for hit in parsed if hit is not None]


def _reports(thread_path: str) -> tuple[list[tuple[str, list[int], str]], int]:
    rows: list[tuple[str, list[int], str]] = []
    questions = 0
    with open(thread_path, encoding="utf-8") as stream:
        lines = list(stream)
    for line in lines:
        msg = json.loads(line)
        content = str(msg.get("content", ""))
        ts = time.strftime("%H:%M:%S", time.localtime(msg.get("created_at", 0)))
        if "CRITICAL" in content and "[INCIDENT]" in content and "命中" in content:
            rows.append((ts, [int(s) for s in re.findall(r"seq=(\d+)", content)], content))
        if content.rstrip().endswith(("?", "？")) or "请告诉我" in content or "怎么办" in content:
            questions += 1
    return rows, questions


def _pair_one(report: tuple[str, list[int], str], hits: list[tuple[int, str]], matched: dict) -> bool:
    ts, seqs, _content = report
    paired = False
    for seq, t in hits:
        if seq in seqs or (seq + 1) in seqs:
            matched.setdefault(seq, (ts, _hms_to_s(ts) - _hms_to_s(t)))
            paired = True
    return paired


def _pair_reports(
    hits: list[tuple[int, str]],
    reports: list[tuple[str, list[int], str]],
) -> tuple[dict[int, tuple[str, int]], list[tuple[str, str]]]:
    matched: dict[int, tuple[str, int]] = {}
    false_pos: list[tuple[str, str]] = []
    for report in reports:
        if not _pair_one(report, hits, matched):
            false_pos.append((report[0], report[2][:100]))
    return matched, false_pos


def main() -> None:
    hits = _true_hits(sys.argv[1])
    reports, questions = _reports(sys.argv[2])
    print(f"日志真命中 {len(hits)} 条: {hits}")
    matched, false_pos = _pair_reports(hits, reports)
    print(f"\n上报配对 {len(matched)}/{len(hits)}:")
    for seq, t in hits:
        if seq in matched:
            rts, delay = matched[seq]
            print(f"  seq={seq} 出现 {t} → 上报 {rts} (延迟 {delay}s)")
        else:
            print(f"  seq={seq} 出现 {t} → ❌ 漏报")
    print(f"\n误报: {len(false_pos)}")
    for ts, c in false_pos:
        print(f"  [{ts}] {c}")
    print(f"以提问收尾的消息: {questions}")


if __name__ == "__main__":
    main()
