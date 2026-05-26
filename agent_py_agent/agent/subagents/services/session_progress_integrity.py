# LLM: Session progress integrity helpers keep artifact repair hints out of the snapshot writer.
# 模块用途: 将 HTML 产物完整性检查结果压成进度摘要和下一步建议，避免 session_progress 主流程变厚。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...tooling.artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity


# LLM: artifact_integrity_progress summarizes current artifact state without reading body into prompts.
# 函数用途: 只把 HTML 完整性和假链接等小型机器结论写进进度包，避免模型写完后继续空转。
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


# LLM: artifact_next_action turns machine artifact state into one runner instruction.
# 函数用途: HTML 未闭合时继续写；HTML 有假链接/结构问题时先修；HTML 完整时要求写 output.json 收口。
def artifact_next_action(integrity: dict[str, Any]) -> str:
    # LLM: Repair advice names generic write/patch behavior indirectly through runner contract.
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
        return f"产物已写出但自检发现 codes={joined}{detail_text}{strategy_text}；先修复这些问题，再写 execution_context.output_json 交父级验收。"
    return "产物已形成完整 HTML；停止继续写正文，按验收条件自检后写 execution_context.output_json（output.json）或 SUBAGENT_RESULT，交父级验收。"


# LLM: artifact_integrity_summary keeps progress summaries readable while preserving issue codes.
# 函数用途: 给 latest_summary 增加 HTML 完整/待修/待续写状态，便于 compact 续跑时快速判断。
def artifact_integrity_summary(integrity: dict[str, Any]) -> str:
    if not integrity:
        return ""
    codes = _integrity_codes(integrity)
    if _has_incomplete_html_codes(codes):
        return "HTML 尚未完整闭合"
    if codes:
        return f"HTML 待修复 codes={', '.join(codes[:4])}"
    return "HTML 已完整闭合，等待结构化收口"


# LLM: _artifact_issue_payload exposes bounded issue details to runners and compact packets.
# 函数用途: 将 ArtifactIntegrityIssue 转成 JSON 小对象，包含 code、数量、示例和中文建议。
def _artifact_issue_payload(issue: object) -> dict[str, Any]:
    return {
        "code": str(getattr(issue, "code", "") or ""),
        "severity": str(getattr(issue, "severity", "") or ""),
        "message": str(getattr(issue, "message", "") or ""),
        "count": int(getattr(issue, "count", 1) or 1),
        "examples": [str(item) for item in list(getattr(issue, "examples", []) or [])[:5]],
    }


# LLM: _integrity_issue_details turns machine issue payloads into one concise repair hint.
# 函数用途: 给 next_action 增加具体 href/文本示例，避免模型只围着抽象 code 反复空修。
def _integrity_issue_details(integrity: dict[str, Any]) -> str:
    details = [_integrity_issue_detail(issue) for issue in integrity.get("issues", [])]
    return "；".join([item for item in details if item][:4])


# LLM: _integrity_issue_detail renders one compact issue line for progress next_action.
# 函数用途: 把单个 issue payload 转成 code/count/examples 文本，主函数保持浅层流程。
def _integrity_issue_detail(issue: object) -> str:
    if not isinstance(issue, dict):
        return ""
    code = str(issue.get("code") or "")
    if not code:
        return ""
    examples = _string_list(issue.get("examples"))
    count = int(issue.get("count") or 1)
    count_text = f"x{count}" if count > 1 else ""
    examples_text = "; ".join(examples[:3])
    return f"{code}{count_text} examples={examples_text}" if examples_text else f"{code}{count_text}"


# LLM: _integrity_repair_strategy gives runners method-level guidance for many similar issues.
# 函数用途: 大量 href="#" 时提示批量修复，防止真实模型一轮只替换一个链接导致长时间空转。
def _integrity_repair_strategy(integrity: dict[str, Any]) -> str:
    if _max_issue_count(integrity, "placeholder_hash_link") < 4:
        return ""
    return (
        "建议一次性批量修复：先搜索全部 href=\"#\"，"
        "能用同一个 old/new 处理时用 apply_patch 批量修改相关片段，"
        "否则重写相关导航、页脚、CTA 或整文件；不要一轮只替换一个链接"
    )


# LLM: _max_issue_count returns the largest count for one integrity issue code.
# 函数用途: 给 repair strategy 判断同类问题规模，不让主流程嵌套扫描 JSON 细节。
def _max_issue_count(integrity: dict[str, Any], code: str) -> int:
    counts = [
        int(issue.get("count") or 0)
        for issue in integrity.get("issues", [])
        if isinstance(issue, dict) and str(issue.get("code") or "") == code
    ]
    return max(counts, default=0)


# LLM: _integrity_codes merges blocker and warning codes in a stable order.
# 函数用途: 将 artifact_integrity 小字段转成去重 issue code 列表。
def _integrity_codes(integrity: dict[str, Any]) -> list[str]:
    return _merge_unique(
        _string_list(integrity.get("blocker_codes")) + _string_list(integrity.get("warning_codes"))
    )


# LLM: _has_incomplete_html_codes separates normal chunking from repair-needed complete artifacts.
# 函数用途: 缺 body/html 结束标签时继续分块写；其他结构/链接问题进入修复状态。
def _has_incomplete_html_codes(codes: list[str]) -> bool:
    return "missing_body_close" in codes or "missing_html_close" in codes


# LLM: _string_list normalizes optional JSON list fields before merging.
# 函数用途: 将已有 examples / codes 统一成字符串列表。
def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return []


# LLM: _merge_unique keeps first-seen order for compact progress facts.
# 函数用途: 按顺序去重 issue codes，不让提示词出现重复代码。
def _merge_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


__all__ = [
    "artifact_integrity_progress",
    "artifact_integrity_summary",
    "artifact_next_action",
]
