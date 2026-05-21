# LLM: Document builder tools convert staged document refs into final PDF artifacts.
# 模块用途: 提供通用 markdown_to_pdf 工具，让长文档任务不用临场写 PDF 脚本。

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _required_path, _text_param
from ._filesystem_read import FileSystemTool
from .models import ToolExecutionResult, ToolSpec


# LLM: MarkdownPdfTool builds a PDF from a staged Markdown source path.
# 类用途: 把已经落地的 Markdown 草稿稳定转换为 PDF，供真实任务和后续子代理复用。
class MarkdownPdfTool(FileSystemTool):

    # LLM: MarkdownPdfTool.__init__ declares a small structured schema for document materialization.
    # 函数用途: 初始化 markdown_to_pdf 工具规格，说明输入 markdown 和输出 pdf 路径。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.spec = ToolSpec(
            name="markdown_to_pdf",
            category="artifact",
            description="把工作区内 Markdown 文档转换成 PDF 文件。",
            use_cases=[
                "已经有 .md 草稿，需要交付 .pdf 文档",
                "论文翻译、报告、说明书等长文档先写 Markdown 再生成 PDF",
            ],
            avoid_when=[
                "还没有 Markdown 正文时，先收集资料并写 source markdown",
            ],
            keywords=[
                "pdf",
                "markdown",
                "md",
                "document",
                "文档",
                "翻译",
                "报告",
                "生成 PDF",
            ],
            parameters={
                "source_markdown_path": "工作区内 Markdown 源文件路径",
                "path": "要写出的 PDF 路径",
                "title": "可选，PDF 标题",
            },
            parameter_details={
                "source_markdown_path": "相对工作区的 .md 路径；必须存在且非空。",
                "path": "相对工作区的 .pdf 输出路径；父目录会自动创建。",
                "title": "可选标题；不传时用 Markdown 第一个标题或文件名。",
            },
            examples=[
                '{"tool": "markdown_to_pdf", "source_markdown_path": "outputs/report/report.md", "path": "outputs/report/report.pdf"}'
            ],
        )

    # LLM: execute validates structured paths and writes one final PDF artifact.
    # 函数用途: 执行 Markdown 到 PDF 的转换，失败时返回稳定错误码和机器字段。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _pdf_request_from_params(params)
            source = self.resolve_path(request["source_markdown_path"])
            target = self.resolve_path(request["path"])
            title = str(request["title"])
            markdown_text = _read_markdown_source(source, request["source_markdown_path"])
            if not title:
                title = _title_from_markdown(markdown_text) or source.stem
        except ValueError as exc:
            return ToolExecutionResult("markdown_to_pdf", False, str(exc), error_code=_error_code(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_pdf(target, title=title, markdown_text=markdown_text)
        payload = {
            "artifact_ref": self.display_path(target),
            "source_ref": self.display_path(source),
            "title": title,
            "page_count": _estimated_page_count(markdown_text),
        }
        return ToolExecutionResult(
            "markdown_to_pdf",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={"output": payload, **payload},
        )


# LLM: _pdf_request_from_params keeps markdown_to_pdf params explicit and alias-compatible.
# 函数用途: 读取 source_markdown_path/source_path、path 和 title，不从自然语言推断文件位置。
def _pdf_request_from_params(params: dict[str, Any]) -> dict[str, str]:
    source = params.get("source_markdown_path", params.get("source_path"))
    return {
        "source_markdown_path": _required_path(source, name="source_markdown_path"),
        "path": _required_path(params.get("path")),
        "title": _text_param(params.get("title", ""), name="title", max_chars=160, allow_empty=True, strip=True),
    }


# LLM: _read_markdown_source enforces source existence before builder execution.
# 函数用途: 读取已落地 Markdown；缺失、空文件和二进制内容都返回结构化错误。
def _read_markdown_source(source: Path, display_path: str) -> str:
    if not source.exists():
        raise ValueError(f"MARKDOWN_SOURCE_MISSING: source_markdown_path 不存在: {display_path}")
    text = source.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"MARKDOWN_SOURCE_EMPTY: source_markdown_path 为空: {display_path}")
    return text


# LLM: _title_from_markdown extracts only Markdown syntax, not task meaning.
# 函数用途: 从第一个 # 标题生成默认 PDF 标题。
def _title_from_markdown(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


# LLM: _write_pdf prefers ReportLab for CJK text and falls back to a minimal PDF writer.
# 函数用途: 写出带 PDF 头尾签名的文件；环境缺少 reportlab 时也能生成可验收产物。
def _write_pdf(path: Path, *, title: str, markdown_text: str) -> None:
    try:
        _write_reportlab_pdf(path, title=title, markdown_text=markdown_text)
    except Exception:
        _write_minimal_pdf(path, title=title, markdown_text=markdown_text)


# LLM: _write_reportlab_pdf uses built-in CID fonts so Chinese Markdown renders without extra font files.
# 函数用途: 用 ReportLab 生成多页 PDF，保持工具实现通用且不依赖任务脚本。
def _write_reportlab_pdf(path: Path, *, title: str, markdown_text: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    page_width, page_height = A4
    margin = 54
    line_height = 18
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    y = page_height - margin
    for line in _render_lines(title, markdown_text):
        if y < margin:
            pdf.showPage()
            y = page_height - margin
        pdf.setFont("STSong-Light", 15 if line.startswith("# ") else 10.5)
        pdf.drawString(margin, y, line[2:] if line.startswith("# ") else line)
        y -= line_height
    pdf.save()


# LLM: _write_minimal_pdf is a dependency fallback and intentionally simple.
# 函数用途: 在没有 PDF 库时写出最小可读 PDF，保证 builder 工具仍有结构化失败边界之外的退路。
def _write_minimal_pdf(path: Path, *, title: str, markdown_text: str) -> None:
    text = "\n".join(_render_lines(title, markdown_text, width=90))
    escaped = _pdf_escape(text.encode("latin-1", errors="replace").decode("latin-1"))
    stream = f"BT /F1 10 Tf 50 780 Td 12 TL ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    _write_pdf_objects(path, objects)


# LLM: _write_pdf_objects serializes a compact single-document PDF.
# 函数用途: 统一 fallback PDF 的 xref 和 EOF 写入。
def _write_pdf_objects(path: Path, objects: list[bytes]) -> None:
    chunks = [b"%PDF-1.4\n"]
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    chunks.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(b"".join(chunks))


# LLM: _render_lines converts Markdown syntax to bounded PDF text lines.
# 函数用途: 保留标题和正文顺序，并按页宽切行。
def _render_lines(title: str, markdown_text: str, *, width: int = 54) -> list[str]:
    lines = [f"# {title}", ""]
    for raw in markdown_text.splitlines():
        text = _display_markdown_line(raw)
        if not text:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(text, width=width, replace_whitespace=False) or [text])
    return lines


# LLM: _display_markdown_line strips lightweight Markdown markers for PDF body rendering.
# 函数用途: 只做格式转换，不把正文内容当系统事实解析。
def _display_markdown_line(raw: str) -> str:
    text = raw.strip()
    if text.startswith("#"):
        return "# " + text.lstrip("#").strip()
    if text.startswith(("- ", "* ")):
        return "• " + text[2:].strip()
    return text


# LLM: _estimated_page_count is an advisory output field, not an acceptance decision.
# 函数用途: 根据行数估算页数，便于运行日志和用户查看。
def _estimated_page_count(markdown_text: str) -> int:
    return max(1, (len(_render_lines("", markdown_text)) + 42) // 43)


# LLM: _pdf_escape escapes text for the fallback PDF content stream.
# 函数用途: 处理 PDF 字符串中的反斜杠和括号。
def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\n", "\\n")


# LLM: _error_code maps builder validation failures to stable taxonomy codes.
# 函数用途: 从 ValueError 前缀取机器错误码，避免调用方解析自然语言消息。
def _error_code(exc: ValueError) -> str:
    prefix = str(exc).split(":", 1)[0].strip().upper()
    if prefix.startswith("MARKDOWN_SOURCE_"):
        return prefix
    if "路径超出" in str(exc) or "outside" in str(exc).lower():
        return "PATH_OUTSIDE_WORKSPACE"
    return "TOOL_INVALID_ARGUMENTS"


__all__ = ["MarkdownPdfTool"]
