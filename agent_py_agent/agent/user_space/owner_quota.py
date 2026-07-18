from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import locked_json_path, read_json_object_report

_QUOTA_LOCK_BASENAME = ".owner-quota"
_DEFAULT_MAX_DISK_MB = 102_400


@dataclass(frozen=True)
class OwnerQuotaChange:
    """One owner-scoped file size change evaluated under the quota lock.

    ``append`` makes ``final_size`` an added byte count.  This is deliberately
    resolved while the owner lock is held, so two concurrent append operations
    cannot both project from the same stale pre-lock file size.
    """

    path: Path
    final_size: int | None
    append: bool = False


@dataclass(frozen=True)
class OwnerQuotaProjection:
    used_bytes: int
    projected_bytes: int
    max_bytes: int


class OwnerQuotaUnavailable(RuntimeError):
    """The authoritative owner quota or current usage could not be read safely."""


class OwnerQuotaExceeded(RuntimeError):
    def __init__(self, projection: OwnerQuotaProjection):
        self.projection = projection
        super().__init__(
            "owner 磁盘配额不足: "
            f"used_bytes={projection.used_bytes} "
            f"projected_bytes={projection.projected_bytes} "
            f"max_bytes={projection.max_bytes}"
        )


class OwnerQuotaAdmission:
    """One owner-quota critical section shared by a whole durable mutation.

    Repository writers often have to lock and read their authority file before
    the exact final byte count is known.  Keeping the owner lock outside that
    repository lock establishes one lock order for every writer:

    ``owner quota -> repository/file lock -> mutation``.

    The admission object deliberately rescans usage at ``check`` time.  This
    lets a caller validate the complete multi-file mutation immediately before
    it writes, without estimating from stale pre-lock state.
    """

    def __init__(self, enforcer: OwnerQuotaEnforcer, *, enabled: bool) -> None:
        self._enforcer = enforcer
        self.enabled = bool(enabled)

    def check(self, changes: Iterable[OwnerQuotaChange]) -> OwnerQuotaProjection | None:
        if not self.enabled:
            return None
        relevant = _normalized_owner_changes(self._enforcer.owner_root, changes)
        if not relevant:
            return None
        try:
            used = owner_logical_usage_bytes(self._enforcer.owner_root)
            current_sizes = {path: _logical_file_size(path) for path in relevant}
        except (OSError, RuntimeError, ValueError) as exc:
            raise OwnerQuotaUnavailable("owner disk usage is unavailable") from exc
        replaced = sum(current_sizes.values())
        final = sum(
            0
            if change.final_size is None
            else (
                current_sizes[path] + change.final_size
                if change.append
                else change.final_size
            )
            for path, change in relevant.items()
        )
        projection = OwnerQuotaProjection(
            used_bytes=used,
            projected_bytes=max(0, used - replaced + final),
            max_bytes=self._enforcer.max_bytes,
        )
        if projection.projected_bytes > projection.max_bytes and projection.projected_bytes > used:
            raise OwnerQuotaExceeded(projection)
        return projection


class OwnerQuotaEnforcer:
    """Cross-process admission gate for structured writes inside one owner home.

    All file tools share the same owner-local lock.  The gate scans the
    canonical owner home while holding that lock, computes the logical final
    size of the whole batch, and only then lets the mutation proceed.  Deletes
    and non-growing repairs remain possible when an owner is already over its
    limit.
    """

    def __init__(self, owner_root: str | Path, *, max_bytes: int, policy_available: bool = True):
        self.owner_root = Path(owner_root).expanduser().resolve(strict=False)
        self.max_bytes = max(0, int(max_bytes))
        self.policy_available = bool(policy_available)
        self._lock_anchor = self.owner_root / _QUOTA_LOCK_BASENAME

    @contextmanager
    def admission(self) -> Iterator[OwnerQuotaAdmission]:
        """Hold the owner gate while a repository computes and applies a mutation."""

        if self.max_bytes <= 0:
            yield OwnerQuotaAdmission(self, enabled=False)
            return
        if not self.policy_available:
            raise OwnerQuotaUnavailable("owner quota policy is unavailable")
        with _locked_owner_quota(self._lock_anchor):
            yield OwnerQuotaAdmission(self, enabled=True)

    @contextmanager
    def reserve(self, changes: Iterable[OwnerQuotaChange]) -> Iterator[OwnerQuotaProjection | None]:
        with self.admission() as admission:
            projection = admission.check(changes)
            yield projection


def owner_quota_enforcer_from_policy(
    owner_root: str | Path,
    *,
    quota_path: str | Path | None = None,
) -> OwnerQuotaEnforcer:
    """Build the canonical gate for non-Agent write entrances.

    Feishu confirmation callbacks do not own a ``SimpleAgent`` instance, but
    they still mutate the same owner Persona authority.  Reading that owner's
    structured quota file here keeps those callbacks on the same gate.  A
    malformed policy is fail-closed; a missing file uses the seeded product
    default, matching ``resolve_effective_owner_policy``.
    """

    root = Path(owner_root).expanduser().resolve(strict=False)
    path = Path(quota_path) if quota_path is not None else root / "quota.json"
    report = read_json_object_report(path, context="owner_policy.quota")
    raw = report.payload.get("max_disk_mb")
    try:
        max_disk_mb = int(raw)
    except (TypeError, ValueError):
        max_disk_mb = _DEFAULT_MAX_DISK_MB
    if max_disk_mb <= 0:
        max_disk_mb = _DEFAULT_MAX_DISK_MB
    return OwnerQuotaEnforcer(
        root,
        max_bytes=max_disk_mb * 1024 * 1024,
        policy_available=report.load_error is None,
    )


def owner_logical_usage_bytes(root: str | Path) -> int:
    """Count regular-file bytes below an owner root without following symlinks.

    Hard links are intentionally counted per visible path.  A logical quota
    must not be bypassable by creating many links to the same inode.
    """

    owner_root = Path(root).expanduser().resolve(strict=False)
    if not owner_root.exists():
        return 0
    total = 0
    try:
        for directory, dirnames, filenames in os.walk(owner_root, followlinks=False):
            base = Path(directory)
            dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
            for name in filenames:
                path = base / name
                if path == owner_root / f"{_QUOTA_LOCK_BASENAME}.lock" or path.is_symlink():
                    continue
                info = path.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    total += max(0, int(info.st_size))
    except OSError as exc:
        raise OwnerQuotaUnavailable("owner disk usage scan failed") from exc
    return total


def _normalized_owner_changes(
    owner_root: Path,
    changes: Iterable[OwnerQuotaChange],
) -> dict[Path, OwnerQuotaChange]:
    normalized: dict[Path, OwnerQuotaChange] = {}
    for change in changes:
        path = Path(change.path).expanduser().resolve(strict=False)
        try:
            path.relative_to(owner_root)
        except ValueError:
            continue
        size = change.final_size
        if size is not None and int(size) < 0:
            raise ValueError("quota final_size must be non-negative")
        if change.append and size is None:
            raise ValueError("quota append change requires a byte count")
        candidate = OwnerQuotaChange(
            path=path,
            final_size=None if size is None else int(size),
            append=bool(change.append),
        )
        previous = normalized.get(path)
        if previous is not None and previous.append and candidate.append:
            candidate = OwnerQuotaChange(
                path=path,
                final_size=int(previous.final_size or 0) + int(candidate.final_size or 0),
                append=True,
            )
        elif previous is not None and previous.append != candidate.append:
            raise ValueError("quota batch cannot mix append and replacement for one path")
        normalized[path] = candidate
    return normalized


def _logical_file_size(path: Path) -> int:
    try:
        if path.is_symlink() or not path.is_file():
            return 0
        return max(0, int(path.stat(follow_symlinks=False).st_size))
    except FileNotFoundError:
        return 0


@contextmanager
def _locked_owner_quota(path: Path) -> Iterator[None]:
    manager = locked_json_path(path)
    try:
        manager.__enter__()
    except (OSError, RuntimeError, ValueError) as exc:
        raise OwnerQuotaUnavailable("owner quota lock is unavailable") from exc
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


__all__ = [
    "OwnerQuotaAdmission",
    "OwnerQuotaChange",
    "OwnerQuotaEnforcer",
    "OwnerQuotaExceeded",
    "OwnerQuotaProjection",
    "OwnerQuotaUnavailable",
    "owner_quota_enforcer_from_policy",
    "owner_logical_usage_bytes",
]
