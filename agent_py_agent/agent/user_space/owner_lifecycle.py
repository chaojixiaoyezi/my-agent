from __future__ import annotations

# LLM: Owner lifecycle state is advisory metadata; it does not gate normal task execution.
# 模块用途: 在 owner home 内记录 provider/local owner 的 created/active/suspended/archived 等状态，供 doctor 和恢复流程读取。
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .owner_resolver import OwnerHomeResult


# LLM: OwnerLifecycleState is the current advisory status of an owner home.
# 类用途: 保存 owner 状态、状态文件路径和变更理由。
@dataclass(frozen=True)
class OwnerLifecycleState:
    owner_id: str
    status: str
    path: Path
    reason: str = ""

    # LLM: to_dict serializes owner lifecycle state for doctor/debug output.
    # 函数用途: 转成 JSON 友好的状态字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "status": self.status,
            "path": str(self.path),
            "reason": self.reason,
        }


# LLM: read_owner_lifecycle returns active when no lifecycle file exists.
# 函数用途: 读取 owner_status.json，缺失或损坏时使用 active。
def read_owner_lifecycle(owner: OwnerHomeResult) -> OwnerLifecycleState:
    path = _lifecycle_path(owner)
    payload = _read_json(path)
    status = str(payload.get("status") or "active")
    return OwnerLifecycleState(
        owner_id=owner.owner_id,
        status=status,
        reason=str(payload.get("reason") or ""),
        path=path,
    )


# LLM: update_owner_lifecycle writes owner status and an audit event.
# 函数用途: 更新 owner_status.json，并追加 owner audit log。
def update_owner_lifecycle(owner: OwnerHomeResult, *, status: str, reason: str = "") -> OwnerLifecycleState:
    path = _lifecycle_path(owner)
    previous = read_owner_lifecycle(owner)
    payload = {
        "schema_version": "owner-lifecycle.v1",
        "owner_id": owner.owner_id,
        "provider": owner.identity.provider,
        "owner_kind": owner.identity.owner_kind,
        "status": _safe_status(status),
        "reason": str(reason or ""),
        "previous_status": previous.status,
        "updated_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_jsonl(
        owner.home_dir / "audit_log.jsonl",
        {
            "schema_version": "owner-audit-event.v1",
            "event_type": "owner_lifecycle_updated",
            "owner_id": owner.owner_id,
            "from_status": previous.status,
            "to_status": payload["status"],
            "reason": payload["reason"],
            "updated_at": payload["updated_at"],
        },
        sort_keys=True,
    )
    return read_owner_lifecycle(owner)


# LLM: _lifecycle_path keeps lifecycle metadata inside the owner home.
# 函数用途: 返回 owner_status.json 路径。
def _lifecycle_path(owner: OwnerHomeResult) -> Path:
    return owner.home_dir / "owner_status.json"


# LLM: _safe_status keeps lifecycle labels open-world but path-safe.
# 函数用途: 清理 owner 状态字符串。
def _safe_status(value: object) -> str:
    text = str(value or "active").strip().lower()
    return "".join(char if char.isalnum() or char in {"_", "-"} else "-" for char in text).strip("-_") or "active"


# LLM: _read_json tolerates missing or malformed lifecycle files.
# 函数用途: 读取 JSON object，失败返回空字典。
def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _now_iso centralizes UTC timestamps for lifecycle metadata.
# 函数用途: 返回当前 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["OwnerLifecycleState", "read_owner_lifecycle", "update_owner_lifecycle"]
