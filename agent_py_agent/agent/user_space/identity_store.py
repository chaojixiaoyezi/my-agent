from __future__ import annotations

# LLM: Identity store keeps provider lookup separate from owner home contents.
# 模块用途: 写入/查询 provider 身份索引和 canonical user 资料目录，供多通道恢复 owner home。
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .home_layout import MyAgentHomePaths


# LLM: ProviderIdentityRecord is the provider-to-owner index row.
# 类用途: 保存外部 provider 身份、owner 归属和可选 canonical user。
@dataclass(frozen=True)
class ProviderIdentityRecord:
    provider: str
    provider_subject_id: str
    owner_kind: str
    owner_id: str
    canonical_user_id: str = ""
    display_name: str = ""
    owner_home_path: str = ""

    # LLM: owner_home derives the relative owner path when the index row omits one.
    # 函数用途: 根据 provider/kind/id 推导 owner home 路径。
    @property
    def owner_home(self) -> Path:
        if self.owner_home_path:
            return Path(self.owner_home_path)
        if self.provider == "local" and self.owner_kind == "main":
            return Path("owners") / "local" / "main"
        bucket = "groups" if self.owner_kind == "group" else "users"
        return Path("owners") / "providers" / _safe_segment(self.provider) / bucket / _safe_segment(self.owner_id)

    # LLM: to_dict serializes provider identity rows for sharded JSONL indexes.
    # 函数用途: 转成 provider_identity/<provider>.jsonl 可写入字典。
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

    # LLM: from_dict restores provider identity rows from JSONL.
    # 函数用途: 从索引字典重建 ProviderIdentityRecord。
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

# LLM: CanonicalIdentityLink records one provider identity linked to a canonical user.
# 类用途: 保存跨平台身份绑定关系和状态。
@dataclass(frozen=True)
class CanonicalIdentityLink:
    canonical_user_id: str
    provider: str
    provider_subject_id: str
    owner_id: str
    status: str = "active"
    path: Path | None = None

    # LLM: to_dict serializes canonical links for append-only ledgers.
    # 函数用途: 转成 linked_identities JSONL 行。
    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "canonical-identity-link.v1",
            "canonical_user_id": _safe_segment(self.canonical_user_id),
            "provider": _safe_segment(self.provider),
            "provider_subject_id": _safe_segment(self.provider_subject_id),
            "owner_id": str(self.owner_id or ""),
            "status": _safe_segment(self.status),
            "updated_at": _now_iso(),
        }

    # LLM: from_dict restores canonical identity link rows.
    # 函数用途: 从 linked_identities 字典重建绑定记录。
    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, path: Path | None = None) -> CanonicalIdentityLink:
        return cls(
            canonical_user_id=str(payload.get("canonical_user_id") or ""),
            provider=str(payload.get("provider") or ""),
            provider_subject_id=str(payload.get("provider_subject_id") or ""),
            owner_id=str(payload.get("owner_id") or ""),
            status=str(payload.get("status") or "active"),
            path=path,
        )

# LLM: CanonicalIdentityLinkRequest keeps link creation arguments bundled.
# 类用途: 保存一次 canonical 绑定写入请求。
@dataclass(frozen=True)
class CanonicalIdentityLinkRequest:
    canonical_user_id: str
    provider: str
    provider_subject_id: str
    owner_id: str
    status: str = "active"

# LLM: CanonicalMemoryNote is a shared cross-provider memory row.
# 类用途: 保存 canonical 用户级记忆内容和来源 owner。
@dataclass(frozen=True)
class CanonicalMemoryNote:
    canonical_user_id: str
    kind: str
    content: str
    source_owner_id: str
    path: Path

# LLM: CanonicalMemoryNoteRequest bundles canonical memory write inputs.
# 类用途: 保存 canonical 记忆写入请求。
@dataclass(frozen=True)
class CanonicalMemoryNoteRequest:
    canonical_user_id: str
    kind: str
    content: str
    source_owner_id: str


# LLM: link_provider_identity appends a provider identity row to the provider shard.
# 函数用途: 写入 identity/provider_identity/<provider>.jsonl。
def link_provider_identity(home: MyAgentHomePaths, record: ProviderIdentityRecord) -> Path:
    path = _provider_index_path(home, record.provider)
    append_jsonl(path, record.to_dict(home.root), sort_keys=True)
    return path


# LLM: lookup_provider_identity reads one provider shard instead of scanning all identities.
# 函数用途: 按 provider 和 subject id 查询最新 owner 绑定。
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


# LLM: ensure_canonical_user_profile creates the canonical user directory profile.
# 函数用途: 初始化 identity/canonical_users/<id>/profile.json。
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


# LLM: link_canonical_identity records a cross-provider identity link without merging memory.
# 函数用途: 写 canonical 用户和全局 linked identity 账本。
def link_canonical_identity(home: MyAgentHomePaths, request: CanonicalIdentityLinkRequest) -> CanonicalIdentityLink:
    canonical_id = _safe_segment(request.canonical_user_id)
    ensure_canonical_user_profile(home, canonical_id)
    link = CanonicalIdentityLink(
        canonical_user_id=canonical_id,
        provider=request.provider,
        provider_subject_id=request.provider_subject_id,
        owner_id=request.owner_id,
        status=request.status,
        path=_canonical_links_path(home, canonical_id),
    )
    append_jsonl(link.path, link.to_dict(), sort_keys=True)
    append_jsonl(home.linked_identities_jsonl, link.to_dict(), sort_keys=True)
    return link


# LLM: list_canonical_identity_links reads canonical links for one canonical user.
# 函数用途: 返回 identity/canonical_users/<id>/linked_identities.jsonl 中的记录。
def list_canonical_identity_links(home: MyAgentHomePaths, *, canonical_user_id: str) -> list[CanonicalIdentityLink]:
    path = _canonical_links_path(home, canonical_user_id)
    if not path.exists():
        return []
    links: list[CanonicalIdentityLink] = []
    for row in path.read_text(encoding="utf-8").splitlines():
        payload = _json_object(row)
        if not payload:
            continue
        links.append(CanonicalIdentityLink.from_dict(payload, path=path))
    return links


# LLM: canonical_memory_note_path keeps shared canonical memory under the canonical profile.
# 函数用途: 返回 canonical long-term notes JSONL 路径。
def canonical_memory_note_path(home: MyAgentHomePaths, canonical_user_id: str) -> Path:
    return home.canonical_users_dir / _safe_segment(canonical_user_id) / "memory" / "long_term" / "notes.jsonl"


# LLM: write_canonical_memory_note writes shared memory without touching provider owner memory.
# 函数用途: 追加 canonical 用户级记忆 note。
def write_canonical_memory_note(home: MyAgentHomePaths, request: CanonicalMemoryNoteRequest) -> CanonicalMemoryNote:
    canonical_id = _safe_segment(request.canonical_user_id)
    ensure_canonical_user_profile(home, canonical_id)
    path = canonical_memory_note_path(home, canonical_id)
    payload = {
        "schema_version": "canonical-memory-note.v1",
        "canonical_user_id": canonical_id,
        "kind": _safe_segment(request.kind),
        "content": str(request.content or ""),
        "source_owner_id": str(request.source_owner_id or ""),
        "created_at": _now_iso(),
    }
    append_jsonl(path, payload, sort_keys=True)
    return CanonicalMemoryNote(
        canonical_user_id=canonical_id,
        kind=str(payload["kind"]),
        content=str(payload["content"]),
        source_owner_id=str(payload["source_owner_id"]),
        path=path,
    )


# LLM: resolve_owner_from_provider_identity turns provider identity into an initialized owner home.
# 函数用途: 查询 provider identity 后调用 owner resolver 初始化对应 owner。
def resolve_owner_from_provider_identity(
    home: MyAgentHomePaths,
    *,
    provider: str,
    provider_subject_id: str,
):
    record = lookup_provider_identity(home, provider=provider, provider_subject_id=provider_subject_id)
    if record is None:
        return None
    from .owner_resolver import OwnerIdentity, ensure_owner_home

    if record.provider == "local" and record.owner_kind == "main":
        identity = OwnerIdentity.local_main()
    elif record.owner_kind == "group":
        identity = OwnerIdentity.provider_group(record.provider, record.owner_id)
    else:
        identity = OwnerIdentity.provider_user(record.provider, record.owner_id)
    return ensure_owner_home(home.root, identity)


# LLM: _provider_index_path keeps provider identity indexes sharded by provider.
# 函数用途: 返回某个 provider 的身份索引文件。
def _provider_index_path(home: MyAgentHomePaths, provider: str) -> Path:
    return home.provider_identity_dir / f"{_safe_segment(provider)}.jsonl"


# LLM: _canonical_links_path stores links next to the canonical profile.
# 函数用途: 返回 canonical user 的 linked_identities.jsonl 路径。
def _canonical_links_path(home: MyAgentHomePaths, canonical_user_id: str) -> Path:
    return home.canonical_users_dir / _safe_segment(canonical_user_id) / "linked_identities.jsonl"


# LLM: _json_object tolerates malformed identity JSONL rows.
# 函数用途: 解析一行 JSON object，失败返回空字典。
def _json_object(row: str) -> dict[str, Any]:
    try:
        payload = json.loads(row)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _safe_segment keeps provider ids usable as path segments.
# 函数用途: 清理身份字段中的路径控制字符。
def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in text)
    return result.strip(".-_/") or "unknown"


# LLM: _now_iso centralizes UTC timestamps for identity ledgers.
# 函数用途: 返回当前 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CanonicalIdentityLink",
    "CanonicalIdentityLinkRequest",
    "CanonicalMemoryNote",
    "CanonicalMemoryNoteRequest",
    "ProviderIdentityRecord",
    "canonical_memory_note_path",
    "ensure_canonical_user_profile",
    "link_provider_identity",
    "link_canonical_identity",
    "list_canonical_identity_links",
    "lookup_provider_identity",
    "resolve_owner_from_provider_identity",
    "write_canonical_memory_note",
]
