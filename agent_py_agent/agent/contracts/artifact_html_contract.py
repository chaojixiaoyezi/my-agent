
from __future__ import annotations

from urllib.parse import urlsplit


def record_resource_ref(resources: list[tuple[str, str, str]], tag: str, values: dict[str, str]) -> None:
    for attr in ("href", "src"):
        value = values.get(attr, "")
        if value and tag in {"link", "script", "source", "video", "audio", "iframe"}:
            resources.append((tag, attr, value))


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


def _quality_requirements(validation_contract: dict[str, object] | None) -> dict[str, object]:
    value = (validation_contract or {}).get("quality_requirements")
    return dict(value) if isinstance(value, dict) else {}


def _complete_html_findings(text: str) -> list[dict[str, str]]:
    missing = _missing_html_markers(text.lower())
    if not missing:
        return []
    return [_finding("HTML_INCOMPLETE_DOCUMENT", "HTML document is missing required structural markers.", value=",".join(missing))]


def _missing_html_markers(lower: str) -> list[str]:
    missing = [label for label, marker in _REQUIRED_HTML_MARKERS if marker not in lower]
    for tag in ("style", "script"):
        if _unbalanced_tag(lower, tag):
            missing.append(f"unbalanced_{tag}")
    return missing


def _unbalanced_tag(lower_text: str, tag: str) -> bool:
    return len(lower_text.split(f"<{tag}")) - 1 != lower_text.count(f"</{tag}>")


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
