# LLM: Identity store keeps provider lookup separate from owner home contents.
# 模块用途: 写入/查询 provider 身份索引和 canonical user 资料目录，供多通道恢复 owner home。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .home_layout import MyAgentHomePaths


@dataclass(frozen=True)
class ProviderIdentityRecord:
    provider: str
    provider_subject_id: str
    owner_kind: str
    owner_id: str
    canonical_user_id: str = ""
    display_name: str = ""
    owner_home_path: str = ""

    @property
    def owner_home(self) -> Path:
        if self.owner_home_path:
            return Path(self.owner_home_path)
        if self.provider == "local" and self.owner_kind == "main":
            return Path("owners") / "local" / "main"
        bucket = "groups" if self.owner_kind == "group" else "users"
        return Path("owners") / "providers" / _safe_segment(self.provider) / bucket / _safe_segment(self.owner_id)

    def to_dict(self, root: Path | None = None) -> dict[str, str]:
        owner_home = self.owner_home if root is None else root / self.owner_home
        return {
            "schema_version": "provider-identity.v1",
            "provider": _safe_segment(self.provider),
            "provider_subject_id": _safe_segment(self.provider_subject_id),
            "owner_kind": _safe_segment(self.owner_kind),
            "owner_id": _safe_segment(self.owner_id),
            "owner_home": str(owner_home),
            "canonical_user_id": _safe_segment(self.canonical_user_id) if self.canonical_user_id else "",
            "display_name": self.display_name,
            "updated_at": _now_iso(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProviderIdentityRecord:
        return cls(
            provider=str(payload.get("provider") or ""),
            provider_subject_id=str(payload.get("provider_subject_id") or ""),
            owner_kind=str(payload.get("owner_kind") or "user"),
            owner_id=str(payload.get("owner_id") or ""),
            canonical_user_id=str(payload.get("canonical_user_id") or ""),
            display_name=str(payload.get("display_name") or ""),
            owner_home_path=str(payload.get("owner_home") or ""),
        )


def link_provider_identity(home: MyAgentHomePaths, record: ProviderIdentityRecord) -> Path:
    path = _provider_index_path(home, record.provider)
    append_jsonl(path, record.to_dict(home.root), sort_keys=True)
    return path


def lookup_provider_identity(home: MyAgentHomePaths, *, provider: str, provider_subject_id: str) -> ProviderIdentityRecord | None:
    wanted = _safe_segment(provider_subject_id)
    latest: ProviderIdentityRecord | None = None
    path = _provider_index_path(home, provider)
    if not path.exists():
        return None
    for row in path.read_text(encoding="utf-8").splitlines():
        payload = _json_object(row)
        if _safe_segment(payload.get("provider_subject_id")) == wanted:
            latest = ProviderIdentityRecord.from_dict(payload)
    return latest


def ensure_canonical_user_profile(home: MyAgentHomePaths, canonical_user_id: str, *, display_name: str = "") -> Path:
    canonical_id = _safe_segment(canonical_user_id)
    profile = home.canonical_users_dir / canonical_id / "profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    if not profile.exists():
        payload = {
            "schema_version": "canonical-user.v1",
            "canonical_user_id": canonical_id,
            "display_name": display_name,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        profile.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return profile


def _provider_index_path(home: MyAgentHomePaths, provider: str) -> Path:
    return home.provider_identity_dir / f"{_safe_segment(provider)}.jsonl"


def _json_object(row: str) -> dict[str, Any]:
    try:
        payload = json.loads(row)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in text)
    return result.strip(".-_/") or "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ProviderIdentityRecord",
    "ensure_canonical_user_profile",
    "link_provider_identity",
    "lookup_provider_identity",
]
