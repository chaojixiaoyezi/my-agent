# LLM: Document extractors normalize supported document formats into machine facts.
# 模块用途: 把 Markdown、HTML、DOCX、PDF、TXT 统一抽成 sections 和内容计数，不做任务质量判断。

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class DocumentSection:
    heading: str
    body: str
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True)
class DocumentContentFacts:
    text: str
    sections: tuple[DocumentSection, ...]
    paragraph_count: int = 0
    table_count: int = 0
    image_count: int = 0
    code_block_count: int = 0
    page_count: int = 0
    has_text_layer: bool = True


# LLM: extract_document_content_facts is the single extractor entrypoint used by quality gates.
# 函数用途: 根据文件后缀提取文档事实，调用方只消费统一 facts，不关心具体格式。
def extract_document_content_facts(path: Path) -> DocumentContentFacts:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return _markdown_facts(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".html", ".htm"}:
        return _html_facts(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".docx":
        return _docx_facts(path)
    if suffix == ".pdf":
        return _pdf_facts(path)
    return _text_facts(path.read_text(encoding="utf-8", errors="replace"))


def meaningful_char_count(text: str) -> int:
    return sum(1 for char in text if not char.isspace() and char not in "#*_`|-")


def _markdown_facts(text: str) -> DocumentContentFacts:
    lines = text.splitlines()
    heading_rows = [(index + 1, _heading_text(line)) for index, line in enumerate(lines) if _heading_text(line)]
    sections = _sections_from_heading_rows(lines, heading_rows)
    return DocumentContentFacts(
        text=text,
        sections=tuple(sections) or (DocumentSection("document", text, 1, len(lines)),),
        paragraph_count=_paragraph_count(text),
        table_count=sum(1 for line in lines if "|" in line),
        code_block_count=text.count("```") // 2,
    )


def _html_facts(text: str) -> DocumentContentFacts:
    plain = _strip_tags(text)
    heading_rows = [
        (1, unescape(match.group(1)).strip())
        for match in re.finditer(r"<h[1-6][^>]*>(.*?)</h[1-6]>", text, re.I | re.S)
        if match.group(1).strip()
    ]
    sections = [DocumentSection(heading, _html_section_body(text, heading), 1, 1) for _, heading in heading_rows]
    return DocumentContentFacts(
        text=plain,
        sections=tuple(sections) or (DocumentSection("document", plain, 1, 1),),
        paragraph_count=len(re.findall(r"<p\b", text, re.I)) or _paragraph_count(plain),
        table_count=len(re.findall(r"<table\b", text, re.I)),
        image_count=len(re.findall(r"<img\b", text, re.I)),
        code_block_count=len(re.findall(r"<pre\b|<code\b", text, re.I)),
    )


def _docx_facts(path: Path) -> DocumentContentFacts:
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[tuple[str, str]] = []
    table_count = len(root.findall(".//w:tbl", ns))
    for para in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in para.findall(".//w:t", ns)).strip()
        if not text:
            continue
        style = ""
        style_node = para.find(".//w:pStyle", ns)
        if style_node is not None:
            style = str(style_node.attrib.get(f"{{{ns['w']}}}val") or "")
        paragraphs.append((style, text))
    sections = _sections_from_styled_paragraphs(paragraphs)
    plain = "\n\n".join(text for _, text in paragraphs)
    return DocumentContentFacts(
        text=plain,
        sections=tuple(sections) or (DocumentSection("document", plain, 1, len(paragraphs)),),
        paragraph_count=len(paragraphs),
        table_count=table_count,
    )


def _pdf_facts(path: Path) -> DocumentContentFacts:
    data = path.read_bytes()
    page_count = data.count(b"/Type /Page")
    text = _pdf_text(data)
    return DocumentContentFacts(
        text=text,
        sections=(DocumentSection("document", text, 1, max(page_count, 1)),),
        paragraph_count=_paragraph_count(text),
        page_count=page_count,
        has_text_layer=bool(meaningful_char_count(text)),
    )


def _text_facts(text: str) -> DocumentContentFacts:
    return DocumentContentFacts(
        text=text,
        sections=(DocumentSection("document", text, 1, len(text.splitlines())),),
        paragraph_count=_paragraph_count(text),
    )


def _sections_from_heading_rows(lines: list[str], heading_rows: list[tuple[int, str]]) -> list[DocumentSection]:
    return [
        DocumentSection(heading, "\n".join(lines[line_no : next_line - 1]), line_no, next_line - 1)
        for (line_no, heading), next_line in zip(
            heading_rows,
            [*[row[0] for row in heading_rows[1:]], len(lines) + 1],
            strict=False,
        )
    ]


def _sections_from_styled_paragraphs(paragraphs: list[tuple[str, str]]) -> list[DocumentSection]:
    sections: list[DocumentSection] = []
    current_heading = ""
    current_body: list[str] = []
    start = 1
    for index, (style, text) in enumerate(paragraphs, start=1):
        if not style.lower().startswith("heading"):
            if current_heading:
                current_body.append(text)
            continue
        if current_heading:
            sections.append(DocumentSection(current_heading, "\n".join(current_body), start, index - 1))
        current_heading = text
        current_body = []
        start = index
    if current_heading:
        sections.append(DocumentSection(current_heading, "\n".join(current_body), start, len(paragraphs)))
    return sections


def _html_section_body(html: str, heading: str) -> str:
    index = html.find(heading)
    if index < 0:
        return ""
    next_heading = re.search(r"<h[1-6][^>]*>", html[index + len(heading) :], re.I)
    end = index + len(heading) + next_heading.start() if next_heading else len(html)
    return _strip_tags(html[index:end])


def _heading_text(line: str) -> str:
    match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
    return match.group(1).strip() if match else ""


def _strip_tags(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", value))


def _pdf_text(data: bytes) -> str:
    text = data.decode("latin-1", errors="ignore")
    chunks = re.findall(r"\(([^()]*)\)\s*Tj", text)
    chunks.extend(match.replace("\\)", ")").replace("\\(", "(") for match in re.findall(r"\(([^()]*)\)", text))
    return "\n".join(chunks)


def _paragraph_count(text: str) -> int:
    return len([part for part in re.split(r"\n\s*\n", text.strip()) if meaningful_char_count(part)])


__all__ = [
    "DocumentContentFacts",
    "DocumentSection",
    "extract_document_content_facts",
    "meaningful_char_count",
]
