# LLM: HTML contract checks stay separate from generic artifact dispatch size limits.
# 模块用途: 根据结构化 validation_contract 检查完整 HTML、单文件外部资源和最小体量。

from __future__ import annotations

from urllib.parse import urlsplit


# LLM: record_resource_ref captures runtime resource refs for single-file artifact contracts.
# 函数用途: 收集 link/script/source/video/audio 这类非图片外部资源，默认不拦，合同要求时才使用。
def record_resource_ref(resources: list[tuple[str, str, str]], tag: str, values: dict[str, str]) -> None:
    for attr in ("href", "src"):
        value = values.get(attr, "")
        if value and tag in {"link", "script", "source", "video", "audio", "iframe"}:
            resources.append((tag, attr, value))


# LLM: html_contract_findings applies structured HTML quality requirements from delivery contracts.
# 函数用途: 返回 finding 字典，调用方再转成本模块自己的 ArtifactFinding 类型，避免循环导入；完整 HTML 结构检查默认开启。
def html_contract_findings(
    text: str,
    resources: list[tuple[str, str, str]],
    validation_contract: dict[str, object] | None,
) -> list[dict[str, str]]:
    requirements = _quality_requirements(validation_contract)
    findings: list[dict[str, str]] = []
    if requirements.get("complete_html_document", True):
        findings.extend(_complete_html_findings(text))
    if requirements.get("single_file_no_external_assets"):
        findings.extend(_external_resource_findings(resources))
    min_bytes = int(requirements.get("min_size_bytes") or 0)
    if min_bytes and len(text.encode("utf-8")) < min_bytes:
        findings.append(_finding("ARTIFACT_TOO_SMALL", "Artifact is smaller than the contract minimum.", value=str(min_bytes)))
    return findings


# LLM: _quality_requirements normalizes optional validation_contract data without trusting prompt text.
# 函数用途: 从结构化合同读取 quality_requirements；缺失或类型错误时返回空对象。
def _quality_requirements(validation_contract: dict[str, object] | None) -> dict[str, object]:
    value = (validation_contract or {}).get("quality_requirements")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _complete_html_findings detects truncated full-page HTML with stable issue codes.
# 函数用途: 检查完整网页所需的核心标签和 style/script 闭合，不使用自然语言判断完成度。
def _complete_html_findings(text: str) -> list[dict[str, str]]:
    missing = _missing_html_markers(text.lower())
    if not missing:
        return []
    return [_finding("HTML_INCOMPLETE_DOCUMENT", "HTML document is missing required structural markers.", value=",".join(missing))]


# LLM: _missing_html_markers lists absent structural markers for one full document.
# 函数用途: 计算缺失的 doctype/head/body/html 和不平衡 style/script 标记。
def _missing_html_markers(lower: str) -> list[str]:
    missing = [label for label, marker in _REQUIRED_HTML_MARKERS if marker not in lower]
    for tag in ("style", "script"):
        if _unbalanced_tag(lower, tag):
            missing.append(f"unbalanced_{tag}")
    return missing


# LLM: _unbalanced_tag is a small structural check for truncated style/script blocks.
# 函数用途: 判断 style/script 开闭标签数量是否一致。
def _unbalanced_tag(lower_text: str, tag: str) -> bool:
    return len(lower_text.split(f"<{tag}")) - 1 != lower_text.count(f"</{tag}>")


# LLM: _external_resource_findings enforces self-contained single-file HTML when requested.
# 函数用途: 对合同声明的单文件网页，拒绝 http/https 运行期资源引用。
def _external_resource_findings(resources: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [
        _finding(
            "HTML_EXTERNAL_RESOURCE_REF",
            "Single-file HTML contract forbids external runtime resources.",
            location=f"{tag}[{attr}]",
            value=value,
        )
        for tag, attr, value in resources
        if urlsplit(value.strip()).scheme.lower() in {"http", "https"}
    ]


# LLM: _finding keeps contract findings JSON-shaped before conversion to ArtifactFinding.
# 函数用途: 生成稳定 finding 字典，避免 artifact_acceptance 与本模块循环依赖。
def _finding(code: str, message: str, *, location: str = "", value: str = "") -> dict[str, str]:
    return {"code": code, "severity": "hard", "message": message, "location": location, "value": value}


_REQUIRED_HTML_MARKERS = (
    ("doctype", "<!doctype"),
    ("html_open", "<html"),
    ("html_close", "</html>"),
    ("head_close", "</head>"),
    ("body_open", "<body"),
    ("body_close", "</body>"),
)


__all__ = ["html_contract_findings", "record_resource_ref"]
