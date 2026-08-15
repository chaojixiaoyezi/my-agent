
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KIND_EXTENSIONS = {
    "csv": (".csv",),
    "docx": (".docx",),
    "html": (".html", ".htm"),
    "htm": (".html", ".htm"),
    "json": (".json",),
    "markdown": (".md", ".markdown"),
    "md": (".md", ".markdown"),
    "pdf": (".pdf",),
    "spreadsheet": (".xlsx", ".xls", ".ods", ".csv", ".tsv"),
    "txt": (".txt",),
    "workbook": (".xlsx", ".xls", ".ods"),
    "xlsx": (".xlsx",),
    "zip": (".zip",),
}
_DEFAULT_SEARCH_ROOTS = ("outputs", "artifacts")
_MAX_CANDIDATES = 128
_SAFE_EXTENSION_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_EXPLICIT_EXTENSION_KEYS = (
    "preferred_extension",
    "preferred_extensions",
    "acceptable_extension",
    "acceptable_extensions",
    "accepted_extension",
    "accepted_extensions",
    "extension",
    "extensions",
    "file_extension",
    "file_extensions",
)


@dataclass(frozen=True)
class ArtifactLocatorResult:
    path: Path | None
    findings: list[dict[str, object]] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


def locate_artifact(item: dict[str, Any], workspace_root: Path) -> ArtifactLocatorResult:
    workspace = Path(workspace_root).resolve(strict=False)
    if raw_path := _artifact_target_path(item):
        path = _resolve_output_path(raw_path, workspace)
        if path is None:
            return ArtifactLocatorResult(None, [_finding("ARTIFACT_PATH_INVALID", raw_path)])
        return ArtifactLocatorResult(path)
    kind = str(item.get("kind") or "").strip().lower()
    extensions = _extensions_for_item(item)
    if not extensions:
        return ArtifactLocatorResult(None, [_finding("ARTIFACT_LOCATOR_EXTENSION_MISSING", kind)])
    candidates = _candidate_paths(item, workspace, extensions)
    if not candidates:
        return ArtifactLocatorResult(None, [_finding("ARTIFACT_LOCATOR_NO_MATCH", kind)])
    selected = _select_by_structured_hint(item, candidates)
    if selected is not None:
        return ArtifactLocatorResult(selected, candidates=[_portable_ref(path, workspace) for path in candidates])
    if len(candidates) == 1:
        return ArtifactLocatorResult(candidates[0], candidates=[_portable_ref(candidates[0], workspace)])
    return ArtifactLocatorResult(
        None,
        [
            _finding(
                "ARTIFACT_LOCATOR_AMBIGUOUS",
                kind,
                evidence={"candidates": [_portable_ref(path, workspace) for path in candidates[:10]]},
            )
        ],
        candidates=[_portable_ref(path, workspace) for path in candidates],
    )


def artifact_can_be_located(item: dict[str, Any]) -> bool:
    if _artifact_target_path(item):
        return True
    return bool(_extensions_for_item(item)) and bool(_search_roots(item))


def _artifact_target_path(item: dict[str, Any]) -> str:
    return str(item.get("preferred_path") or item.get("path") or "").strip()


def _candidate_paths(item: dict[str, Any], workspace: Path, extensions: tuple[str, ...]) -> list[Path]:
    candidates: list[Path] = []
    for root in _search_roots(item):
        root_path = _resolve_output_path(root, workspace)
        candidates.extend(_candidate_paths_under_root(root_path, extensions, remaining=_MAX_CANDIDATES - len(candidates)))
        if len(candidates) >= _MAX_CANDIDATES:
            break
    candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    return candidates


def _candidate_paths_under_root(root_path: Path | None, extensions: tuple[str, ...], *, remaining: int) -> list[Path]:
    if remaining <= 0 or root_path is None or not root_path.exists() or not root_path.is_dir():
        return []
    candidates: list[Path] = []
    for path in root_path.rglob("*"):
        if len(candidates) >= remaining:
            break
        if path.is_file() and path.suffix.lower() in extensions:
            candidates.append(path.resolve(strict=False))
    return candidates


def _extensions_for_item(item: dict[str, Any]) -> tuple[str, ...]:
    explicit = _explicit_extensions(item)
    if explicit:
        return explicit
    kind = str(item.get("kind") or "").strip().lower()
    mime_type = str(item.get("mime_type") or item.get("content_type") or "").strip().lower()
    if kind in _KIND_EXTENSIONS:
        return _KIND_EXTENSIONS[kind]
    return _extensions_from_open_key(kind) or _extensions_from_mime_type(mime_type or kind)


def _explicit_extensions(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[object] = []
    for holder in _extension_holders(item):
        values.extend(_extension_values(holder))
    return tuple(dict.fromkeys(_normalized_extension(value) for value in values if _normalized_extension(value)))


def _extension_holders(item: dict[str, Any]) -> list[dict[str, Any]]:
    holders = [item]
    intent = item.get("artifact_intent")
    if isinstance(intent, dict):
        holders.append(intent)
    validation = item.get("validation_contract")
    if isinstance(validation, dict):
        holders.append(validation)
    return holders


def _extension_values(holder: dict[str, Any]) -> list[object]:
    return [
        value
        for key in _EXPLICIT_EXTENSION_KEYS
        for value in _extension_value_items(holder.get(key))
    ]


def _extension_value_items(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return [value] if value else []


def _extensions_from_open_key(key: str) -> tuple[str, ...]:
    if not key or "/" in key:
        return ()
    extension = _normalized_extension(key)
    return (extension,) if extension else ()


def _extensions_from_mime_type(mime_type: str) -> tuple[str, ...]:
    if "/" not in mime_type:
        return ()
    guessed = tuple(
        dict.fromkeys(
            value
            for value in mimetypes.guess_all_extensions(mime_type, strict=False)
            if _normalized_extension(value)
        )
    )
    if guessed:
        return guessed
    subtype = mime_type.rsplit("/", 1)[-1].removeprefix("x-").split("+", 1)[0]
    extension = _normalized_extension(subtype)
    return (extension,) if extension else ()


def _normalized_extension(value: object) -> str:
    text = str(value or "").strip().lower().lstrip(".")
    if "/" in text or "\\" in text or not _SAFE_EXTENSION_RE.fullmatch(text):
        return ""
    return f".{text}"


def _search_roots(item: dict[str, Any]) -> list[str]:
    raw = item.get("allowed_output_roots") or item.get("search_roots") or item.get("artifact_roots")
    if isinstance(raw, list):
        roots = [str(value).strip() for value in raw if str(value).strip()]
        if roots:
            return roots
    return list(_DEFAULT_SEARCH_ROOTS)


def _select_by_structured_hint(item: dict[str, Any], candidates: list[Path]) -> Path | None:
    hints = [
        str(item.get("artifact_id") or "").strip().lower().replace("-", "_"),
        str(item.get("name") or "").strip().lower().replace("-", "_"),
    ]
    hints = [hint for hint in hints if hint]
    matches = [
        path
        for path in candidates
        for stem in [path.stem.lower().replace("-", "_")]
        if any(hint in stem for hint in hints)
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_output_path(raw_path: str, workspace: Path) -> Path | None:
    candidate = Path(raw_path).expanduser()
    try:
        return candidate.resolve(strict=False) if candidate.is_absolute() else (workspace / candidate).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _finding(code: str, value: str, *, evidence: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "code": code,
        "severity": "hard",
        "message": code.lower(),
        "value": value,
        **({"evidence": evidence} if evidence else {}),
    }


def _portable_ref(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(path)


__all__ = ["ArtifactLocatorResult", "artifact_can_be_located", "locate_artifact"]
