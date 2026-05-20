# LLM: HTML ref scanning extracts machine facts for artifact acceptance without owning report classes.
# 模块用途: 扫描 HTML 的链接、图片和运行期资源，并返回 JSON-shaped findings 给通用验收器包装。

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .artifact_html_contract import record_resource_ref


# LLM: HtmlArtifactRefs carries bounded refs from one HTML artifact.
# 类用途: 保存链接、图片和外部资源事实，不保存完整正文。
@dataclass
class HtmlArtifactRefs:
    links: list[tuple[str, str]] = field(default_factory=list)
    images: list[tuple[str, str]] = field(default_factory=list)
    resources: list[tuple[str, str, str]] = field(default_factory=list)


# LLM: HtmlArtifactRefParser extracts only refs relevant to generic HTML acceptance.
# 类用途: 使用标准库 HTMLParser 扫描 href/src，不执行网页代码。
class HtmlArtifactRefParser(HTMLParser):
    # LLM: __init__ initializes the refs bundle for one HTML document.
    # 函数用途: 准备扫描容器；不做文件 I/O。
    def __init__(self) -> None:
        super().__init__()
        self.refs = HtmlArtifactRefs()

    # LLM: handle_starttag records links, images, and runtime resources as structured facts.
    # 函数用途: 从 a/img/link/script 等标签读取关键属性，供验收规则消费。
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag_name = tag.lower()
        if tag_name == "a":
            self.refs.links.append(("href", values.get("href", "")))
        if tag_name == "img":
            self.refs.images.append(("src", values.get("src", "")))
        record_resource_ref(self.refs.resources, tag_name, values)


# LLM: scan_html_refs parses a text document into bounded link/image/resource refs.
# 函数用途: 对 HTML 正文做一次轻量扫描，返回后续验收能复用的结构化引用。
def scan_html_refs(text: str) -> HtmlArtifactRefs:
    parser = HtmlArtifactRefParser()
    parser.feed(text)
    return parser.refs


# LLM: image_ref_findings marks external or missing local image refs.
# 函数用途: 标记外部图片和缺失本地图片；不把 Google Fonts 等非图片链接误判为图片问题。
def image_ref_findings(refs: HtmlArtifactRefs, *, path: Path, workspace_root: Path | None) -> list[dict[str, str]]:
    findings = [
        finding
        for attr, value in refs.images
        if (finding := _image_ref_finding(attr, value, path=path, workspace_root=workspace_root)) is not None
    ]
    return findings


# LLM: _image_ref_finding classifies one image ref without deepening the batch loop.
# 函数用途: 判断单个 img[src] 是外部图片、缺失本地图片还是可接受引用。
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


# LLM: _local_image_ref_exists resolves relative image paths against artifact and workspace roots.
# 函数用途: 判断本地图片引用是否存在，支持相对 HTML 文件和相对 workspace 两种常见写法。
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


# LLM: _finding keeps HTML ref issues JSON-shaped before conversion to ArtifactFinding.
# 函数用途: 生成稳定 finding 字典，避免和 artifact_acceptance 的报告类循环导入。
def _finding(code: str, message: str, *, location: str = "", value: str = "") -> dict[str, str]:
    return {"code": code, "severity": "hard", "message": message, "location": location, "value": value}


__all__ = ["HtmlArtifactRefs", "image_ref_findings", "scan_html_refs"]
