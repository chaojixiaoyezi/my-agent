from __future__ import annotations

# LLM: Compact layout keeps task/run/agent recovery packages structurally identical.
# 模块用途: 创建 compact_0001 这类压缩包基础文件，避免三层恢复包各自长出不同形状。
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..io import append_jsonl


# LLM: CompactPackagePaths lists every file in a task/run/agent compact package.
# 类用途: 保存 compact 包根目录、latest 指针和基础恢复文件路径。
@dataclass(frozen=True)
class CompactPackagePaths:
    root: Path
    package_dir: Path
    ledger_jsonl: Path
    latest_symlink: Path
    latest_pointer: Path
    compact_context_md: Path
    handoff_summary_md: Path
    work_state_snapshot_json: Path
    continue_packet_json: Path
    refs_json: Path
    metadata_json: Path

# LLM: CompactPackageRequest bundles compact creation metadata.
# 类用途: 保存 compact 序号、层级、分支和父 compact 引用。
@dataclass(frozen=True)
class CompactPackageRequest:
    compact_index: int
    scope: str
    branch_id: str = "main",
    parent_compact_id: str = "",


# LLM: ensure_compact_package materializes the shared compact package shape.
# 函数用途: 创建 task/run/agent 通用 compact 包、latest 指针、ledger 和 branch 元数据。
def ensure_compact_package(root: str | Path, request: CompactPackageRequest) -> CompactPackagePaths:
    paths = compact_package_paths(root, compact_index=request.compact_index)
    branch = _safe_branch_id(request.branch_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.package_dir.mkdir(parents=True, exist_ok=True)
    _write_seed_file(paths.compact_context_md, "# Compact Context\n\n")
    _write_seed_file(paths.handoff_summary_md, "# Handoff Summary\n\n")
    _write_seed_json(paths.work_state_snapshot_json, {"schema_version": "work-state-snapshot.v1", "scope": request.scope})
    _write_seed_json(paths.continue_packet_json, _continue_packet_payload(scope=request.scope))
    _write_seed_json(paths.refs_json, {"schema_version": "compact-refs.v1", "refs": []})
    _write_seed_json(
        paths.metadata_json,
        _metadata_payload(
            scope=request.scope,
            compact_index=request.compact_index,
            branch_id=branch,
            parent_compact_id=request.parent_compact_id,
        ),
    )
    _write_latest(paths)
    _update_branch_refs(paths, branch_id=branch, parent_compact_id=request.parent_compact_id)
    append_jsonl(
        paths.ledger_jsonl,
        {
            "event_type": "compact_package_initialized",
            "compact_index": int(request.compact_index),
            "compact_id": paths.package_dir.name,
            "branch_id": branch,
            "parent_compact_id": str(request.parent_compact_id or ""),
            "package_dir": str(paths.package_dir),
            "scope": request.scope,
            "created_at": _now_iso(),
        },
        sort_keys=True,
    )
    return paths


# LLM: compact_package_paths computes compact paths without touching disk.
# 函数用途: 根据 compact root 和序号返回标准文件路径集合。
def compact_package_paths(root: str | Path, *, compact_index: int) -> CompactPackagePaths:
    compact_root = Path(root)
    package_dir = compact_root / f"compact_{int(compact_index):04d}"
    return CompactPackagePaths(
        root=compact_root,
        package_dir=package_dir,
        ledger_jsonl=compact_root / "compact_ledger.jsonl",
        latest_symlink=compact_root / "latest",
        latest_pointer=compact_root / "latest.txt",
        compact_context_md=package_dir / "compact_context.md",
        handoff_summary_md=package_dir / "handoff_summary.md",
        work_state_snapshot_json=package_dir / "work_state_snapshot.json",
        continue_packet_json=package_dir / "continue_packet.json",
        refs_json=package_dir / "refs.json",
        metadata_json=package_dir / "metadata.json",
    )


# LLM: _continue_packet_payload seeds the machine-readable continuation contract.
# 函数用途: 生成空的 continue_packet 基础结构。
def _continue_packet_payload(*, scope: str) -> dict[str, object]:
    return {
        "schema_version": "continue-packet.v1",
        "scope": scope,
        "next_action": "",
        "completed_headings": [],
        "avoid_repeating": [],
        "active_refs": [],
        "pending_work": [],
    }


# LLM: _metadata_payload records compact lineage without reading package bodies.
# 函数用途: 生成 compact metadata，包括 scope、序号、分支和父包。
def _metadata_payload(*, scope: str, compact_index: int, branch_id: str, parent_compact_id: str) -> dict[str, object]:
    return {
        "schema_version": "compact-package.v1",
        "scope": scope,
        "compact_index": int(compact_index),
        "compact_id": f"compact_{int(compact_index):04d}",
        "branch_id": branch_id,
        "parent_compact_id": str(parent_compact_id or ""),
        "created_at": _now_iso(),
    }


# LLM: _write_latest points recovery to the newest compact package.
# 函数用途: 优先写 latest symlink，失败时写 latest.txt 兜底。
def _write_latest(paths: CompactPackagePaths) -> None:
    try:
        if paths.latest_symlink.exists() or paths.latest_symlink.is_symlink():
            paths.latest_symlink.unlink()
        paths.latest_symlink.symlink_to(paths.package_dir.name)
    except OSError:
        paths.latest_pointer.write_text(paths.package_dir.name + "\n", encoding="utf-8")


# LLM: _update_branch_refs tracks the current compact branch head.
# 函数用途: 更新 branches.json 和 current_branch.txt。
def _update_branch_refs(paths: CompactPackagePaths, *, branch_id: str, parent_compact_id: str) -> None:
    branches_path = paths.root / "branches.json"
    branches = _read_json_object(branches_path)
    branch_map = branches.get("branches") if isinstance(branches.get("branches"), dict) else {}
    branch_map[branch_id] = {
        "branch_id": branch_id,
        "head": paths.package_dir.name,
        "parent_compact_id": str(parent_compact_id or ""),
        "status": "active",
        "updated_at": _now_iso(),
    }
    payload = {
        "schema_version": "compact-branches.v1",
        "current_branch": branch_id,
        "branches": branch_map,
    }
    branches_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (paths.root / "current_branch.txt").write_text(branch_id + "\n", encoding="utf-8")


# LLM: _read_json_object keeps branch metadata updates tolerant of missing files.
# 函数用途: 读取 JSON object，失败时返回空字典。
def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _safe_branch_id accepts open-world branch names but removes path control characters.
# 函数用途: 把 compact branch 名称转换成安全标识。
def _safe_branch_id(value: str) -> str:
    text = str(value or "main").strip()
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in text)
    return safe.strip(".-_") or "main"


# LLM: _write_seed_file avoids overwriting an already-filled compact file.
# 函数用途: 文件不存在时写入默认 Markdown 内容。
def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


# LLM: _write_seed_json avoids overwriting an already-filled compact JSON file.
# 函数用途: 文件不存在时写入默认 JSON 内容。
def _write_seed_json(path: Path, payload: dict[str, object]) -> None:
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# LLM: _now_iso centralizes UTC timestamps for compact metadata.
# 函数用途: 返回当前 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["CompactPackagePaths", "CompactPackageRequest", "compact_package_paths", "ensure_compact_package"]
