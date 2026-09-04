"""LLM: Parse HTML references and validate local images only through explicit no-follow roots.

模块用途: 提取 HTML 链接、图片和资源引用，并阻止绝对外部路径或目录链接冒充可交付本地资源。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from ..common.nofollow_fs import regular_file_exists_beneath
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


# LLM: `reference_roots` carries host-selected logical artifact locations when the HTML bytes are
# validated from an immutable private snapshot. It only supplies lookup roots and grants no write access.
# 函数用途: 检查 HTML 图片引用，并允许从真实产物目录查找快照旁边不存在的资源。
def image_ref_findings(
    refs: HtmlArtifactRefs,
    *,
    path: Path,
    workspace_root: Path | None,
    reference_roots: tuple[Path | str, ...] = (),
) -> list[dict[str, str]]:
    findings = [
        finding
        for attr, value in refs.images
        if (
            finding := _image_ref_finding(
                attr,
                value,
                path=path,
                workspace_root=workspace_root,
                reference_roots=reference_roots,
            )
        )
        is not None
    ]
    return findings


def _image_ref_finding(
    attr: str,
    value: str,
    *,
    path: Path,
    workspace_root: Path | None,
    reference_roots: tuple[Path | str, ...],
) -> dict[str, str] | None:
    src = value.strip()
    # Jinja/Django/ERB/JS 模板资源要到运行时才能解析；它们不是静态本地路径，不能拿
    # Path.exists() 判缺失。只跳过成对的模板表达式，普通 missing.png 仍严格失败。
    if _dynamic_template_ref(src):
        return None
    if src.lower().startswith(("http://", "https://")):
        return _finding(
            "HTML_EXTERNAL_IMAGE_REF",
            "Image uses an external URL; local/offline validation cannot guarantee it will render.",
            location=f"img[{attr}]",
            value=src,
        )
    if src and not _local_image_ref_exists(
        src,
        path=path,
        workspace_root=workspace_root,
        reference_roots=reference_roots,
    ):
        return _finding(
            "HTML_LOCAL_IMAGE_MISSING",
            "Image points to a local file that does not exist.",
            location=f"img[{attr}]",
            value=src,
        )
    return None


def _dynamic_template_ref(value: str) -> bool:
    return any(start in value and end in value[value.find(start) + len(start) :] for start, end in (
        ("{{", "}}"),
        ("{%", "%}"),
        ("<%", "%>"),
        ("${", "}"),
    ))


def _local_image_ref_exists(
    src: str,
    *,
    path: Path,
    workspace_root: Path | None,
    reference_roots: tuple[Path | str, ...],
) -> bool:
    if src.startswith(("data:", "#")):
        return True
    candidate = Path(src).expanduser()
    roots = [_lexical_absolute(path.parent)]
    if workspace_root is not None:
        roots.append(_lexical_absolute(workspace_root))
    roots.extend(_lexical_absolute(root) for root in reference_roots)
    roots = list(dict.fromkeys(roots))
    candidates = (
        [_lexical_absolute(candidate)]
        if candidate.is_absolute()
        else [_lexical_absolute(root / candidate) for root in roots]
    )
    for item in candidates:
        for root in roots:
            try:
                relative = item.relative_to(root)
            except ValueError:
                continue
            if relative.parts and regular_file_exists_beneath(root, tuple(relative.parts)):
                return True
    return False


# LLM: Reference validation must not resolve symlinks before the no-follow filesystem seam runs.
# 函数用途: 把 HTML 引用和受信根做纯词法绝对化，保留后续链接检查能力。
def _lexical_absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(Path(value).expanduser()))))


def _finding(code: str, message: str, *, location: str = "", value: str = "") -> dict[str, str]:
    return {"code": code, "severity": "hard", "message": message, "location": location, "value": value}


__all__ = ["HtmlArtifactRefs", "image_ref_findings", "scan_html_refs"]
