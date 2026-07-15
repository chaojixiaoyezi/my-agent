from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# LLM: 最终用户说明只能引用这份收口瞬间的只读事实；旧模型摘要、早先 stat 结果和自然语言
# 都不是权威。快照不含宿主机绝对路径，既可安全交给模型，也不会把 owner 目录泄到 IM。
# 函数用途: 在全部收口门完成后冻结文件名、实际字节数、哈希和门状态，并给快照本身算指纹。
def attach_delivery_snapshot(
    report: dict[str, Any],
    *,
    validated: bool,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "schema_version": "delivery_snapshot.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "closeout_ok": report.get("ok") is True,
        "validated": bool(validated),
        "delivery_mode": str(report.get("delivery_mode") or "artifact"),
        "artifacts": _artifact_snapshots(report.get("artifacts")),
        "gate_states": _gate_states(report),
        "quality_advisories": _quality_advisories(report),
    }
    progress = _task_progress_snapshot(report)
    if progress:
        snapshot["task_progress"] = progress
    snapshot["snapshot_id"] = _snapshot_id(snapshot)
    report["delivery_snapshot"] = snapshot
    return snapshot


# LLM: 文件哈希按当前字节流计算；读取失败要显式留 error，不能继续沿用旧 registry 数字。
# 函数用途: 把 report 里的成功产物转成不带宿主路径的最终事实行。
def _artifact_snapshots(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        row
        for item in value
        if isinstance(item, dict) and item.get("ok") is True
        if (row := _artifact_snapshot(item)) is not None
    ]


def _artifact_snapshot(item: dict[str, Any]) -> dict[str, Any] | None:
    raw_path = str(item.get("path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    row: dict[str, Any] = {
        "artifact_id": str(item.get("artifact_id") or path.name),
        "name": path.name,
        "kind": str(item.get("kind") or "file"),
    }
    try:
        if path.is_file():
            row["size_bytes"] = path.stat().st_size
            row["sha256"] = _sha256_file(path)
        elif path.is_dir():
            row["entry_type"] = "directory"
        else:
            row["snapshot_error"] = "artifact_not_found_at_snapshot"
    except OSError as exc:
        row["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    return row


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gate_states(report: dict[str, Any]) -> dict[str, dict[str, object]]:
    states: dict[str, dict[str, object]] = {}
    for key, value in sorted(report.items()):
        if not key.endswith("_gate") or not isinstance(value, dict):
            continue
        states[key] = {
            "allowed": value.get("allowed") is True,
            "status": str(value.get("status") or ""),
        }
    return states


def _quality_advisories(report: dict[str, Any]) -> list[dict[str, str]]:
    value = report.get("quality_advisories")
    if not isinstance(value, list):
        return []
    return [
        {
            "gate": str(item.get("gate") or ""),
            "status": str(item.get("status") or ""),
        }
        for item in value
        if isinstance(item, dict)
    ]


def _task_progress_snapshot(report: dict[str, Any]) -> dict[str, object]:
    gate = report.get("task_progress_closeout_gate")
    evidence = gate.get("evidence") if isinstance(gate, dict) else None
    if not isinstance(evidence, dict):
        return {}
    return {
        "allowed": gate.get("allowed") is True,
        "status": str(gate.get("status") or ""),
        "counts": dict(evidence.get("counts")) if isinstance(evidence.get("counts"), dict) else {},
        "open_count": int(evidence.get("open_count") or 0),
    }


def _snapshot_id(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["attach_delivery_snapshot"]
