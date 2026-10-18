# LLM: 身份映射只由C/默认入口写；受限执行器仅复用既有canonical profile，原受控owner注记及展示追加不扩权。
# 模块用途: 管理provider与规范身份的原映射和注记，拒绝W新建身份而保留原私有写格式。
# LLM: 身份关联仍由原provider索引保存；认证来源的可选姓名只能改展示字段，不得改owner/canonical关联或授予权限。
# 模块用途: 管理规范身份链接和原展示索引，姓名更新沿同一文件锁保留原记录，不创建用户或平台连接。
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.json_io import (
    append_private_jsonl_records,
    jsonl_lines,
    locked_json_path,
    read_jsonl_objects_report,
    write_private_text_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..common.path_segments import safe_path_segment
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


# LLM: 身份链接账本走私有写（0600/0700）；owner 内与 home 级两处账本同口径。
# 函数用途: 把一条第三方身份链接写进两级账本。
# LLM: provider身份链接是全局归属事实；拒门先于原私有追加，姓名展示与受控owner note不借此提权。
# 函数用途: 阻止W改身份映射，保留原两级索引及JSONL格式。
def link_provider_identity(home: MyAgentHomePaths, record: ProviderIdentityRecord) -> Path:
    from ..common.global_write_guard import require_global_writer

    require_global_writer("identity_bindings")
    path = _provider_index_path(home, record.provider)
    # 私有追加：0600/0700，存量宽权限身份索引下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(path, [record.to_dict(home.root)], sort_keys=True)
    return path


# LLM: 调用方已核provider和主体来源；姓名不决定owner，既有关联及扩展字段保留；原锁忙明确报错不阻塞授权，空名/未变化零写入。
# 函数用途: 在原索引非阻塞锁内追加变化展示快照，坏索引不重建、不创建owner目录或执行网络/授权。
def remember_provider_display_names(home: MyAgentHomePaths, *, provider: str, names: dict[str, str]) -> int:
    if not isinstance(provider, str) or not provider or safe_path_segment(provider) != provider or not isinstance(names, dict):
        raise ValueError('provider display identity is invalid')
    wanted = {}
    for subject, name in names.items():
        if not isinstance(subject, str) or not subject or safe_path_segment(subject) != subject:
            raise ValueError('provider display subject is invalid')
        if isinstance(name, str) and name.strip():
            wanted[subject] = ' '.join(name.split())[:80]
    if not wanted:
        return 0
    path = _provider_index_path(home, provider)
    with locked_json_path(path, blocking=False):
        try:
            prefix = path.read_bytes().decode('utf-8')
        except FileNotFoundError:
            prefix = ''
        rows = [json.loads(line) for line in jsonl_lines(prefix) if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('provider display index is invalid')
        latest = {row.get('provider_subject_id'): row for row in rows if row.get('provider_subject_id') in wanted}
        changed = _display_name_rows(home, provider, wanted, latest)
        if changed:
            suffix = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in changed)
            write_private_text_file_atomic_unlocked(path, prefix + ('\n' if prefix and not prefix.endswith('\n') else '') + suffix)
        return len(changed)


# LLM: 既有身份只改display_name/updated_at；缺索引的展示行仅等价原provider默认user解析，名称不生成管理员或canonical关联。
# 函数用途: 构造确实变化的展示行供同一索引事务保存，不做IO或姓名授权判断。
def _display_name_rows(home, provider, wanted, latest):
    changed = []
    for subject, name in wanted.items():
        original = latest.get(subject)
        if original is not None:
            if original.get('provider') != provider:
                raise ValueError('provider display record conflicts')
            if original.get('display_name') == name:
                continue
            updated = {**original, 'display_name': name, 'updated_at': _now_iso()}
        else:
            updated = ProviderIdentityRecord(provider=provider, provider_subject_id=subject,
                owner_kind='user', owner_id=subject, display_name=name).to_dict(home.root)
        changed.append(updated)
    return changed


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


# LLM: 规范用户档案目录缺失按 0700 新建（pdp）；档案 JSON 走私有写（0600），已存在不覆盖。
# 函数用途: 确保某个规范用户 ID 的 profile.json 存在并返回路径。
# LLM: 新canonical身份仅C可物化；受限执行器只能复用已存在profile且不chmod；Count0原目录创建/复用与原子写保持。
# 函数用途: 对缺失规范身份先拒绝，已有身份供原受控注记复用而不修改全局资料。
def ensure_canonical_user_profile(home: MyAgentHomePaths, canonical_user_id: str, *, display_name: str = "") -> Path:
    from ..common.global_write_guard import GlobalWriteInWorkerError, require_global_writer

    try:
        require_global_writer("identity_bindings")
    except GlobalWriteInWorkerError:
        existing = home.canonical_users_dir / safe_path_segment(canonical_user_id) / "profile.json"
        if existing.exists():
            return existing
        raise

    canonical_id = safe_path_segment(canonical_user_id)
    profile = home.canonical_users_dir / canonical_id / "profile.json"
    ensure_private_dir(profile.parent)
    if not profile.exists():
        payload = {
            "schema_version": "canonical-user.v1",
            "canonical_user_id": canonical_id,
            "display_name": display_name,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        # 私有原子写：0600/0700，存量宽权限档案下次写入即收紧；序列化内容逐字节不变。
        write_private_text_file_atomic(profile, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return profile


# LLM: 规范身份链接走私有写（0600/0700）；JSONL 行格式不变。
# 函数用途: 把一条规范身份链接追加进身份账本。
# LLM: 规范身份链接仍是原全局账；拒门先于profile首建和两级追加，不把已有profile当写链接许可。
# 函数用途: 阻止W借已有规范档案新增身份绑定，普通模式的原回执和行格式不变。
def link_canonical_identity(home: MyAgentHomePaths, request: CanonicalIdentityLinkRequest) -> CanonicalIdentityLink:
    from ..common.global_write_guard import require_global_writer

    require_global_writer("identity_bindings")
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
    # 私有追加：0600/0700，存量宽权限关联账下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(link.path, [link.to_dict()], sort_keys=True)
    append_private_jsonl_records(home.linked_identities_jsonl, [link.to_dict()], sort_keys=True)
    return link


def list_canonical_identity_links(home: MyAgentHomePaths, *, canonical_user_id: str) -> list[CanonicalIdentityLink]:
    path = _canonical_links_path(home, canonical_user_id)
    if not path.exists():
        return []
    links: list[CanonicalIdentityLink] = []
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    for row in jsonl_lines(path.read_text(encoding="utf-8")):
        payload = _json_object(row)
        if not payload:
            continue
        links.append(CanonicalIdentityLink.from_dict(payload, path=path))
    return links


def canonical_memory_note_path(home: MyAgentHomePaths, canonical_user_id: str) -> Path:
    return home.canonical_users_dir / safe_path_segment(canonical_user_id) / "memory" / "long_term" / "notes.jsonl"


# LLM: 记忆注记走私有写（0600/0700）；JSONL 行格式不变。
# 函数用途: 为规范用户写一条记忆注记。
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
    # 私有追加：0600/0700，存量宽权限 canonical 记忆账下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(path, [payload], sort_keys=True)
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
    "remember_provider_display_names",
    "resolve_owner_from_provider_identity",
    "resolve_owner_from_provider_identity_report",
    "write_canonical_memory_note",
]
