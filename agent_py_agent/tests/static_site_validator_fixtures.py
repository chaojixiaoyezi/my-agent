from __future__ import annotations

from pathlib import Path


def _write_site(root: Path, files: dict[str, str]) -> Path:
    site = root / "site"
    for rel_path, content in files.items():
        path = site / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return site
