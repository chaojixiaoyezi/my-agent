#!/usr/bin/env python3
"""B6 只读证据索引器: 把隔离 MY_AGENT_HOME 的 Memory Curator 证据归一化为脱敏 JSON 索引。

角色: B6 preflight 的证据收集器(只归一化, 不裁决)。只读解析调用者显式指定的
隔离 owner home(或其脱敏副本), 逐 reason 汇总 curator run 生命周期事实
(state/cursor/lease/run/daily/candidate/audit), 输出为脱敏 JSON 索引。

冻结合同(群 seq1379/1382/1385/1389):
- 只读: 不写被测 home, 不执行其中代码, 不开网络, 不 import 被测代码。
- 脱敏: 字段白名单直出, 不读正文再正则替换; 正文/tool args/raw response/密钥永不输出。
- 不裁决: 只输出索引/解析状态(missing/parse_error/empty/incomplete), 不输出通过/失败。
- 不覆盖: 不改写、不删除原始证据; 本产物不替代原始 evidence。
- 原子写: --output 同目录临时文件 + fsync + replace, 失败不留半成品。
- 路径隔离: --home 与 --output 必须 resolve 后独立; 拒绝默认生产 home 及其
  子路径/等价 symlink; --output 父目录不得位于 --home 内。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = "b6-collector/1"
PARSER_VERSION = "1"

# 默认生产 home: resolve 后与这些路径相等、为其子路径或为其祖先的 --home 一律拒绝。
DEFAULT_PRODUCTION_HOMES: tuple[str, ...] = (
    "/root/.my-agent",
    os.path.join(os.path.expanduser("~"), ".my-agent"),
)

# 白名单键: 允许从证据行输出的字段(仅稳定元数据/ID, 不含正文)。
_RUN_FIELDS = (
    "run_id",
    "lease_id",
    "status",
    "reason",
    "provider",
    "model",
    "started_at",
    "finished_at",
    "phase",
    "processed_messages",
    "processed_audit_events",
    "daily_events",
    "candidates",
    "promoted",
    "failure_code",
    "schema_version",
    "record_id",
)
_CANDIDATE_FIELDS = (
    "candidate_id",
    "status",
    "scope",
    "promotion_mode",
    "proposed_action",
    "promotion_target",
    "origin",
    "confidence",
)
_STATE_FIELDS = (
    "lease_id",
    "run_id",
    "reason",
    "last_processed_audit_event_id",
    "processed_messages",
    "processed_audit_events",
    "candidate_count",
    "daily_event_count",
    "last_daily_finalize_date",
    "committed_at",
)
_STATE_LIST_FIELDS = ("pending_reasons",)

# 允许输出的 cursor/ref 值形态: 短 ID, 防止正文/长内容混入。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,80}$")

_CANDIDATE_ID_CAP = 500


class CliError(RuntimeError):
    """CLI 合同错误(路径隔离/默认 home 拒绝等), 以 RC2 退出且不写输出。"""


# ---------------------------------------------------------------- 路径安全

def _reject_default_production_home(home: Path) -> None:
    """resolve 后拒绝默认生产 home 及其子路径/祖先/等价 symlink。"""
    resolved = home.resolve()
    for default in DEFAULT_PRODUCTION_HOMES:
        d = Path(default).resolve()
        if resolved == d:
            raise CliError(f"--home 指向默认生产 home: {resolved}")
        if d in resolved.parents:
            raise CliError(f"--home 位于默认生产 home 之下: {resolved}")
        if resolved in d.parents:
            raise CliError(f"--home 是默认生产 home 的祖先(会暴露生产数据): {resolved}")


def _validate_output(output: Path, home: Path) -> None:
    """--output 父目录须已存在且 resolve 后不在 --home 内; symlink 越界一律拒绝。"""
    home_resolved = home.resolve()
    parent = output.parent
    if not parent.exists() or not parent.is_dir():
        raise CliError(f"--output 父目录不存在或不是目录: {parent}")
    parent_resolved = parent.resolve()
    if parent_resolved == home_resolved or home_resolved in parent_resolved.parents:
        raise CliError(f"--output 父目录位于 --home 内: {parent_resolved}")
    if output.is_symlink():
        target = output.resolve()
        if target == home_resolved or home_resolved in target.parents:
            raise CliError(f"--output 文件 symlink 越界进入 --home: {target}")


# ---------------------------------------------------------------- 读取

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_lines(path: Path) -> tuple[list[dict[str, object]], list[int]]:
    """逐行读 JSONL; 坏行记录行号(fail-closed), 好行仍参与汇总。"""
    rows: list[dict[str, object]] = []
    bad: list[int] = []
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad.append(index)
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows, bad


def _read_json_file(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("state.json 顶层不是 JSON object")
    return payload


def _pick(row: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {key: row[key] for key in keys if key in row}


def _safe_id_values(value: object) -> list[str]:
    """cursor/lease 等 dict 值只保留 ID 形态的字符串(白名单直出, 防正文)。"""
    if not isinstance(value, dict):
        return []
    out: list[str] = []
    for key, item in value.items():
        if isinstance(item, str) and _ID_PATTERN.match(item):
            out.append(f"{key}={item}")
    return out


# ---------------------------------------------------------------- 汇总

def _scan_input(path: Path) -> dict[str, object]:
    """单文件扫描: 区分 missing/empty/ok/parse_error。"""
    if not path.exists():
        return {"status": "missing", "sha256": None, "lines": 0, "bad_lines": []}
    if path.is_dir():
        return {"status": "missing", "sha256": None, "lines": 0, "bad_lines": []}
    rows, bad = _read_json_lines(path)
    status = "ok"
    if bad:
        status = "parse_error"
    elif not rows:
        status = "empty"
    return {
        "status": status,
        "sha256": _sha256(path),
        "lines": len(rows),
        "bad_lines": bad,
    }


def _scan_jsonl_dir(dir_path: Path) -> tuple[list[Path], dict[str, object]]:
    if not dir_path.exists() or not dir_path.is_dir():
        return [], {"status": "missing", "files": 0, "sha256": None, "lines": 0, "bad_lines": []}
    paths = sorted(dir_path.glob("*.jsonl"))
    total_lines = 0
    bad_lines: list[int] = []
    digests: list[str] = []
    for path in paths:
        rows, bad = _read_json_lines(path)
        total_lines += len(rows)
        bad_lines.extend((path.name, line) for line in bad)
        digests.append(f"{path.name}:{_sha256(path)}")
    if not paths:
        return [], {"status": "empty", "files": 0, "sha256": None, "lines": 0, "bad_lines": []}
    status = "parse_error" if bad_lines else "ok"
    return paths, {
        "status": status,
        "files": len(paths),
        "sha256": digests,
        "lines": total_lines,
        "bad_lines": bad_lines,
    }


def _summarize_state(state_path: Path) -> dict[str, object]:
    if not state_path.exists():
        return {"present": False, "status": "missing"}
    try:
        state = _read_json_file(state_path)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        return {"present": True, "status": "parse_error", "error": type(exc).__name__}
    summary: dict[str, object] = {"present": True, "status": "ok"}
    summary.update(_pick(state, _STATE_FIELDS))
    for key in _STATE_LIST_FIELDS:
        value = state.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            summary[key] = value
    # cursor 只输出 ID 形态值。
    cursors = _safe_id_values(state.get("per_thread_cursors"))
    summary["per_thread_cursors"] = cursors
    summary["per_thread_cursor_count"] = len(cursors)
    return summary


def _summarize_runs(run_paths: list[Path]) -> dict[str, object]:
    by_reason: dict[str, dict[str, object]] = {}
    for path in run_paths:
        rows, _ = _read_json_lines(path)
        for row in rows:
            reason = row.get("reason")
            if not isinstance(reason, str) or not reason:
                reason = "<missing>"
            entry = by_reason.setdefault(
                reason,
                {
                    "runs": 0,
                    "status_counts": {},
                    "phases": [],
                    "providers": [],
                    "models": [],
                    "failure_codes": {},
                    "cursor_before": [],
                    "cursor_after": [],
                    "processed_messages_total": 0,
                    "processed_audit_events_total": 0,
                    "promoted_total": 0,
                    "candidates_total": 0,
                    "daily_events_total": 0,
                },
            )
            entry["runs"] = int(entry["runs"]) + 1
            status = row.get("status")
            if isinstance(status, str):
                counts = entry["status_counts"]
                counts[status] = int(counts.get(status, 0)) + 1
            # 行字段是单数(phase/provider/model), 聚合到复数键。
            for source, target in (
                ("phase", "phases"),
                ("provider", "providers"),
                ("model", "models"),
            ):
                value = row.get(source)
                if isinstance(value, str) and value not in entry[target]:
                    entry[target].append(value)
            failure_code = row.get("failure_code")
            if isinstance(failure_code, str) and failure_code:
                codes = entry["failure_codes"]
                codes[failure_code] = int(codes.get(failure_code, 0)) + 1
            # 游标只输出 ID 形态值(白名单直出, 防正文), 供 before/after 推进核对。
            for cursor_key in ("cursor_before", "cursor_after"):
                for item in _safe_id_values(row.get(cursor_key)):
                    if item not in entry[cursor_key]:
                        entry[cursor_key].append(item)
            for numeric, source in (
                ("processed_messages_total", "processed_messages"),
                ("processed_audit_events_total", "processed_audit_events"),
                ("promoted_total", "promoted"),
                ("candidates_total", "candidates"),
                ("daily_events_total", "daily_events"),
            ):
                value = row.get(source)
                if isinstance(value, (int, float)):
                    entry[numeric] = int(entry[numeric]) + int(value)
    return by_reason


def _summarize_candidates(candidates_path: Path) -> dict[str, object]:
    scan = _scan_input(candidates_path)
    if scan["status"] == "missing":
        return {"present": False, "count": 0, "by_status": {}}
    rows, _ = _read_json_lines(candidates_path)
    by_status: dict[str, int] = {}
    ids: list[str] = []
    for row in rows:
        status = row.get("status")
        if isinstance(status, str):
            by_status[status] = by_status.get(status, 0) + 1
        candidate_id = row.get("candidate_id")
        if isinstance(candidate_id, str) and len(ids) < _CANDIDATE_ID_CAP:
            ids.append(candidate_id)
    return {
        "present": True,
        "count": len(rows),
        "by_status": by_status,
        "candidate_ids": ids,
        "ids_truncated": len(rows) > _CANDIDATE_ID_CAP,
        "scan": scan,
    }


def _summarize_audit(audit_paths: list[Path], state_summary: dict[str, object]) -> dict[str, object]:
    events = 0
    event_ids: set[str] = set()
    for path in audit_paths:
        rows, _ = _read_json_lines(path)
        events += len(rows)
        for row in rows:
            event_id = row.get("event_id")
            if isinstance(event_id, str):
                event_ids.add(event_id)
    target = state_summary.get("last_processed_audit_event_id")
    cursor_found = isinstance(target, str) and bool(target) and target in event_ids
    return {
        "events": events,
        "files": len(audit_paths),
        "cursor_found": cursor_found,
    }


def _count_md_files(dir_path: Path) -> dict[str, object]:
    if not dir_path.exists() or not dir_path.is_dir():
        return {"status": "missing", "present": False, "count": 0, "files": []}
    paths = sorted(dir_path.glob("*.md"))
    return {
        "status": "ok",
        "present": True,
        "count": len(paths),
        "files": [path.name for path in paths],
    }


# ---------------------------------------------------------------- 主流程

def collect_evidence(home: Path, output: Path) -> dict[str, object]:
    """只读收集隔离 home 的 curator 证据并原子写出脱敏索引; 不裁决、不覆盖。"""
    home_resolved = home.resolve()
    # 安全拒绝先于任何文件访问: 默认生产 home(含 symlink/祖先/子路径)永不触碰。
    _reject_default_production_home(home_resolved)
    if not home.exists() or not home.is_dir():
        raise FileNotFoundError(f"--home 不存在或不是目录: {home}")
    _validate_output(output, home_resolved)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "home": str(home_resolved),
        "inputs": {},
        "summary": {},
        "incomplete": [],
        "note": "证据索引/归一化产物; 不替代原始 evidence; 不对未观测 trigger 作结论",
    }

    state_path = home_resolved / "memory" / "curator" / "state.json"
    state_summary = _summarize_state(state_path)
    runs_paths, runs_scan = _scan_jsonl_dir(home_resolved / "memory" / "curator" / "runs")
    daily_paths, daily_scan = _scan_jsonl_dir(home_resolved / "memory" / "daily")
    candidates_path = home_resolved / "memory" / "candidates.jsonl"
    long_term_path = home_resolved / "memory" / "long_term" / "memory.jsonl"
    long_term_scan = _scan_json(long_term_path)
    audit_paths, audit_scan = _scan_jsonl_dir(home_resolved / "audit")

    result["inputs"] = {
        "state": {"relpath": "memory/curator/state.json", **_scan_json(state_path)},
        "runs": {"relpath": "memory/curator/runs", **runs_scan},
        "daily": {"relpath": "memory/daily", **daily_scan},
        "candidates": {"relpath": "memory/candidates.jsonl", **_scan_input(candidates_path)},
        "long_term": {"relpath": "memory/long_term/memory.jsonl", **_scan_input(long_term_path)},
        "audit": {"relpath": "audit", **audit_scan},
        "lessons": {"relpath": "memory/lessons", **_count_md_files(home_resolved / "memory" / "lessons")},
    }

    result["summary"] = {
        "state": state_summary,
        "runs_by_reason": _summarize_runs(runs_paths),
        "daily": {"present": daily_scan["status"] != "missing", "count": daily_scan["lines"]},
        "candidates": _summarize_candidates(candidates_path),
        "long_term": {"present": long_term_path.exists(), "count": long_term_scan["lines"]},
        "lessons": _count_md_files(home_resolved / "memory" / "lessons"),
        "audit": _summarize_audit(audit_paths, state_summary),
    }

    # incomplete: 缺失或解析失败的输入, 如实标记; 0 计数不算缺失。
    for name, scan in result["inputs"].items():
        if scan.get("status") in ("missing", "parse_error"):
            result["incomplete"].append(name)

    _atomic_write_json(output, result)
    return result


def _scan_json(path: Path) -> dict[str, object]:
    """单 JSON 文件扫描(状态/哈希/行数), 区分 missing/ok/parse_error。"""
    if not path.exists() or path.is_dir():
        return {"status": "missing", "sha256": None, "lines": 0, "bad_lines": []}
    try:
        _read_json_file(path)
        return {"status": "ok", "sha256": _sha256(path), "lines": 1, "bad_lines": []}
    except (ValueError, json.JSONDecodeError, OSError):
        return {"status": "parse_error", "sha256": _sha256(path), "lines": 0, "bad_lines": [1]}


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """同目录临时文件 + fsync + 原子 replace; 失败清理临时文件, 不留半成品。"""
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".collect-", suffix=".tmp", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collect_curator_evidence",
        description="只读收集隔离 MY_AGENT_HOME 的 Memory Curator 证据为脱敏 JSON 索引(不裁决)。",
    )
    parser.add_argument("--home", required=True, help="隔离 owner home 或脱敏副本(已存在目录)")
    parser.add_argument("--output", required=True, help="输出 JSON 索引路径(父目录须已存在且不在 --home 内)")
    args = parser.parse_args(argv)

    home = Path(args.home)
    if not home.exists() or not home.is_dir():
        print(f"collect_curator_evidence: --home 不存在或不是目录: {home}", file=sys.stderr)
        return 2
    output = Path(args.output)
    try:
        collect_evidence(home, output)
    except CliError as exc:
        print(f"collect_curator_evidence: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"collect_curator_evidence: 读取失败: {exc}", file=sys.stderr)
        return 3
    print(f"collect_curator_evidence: 索引已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
