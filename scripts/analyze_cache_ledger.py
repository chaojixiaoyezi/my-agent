#!/usr/bin/env python3
"""离线分析显式请求计数；只读输入、只向 stdout 输出 JSON，不接生产热路径。

输入为 JSON 的 {"requests": [...], "segments": [...]} 或请求数组；也支持
JSONL，每行是 record_type=request（默认）或 record_type=segment。
请求必填 call_id/domain/segment/purpose/source；prompt_tokens/cached_tokens
为非负整数或 null，缺失也视为未知。sequence 为段内从 1 开始的请求序号。
段声明示例：{"domain":"official-sol", "segment":"s1", "confirmations":
{"append_only":true,"cold_start":true,"input_only_cache":true,
"complete_sequence":true}}。仅在明确确认且序列完整时计算条件性 floor；
这只是调用者对输入数据的声明，不是脚本已经证明了真实运行条件。
source 只作为来源标签保留，本脚本不把模拟/估算来源提升为生产实测。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

CONFIRMATIONS = ("append_only", "cold_start", "input_only_cache", "complete_sequence")


def parse_input(text: str) -> tuple[list[dict], list[dict]]:
    """解析 JSON/JSONL；内容正文不会进入输出。"""
    if not text.strip():
        raise ValueError("empty_input")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_json_at_line_{number}") from exc
        return _split_records(records)
    if isinstance(payload, list):
        return _split_records(payload)
    if isinstance(payload, dict) and "requests" in payload:
        requests, segments = payload["requests"], payload.get("segments", [])
        if not isinstance(requests, list) or not isinstance(segments, list):
            raise ValueError("requests_and_segments_must_be_arrays")
        return requests, segments
    if isinstance(payload, dict):
        return _split_records([payload])
    raise ValueError("top_level_must_be_object_or_array")


def _split_records(records: list) -> tuple[list[dict], list[dict]]:
    requests, segments = [], []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"record_{index}_must_be_object")
        kind = record.get("record_type", "request")
        if kind == "request":
            requests.append(record)
        elif kind == "segment":
            segments.append(record)
        else:
            raise ValueError(f"record_{index}_has_unknown_type")
    return requests, segments


def _label(row: dict, field: str, location: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}_{field}_must_be_nonempty_string")
    return value


def _counter(row: dict, field: str, location: str, *, positive: bool = False) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f"{location}_{field}_must_be_{'positive' if positive else 'nonnegative'}_integer_or_null")
    return value


def validate(requests: list[dict], declarations: list[dict]) -> tuple[list[dict], dict]:
    """拒绝负数、布尔/浮点假计数、重复 call_id 和重复段内序号。"""
    rows, call_ids, positions = [], set(), set()
    for index, raw in enumerate(requests, 1):
        loc = f"request_{index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{loc}_must_be_object")
        row = {field: _label(raw, field, loc) for field in ("call_id", "domain", "segment", "purpose", "source")}
        if row["call_id"] in call_ids:
            raise ValueError(f"{loc}_duplicate_call_id")
        call_ids.add(row["call_id"])
        for field in ("prompt_tokens", "cached_tokens"):
            row[field] = _counter(raw, field, loc)
        row["sequence"] = _counter(raw, "sequence", loc, positive=True)
        p, c = row["prompt_tokens"], row["cached_tokens"]
        if p is not None and c is not None and c > p:
            raise ValueError(f"{loc}_cached_tokens_exceeds_prompt_tokens")
        if row["sequence"] is not None:
            position = (row["domain"], row["segment"], row["sequence"])
            if position in positions:
                raise ValueError(f"{loc}_duplicate_segment_sequence")
            positions.add(position)
        rows.append(row)
    segments = {}
    for index, raw in enumerate(declarations, 1):
        loc = f"segment_declaration_{index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{loc}_must_be_object")
        key = (_label(raw, "domain", loc), _label(raw, "segment", loc))
        if key in segments:
            raise ValueError(f"{loc}_duplicate_segment_declaration")
        flags = raw.get("confirmations", {})
        if not isinstance(flags, dict):
            raise ValueError(f"{loc}_confirmations_must_be_object")
        for flag in CONFIRMATIONS:
            if flag in flags and flags[flag] is not None and type(flags[flag]) is not bool:
                raise ValueError(f"{loc}_{flag}_must_be_boolean_or_null")
        segments[key] = {flag: flags.get(flag) for flag in CONFIRMATIONS}
    return rows, segments


def summarize(rows: list[dict]) -> dict:
    """有缺失时，命中率仅对同时提供两项计数的子集计算并显式报告覆盖率。"""
    paired = [r for r in rows if r["prompt_tokens"] is not None and r["cached_tokens"] is not None]
    prompt = sum(r["prompt_tokens"] for r in paired)
    cached = sum(r["cached_tokens"] for r in paired)
    known_prompt = sum(r["prompt_tokens"] for r in rows if r["prompt_tokens"] is not None)
    return {
        "request_count": len(rows),
        "complete_counter_pair_count": len(paired),
        "missing_prompt_count": sum(r["prompt_tokens"] is None for r in rows),
        "missing_cached_count": sum(r["cached_tokens"] is None for r in rows),
        "counter_pair_request_coverage": len(paired) / len(rows) if rows else None,
        "known_prompt_tokens_total": known_prompt,
        "paired_prompt_tokens": prompt,
        "paired_cached_tokens": cached,
        "paired_uncached_tokens": prompt - cached,
        "paired_hit_rate": cached / prompt if prompt else None,
        "paired_share_of_known_prompt_tokens": prompt / known_prompt if known_prompt else None,
        "all_requests_have_both_counters": bool(rows) and len(paired) == len(rows),
    }


def analyze_segment(rows: list[dict], flags: dict | None) -> dict:
    """计算明确前提下的条件性数学下限，保留负 residual 以揭示前提矛盾。"""
    result = {
        "domain": rows[0]["domain"], "segment": rows[0]["segment"],
        "coverage": summarize(rows), "confirmations": flags or {},
        "status": "floor_not_computed", "reasons": [],
        "prompt_sum_for_floor": None, "conditional_uncached_floor_tokens": None,
        "conditional_ideal_hit_rate": None, "observed_uncached_tokens": None,
        "signed_residual_tokens": None, "signed_residual_percentage_points": None,
        "residual_attribution": "not_attributed_to_prefix_churn",
    }
    reasons = result["reasons"]
    for flag in CONFIRMATIONS:
        if not flags or flags.get(flag) is not True:
            reasons.append(f"unconfirmed_{flag}")
    if len({(r["purpose"], r["source"]) for r in rows}) != 1:
        reasons.append("mixed_purpose_or_source_within_segment")
    if any(r["sequence"] is None for r in rows):
        reasons.append("missing_sequence")
    elif sorted(r["sequence"] for r in rows) != list(range(1, len(rows) + 1)):
        reasons.append("sequence_not_complete_from_one")
    if any(r["prompt_tokens"] is None for r in rows):
        reasons.append("missing_prompt_counter")
    if reasons:
        return result
    ordered = sorted(rows, key=lambda r: r["sequence"])
    prompts = [r["prompt_tokens"] for r in ordered]
    if any(a > b for a, b in zip(prompts, prompts[1:])):
        reasons.append("prompt_decreased_contradicts_append_only")
        result["status"] = "confirmations_inconsistent"
        return result
    total, floor = sum(prompts), prompts[-1]
    result.update(prompt_sum_for_floor=total, conditional_uncached_floor_tokens=floor,
                  conditional_ideal_hit_rate=1 - floor / total if total else None,
                  status="conditional_floor_only")
    if any(r["cached_tokens"] is None for r in ordered):
        reasons.append("missing_cached_counter_residual_unknown")
        return result
    uncached = sum(r["prompt_tokens"] - r["cached_tokens"] for r in ordered)
    residual = uncached - floor
    result.update(observed_uncached_tokens=uncached, signed_residual_tokens=residual,
                  signed_residual_percentage_points=100 * residual / total if total else None,
                  status="conditional_accounting_complete")
    if ordered[0]["cached_tokens"] > 0:
        reasons.append("first_request_cached_contradicts_cold_start")
    if any(r["cached_tokens"] > prev["prompt_tokens"] for prev, r in zip(ordered, ordered[1:])):
        reasons.append("cached_exceeds_previous_input_recheck_cache_credit")
    if residual < 0:
        reasons.append("negative_residual_recheck_assumptions_or_counters")
    if reasons:
        result["status"] = "confirmations_inconsistent"
    return result


def analyze(requests: list[dict], declarations: list[dict]) -> dict:
    rows, flags = validate(requests, declarations)
    groups, segments = defaultdict(list), defaultdict(list)
    for row in rows:
        groups[(row["domain"], row["purpose"], row["source"])].append(row)
        segments[(row["domain"], row["segment"])].append(row)
    return {
        "ok": True, "schema_version": 1,
        "basis": "provided_counters_only_not_live_validation",
        "notes": ["Unknown counters are not zero; ratios describe only paired-counter rows.",
                  "Domain/source groups are separate; pooled tokens are not cross-model cost equivalence.",
                  "Floors are conditional on explicit declarations, not measured cache opportunities.",
                  "Residual includes policy/routing/expiry/accounting effects and does not prove prefix churn."],
        "coverage": summarize(rows),
        "groups": [{"domain": key[0], "purpose": key[1], "source": key[2], **summarize(group)}
                   for key, group in sorted(groups.items())],
        "segments": [analyze_segment(group, flags.get(key)) for key, group in sorted(segments.items())],
        "declared_segments_without_requests": [{"domain": key[0], "segment": key[1]}
                                               for key in sorted(flags.keys() - segments.keys())],
    }


class StdoutParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid_command_line")


def main(argv: list[str] | None = None) -> int:
    parser = StdoutParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="JSON/JSONL文件路径；- 表示stdin")
    try:
        args = parser.parse_args(argv)
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        requests, declarations = parse_input(raw)
        report = analyze(requests, declarations)
        code = 0
    except (ValueError, OSError, UnicodeError) as exc:
        report = {"ok": False, "error": str(exc) if isinstance(exc, ValueError) else "input_read_failed"}
        code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
