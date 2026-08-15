from __future__ import annotations

"""Immutable per-turn Skill catalog and guarded body reads."""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from .skills import SkillCard


class SkillSnapshotError(RuntimeError):
    """A selected Skill can no longer be read from the immutable turn snapshot."""


@dataclass(frozen=True)
class SkillLoadError:
    path: str
    code: str
    message: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }


@dataclass(frozen=True)
class SkillSnapshotEntry:
    stable_id: str
    name: str
    description: str
    path: str
    source: str
    category: str
    content_sha256: str
    enabled: bool = True
    when_to_use: str = ""
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    tools_required: tuple[str, ...] = ()
    risk_level: str = "low"

    def to_card(self) -> SkillCard:
        return SkillCard(
            name=self.name,
            description=self.description,
            path=Path(self.path),
            when_to_use=self.when_to_use,
            category=self.category,
            platforms=list(self.platforms),
            scope=self.source,
            tags=list(self.tags),
            capabilities=list(self.capabilities),
            tools_required=list(self.tools_required),
            risk_level=self.risk_level,
            source=self.source,
        )

    def to_dict(self, *, include_path: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "stable_id": self.stable_id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "category": self.category,
            "enabled": self.enabled,
            "content_sha256": self.content_sha256,
        }
        if include_path:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class SkillSnapshot:
    entries: tuple[SkillSnapshotEntry, ...]
    errors: tuple[SkillLoadError, ...]
    fingerprint: str
    owner_id: str
    workspace_root: str

    def enabled_entries(self) -> tuple[SkillSnapshotEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    def resolve(self, reference: str, *, enabled_only: bool = True) -> SkillSnapshotEntry | None:
        value = str(reference or "").strip()
        if not value:
            return None
        candidates = self.enabled_entries() if enabled_only else self.entries
        for entry in candidates:
            if value in {entry.stable_id, entry.name}:
                return entry
        return None

    def read_body(self, reference: str, *, max_chars: int = 0) -> str:
        entry = self.resolve(reference)
        if entry is None:
            raise SkillSnapshotError(f"SKILL_NOT_AVAILABLE reference={reference}")
        path = Path(entry.path)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillSnapshotError(f"SKILL_READ_FAILED skill={entry.stable_id}: {exc}") from exc
        if _content_sha256(body) != entry.content_sha256:
            raise SkillSnapshotError(f"SKILL_SNAPSHOT_STALE skill={entry.stable_id}")
        decision = evaluate_skill_guard_gate(
            path.parent,
            SkillGuardRequest(source=entry.source, skill_name=entry.name),
        )
        if not decision.allowed:
            raise SkillSnapshotError(f"SKILL_GUARD_DENIED skill={entry.stable_id}")
        if max_chars and len(body) > max_chars:
            return body[:max_chars] + "\n... 已截断"
        return body

    def restricted(
        self,
        references: Iterable[str],
        *,
        expected_sha256: Mapping[str, str] | None = None,
    ) -> SkillSnapshot:
        """Return a fail-closed subset for a delegated runner."""

        expected = dict(expected_sha256 or {})
        selected: list[SkillSnapshotEntry] = []
        seen: set[str] = set()
        for raw in references:
            reference = str(raw or "").strip()
            if not reference:
                continue
            entry = self.resolve(reference)
            if entry is None:
                raise SkillSnapshotError(f"SKILL_NOT_AVAILABLE reference={reference}")
            wanted_sha = expected.get(entry.stable_id) or expected.get(reference)
            if wanted_sha and wanted_sha != entry.content_sha256:
                raise SkillSnapshotError(f"SKILL_SNAPSHOT_STALE skill={entry.stable_id}")
            if entry.stable_id not in seen:
                selected.append(entry)
                seen.add(entry.stable_id)
        fingerprint = _content_sha256(
            self.fingerprint + "\0" + "\0".join(entry.stable_id for entry in selected)
        )
        return SkillSnapshot(
            entries=tuple(selected),
            errors=self.errors,
            fingerprint=fingerprint,
            owner_id=self.owner_id,
            workspace_root=self.workspace_root,
        )


def skill_content_sha256(text: str) -> str:
    return _content_sha256(text)


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SkillLoadError",
    "SkillSnapshot",
    "SkillSnapshotEntry",
    "SkillSnapshotError",
    "skill_content_sha256",
]
