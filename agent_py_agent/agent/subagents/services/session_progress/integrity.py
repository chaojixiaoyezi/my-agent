
from __future__ import annotations

from pathlib import Path
from typing import Any

from ....common.value_parsing import sequence_strings
from ....tooling.artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity


def artifact_integrity_progress(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        decision = check_artifact_integrity(
            ArtifactIntegrityCheckRequest(path=Path(path), require_complete=True)
        )
    except OSError:
        return {}
    if decision.kind != "html":
        return {}
    return {
        "kind": decision.kind,
        "ok": decision.ok,
        "blocker_codes": decision.blocker_codes,
        "warning_codes": decision.warning_codes,
        "issues": [_artifact_issue_payload(issue) for issue in decision.issues[:8]],
    }


def artifact_next_action(integrity: dict[str, Any]) -> str:
    if not integrity:
        return "继续从 latest_tool_progress.json 接续；写作前先对照 headings，避免重复已记录章节。"
    codes = _integrity_codes(integrity)
    if _has_incomplete_html_codes(codes):
        return "继续从 latest_tool_progress.json 接续；HTML 还未完整闭合，最后一块再写 </body></html>。"
    if codes:
        joined = ", ".join(codes[:6])
        details = _integrity_issue_details(integrity)
        detail_text = f"；具体位置：{details}" if details else ""
        strategy = _integrity_repair_strategy(integrity)
        strategy_text = f"；{strategy}" if strategy else ""
        return f"产物已写出但自检发现 codes={joined}{detail_text}{strategy_text}；先修复这些问题，再写 execution_context.output_json 交最终收口。"
    return "产物已形成完整 HTML；停止继续写正文，按验收条件自检后写 execution_context.output_json（output.json）或 SUBAGENT_RESULT，交最终收口。"


def artifact_integrity_summary(integrity: dict[str, Any]) -> str:
    if not integrity:
        return ""
    codes = _integrity_codes(integrity)
    if _has_incomplete_html_codes(codes):
        return "HTML 尚未完整闭合"
    if codes:
        return f"HTML 待修复 codes={', '.join(codes[:4])}"
    return "HTML 已完整闭合，等待结构化收口"


def _artifact_issue_payload(issue: object) -> dict[str, Any]:
    return {
        "code": str(getattr(issue, "code", "") or ""),
        "severity": str(getattr(issue, "severity", "") or ""),
        "message": str(getattr(issue, "message", "") or ""),
        "count": int(getattr(issue, "count", 1) or 1),
        "examples": [str(item) for item in list(getattr(issue, "examples", []) or [])[:5]],
    }


def _integrity_issue_details(integrity: dict[str, Any]) -> str:
    details = [_integrity_issue_detail(issue) for issue in integrity.get("issues", [])]
    return "；".join([item for item in details if item][:4])


def _integrity_issue_detail(issue: object) -> str:
    if not isinstance(issue, dict):
        return ""
    code = str(issue.get("code") or "")
    if not code:
        return ""
    examples = sequence_strings(issue.get("examples"))
    count = int(issue.get("count") or 1)
    count_text = f"x{count}" if count > 1 else ""
    examples_text = "; ".join(examples[:3])
    return f"{code}{count_text} examples={examples_text}" if examples_text else f"{code}{count_text}"


def _integrity_repair_strategy(integrity: dict[str, Any]) -> str:
    if _max_issue_count(integrity, "placeholder_hash_link") < 4:
        return ""
    return (
        "建议一次性批量修复：先搜索全部 href=\"#\"，"
        "能用同一个 old/new 处理时用 apply_patch 批量修改相关片段，"
        "否则重写相关导航、页脚、CTA 或整文件；不要一轮只替换一个链接"
    )


def _max_issue_count(integrity: dict[str, Any], code: str) -> int:
    counts = [
        int(issue.get("count") or 0)
        for issue in integrity.get("issues", [])
        if isinstance(issue, dict) and str(issue.get("code") or "") == code
    ]
    return max(counts, default=0)


def _integrity_codes(integrity: dict[str, Any]) -> list[str]:
    return _merge_unique(
        sequence_strings(integrity.get("blocker_codes")) + sequence_strings(integrity.get("warning_codes"))
    )


def _has_incomplete_html_codes(codes: list[str]) -> bool:
    return "missing_body_close" in codes or "missing_html_close" in codes


def _merge_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


__all__ = [
    "artifact_integrity_progress",
    "artifact_integrity_summary",
    "artifact_next_action",
]
