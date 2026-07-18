"""Owner-scoped Persona repository with guarded loading and versioned writes."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_jsonl_records,
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic,
    write_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaChange,
    OwnerQuotaEnforcer,
    owner_quota_enforcer_from_policy,
)
from .memory_threat_scan import scan_memory_content

_TARGET_ATTR = {
    "soul": "owner_soul_md",
    "user": "owner_user_md",
    "agents": "owner_agents_md",
}
_MAX_PERSONA_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_PROMPT_MAX_CHARS = 20_000


class PersonaRepositoryError(RuntimeError):
    """Base error for guarded Persona operations."""


class PersonaConflictError(PersonaRepositoryError):
    """The caller's file snapshot is stale."""


class PersonaEntryNotFoundError(PersonaRepositoryError):
    """A stable Persona entry id no longer exists."""


class PersonaSecurityError(PersonaRepositoryError):
    """A Persona file failed a boundary or injection guard."""


@dataclass(frozen=True)
class PersonaLoadDiagnostic:
    target: str
    state: str
    sha256: str = ""
    bytes: int = 0
    chars: int = 0
    truncated: bool = False
    blocked_lines: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PersonaDocumentSnapshot:
    target: str
    content: str
    diagnostic: PersonaLoadDiagnostic


@dataclass(frozen=True)
class PersonaMutationRequest:
    """Typed mutation options keep the repository API and lock path compact."""

    target: str
    action: str
    content: str = ""
    entry_id: str = ""
    source_quote: str = ""
    confirmed: bool = False
    expected_sha256: str = ""
    rollback_version: int | None = None
    source: str = "agent_tool"

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target or "").strip().lower())
        object.__setattr__(self, "action", str(self.action or "add").strip().lower())
        if self.rollback_version is not None:
            object.__setattr__(self, "rollback_version", int(self.rollback_version))


@dataclass(frozen=True)
class _PreparedPersonaMutation:
    """Complete multi-file mutation projected before any authority write."""

    previous: str
    previous_sha: str
    next_text: str
    next_sha: str
    prior_content: str
    next_entry_id: str
    version: int
    backup_path: Path
    record: dict[str, object]


class PersonaRepository:
    """Single owner-local authority for SOUL/USER/AGENTS loading and mutation."""

    def __init__(
        self,
        *,
        owner_home: str | Path,
        soul_path: str | Path,
        user_path: str | Path,
        agents_path: str | Path,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ) -> None:
        self.owner_home = Path(owner_home).resolve(strict=False)
        self.paths = {
            "soul": Path(soul_path),
            "user": Path(user_path),
            "agents": Path(agents_path),
        }
        self.prompt_max_chars = max(256, int(prompt_max_chars))
        self.versions_path = self.owner_home / "persona" / "versions.jsonl"
        self.backups_dir = self.owner_home / "persona" / "backups"
        self.quota_enforcer = quota_enforcer or owner_quota_enforcer_from_policy(self.owner_home)

    @classmethod
    def from_home_paths(
        cls,
        home_paths: object,
        *,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ):
        soul_path = getattr(home_paths, "owner_soul_md", "")
        user_path = getattr(home_paths, "owner_user_md", "")
        agents_path = getattr(home_paths, "owner_agents_md", "")
        owner_home = getattr(home_paths, "owner_home_dir", "")
        if not owner_home:
            candidates = [
                Path(value).parent for value in (soul_path, user_path, agents_path) if value
            ]
            owner_home = candidates[0] if candidates else ""
        if not owner_home:
            raise PersonaRepositoryError("owner home is unavailable")
        return cls(
            owner_home=owner_home,
            soul_path=soul_path,
            user_path=user_path,
            agents_path=agents_path,
            prompt_max_chars=prompt_max_chars,
            quota_enforcer=quota_enforcer,
        )

    @classmethod
    def from_owner_result(
        cls,
        owner: object,
        *,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ):
        return cls(
            owner_home=getattr(owner, "home_dir", ""),
            soul_path=getattr(owner, "soul_md", ""),
            user_path=getattr(owner, "user_md", ""),
            agents_path=getattr(owner, "agents_md", ""),
            prompt_max_chars=prompt_max_chars,
            quota_enforcer=quota_enforcer
            or owner_quota_enforcer_from_policy(
                getattr(owner, "home_dir", ""),
                quota_path=getattr(owner, "quota_json", None),
            ),
        )

    def path_for(self, target: str) -> Path:
        if target not in self.paths:
            raise ValueError(f"unknown persona target: {target}")
        path = self.paths[target]
        if not str(path):
            raise PersonaRepositoryError(f"{target} path is unavailable")
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.owner_home)
        except ValueError as exc:
            raise PersonaSecurityError(f"{target} path escapes owner home") from exc
        return path

    def snapshot(self) -> dict[str, PersonaDocumentSnapshot]:
        return {target: self.load(target) for target in ("agents", "soul", "user")}

    def runtime_snapshot(self) -> dict[str, object]:
        """Project guarded Persona load health without content or private paths."""

        try:
            snapshots = self.snapshot()
            targets = []
            for target in ("agents", "soul", "user"):
                diagnostic = snapshots[target].diagnostic
                targets.append(
                    {
                        "target": target,
                        "state": diagnostic.state,
                        "truncated": diagnostic.truncated,
                        "blocked_lines": diagnostic.blocked_lines,
                        "version": self._current_version(target, diagnostic.sha256),
                    }
                )
        except Exception as exc:
            return {
                "state": "unavailable",
                "health": "unavailable",
                "targets": [],
                "load_error_codes": [type(exc).__name__],
            }
        degraded = any(row["state"] not in {"ok", "missing"} for row in targets)
        return {
            "state": "available",
            "health": "degraded" if degraded else "healthy",
            "targets": targets,
            "confirmation_required": ["soul", "agents"],
            "user_autonomous": True,
            "versioned": True,
        }

    def load(self, target: str) -> PersonaDocumentSnapshot:
        try:
            path = self.path_for(target)
        except PersonaSecurityError as exc:
            return PersonaDocumentSnapshot(
                target,
                "",
                PersonaLoadDiagnostic(target=target, state="security", detail=str(exc)),
            )
        if not path.exists():
            return PersonaDocumentSnapshot(
                target,
                "",
                PersonaLoadDiagnostic(target=target, state="missing"),
            )
        try:
            if path.is_symlink() or not path.is_file():
                return PersonaDocumentSnapshot(
                    target,
                    "",
                    PersonaLoadDiagnostic(
                        target=target,
                        state="security",
                        detail="persona source must be a regular file, not a symlink",
                    ),
                )
            stat = path.stat()
            if stat.st_size > _MAX_PERSONA_FILE_BYTES:
                return PersonaDocumentSnapshot(
                    target,
                    "",
                    PersonaLoadDiagnostic(
                        target=target,
                        state="security",
                        bytes=int(stat.st_size),
                        detail="persona source exceeds guarded file-size limit",
                    ),
                )
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return PersonaDocumentSnapshot(
                target,
                "",
                PersonaLoadDiagnostic(target=target, state="io", detail=str(exc)),
            )
        sha256 = _sha256_text(raw)
        sanitized, blocked = _sanitize_persona_text(raw)
        content, truncated = _bounded_prompt_content(sanitized, self.prompt_max_chars)
        state = "blocked" if blocked else ("truncated" if truncated else "ok")
        return PersonaDocumentSnapshot(
            target,
            content,
            PersonaLoadDiagnostic(
                target=target,
                state=state,
                sha256=sha256,
                bytes=int(stat.st_size),
                chars=len(raw),
                truncated=truncated,
                blocked_lines=blocked,
            ),
        )

    def list_entries(self, target: str) -> dict[str, object]:
        path = self.path_for(target)
        snapshot = self.load(target)
        if snapshot.diagnostic.state in {"security", "io"}:
            raise PersonaRepositoryError(snapshot.diagnostic.detail or snapshot.diagnostic.state)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        return {
            "target": target,
            # Listing is model-visible. Unsafe source lines retain a stable id
            # so they can be removed, but their untrusted text is never echoed.
            "entries": _persona_entries_for_output(raw, target),
            "sha256": _sha256_text(raw),
            "version": self._current_version(target, _sha256_text(raw)),
            "diagnostic": snapshot.diagnostic.to_dict(),
        }

    def history(self, target: str) -> list[dict[str, object]]:
        self.path_for(target)
        report = read_jsonl_objects_report(
            self.versions_path,
            context="persona_repository.history",
        )
        return [row for row in report.records if str(row.get("target") or "") == target]

    def current_sha256(self, target: str) -> str:
        path = self.path_for(target)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        return _sha256_text(raw)

    def mutate(self, request: PersonaMutationRequest) -> dict[str, object]:
        with self.quota_enforcer.admission() as admission:
            return _mutate_persona_document(self, admission, request)

    def _write_version_snapshot(
        self,
        target: str,
        version: int,
        sha256: str,
        content: str,
    ) -> Path:
        path = self._version_snapshot_path(target, version, sha256)
        if not path.exists():
            write_text_file_atomic(path, content)
        return path

    def _version_snapshot_path(self, target: str, version: int, sha256: str) -> Path:
        return self.backups_dir / target / f"{version:08d}-{sha256}.md"

    def _content_for_version(self, target: str, version: int) -> str:
        rows = [row for row in self.history(target) if int(row.get("version") or 0) == version]
        if not rows:
            raise PersonaEntryNotFoundError(f"persona version {version} not found")
        ref = str(rows[-1].get("backup_ref") or "")
        path = (self.owner_home / ref).resolve(strict=False)
        try:
            path.relative_to(self.backups_dir.resolve(strict=False))
        except ValueError as exc:
            raise PersonaSecurityError("persona backup path escapes owner backup root") from exc
        content = path.read_text(encoding="utf-8")
        if _sha256_text(content) != str(rows[-1].get("sha256") or ""):
            raise PersonaSecurityError("persona backup hash mismatch")
        return content

    def _next_version(self, target: str) -> int:
        versions = [int(row.get("version") or 0) for row in self.history(target)]
        return max(versions, default=0) + 1

    def _current_version(self, target: str, current_sha: str) -> int:
        rows = [row for row in self.history(target) if str(row.get("sha256") or "") == current_sha]
        return max((int(row.get("version") or 0) for row in rows), default=0)


# LLM: One file-lock critical section prepares, admits, and commits the complete Persona mutation.
# 函数用途: 保证 Persona 正文、备份和版本记录要么一起提交，要么都不改变。
def _mutate_persona_document(
    repository: PersonaRepository,
    admission: OwnerQuotaAdmission,
    request: PersonaMutationRequest,
) -> dict[str, object]:
    if request.action not in {"add", "replace", "remove", "rollback"}:
        raise ValueError(f"unsupported persona action: {request.action}")
    path = repository.path_for(request.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise PersonaSecurityError("persona target must be a regular file")
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        prepared = _prepare_persona_mutation(repository, request, previous)
        if isinstance(prepared, dict):
            return prepared
        _admit_persona_mutation(repository, admission, path, prepared)
        _commit_persona_mutation(repository, path, prepared)
    return _persona_mutation_result(request, prepared)


# LLM: Preparation derives the next authority bytes and audit record without writing any file.
# 函数用途: 在配额检查前完成 CAS、操作应用、版本号和备份位置计算。
def _prepare_persona_mutation(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous: str,
) -> _PreparedPersonaMutation | dict[str, object]:
    previous_sha = _sha256_text(previous)
    if request.expected_sha256 and request.expected_sha256 != previous_sha:
        raise PersonaConflictError(
            f"persona changed: expected {request.expected_sha256}, current {previous_sha}"
        )
    if request.action == "rollback":
        next_text, prior_content, next_entry_id = _prepare_persona_rollback(repository, request)
    else:
        next_text, prior_content, next_entry_id, changed = _apply_operation_to_text(
            previous,
            target=request.target,
            action=request.action,
            content=request.content,
            entry_id=request.entry_id,
        )
        if not changed:
            return _unchanged_persona_result(repository, request, previous_sha, next_entry_id)
    if len(next_text.encode("utf-8")) > _MAX_PERSONA_FILE_BYTES:
        raise ValueError("persona file exceeds maximum size")
    next_sha = _sha256_text(next_text)
    version = repository._next_version(request.target)
    backup_path = repository._version_snapshot_path(request.target, version, next_sha)
    record = _persona_version_record(
        repository, request, previous_sha, next_sha, version, backup_path, next_entry_id
    )
    return _PreparedPersonaMutation(
        previous=previous,
        previous_sha=previous_sha,
        next_text=next_text,
        next_sha=next_sha,
        prior_content=prior_content,
        next_entry_id=next_entry_id,
        version=version,
        backup_path=backup_path,
        record=record,
    )


def _prepare_persona_rollback(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
) -> tuple[str, str, str]:
    if request.rollback_version is None:
        raise ValueError("rollback_version is required")
    next_text = repository._content_for_version(request.target, request.rollback_version)
    if not scan_memory_content(next_text).safe:
        raise PersonaSecurityError("rollback snapshot failed persona threat scan")
    return next_text, "", ""


def _unchanged_persona_result(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous_sha: str,
    next_entry_id: str,
) -> dict[str, object]:
    return {
        "ok": True,
        "changed": False,
        "action": request.action,
        "target": request.target,
        "entry_id": next_entry_id,
        "sha256": previous_sha,
        "version": repository._current_version(request.target, previous_sha),
    }


def _persona_version_record(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous_sha: str,
    next_sha: str,
    version: int,
    backup_path: Path,
    next_entry_id: str,
) -> dict[str, object]:
    return {
        "target": request.target,
        "version": version,
        "sha256": next_sha,
        "previous_sha256": previous_sha,
        "source_quote": request.source_quote,
        "confirmed": request.confirmed,
        "created_at": time.time(),
        "backup_ref": str(backup_path.relative_to(repository.owner_home)),
        "action": request.action,
        "entry_id": next_entry_id or request.entry_id,
        "source": request.source,
    }


def _admit_persona_mutation(
    repository: PersonaRepository,
    admission: OwnerQuotaAdmission,
    path: Path,
    prepared: _PreparedPersonaMutation,
) -> None:
    version_blob = json.dumps(prepared.record, ensure_ascii=False, sort_keys=True) + "\n"
    admission.check(
        [
            OwnerQuotaChange(path, len(prepared.next_text.encode("utf-8"))),
            OwnerQuotaChange(prepared.backup_path, len(prepared.next_text.encode("utf-8"))),
            OwnerQuotaChange(
                repository.versions_path,
                len(version_blob.encode("utf-8")),
                append=True,
            ),
        ]
    )


def _commit_persona_mutation(
    repository: PersonaRepository,
    path: Path,
    prepared: _PreparedPersonaMutation,
) -> None:
    backup_path = repository._write_version_snapshot(
        str(prepared.record["target"]),
        prepared.version,
        prepared.next_sha,
        prepared.next_text,
    )
    write_text_file_atomic_unlocked(path, prepared.next_text)
    try:
        append_jsonl_records(repository.versions_path, [prepared.record])
    except Exception:
        write_text_file_atomic_unlocked(path, prepared.previous)
        backup_path.unlink(missing_ok=True)
        raise


def _persona_mutation_result(
    request: PersonaMutationRequest,
    prepared: _PreparedPersonaMutation,
) -> dict[str, object]:
    return {
        "ok": True,
        "changed": True,
        "action": request.action,
        "target": request.target,
        "entry_id": prepared.next_entry_id or request.entry_id,
        "prior_content": prepared.prior_content,
        "content": request.content if request.action in {"add", "replace"} else "",
        "sha256": prepared.next_sha,
        "version": prepared.version,
        "backup_ref": prepared.record["backup_ref"],
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def persona_entry_id(target: str, content: str) -> str:
    digest = hashlib.sha256(f"{target}\0{content}".encode()).hexdigest()[:16]
    return f"persona-{digest}"


def _persona_entries_from_text(text: str, target: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if content:
            entries.append({"entry_id": persona_entry_id(target, content), "content": content})
    return entries


def _persona_entries_for_output(
    text: str,
    target: str,
) -> list[dict[str, str | bool]]:
    """Return model-visible entries without replaying poisoned source text."""

    entries: list[dict[str, str | bool]] = []
    for entry in _persona_entries_from_text(text, target):
        content = entry["content"]
        if scan_memory_content(content).safe:
            entries.append(entry)
            continue
        entries.append(
            {
                "entry_id": entry["entry_id"],
                "content": "[BLOCKED unsafe persona entry]",
                "blocked": True,
            }
        )
    return entries


def _apply_operation_to_text(
    existing: str,
    *,
    target: str,
    action: str,
    content: str,
    entry_id: str,
) -> tuple[str, str, str, bool]:
    if action in {"add", "replace"}:
        content = str(content or "").strip()
        if not content:
            raise ValueError("persona content is required")
        scan = scan_memory_content(content)
        if not scan.safe:
            raise PersonaSecurityError(scan.reason())
    entries = _persona_entries_from_text(existing, target)
    if action == "add":
        next_id = persona_entry_id(target, content)
        if any(row["entry_id"] == next_id for row in entries):
            return existing, "", next_id, False
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return f"{existing}{separator}- {content}\n", "", next_id, True
    lines = existing.splitlines(keepends=True)
    match_index = -1
    prior_content = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        candidate = stripped[2:].strip()
        if persona_entry_id(target, candidate) == entry_id:
            match_index = index
            prior_content = candidate
            break
    if match_index < 0:
        raise PersonaEntryNotFoundError(entry_id)
    if action == "remove":
        del lines[match_index]
        return "".join(lines), prior_content, entry_id, True
    newline = "\n" if lines[match_index].endswith("\n") else ""
    lines[match_index] = f"- {content}{newline}"
    return "".join(lines), prior_content, persona_entry_id(target, content), True


def _sanitize_persona_text(content: str) -> tuple[str, int]:
    lines: list[str] = []
    blocked = 0
    for line in content.splitlines(keepends=True):
        scan = scan_memory_content(line)
        if scan.safe:
            lines.append(line)
            continue
        blocked += 1
        suffix = "\n" if line.endswith("\n") else ""
        pattern_ids = ",".join(finding.pattern_id for finding in scan.findings[:4])
        lines.append(f"[BLOCKED persona line: {pattern_ids}]{suffix}")
    return "".join(lines), blocked


def _bounded_prompt_content(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    marker = "\n[... persona content truncated by configured prompt budget ...]\n"
    budget = max(0, max_chars - len(marker))
    head = int(budget * 0.75)
    tail = budget - head
    return content[:head] + marker + (content[-tail:] if tail else ""), True


__all__ = [
    "PersonaConflictError",
    "PersonaDocumentSnapshot",
    "PersonaEntryNotFoundError",
    "PersonaLoadDiagnostic",
    "PersonaMutationRequest",
    "PersonaRepository",
    "PersonaRepositoryError",
    "PersonaSecurityError",
    "persona_entry_id",
]
