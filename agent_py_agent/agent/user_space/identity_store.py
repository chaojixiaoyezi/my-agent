from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.json_io import read_jsonl_objects_report
from ..common.path_segments import safe_path_segment
from ..io import append_jsonl
from .home_layout import MyAgentHomePaths

if TYPE_CHECKING:
    from .owner_resolver import OwnerHomeResult


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
        return Path("owners") / "providers" / safe_path_segment(self.provider) / bucket / safe_path_segment(self.owner_id)

    def to_dict(self, root: Path | None = None) -> dict[str, str]:
        owner_home = self.owner_home if root is None else root / self.owner_home
        return {
            "schema_version": "provider-identity.v1",
            "provider": safe_path_segment(self.provider),
            "provider_subject_id": safe_path_segment(self.provider_subject_id),
            "owner_kind": safe_path_segment(self.owner_kind),
            "owner_id": safe_path_segment(self.owner_id),
            "owner_home": str(owner_home),
            "canonical_user_id": safe_path_segment(self.canonical_user_id) if self.canonical_user_id else "",
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

@dataclass(frozen=True)
class ProviderIdentityLookupReport:
    record: ProviderIdentityRecord | None
    load_errors: list[dict[str, object]]


@dataclass(frozen=True)
class ProviderOwnerResolutionReport:
    owner: OwnerHomeResult | None
    load_errors: list[dict[str, object]]


@dataclass(frozen=True)
class CanonicalIdentityLink:
    canonical_user_id: str
    provider: str
    provider_subject_id: str
    owner_id: str
    status: str = "active"
    path: Path | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "canonical-identity-link.v1",
            "canonical_user_id": safe_path_segment(self.canonical_user_id),
            "provider": safe_path_segment(self.provider),
            "provider_subject_id": safe_path_segment(self.provider_subject_id),
            "owner_id": str(self.owner_id or ""),
            "status": safe_path_segment(self.status),
            "updated_at": _now_iso(),
        }

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

@dataclass(frozen=True)
class CanonicalIdentityLinkRequest:
    canonical_user_id: str
    provider: str
    provider_subject_id: str
    owner_id: str
    status: str = "active"

@dataclass(frozen=True)
class CanonicalMemoryNote:
    canonical_user_id: str
    kind: str
    content: str
    source_owner_id: str
    path: Path

@dataclass(frozen=True)
class CanonicalMemoryNoteRequest:
    canonical_user_id: str
    kind: str
    content: str
    source_owner_id: str


def link_provider_identity(home: MyAgentHomePaths, record: ProviderIdentityRecord) -> Path:
    path = _provider_index_path(home, record.provider)
    append_jsonl(path, record.to_dict(home.root), sort_keys=True)
    return path


def lookup_provider_identity(home: MyAgentHomePaths, *, provider: str, provider_subject_id: str) -> ProviderIdentityRecord | None:
    return lookup_provider_identity_report(home, provider=provider, provider_subject_id=provider_subject_id).record


def lookup_provider_identity_report(
    home: MyAgentHomePaths,
    *,
    provider: str,
    provider_subject_id: str,
) -> ProviderIdentityLookupReport:
    wanted = safe_path_segment(provider_subject_id)
    latest: ProviderIdentityRecord | None = None
    path = _provider_index_path(home, provider)
    report = read_jsonl_objects_report(path, context="identity_store.provider_identity")
    for payload in report.records:
        if safe_path_segment(payload.get("provider_subject_id")) == wanted:
            latest = ProviderIdentityRecord.from_dict(payload)
    return ProviderIdentityLookupReport(latest, report.load_errors)


def ensure_canonical_user_profile(home: MyAgentHomePaths, canonical_user_id: str, *, display_name: str = "") -> Path:
    canonical_id = safe_path_segment(canonical_user_id)
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


def link_canonical_identity(home: MyAgentHomePaths, request: CanonicalIdentityLinkRequest) -> CanonicalIdentityLink:
    canonical_id = safe_path_segment(request.canonical_user_id)
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


def canonical_memory_note_path(home: MyAgentHomePaths, canonical_user_id: str) -> Path:
    return home.canonical_users_dir / safe_path_segment(canonical_user_id) / "memory" / "long_term" / "notes.jsonl"


def write_canonical_memory_note(home: MyAgentHomePaths, request: CanonicalMemoryNoteRequest) -> CanonicalMemoryNote:
    canonical_id = safe_path_segment(request.canonical_user_id)
    ensure_canonical_user_profile(home, canonical_id)
    path = canonical_memory_note_path(home, canonical_id)
    payload = {
        "schema_version": "canonical-memory-note.v1",
        "canonical_user_id": canonical_id,
        "kind": safe_path_segment(request.kind),
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


def resolve_owner_from_provider_identity(
    home: MyAgentHomePaths,
    *,
    provider: str,
    provider_subject_id: str,
):
    return resolve_owner_from_provider_identity_report(home, provider=provider, provider_subject_id=provider_subject_id).owner


def resolve_owner_from_provider_identity_report(
    home: MyAgentHomePaths,
    *,
    provider: str,
    provider_subject_id: str,
) -> ProviderOwnerResolutionReport:
    lookup = lookup_provider_identity_report(home, provider=provider, provider_subject_id=provider_subject_id)
    if lookup.record is None:
        return ProviderOwnerResolutionReport(None, lookup.load_errors)
    from .owner_resolver import OwnerIdentity, ensure_owner_home

    record = lookup.record
    if record.provider == "local" and record.owner_kind == "main":
        identity = OwnerIdentity.local_main()
    elif record.owner_kind == "group":
        identity = OwnerIdentity.provider_group(record.provider, record.owner_id)
    else:
        identity = OwnerIdentity.provider_user(record.provider, record.owner_id)
    return ProviderOwnerResolutionReport(ensure_owner_home(home.root, identity), lookup.load_errors)


def _provider_index_path(home: MyAgentHomePaths, provider: str) -> Path:
    return home.provider_identity_dir / f"{safe_path_segment(provider)}.jsonl"


def _canonical_links_path(home: MyAgentHomePaths, canonical_user_id: str) -> Path:
    return home.canonical_users_dir / safe_path_segment(canonical_user_id) / "linked_identities.jsonl"


def _json_object(row: str) -> dict[str, Any]:
    try:
        payload = json.loads(row)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CanonicalIdentityLink",
    "CanonicalIdentityLinkRequest",
    "CanonicalMemoryNote",
    "CanonicalMemoryNoteRequest",
    "ProviderIdentityLookupReport",
    "ProviderOwnerResolutionReport",
    "ProviderIdentityRecord",
    "canonical_memory_note_path",
    "ensure_canonical_user_profile",
    "link_provider_identity",
    "link_canonical_identity",
    "list_canonical_identity_links",
    "lookup_provider_identity",
    "lookup_provider_identity_report",
    "resolve_owner_from_provider_identity",
    "resolve_owner_from_provider_identity_report",
    "write_canonical_memory_note",
]
