# LLM: artifact integrity checks are small machine gates around model-written files, not business acceptance.
# 模块用途: 对模型产出的本地文件做低成本结构检查，防止明显损坏的产物进入父级验收。

from __future__ import annotations

from dataclasses import dataclass, field
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
    return f"HTML 完整性提示: 发现需要修复的结构问题 codes={codes}。"


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
    if body_count and html_count and lowered.rfind("</body>") > lowered.rfind("</html>"):
        issues.append(_issue("body_close_after_html_close", "</body> 出现在 </html> 之后。"))
    if html_count:
        html_end = lowered.rfind("</html>") + len("</html>")
        if text[html_end:].strip():
            issues.append(_issue("content_after_html_close", "</html> 后面还有非空内容。"))
    return _decision("html", issues)


# LLM: _looks_like_html_path keeps the first integrity gate scoped to web artifacts only.
# 函数用途: 判断路径是否应按 HTML 结构检查，避免影响普通文本和代码文件。
def _looks_like_html_path(path: Path) -> bool:
    return path.suffix.lower() in {".html", ".htm"}


# LLM: _issue keeps issue construction readable at call sites.
# 函数用途: 创建统一的产物完整性 issue 对象。
def _issue(code: str, message: str, *, severity: str = "blocker") -> ArtifactIntegrityIssue:
    return ArtifactIntegrityIssue(code=code, message=message, severity=severity)


# LLM: _decision centralizes ok semantics so warnings do not block acceptance.
# 函数用途: 根据 blocker 是否存在生成最终完整性检查结果。
def _decision(kind: str, issues: list[ArtifactIntegrityIssue]) -> ArtifactIntegrityDecision:
    return ArtifactIntegrityDecision(
        ok=not any(issue.severity == "blocker" for issue in issues),
        kind=kind,
        issues=issues,
    )
