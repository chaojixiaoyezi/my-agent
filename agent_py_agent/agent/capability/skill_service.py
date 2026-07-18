from __future__ import annotations

"""Single owner-scoped Skill discovery, policy, cache, and snapshot service."""

import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from ..user_space.owner_policy import EffectiveOwnerPolicy
from .skill_snapshot import (
    SkillLoadError,
    SkillSnapshot,
    SkillSnapshotEntry,
    skill_content_sha256,
)
from .skills import SkillCard, parse_skill_file

_MAX_SCAN_DEPTH = 6
_MAX_SKILLS_PER_ROOT = 2000
_NAME_MAX_CHARS = 128
_DESCRIPTION_MAX_CHARS = 1024


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: str


@dataclass(frozen=True)
class SkillManifestItem:
    root: SkillRoot
    path: Path
    text: str
    content_sha256: str


class SkillsService:
    """Owns the one Skill catalog used by prompts, tools, and subagents."""

    def __init__(
        self,
        *,
        home_paths: object,
        workspace_root: str | Path,
        policy_provider: Callable[[], EffectiveOwnerPolicy],
    ) -> None:
        self.home_paths = home_paths
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.policy_provider = policy_provider
        self._extra_roots: tuple[Path, ...] = ()
        self._cache: dict[tuple[str, str], SkillSnapshot] = {}
        self._lock = threading.RLock()

    def set_extra_roots(self, roots: Iterable[str | Path]) -> None:
        normalized = tuple(
            Path(root).expanduser().resolve(strict=False)
            for root in roots
            if str(root or "").strip()
        )
        with self._lock:
            self._extra_roots = normalized
            self._cache.clear()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def snapshot_for(
        self,
        workspace_root: str | Path | None = None,
        *,
        force_reload: bool = False,
    ) -> SkillSnapshot:
        workspace = Path(workspace_root or self.workspace_root).expanduser().resolve(strict=False)
        policy = self.policy_provider()
        roots = self._skill_roots(workspace, policy)
        manifest, discovery_errors = _build_manifest(roots)
        fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace)
        cache_key = (policy.owner_id, str(workspace))
        if not force_reload:
            with self._lock:
                cached = self._cache.get(cache_key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached
        snapshot = _build_snapshot(manifest, discovery_errors, policy, workspace)
        with self._lock:
            self._cache[cache_key] = snapshot
        return snapshot

    def _skill_roots(
        self,
        workspace_root: Path,
        policy: EffectiveOwnerPolicy,
    ) -> tuple[SkillRoot, ...]:
        enabled = set(policy.enabled_skill_sources)
        roots: list[SkillRoot] = []
        if not enabled or "workspace" in enabled:
            roots.extend(SkillRoot(path, "workspace") for path in self._extra_roots)
            roots.append(SkillRoot(workspace_root / ".agents" / "skills", "workspace"))
        if not enabled or "owner" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.owner_home_dir) / "skills", "owner"))
        if not enabled or "shared" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.shared_skills_dir), "shared"))
        if not enabled or "builtin" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.shared_builtin_dir), "builtin"))
        return _dedupe_roots(roots)


def _build_manifest(roots: tuple[SkillRoot, ...]) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    items: list[SkillManifestItem] = []
    errors: list[SkillLoadError] = []
    for root in roots:
        discovered, root_errors = _discover_root(root)
        items.extend(discovered)
        errors.extend(root_errors)
    return items, errors


def _discover_root(root: SkillRoot) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    if not root.path.is_dir():
        return [], []
    resolved_root = root.path.resolve(strict=False)
    files = [path for path in sorted(root.path.rglob("SKILL.md")) if _visible_skill_path(root.path, path)]
    errors: list[SkillLoadError] = []
    if len(files) > _MAX_SKILLS_PER_ROOT:
        errors.append(_error(root, root.path, "SKILL_ROOT_LIMIT", f"skill count exceeds {_MAX_SKILLS_PER_ROOT}"))
        files = files[:_MAX_SKILLS_PER_ROOT]
    items: list[SkillManifestItem] = []
    for path in files:
        item, error = _manifest_item(root, resolved_root, path)
        if item is not None:
            items.append(item)
        if error is not None:
            errors.append(error)
    return items, errors


def _manifest_item(
    root: SkillRoot,
    resolved_root: Path,
    path: Path,
) -> tuple[SkillManifestItem | None, SkillLoadError | None]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        return None, _error(root, path, "SKILL_PATH_ESCAPE", str(exc))
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, _error(root, path, "SKILL_READ_FAILED", str(exc))
    return SkillManifestItem(root, resolved, text, skill_content_sha256(text)), None


def _build_snapshot(
    manifest: list[SkillManifestItem],
    discovery_errors: list[SkillLoadError],
    policy: EffectiveOwnerPolicy,
    workspace_root: Path,
) -> SkillSnapshot:
    entries: dict[str, SkillSnapshotEntry] = {}
    errors = list(discovery_errors)
    for item in manifest:
        entry, error = _entry_from_manifest(item, policy)
        if error is not None:
            errors.append(error)
            continue
        if entry is not None and entry.name not in entries:
            entries[entry.name] = entry
    fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace_root)
    return SkillSnapshot(
        entries=tuple(sorted(entries.values(), key=lambda entry: (entry.source, entry.name))),
        errors=tuple(errors),
        fingerprint=fingerprint,
        owner_id=policy.owner_id,
        workspace_root=str(workspace_root),
    )


def _entry_from_manifest(
    item: SkillManifestItem,
    policy: EffectiveOwnerPolicy,
) -> tuple[SkillSnapshotEntry | None, SkillLoadError | None]:
    try:
        card = parse_skill_file(
            item.path,
            source=item.root.source,
            require_frontmatter=True,
        )
        _validate_card(card)
    except (OSError, TypeError, ValueError) as exc:
        return None, _error(item.root, item.path, "SKILL_PARSE_FAILED", str(exc))
    decision = evaluate_skill_guard_gate(
        item.path.parent,
        SkillGuardRequest(source=item.root.source, skill_name=card.name),
    )
    if not decision.allowed:
        return None, _error(item.root, item.path, "SKILL_GUARD_DENIED", _guard_codes(decision.to_dict()))
    category = card.category if card.category != "general" else _derived_category(item.root.path, item.path)
    enabled = card.name not in set(policy.disabled_skills)
    if item.root.source == "shared" and policy.enabled_shared_skills:
        enabled = enabled and card.name in set(policy.enabled_shared_skills)
    return _snapshot_entry(item, card, category, enabled), None


def _snapshot_entry(
    item: SkillManifestItem,
    card: SkillCard,
    category: str,
    enabled: bool,
) -> SkillSnapshotEntry:
    source = item.root.source
    return SkillSnapshotEntry(
        stable_id=f"{source}:{card.name}",
        name=card.name,
        description=card.description,
        path=str(item.path),
        source=source,
        category=category or "general",
        content_sha256=item.content_sha256,
        enabled=enabled,
        when_to_use=card.when_to_use,
        platforms=tuple(card.platforms),
        tags=tuple(card.tags),
        capabilities=tuple(card.capabilities),
        tools_required=tuple(card.tools_required),
        risk_level=card.risk_level,
    )


def _snapshot_fingerprint(
    manifest: list[SkillManifestItem],
    errors: list[SkillLoadError],
    policy: EffectiveOwnerPolicy,
    workspace_root: Path,
) -> str:
    digest = hashlib.sha256()
    digest.update(str(workspace_root).encode("utf-8"))
    digest.update(
        repr(
            (
                policy.owner_id,
                policy.enabled_skill_sources,
                policy.disabled_skills,
                policy.enabled_shared_skills,
            )
        ).encode()
    )
    for item in manifest:
        digest.update(f"{item.root.source}\0{item.path}\0{item.content_sha256}".encode())
    for error in errors:
        digest.update(repr(error).encode())
    return digest.hexdigest()


def _validate_card(card: SkillCard) -> None:
    if not card.name:
        raise ValueError("missing name")
    if len(card.name) > _NAME_MAX_CHARS:
        raise ValueError(f"name exceeds {_NAME_MAX_CHARS} characters")
    if not card.description:
        raise ValueError("missing description")
    if len(card.description) > _DESCRIPTION_MAX_CHARS:
        raise ValueError(f"description exceeds {_DESCRIPTION_MAX_CHARS} characters")


def _visible_skill_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) - 1 > _MAX_SCAN_DEPTH:
        return False
    return not any(part.startswith(".") for part in relative.parts[:-1])


def _derived_category(root: Path, path: Path) -> str:
    try:
        return "/".join(path.parent.relative_to(root.resolve(strict=False)).parts[:-1])
    except ValueError:
        return "general"


def _dedupe_roots(roots: list[SkillRoot]) -> tuple[SkillRoot, ...]:
    result: list[SkillRoot] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        key = (root.source, str(root.path.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            result.append(SkillRoot(Path(key[1]), root.source))
    return tuple(result)


def _guard_codes(decision: dict[str, object]) -> str:
    findings = decision.get("findings") if isinstance(decision, dict) else []
    codes = [str(item.get("code") or "") for item in findings if isinstance(item, dict)]
    return ",".join(code for code in codes if code) or "guard denied"


def _error(root: SkillRoot, path: Path, code: str, message: str) -> SkillLoadError:
    return SkillLoadError(path=str(path), code=code, message=message, source=root.source)


__all__ = ["SkillRoot", "SkillsService"]
