
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactIntegrityCheckRequest:
    path: Path
    text: str | None = None
    require_complete: bool = True


@dataclass(frozen=True)
class ArtifactIntegrityIssue:
    code: str
    message: str
    severity: str = "blocker"
    count: int = 1
    examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactIntegrityDecision:
    ok: bool
    kind: str = "generic"
    issues: list[ArtifactIntegrityIssue] = field(default_factory=list)

    @property
    def blocker_codes(self) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == "blocker"]

    @property
    def warning_codes(self) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == "warning"]


_HTML_ID_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_HTML_ANCHOR_TAG_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", re.IGNORECASE | re.DOTALL)
_HTML_HREF_ATTR_RE = re.compile(r"\bhref\s*=\s*(['\"])(?P<href>.*?)\1", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_LINK_ISSUE_EXAMPLES = 5
_MAX_LINK_LABEL_CHARS = 60


def check_artifact_integrity(request: ArtifactIntegrityCheckRequest) -> ArtifactIntegrityDecision:
    path = Path(request.path)
    if not _looks_like_html_path(path):
        return ArtifactIntegrityDecision(ok=True)
    if request.text is None:
        if not path.exists():
            return _decision("html", [_issue("artifact_missing", f"产物不存在: {path}")])
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _decision("html", [_issue("not_utf8_text", f"HTML 产物不是 UTF-8 文本: {path}")])
    else:
        text = request.text
    return _check_html_text(text, require_complete=request.require_complete)


def check_web_project_post_write(path: Path, workspace_root: Path) -> ArtifactIntegrityDecision:
    from .web_project_integrity import check_web_project_post_write as _check

    return _check(path, workspace_root)


def artifact_integrity_payload(decision: ArtifactIntegrityDecision, path: Path) -> dict[str, Any]:
    return {
        "kind": decision.kind,
        "path": str(Path(path)),
        "ok": decision.ok,
        "blocker_codes": decision.blocker_codes,
        "warning_codes": decision.warning_codes,
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "count": issue.count,
                "examples": issue.examples[:3],
            }
            for issue in decision.issues[:12]
        ],
    }


def web_project_post_write_note(decision: ArtifactIntegrityDecision) -> str:
    if decision.kind != "web_project" or decision.ok:
        return ""
    codes = ", ".join(issue.code for issue in decision.issues[:6])
    return (
        "Web 项目完整性失败: web_project_integrity_failed=true "
        f"codes={codes}。请修复这些结构化问题后再声明完成。"
    )


def html_post_write_note(path: Path, text: str) -> str:
    if not _looks_like_html_path(path):
        return ""
    decision = _check_html_text(text, require_complete=False)
    if not decision.issues:
        return "HTML 完整性提示: 当前结构没有发现明显问题。"
    codes = ", ".join(issue.code for issue in decision.issues[:3])
    if "missing_html_close" in decision.warning_codes or "missing_body_close" in decision.warning_codes:
        return (
            "HTML 完整性提示: 当前文件还未闭合，继续分块可以；"
            "最后一块再写 </body></html>，闭合后不要再 append。"
        )
    detail = "；".join(_issue_brief(issue) for issue in decision.issues[:3])
    return f"HTML 完整性提示: 发现需要修复的结构问题 codes={codes}；{detail}。"


def _check_html_text(text: str, *, require_complete: bool) -> ArtifactIntegrityDecision:
    lowered = text.lower()
    issues: list[ArtifactIntegrityIssue] = []
    body_count = lowered.count("</body>")
    html_count = lowered.count("</html>")
    if require_complete and body_count == 0:
        issues.append(_issue("missing_body_close", "HTML 缺少 </body> 结束标签。"))
    if require_complete and html_count == 0:
        issues.append(_issue("missing_html_close", "HTML 缺少 </html> 结束标签。"))
    if not require_complete and body_count == 0:
        issues.append(_issue("missing_body_close", "HTML 当前还未写到 </body>。", severity="warning"))
    if not require_complete and html_count == 0:
        issues.append(_issue("missing_html_close", "HTML 当前还未写到 </html>。", severity="warning"))
    if body_count > 1:
        issues.append(_issue("multiple_body_close", "HTML 出现多个 </body> 结束标签。"))
    if html_count > 1:
        issues.append(_issue("multiple_html_close", "HTML 出现多个 </html> 结束标签。"))
    if body_count and html_count and lowered.rfind("</body>") > lowered.rfind("</html>"):
        issues.append(_issue("body_close_after_html_close", "</body> 出现在 </html> 之后。"))
    if html_count:
        html_end = lowered.rfind("</html>") + len("</html>")
        if text[html_end:].strip():
            issues.append(_issue("content_after_html_close", "</html> 后面还有非空内容。"))
    issues.extend(_html_link_issues(text))
    return _decision("html", issues)


def _html_link_issues(text: str) -> list[ArtifactIntegrityIssue]:
    ids = set(_HTML_ID_RE.findall(text or ""))
    buckets: dict[str, dict[str, object]] = {}
    for href, label in _html_anchor_hrefs(text or ""):
        cleaned = href.strip()
        code = _html_link_issue_code(cleaned, ids)
        if not code:
            continue
        bucket = buckets.setdefault(code, {"count": 0, "examples": [], "first_href": cleaned})
        bucket["count"] = int(bucket["count"]) + 1
        examples = bucket["examples"]
        if isinstance(examples, list) and len(examples) < _MAX_LINK_ISSUE_EXAMPLES:
            examples.append(_html_link_issue_example(label, cleaned))
    return [
        ArtifactIntegrityIssue(
            code=code,
            message=_html_link_issue_message(
                code,
                str(bucket.get("first_href") or ""),
                count=int(bucket.get("count") or 1),
                examples=[str(item) for item in bucket.get("examples", [])],
            ),
            severity="warning",
            count=int(bucket.get("count") or 1),
            examples=[str(item) for item in bucket.get("examples", [])],
        )
        for code, bucket in buckets.items()
    ]


def _html_anchor_hrefs(text: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for match in _HTML_ANCHOR_TAG_RE.finditer(text or ""):
        href_match = _HTML_HREF_ATTR_RE.search(match.group("attrs") or "")
        if not href_match:
            continue
        items.append((href_match.group("href"), _clean_anchor_label(match.group("label") or "")))
    return items


def _html_link_issue_code(href: str, ids: set[str]) -> str:
    if href == "#":
        return "placeholder_hash_link"
    if href.startswith("#") and href[1:] not in ids:
        return "missing_hash_target"
    return ""


def _html_link_issue_message(
    code: str,
    href: str,
    *,
    count: int = 1,
    examples: list[str] | None = None,
) -> str:
    example_text = _examples_text(examples or [])
    suffix = f" 共 {count} 处{example_text}。" if count > 1 else f"{example_text}。"
    if code == "placeholder_hash_link":
        return f'HTML 存在 href="#" 占位链接，{suffix}请改成真实页面内 id、真实 URL、tel/mailto 或移除链接。'
    return f"HTML 链接 {href} 指向不存在的页面内 id，{suffix}请补对应 id 或改成真实目标。"


def _html_link_issue_example(label: str, href: str) -> str:
    clean_label = _clip(_clean_anchor_label(label) or "<empty>", _MAX_LINK_LABEL_CHARS)
    return f"{clean_label} href={href or '<empty>'}"


def _clean_anchor_label(label: str) -> str:
    without_tags = _HTML_TAG_RE.sub(" ", label or "")
    return _clip(" ".join(unescape(without_tags).split()), _MAX_LINK_LABEL_CHARS)


def _examples_text(examples: list[str]) -> str:
    if not examples:
        return ""
    return f"（示例: {'; '.join(examples[:_MAX_LINK_ISSUE_EXAMPLES])}）"


def _issue_brief(issue: ArtifactIntegrityIssue) -> str:
    count = f" x{issue.count}" if issue.count > 1 else ""
    examples = f" examples={'; '.join(issue.examples[:3])}" if issue.examples else ""
    return f"{issue.code}{count}{examples}"


def _looks_like_html_path(path: Path) -> bool:
    return path.suffix.lower() in {".html", ".htm"}


def _issue(code: str, message: str, *, severity: str = "blocker") -> ArtifactIntegrityIssue:
    return ArtifactIntegrityIssue(code=code, message=message, severity=severity)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def _decision(kind: str, issues: list[ArtifactIntegrityIssue]) -> ArtifactIntegrityDecision:
    return ArtifactIntegrityDecision(
        ok=not any(issue.severity == "blocker" for issue in issues),
        kind=kind,
        issues=issues,
    )
