
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .artifact_html_contract import record_resource_ref


@dataclass
class HtmlArtifactRefs:
    links: list[tuple[str, str]] = field(default_factory=list)
    images: list[tuple[str, str]] = field(default_factory=list)
    resources: list[tuple[str, str, str]] = field(default_factory=list)


class HtmlArtifactRefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs = HtmlArtifactRefs()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag_name = tag.lower()
        if tag_name == "a":
            self.refs.links.append(("href", values.get("href", "")))
        if tag_name == "img":
            self.refs.images.append(("src", values.get("src", "")))
        record_resource_ref(self.refs.resources, tag_name, values)


def scan_html_refs(text: str) -> HtmlArtifactRefs:
    parser = HtmlArtifactRefParser()
    parser.feed(text)
    return parser.refs


def image_ref_findings(refs: HtmlArtifactRefs, *, path: Path, workspace_root: Path | None) -> list[dict[str, str]]:
    findings = [
        finding
        for attr, value in refs.images
        if (finding := _image_ref_finding(attr, value, path=path, workspace_root=workspace_root)) is not None
    ]
    return findings


def _image_ref_finding(attr: str, value: str, *, path: Path, workspace_root: Path | None) -> dict[str, str] | None:
    src = value.strip()
    if src.lower().startswith(("http://", "https://")):
        return _finding(
            "HTML_EXTERNAL_IMAGE_REF",
            "Image uses an external URL; local/offline validation cannot guarantee it will render.",
            location=f"img[{attr}]",
            value=src,
        )
    if src and not _local_image_ref_exists(src, path=path, workspace_root=workspace_root):
        return _finding(
            "HTML_LOCAL_IMAGE_MISSING",
            "Image points to a local file that does not exist.",
            location=f"img[{attr}]",
            value=src,
        )
    return None


def _local_image_ref_exists(src: str, *, path: Path, workspace_root: Path | None) -> bool:
    if src.startswith(("data:", "#")):
        return True
    candidate = Path(src)
    if candidate.is_absolute():
        return candidate.exists()
    candidates = [path.parent / candidate]
    if workspace_root is not None:
        candidates.append(Path(workspace_root) / candidate)
    return any(item.exists() for item in candidates)


def _finding(code: str, message: str, *, location: str = "", value: str = "") -> dict[str, str]:
    return {"code": code, "severity": "hard", "message": message, "location": location, "value": value}


__all__ = ["HtmlArtifactRefs", "image_ref_findings", "scan_html_refs"]
