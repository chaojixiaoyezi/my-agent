# LLM: artifact integrity checks are small machine gates around model-written files, not business acceptance.
# 模块用途: 对模型产出的本地文件做低成本结构检查，防止明显损坏的产物进入父级验收。

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path


# LLM: ArtifactIntegrityCheckRequest keeps validation inputs bundled for future file types.
# 类用途: 描述一次产物完整性检查；path 是本地文件，text 可由调用方传入以避免重复读取。
@dataclass(frozen=True)
class ArtifactIntegrityCheckRequest:
    path: Path
    text: str | None = None
    require_complete: bool = True


# LLM: ArtifactIntegrityIssue gives parent agents compact, code-based reasons instead of long file bodies.
# 类用途: 保存产物检查发现的问题；code 供测试和上层决策使用，message 给模型修复时阅读。
@dataclass(frozen=True)
class ArtifactIntegrityIssue:
    code: str
    message: str
    severity: str = "blocker"
    count: int = 1
    examples: list[str] = field(default_factory=list)


# LLM: ArtifactIntegrityDecision is the machine-readable result consumed by tools and runner finalize.
# 类用途: 汇总产物是否可继续验收，以及具体 blocker/warning 代码。
@dataclass(frozen=True)
class ArtifactIntegrityDecision:
    ok: bool
    kind: str = "generic"
    issues: list[ArtifactIntegrityIssue] = field(default_factory=list)

    # LLM: blocker_codes keeps tests and status messages stable as human wording evolves.
    # 函数用途: 返回所有阻塞级问题代码，方便 runner 写入 blocked_reason。
    @property
    def blocker_codes(self) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == "blocker"]

    # LLM: warning_codes mirrors blocker_codes for non-blocking guidance.
    # 函数用途: 返回所有提示级问题代码，供工具输出给模型纠偏。
    @property
    def warning_codes(self) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == "warning"]


# LLM: HtmlAppendGuardRequest separates append safety from final artifact acceptance.
# 类用途: 描述一次 HTML append 是否允许；用于阻止把内容追加到已经闭合的 HTML 后面。
@dataclass(frozen=True)
class HtmlAppendGuardRequest:
    path: Path
    existing_text: str
    append_text: str


# LLM: HtmlAppendGuardDecision is intentionally tiny so filesystem tools can fail fast.
# 类用途: 返回 append 是否允许，以及给模型的下一步修复提示。
@dataclass(frozen=True)
class HtmlAppendGuardDecision:
    allowed: bool
    message: str = ""


_HTML_ID_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_HTML_ANCHOR_TAG_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", re.IGNORECASE | re.DOTALL)
_HTML_HREF_ATTR_RE = re.compile(r"\bhref\s*=\s*(['\"])(?P<href>.*?)\1", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_LINK_ISSUE_EXAMPLES = 5
_MAX_LINK_LABEL_CHARS = 60


# LLM: check_artifact_integrity validates supported artifact formats with bounded local parsing.
# 函数用途: 检查本地产物是否存在基础结构问题；目前先覆盖 HTML，后续可扩展到 JSON、Markdown、图片清单等。
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


# LLM: check_html_append_allowed blocks the exact failure mode where chunks continue after </html>.
# 函数用途: append_file 写 HTML 时，如果文件已经闭合且新内容不是空白，就拒绝追加并提示正确修复路径。
def check_html_append_allowed(request: HtmlAppendGuardRequest) -> HtmlAppendGuardDecision:
    path = Path(request.path)
    if not _looks_like_html_path(path):
        return HtmlAppendGuardDecision(allowed=True)
    if "</html>" not in request.existing_text.lower():
        return HtmlAppendGuardDecision(allowed=True)
    if not request.append_text.strip():
        return HtmlAppendGuardDecision(allowed=True)
    return HtmlAppendGuardDecision(
        allowed=False,
        message=(
            "HTML 文件已经闭合，不能继续把正文 append 到 </html> 后面。"
            "请用 replace_in_file 插入到 </body> 前，或重写完整文件；"
            "如果在分块写长 HTML，请只在最后一块写 </body></html>。"
        ),
    )


# LLM: html_post_write_note gives models immediate bounded feedback after successful writes.
# 函数用途: 写入/追加 HTML 后返回一行结构提示，避免模型继续用错误分块策略。
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


# LLM: _check_html_text implements conservative structural checks without becoming a browser validator.
# 函数用途: 只检查最容易导致 E2E 失败的 HTML 闭合和追加污染问题。
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
    if require_complete:
        issues.extend(_html_skeleton_issues(lowered))
    if body_count and html_count and lowered.rfind("</body>") > lowered.rfind("</html>"):
        issues.append(_issue("body_close_after_html_close", "</body> 出现在 </html> 之后。"))
    if html_count:
        html_end = lowered.rfind("</html>") + len("</html>")
        if text[html_end:].strip():
            issues.append(_issue("content_after_html_close", "</html> 后面还有非空内容。"))
    if require_complete:
        issues.extend(_html_balanced_tag_issues(lowered))
    issues.extend(_html_link_issues(text))
    return _decision("html", issues)


# LLM: _html_skeleton_issues is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _html_skeleton_issues(lowered: str) -> list[ArtifactIntegrityIssue]:
    issues: list[ArtifactIntegrityIssue] = []
    for tag in ("html", "head", "body"):
        opens = len(re.findall(rf"<{tag}\b", lowered))
        closes = lowered.count(f"</{tag}>")
        if opens > 1:
            issues.append(_issue(f"multiple_{tag}_open", f"HTML 出现多个 <{tag}> 起始标签。"))
        if tag == "head" and closes > 1:
            issues.append(_issue("multiple_head_close", "HTML 出现多个 </head> 结束标签。"))
    return issues


# LLM: _html_balanced_tag_issues is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _html_balanced_tag_issues(lowered: str) -> list[ArtifactIntegrityIssue]:
    issues: list[ArtifactIntegrityIssue] = []
    for tag in ("style", "script"):
        opens = len(re.findall(rf"<{tag}\b", lowered))
        closes = lowered.count(f"</{tag}>")
        if opens != closes:
            issues.append(_issue(f"unbalanced_{tag}", f"HTML 的 <{tag}> 和 </{tag}> 数量不一致。"))
    return issues


# LLM: _html_link_issues catches fake in-page links without turning this into a full browser validator.
# 函数用途: 识别 href="#" 和缺失目标 id 的 hash 链接，给模型即时修复提示；最终验收仍由静态站点 validator 深查。
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


# LLM: _html_anchor_hrefs extracts only bounded anchor metadata for repair diagnostics.
# 函数用途: 从 HTML 中取 a 标签的 href 和可读文本，避免把完整页面正文塞进进度包。
def _html_anchor_hrefs(text: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for match in _HTML_ANCHOR_TAG_RE.finditer(text or ""):
        href_match = _HTML_HREF_ATTR_RE.search(match.group("attrs") or "")
        if not href_match:
            continue
        items.append((href_match.group("href"), _clean_anchor_label(match.group("label") or "")))
    return items


# LLM: _html_link_issue_code classifies only local inert anchors; normal external links are not touched.
# 函数用途: href="#" 属于占位链接；#id 必须对应真实 id，否则也提示修复。
def _html_link_issue_code(href: str, ids: set[str]) -> str:
    if href == "#":
        return "placeholder_hash_link"
    if href.startswith("#") and href[1:] not in ids:
        return "missing_hash_target"
    return ""


# LLM: _html_link_issue_message keeps user-facing write feedback short and actionable.
# 函数用途: 根据 issue code 生成中文修复建议，避免模型继续把假链接当可验收功能。
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


# LLM: _html_link_issue_example gives the model a concrete repair handle without including whole tags.
# 函数用途: 把 `<a>` 标签压成“文本 href=目标”的小例子，帮助模型精准替换对应链接。
def _html_link_issue_example(label: str, href: str) -> str:
    clean_label = _clip(_clean_anchor_label(label) or "<empty>", _MAX_LINK_LABEL_CHARS)
    return f"{clean_label} href={href or '<empty>'}"


# LLM: _clean_anchor_label normalizes nested anchor text for compact issue examples.
# 函数用途: 去掉标签、解码实体、压缩空白，避免完整 HTML 进入提示词。
def _clean_anchor_label(label: str) -> str:
    without_tags = _HTML_TAG_RE.sub(" ", label or "")
    return _clip(" ".join(unescape(without_tags).split()), _MAX_LINK_LABEL_CHARS)


# LLM: _examples_text keeps issue messages useful but bounded.
# 函数用途: 将最多几个链接示例拼进 message，给模型看具体修复点。
def _examples_text(examples: list[str]) -> str:
    if not examples:
        return ""
    return f"（示例: {'; '.join(examples[:_MAX_LINK_ISSUE_EXAMPLES])}）"


# LLM: _issue_brief renders one issue for short post-write tool feedback.
# 函数用途: 写工具返回时用一行说明 code、数量和示例，避免模型只看到抽象错误码。
def _issue_brief(issue: ArtifactIntegrityIssue) -> str:
    count = f" x{issue.count}" if issue.count > 1 else ""
    examples = f" examples={'; '.join(issue.examples[:3])}" if issue.examples else ""
    return f"{issue.code}{count}{examples}"


# LLM: _looks_like_html_path keeps the first integrity gate scoped to web artifacts only.
# 函数用途: 判断路径是否应按 HTML 结构检查，避免影响普通文本和代码文件。
def _looks_like_html_path(path: Path) -> bool:
    return path.suffix.lower() in {".html", ".htm"}


# LLM: _issue keeps issue construction readable at call sites.
# 函数用途: 创建统一的产物完整性 issue 对象。
def _issue(code: str, message: str, *, severity: str = "blocker") -> ArtifactIntegrityIssue:
    return ArtifactIntegrityIssue(code=code, message=message, severity=severity)


# LLM: _clip bounds diagnostic text that may come from model-written artifacts.
# 函数用途: 裁剪链接文本和提示片段，防止长正文进入 tool feedback 或 progress packet。
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


# LLM: _decision centralizes ok semantics so warnings do not block acceptance.
# 函数用途: 根据 blocker 是否存在生成最终完整性检查结果。
def _decision(kind: str, issues: list[ArtifactIntegrityIssue]) -> ArtifactIntegrityDecision:
    return ArtifactIntegrityDecision(
        ok=not any(issue.severity == "blocker" for issue in issues),
        kind=kind,
        issues=issues,
    )
