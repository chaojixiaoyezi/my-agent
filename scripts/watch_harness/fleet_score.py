#!/usr/bin/env python3
"""能力一测试台打分器:对账 agent 上报 vs answer-key(可复用)。

监控类任务的"命中上报"常是【消息/响应】不是文件——本脚本扫【会话 jsonl / 网关响应 /
任意文本产物】里出现的事件 ID(EVT-<源字母>-<seq>),与 multi_source_simulator.py 写的
answer-key 对账,输出:命中数/漏报/误报 + 每条命中的端到端延迟(上报时刻 − 产生时刻)。

用法:
  python3 scripts/watch_harness/fleet_score.py --answer-key /tmp/watch_answer_key.jsonl \
      <owner_home>/... 的若干 jsonl/md/txt 文件或目录(可多个,目录会递归扫)
上报时刻的取法:jsonl 行里的时间字段(created_at/ts/timestamp,ISO 或 epoch)优先,
取不到用文件 mtime 兜底(会标注 latency_source=mtime,只当上界看)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_EVENT_ID = re.compile(r"EVT-[A-Z]-\d{6}")
_SCAN_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".log"}

# 只扫「上报面」(最终交付 output/ + 会话消息),排除「过程面」——摄取层 watch_stream 会把
# 初筛出的【原始候选批】(含原始事件 ID)落进 tasks/<req>/work/(blobs/tool_outputs、子代理
# 未整合的 child_outputs、timeline),这些是给 LLM 研判的原料,不是上报;混进来误报虚高
# (真机实测:两路盯守刷出 89 个"误报"全是候选原料)。规则对齐 handoff §4「别扫原始拉取的数据」:
#   · 交付面 = 路径里有 /output/(即便嵌了 output/work/child_outputs 也是【已整合交付】,保留);
#   · 过程面 = 任务的 work/ 兄弟目录(有 /work/ 且不在 /output/ 下)——排除;
#   · 审计账 *.audit.ndjson 靠后缀天然不在扫描集。
def _is_process_surface(path: Path) -> bool:
    posix = path.as_posix()
    if "/output/" in posix:
        return False
    if "/work/" in posix or "/blobs/tool_outputs/" in posix:
        return True
    return path.name.endswith(".audit.ndjson")


def _load_answer_key(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event_id"):
            rows[str(row["event_id"])] = row
    return rows


def _iter_files(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        files.extend(_files_for_target(Path(target)))
    return files


def _count_excluded(targets: list[str]) -> int:
    total = 0
    for target in targets:
        path = Path(target)
        if not path.is_dir():
            continue
        total += sum(
            1 for p in path.rglob("*")
            if p.suffix in _SCAN_SUFFIXES and p.is_file() and _is_process_surface(p)
        )
    return total


def _files_for_target(path: Path) -> list[Path]:
    if path.is_dir():
        return [
            p for p in sorted(path.rglob("*"))
            if p.suffix in _SCAN_SUFFIXES and p.is_file() and not _is_process_surface(p)
        ]
    # 显式点名单个文件时尊重用户选择(不做过程面过滤),只有目录递归才排除过程材料。
    return [path] if path.is_file() else []


def _record_time(raw_line: str) -> float | None:
    """尽力从 jsonl 行提取时间戳(created_at/ts/timestamp/time,ISO 或 epoch 秒/毫秒)。"""
    try:
        row = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    for key in ("created_at", "ts", "timestamp", "time", "at"):
        value = row.get(key) if isinstance(row, dict) else None
        parsed = _parse_ts(value)
        if parsed is not None:
            return parsed
    return None


def _parse_ts(value: object) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    return _parse_iso_ts(value) if isinstance(value, str) and value else None


def _parse_iso_ts(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _scan_reports(files: list[Path]) -> dict[str, tuple[float, str, str]]:
    """event_id -> (最早上报时刻, 文件, latency_source)。"""
    found: dict[str, tuple[float, str, str]] = {}
    for path in files:
        _scan_one_file(path, found)
    return found


def _scan_one_file(path: Path, found: dict[str, tuple[float, str, str]]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return
    lines = [line for line in text.splitlines() if _EVENT_ID.search(line)]
    matches = [(event_id, line) for line in lines for event_id in _EVENT_ID.findall(line)]
    for event_id, line in matches:
        ts = _record_time(line)
        stamp, source = (ts, "record") if ts is not None else (mtime, "mtime")
        current = found.get(event_id)
        if current is None or stamp < current[0]:
            found[event_id] = (stamp, str(path), source)


class _Scored:
    # 打分结果聚合(plain class 而非 dataclass:本脚本会被测试用 importlib 独立加载,
    # 3.14 dataclass 注解解析要求模块在 sys.modules 里,独立加载会 NoneType.__dict__ 崩)。
    def __init__(self, key, hits, misses, latencies, false_positives):
        self.key = key
        self.hits = hits
        self.misses = misses
        self.latencies = latencies
        self.false_positives = false_positives
        self.scanned = 0
        self.excluded = 0


def _score(key: dict[str, dict], reports: dict[str, tuple[float, str, str]]) -> _Scored:
    hits, misses, latencies = [], [], []
    for event_id, row in sorted(key.items()):
        report = reports.get(event_id)
        if report is None:
            misses.append({"event_id": event_id, "source": row.get("source"), "emitted_at": row.get("emitted_at")})
            continue
        latency = report[0] - float(row.get("emitted_at") or 0.0)
        hits.append({
            "event_id": event_id, "source": row.get("source"),
            "latency_s": round(latency, 1), "latency_source": report[2], "reported_in": report[1],
        })
        latencies.append(latency)
    return _Scored(key, hits, misses, latencies, sorted(set(reports) - set(key)))


def _summary(scored: _Scored) -> dict:
    latencies = scored.latencies
    return {
        "answer_key_total": len(scored.key),
        "hits": len(scored.hits), "misses": len(scored.misses), "false_positive_ids": scored.false_positives,
        "latency_s": {
            "min": round(min(latencies), 1) if latencies else None,
            "median": round(sorted(latencies)[len(latencies) // 2], 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "report_files_scanned": scored.scanned,
        "process_files_excluded": scored.excluded,
        "hit_rows": scored.hits, "miss_rows": scored.misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--answer-key", required=True)
    parser.add_argument("targets", nargs="+", help="要扫的 jsonl/md/txt 文件或目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()

    key = _load_answer_key(Path(args.answer_key))
    scanned = _iter_files(args.targets)
    reports = _scan_reports(scanned)
    scored = _score(key, reports)
    scored.scanned = len(scanned)
    scored.excluded = _count_excluded(args.targets)
    if args.json:
        print(json.dumps(_summary(scored), ensure_ascii=False, indent=2))
    else:
        print(f"命中 {len(scored.hits)}/{len(key)}  漏报 {len(scored.misses)}  误报 {len(scored.false_positives)}")
        print(f"(扫上报面 {len(scanned)} 文件;排除过程面 {scored.excluded} 文件=候选原料/审计账,不计入误报)")
        for row in scored.hits:
            print(f"  ✓ {row['event_id']} ({row['source']})  延迟 {row['latency_s']}s [{row['latency_source']}]")
        for row in scored.misses:
            print(f"  ✗ 漏 {row['event_id']} ({row['source']})")
        for event_id in scored.false_positives:
            print(f"  ! 误报 {event_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
