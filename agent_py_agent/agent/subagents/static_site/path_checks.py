
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

REMOTE_SCHEMES = {"http", "https", "mailto", "tel", "data", "javascript"}


def broken_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[str]:
    broken: list[str] = []
    for attr, ref in refs:
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and not target.exists():
            broken.append(f"{rel(html_file, site_root)}:{attr}={ref}")
    return broken


def local_ref_target(ref: str, html_file: Path, site_root: Path) -> Path | None:
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("#") or cleaned.startswith("//"):
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() in REMOTE_SCHEMES:
        return None
    path_part = parsed.path.strip()
    if not path_part:
        return None
    candidate = _local_candidate(path_part, html_file, site_root)
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if inside(candidate, site_root) else None


def local_script_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[Path]:
    paths: list[Path] = []
    for attr, ref in refs:
        if attr != "src":
            continue
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and target.suffix.lower() == ".js" and target.exists():
            paths.append(target)
    return paths


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def small_text(path: Path, max_bytes: int = 262144) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _local_candidate(path_part: str, html_file: Path, site_root: Path) -> Path:
    if path_part.startswith("/"):
        return (site_root / path_part.lstrip("/")).resolve()
    return (html_file.parent / path_part).resolve()
