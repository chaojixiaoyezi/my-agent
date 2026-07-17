from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class CompactLatestPackageReport:
    package: Path | None
    load_errors: list[dict[str, object]]


def task_compact_payload(compact_root: Path) -> dict[str, Any]:
    rollup = compact_root / "task_rollup.json"
    rollup_md = compact_root / "task_rollup.md"
    latest_report = compact_latest_package_report(compact_root)
    latest = latest_report.package
    return {
        "exists": compact_root.exists(),
        "task_rollup_json": str(rollup) if rollup.exists() else "",
        "task_rollup_markdown": str(rollup_md) if rollup_md.exists() else "",
        "latest_package": str(latest) if latest else "",
        "continue_packet": str(latest / "continue_packet.json") if latest and (latest / "continue_packet.json").exists() else "",
        "work_state_snapshot": str(latest / "work_state_snapshot.json") if latest and (latest / "work_state_snapshot.json").exists() else "",
        "load_errors": latest_report.load_errors,
    }


def compact_read_paths(item: dict[str, Any]) -> list[str]:
    compact = item.get("compact") if isinstance(item.get("compact"), dict) else {}
    return [
        str(compact.get("task_rollup_json") or ""),
        str(compact.get("continue_packet") or ""),
        str(compact.get("work_state_snapshot") or ""),
    ]


def compact_latest_package(compact_root: Path) -> Path | None:
    return compact_latest_package_report(compact_root).package


def compact_latest_package_report(compact_root: Path) -> CompactLatestPackageReport:
    pointed = _latest_pointer_package_report(compact_root)
    linked = _latest_symlink_package_report(compact_root)
    return CompactLatestPackageReport(
        package=pointed.package or linked.package,
        load_errors=[*pointed.load_errors, *linked.load_errors],
    )


def _latest_pointer_package_report(compact_root: Path) -> CompactLatestPackageReport:
    latest_pointer = compact_root / "latest.txt"
    if not latest_pointer.exists():
        return CompactLatestPackageReport(None, [])
    try:
        name = latest_pointer.read_text(encoding="utf-8").strip()
        candidate = _resolved_compact_package(compact_root, compact_root / name)
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        return CompactLatestPackageReport(None, [_compact_ref_error(latest_pointer, exc, "home_runtime_compact_refs.latest_pointer")])
    return CompactLatestPackageReport(candidate, [])


def _latest_symlink_package_report(compact_root: Path) -> CompactLatestPackageReport:
    latest_link = compact_root / "latest"
    if not (latest_link.exists() or latest_link.is_symlink()):
        return CompactLatestPackageReport(None, [])
    try:
        resolved = _resolved_compact_package(compact_root, latest_link)
    except (OSError, RuntimeError, ValueError) as exc:
        return CompactLatestPackageReport(None, [_compact_ref_error(latest_link, exc, "home_runtime_compact_refs.latest_link")])
    return CompactLatestPackageReport(resolved, [])


# LLM: compact package refs are read by host runtime; task-authored pointers must never escape their compact root.
# 函数用途: 解析并校验 latest 指针，只接受当前根下的直属 compact_NNNN 目录。
def _resolved_compact_package(compact_root: Path, candidate: Path) -> Path:
    if compact_root.is_symlink() or compact_root.parent.is_symlink():
        raise ValueError("compact root cannot use symbolic links")
    root = compact_root.expanduser().resolve(strict=False)
    resolved = candidate.expanduser().resolve(strict=False)
    resolved.relative_to(root)
    if resolved.parent != root:
        raise ValueError(f"compact package must be a direct child of compact root: {resolved}")
    from .task_compact_rollup import compact_index_from_name

    if compact_index_from_name(resolved.name) <= 0:
        raise ValueError(f"invalid compact package name: {resolved.name}")
    if not resolved.is_dir():
        raise ValueError(f"latest package not found: {resolved.name}")
    return resolved


def _compact_ref_error(path: Path, exc: BaseException, context: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


__all__ = ["compact_latest_package", "compact_latest_package_report", "compact_read_paths", "task_compact_payload"]
