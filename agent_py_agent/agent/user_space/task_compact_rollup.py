# LLM: Task compact rollups summarize child run recovery refs without replacing child facts.
# 模块用途: 给一个 task 下所有 agent/run 生成任务级 compact 汇总，父代理恢复时先读它再按需读子代理细节。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .compact_layout import ensure_compact_package


# LLM: TaskCompactRollupResult returns the files produced by a task-level compact rollup.
# 类用途: 保存 task rollup 的主要路径和子运行数量，供调用方写日志或测试断言。
@dataclass(frozen=True)
class TaskCompactRollupResult:
    task_workspace: Path
    compact_root: Path
    rollup_json: Path
    rollup_markdown: Path
    compact_package_dir: Path
    child_count: int


# LLM: sync_task_compact_rollup creates a task-level summary without mutating child run state.
# 函数用途: 汇总 task 下子代理状态、产物引用和恢复入口，并写入标准 compact 包。
def sync_task_compact_rollup(task_workspace: str | Path, *, compact_index: int | None = None) -> TaskCompactRollupResult:
    task_root = Path(task_workspace)
    compact_root = task_root / "compact"
    index = compact_index if compact_index is not None else _next_compact_index(compact_root / "compact_ledger.jsonl")
    package = ensure_compact_package(compact_root, compact_index=index, scope="task")
    child_runs = _child_run_records(task_root)
    rollup_json = compact_root / "task_rollup.json"
    rollup_md = compact_root / "task_rollup.md"
    rollup = _rollup_payload(task_root, child_runs, package.package_dir, rollup_json=rollup_json)
    _write_json(rollup_json, rollup)
    rollup_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    _write_json(package.work_state_snapshot_json, _work_state_payload(rollup))
    _write_json(package.refs_json, _refs_payload(rollup_json, rollup_md, child_runs))
    _write_json(package.continue_packet_json, _continue_packet_payload(rollup))
    package.handoff_summary_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    append_jsonl(
        compact_root / "rollup_ledger.jsonl",
        {
            "schema_version": "task-compact-rollup-event.v1",
            "task_workspace": str(task_root),
            "rollup_json": str(rollup_json),
            "compact_package": str(package.package_dir),
            "child_count": len(child_runs),
            "updated_at": _now_iso(),
        },
        sort_keys=True,
    )
    return TaskCompactRollupResult(
        task_workspace=task_root,
        compact_root=compact_root,
        rollup_json=rollup_json,
        rollup_markdown=rollup_md,
        compact_package_dir=package.package_dir,
        child_count=len(child_runs),
    )


# LLM: _child_run_records collects lightweight child state rows from agent state files.
# 函数用途: 从 task/agents/*/state.json 提取状态、进度、摘要、产物引用和阻塞原因。
def _child_run_records(task_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    agents_root = task_root / "agents"
    for state_path in sorted(agents_root.glob("*/state.json")):
        payload = _read_json(state_path)
        run_id = str(payload.get("id") or payload.get("run_id") or state_path.parent.name)
        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        system_tree = attrs.get("system_tree") if isinstance(attrs.get("system_tree"), dict) else {}
        rows.append(
            {
                "run_id": run_id,
                "status": str(payload.get("status") or system_tree.get("status") or ""),
                "progress": _safe_float(payload.get("progress") or system_tree.get("progress")),
                "summary": str(payload.get("latest_summary") or system_tree.get("latest_summary") or "")[:500],
                "refs": {
                    "state": str(state_path),
                    "compact": str(state_path.parent / "compactions"),
                    "summary": str(state_path.parent / "summary.md"),
                    "final_report": str(state_path.parent / "final_report.md"),
                    "artifact_manifest": str(state_path.parent / "artifacts" / "manifest.jsonl"),
                },
                "artifact_refs": _list_strings(payload.get("artifact_refs") or system_tree.get("artifact_refs")),
                "blockers": _list_strings(payload.get("blockers") or system_tree.get("blockers")),
            }
        )
    return rows


# LLM: _rollup_payload builds the machine-readable task compact summary.
# 函数用途: 生成 task_rollup.json 的主体数据，包括状态分组和产物引用汇总。
def _rollup_payload(
    task_root: Path,
    child_runs: list[dict[str, object]],
    package_dir: Path,
    *,
    rollup_json: Path,
) -> dict[str, object]:
    state = _read_json(task_root / "state.json")
    status_groups = _status_groups(child_runs)
    return {
        "schema_version": "task-compact-rollup.v1",
        "task_id": str(state.get("task_id") or task_root.name),
        "task_workspace": str(task_root),
        "status": str(state.get("status") or ""),
        "progress": _safe_float(state.get("progress")),
        "primary_run_id": str(state.get("primary_run_id") or ""),
        "compact_package": str(package_dir),
        "rollup_json": str(rollup_json),
        "child_runs": child_runs,
        "child_count": len(child_runs),
        "status_counts": _status_counts(child_runs),
        "completed_run_ids": status_groups["completed"],
        "pending_run_ids": status_groups["pending"],
        "blocked_run_ids": status_groups["blocked"],
        "artifact_refs": _unique_strings(
            ref
            for row in child_runs
            for ref in _list_strings(row.get("artifact_refs"))
        ),
        "updated_at": _now_iso(),
    }


# LLM: _work_state_payload narrows rollup data to the standard compact work-state snapshot.
# 函数用途: 生成 compact/work_state_snapshot.json 的任务级恢复摘要。
def _work_state_payload(rollup: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "work-state-snapshot.v1",
        "scope": "task",
        "task_id": rollup.get("task_id", ""),
        "status": rollup.get("status", ""),
        "progress": rollup.get("progress", 0.0),
        "child_count": rollup.get("child_count", 0),
        "status_counts": rollup.get("status_counts", {}),
        "pending_run_ids": rollup.get("pending_run_ids", []),
        "blocked_run_ids": rollup.get("blocked_run_ids", []),
        "updated_at": rollup.get("updated_at", ""),
    }


# LLM: _refs_payload lists only recovery references, not full child artifacts.
# 函数用途: 生成 compact/refs.json，指向 task rollup 和子 run state。
def _refs_payload(rollup_json: Path, rollup_md: Path, child_runs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "compact-refs.v1",
        "refs": [
            {"kind": "task_rollup", "path": str(rollup_json)},
            {"kind": "task_rollup_markdown", "path": str(rollup_md)},
            *[
                {"kind": "child_run_state", "run_id": row.get("run_id", ""), "path": row["refs"]["state"]}
                for row in child_runs
                if isinstance(row.get("refs"), dict)
            ],
        ],
    }


# LLM: _continue_packet_payload tells the next worker how to resume from the task rollup.
# 函数用途: 生成 compact/continue_packet.json，列出先读什么和未完成子任务。
def _continue_packet_payload(rollup: dict[str, object]) -> dict[str, object]:
    pending = [
        f"{row.get('run_id')}: {row.get('status')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict) and str(row.get("status") or "").upper() not in {"DONE", "COMPLETED", "SUCCEEDED"}
    ]
    return {
        "schema_version": "continue-packet.v1",
        "scope": "task",
        "next_action": "先读 task_rollup.json，再按 pending_work 读取对应 child run refs。",
        "completed_headings": [],
        "avoid_repeating": [],
        "active_refs": [
            str(rollup.get("rollup_json") or ""),
            str(rollup.get("compact_package") or ""),
            str(rollup.get("task_workspace") or ""),
        ],
        "pending_work": pending,
    }


# LLM: _rollup_markdown provides a human-readable task compact summary.
# 函数用途: 把 task rollup 渲染成 Markdown，方便父代理和人快速查看。
def _rollup_markdown(rollup: dict[str, object]) -> str:
    rows = [
        f"- {row.get('run_id')}: {row.get('status')} progress={row.get('progress')} summary={row.get('summary')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict)
    ]
    return (
        "# Task Compact Rollup\n\n"
        f"- task_id: {rollup.get('task_id', '')}\n"
        f"- status: {rollup.get('status', '')}\n"
        f"- child_count: {rollup.get('child_count', 0)}\n"
        f"- status_counts: {json.dumps(rollup.get('status_counts', {}), ensure_ascii=False, sort_keys=True)}\n"
        f"- updated_at: {rollup.get('updated_at', '')}\n\n"
        "## Child Runs\n\n"
        + ("\n".join(rows) if rows else "- 暂无")
        + "\n"
    )


# LLM: _next_compact_index appends compact packages in a stable linear sequence.
# 函数用途: 根据 ledger 非空行数量计算下一次 compact 序号。
def _next_compact_index(ledger: Path) -> int:
    if not ledger.exists():
        return 1
    return sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()) + 1


# LLM: _read_json tolerates missing or malformed child state during recovery rollups.
# 函数用途: 读取 JSON 对象；失败或非对象时返回空字典。
def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _write_json writes deterministic JSON payloads for compact artifacts.
# 函数用途: 创建父目录并写入带排序键的 UTF-8 JSON 文件。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# LLM: _list_strings normalizes optional list-like fields from child state.
# 函数用途: 从列表或元组中提取非空字符串。
def _list_strings(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


# LLM: _status_counts gives parents an aggregate view before opening child details.
# 函数用途: 统计子运行状态桶数量。
def _status_counts(child_runs: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in child_runs:
        status = _status_bucket(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


# LLM: _status_groups separates completed, pending, and blocked child run IDs.
# 函数用途: 生成子运行 ID 分组，供 rollup 和 continue packet 使用。
def _status_groups(child_runs: list[dict[str, object]]) -> dict[str, list[str]]:
    groups = {"completed": [], "pending": [], "blocked": []}
    for row in child_runs:
        run_id = str(row.get("run_id") or "")
        status = _status_bucket(row.get("status"))
        if status == "done":
            groups["completed"].append(run_id)
        else:
            groups["pending"].append(run_id)
        if status in {"blocked", "failed", "timeout"}:
            groups["blocked"].append(run_id)
    return groups


# LLM: _status_bucket maps variant runtime words into broad lifecycle buckets.
# 函数用途: 把 DONE/RUNNING/BLOCKED 等不同写法归一成通用状态桶。
def _status_bucket(value: object) -> str:
    status = str(value or "").strip().upper()
    if status in {"DONE", "COMPLETED", "SUCCEEDED", "SUCCESS"}:
        return "done"
    if status in {"BLOCKED", "WAITING_HUMAN", "WAITING_INPUT"}:
        return "blocked"
    if status in {"FAILED", "ERROR"}:
        return "failed"
    if status in {"TIMEOUT", "TIMED_OUT"}:
        return "timeout"
    if status in {"RUNNING", "IN_PROGRESS", "WORKING"}:
        return "running"
    if status in {"PENDING", "QUEUED", "CREATED"}:
        return "pending"
    return status.lower() or "unknown"


# LLM: _unique_strings deduplicates artifact refs while preserving first-seen order.
# 函数用途: 去重字符串序列，保留原始顺序。
def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


# LLM: _safe_float keeps progress parsing tolerant of missing or invalid values.
# 函数用途: 把输入转换为 float，失败时返回 0.0。
def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: _now_iso centralizes UTC timestamps for task rollup records.
# 函数用途: 返回当前 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["TaskCompactRollupResult", "sync_task_compact_rollup"]
